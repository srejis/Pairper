import base64
import os
import sqlite3
from functools import wraps
from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pairper.document.models import Block, Document, Omission, Region
from pairper.storage.library import Library

WORKFLOW = """Pairper 논문 업데이트 절차:
1. list_pending_papers로 새 대상을 찾는다. 기존 뷰의 실제 오류를 수정하는 명시적 작업은
begin_view_build(revise_existing=true)로 이전 문서의 별도 교정본을 만든다. 원본과 이전 뷰는 보존한다.
2. get_paper_manifest, 모든 get_page_content와 get_page_image를 읽는다.
이미지가 모델에 전달되지 않으면 중단하고 연결 문제를 보고한다.
3. document_schema 리소스를 읽는다. 원문을 요약/번역/의역하지 않는다.
텍스트는 runs의 refs로 연결한다. PDF에서 문자 대신 선으로 그린 밑줄처럼 원본 추출이
실제로 손실한 문자는 원본 대조 후에만 text_override와 source_verified=true로 복원한다.
원본 refs는 유지하며 요약이나 의역에는 사용하지 않는다. 각주 표지는 text run의
superscript=true로 표시한다. 문단을 단과 페이지 경계를 넘어 재구성한다.
그림/벡터 도식은 이미지로 보존할 수 있다. 수식은 원본과 대조한 LaTeX로, 표는 행·열·
병합 셀 및 셀의 원문 refs/수식을 보존하여 구조화한다. 수식·표 이미지는 완료로 인정하지
않는다. 알고리즘·코드는 code의 code_lines에 원본 들여쓰기와 refs/LaTeX를 보존한다.
불확실한 내용은 추측하지 않고 검토 중인 빌드에 남긴다. 본문·수식·표를 그림이나
source_region으로 바꾸어 우회하지 않는다. 작은 원본 영역에서 기호와 숫자를 대조한다.
4. begin_view_build 후 append_document_blocks로 배치 저장한다. 같은 블록 ID는 수정한다.
source_refs는 실제 표시 텍스트 ID와 원본 영역에 완전히 포함된 텍스트 ID의 합집합이다.
각 페이지를 시각 검토한 후 visual_review_pages에 기록한다. 이는 검토자의 진술이다.
생략은 장식적 페이지 번호/반복 머리말/꼬리말만 허용하며 omissions에 근거를 기록한다.
5. validate_view_build와 get_build_report에서 오류를 확인하고 같은 ID로 수정한다.
finalize_view_build가 성공하고 실제 렌더링도 확인했을 때만 완료를 보고한다.
수식·표의 미검증 이미지 대체는 저장 검증을 통과하지 않는다.
6. 새 완료 수, 기존 완료 수, 검토 필요 수를 구분해 보고한다. 자동 검증이 의미적
동일성이나 읽기 순서를 증명한다고 주장하지 않는다.
"""


def create_server(library: Library | None = None):
    library = library or Library()
    server = MCPServer("Pairper", instructions=WORKFLOW)

    def tool():
        def decorate(function):
            @wraps(function)
            def guarded(*args, **kwargs):
                try:
                    result = function(*args, **kwargs)
                    if os.environ.get("PAIRPER_TUNNEL_PROCESS") == "1":
                        from pairper.storage.library import now, write_json
                        from pairper.tunnel.config import project_root

                        runtime = project_root() / ".pairper"
                        if runtime.is_dir():
                            try:
                                write_json(
                                    runtime / "tool-call.json",
                                    {"tool": function.__name__, "at": now()},
                                )
                            except OSError:
                                # Audit metadata must never turn an already-committed write into an error.
                                pass
                    return result
                except (ValueError, OSError, sqlite3.DatabaseError) as exc:
                    raise ToolError(str(exc)) from exc

            return server.tool()(guarded)

        return decorate

    @tool()
    def list_papers() -> list[dict[str, Any]]:
        """List registered papers, processing status and saved view availability."""
        return library.list_papers()

    @tool()
    def list_pending_papers() -> list[dict[str, Any]]:
        """Find papers without valid saved views. Start updates here; skip completed papers."""
        return library.list_papers(pending=True)

    @tool()
    def get_paper_manifest(paper_id: str) -> dict[str, Any]:
        """Read hash, metadata, page dimensions and extracted text/visual counts."""
        return library.manifest(paper_id)

    @tool()
    def get_page_content(paper_id: str, page: int) -> dict[str, Any]:
        """Read original span IDs/text/coordinates/fonts/directions. Page numbers start at 1."""
        return library.page_content(paper_id, page)

    @tool()
    def get_page_image(paper_id: str, page: int) -> types.ImageContent:
        """Return actual PNG image content, not a path. Inspect every page visually."""
        return types.ImageContent(
            data=base64.b64encode(library.image(paper_id, page)).decode(), mime_type="image/png"
        )

    @tool()
    def get_region_image(paper_id: str, region: Region, scale: float = 3) -> types.ImageContent:
        """Return enlarged source PNG for equations/tables/figures. PDF points, top-left origin."""
        return types.ImageContent(
            data=base64.b64encode(library.image(paper_id, region.page, region, scale)).decode(),
            mime_type="image/png",
        )

    @tool()
    def begin_view_build(
        paper_id: str, source_sha256: str, revise_existing: bool = False
    ) -> dict[str, Any]:
        """Begin/resume one build. Explicit revisions copy the saved document, preserving its view."""
        return library.begin(paper_id, source_sha256, revise_existing=revise_existing)

    @tool()
    def append_document_blocks(
        job_id: str,
        blocks: list[Block],
        omissions: list[Omission] | None = None,
        visual_review_pages: list[int] | None = None,
        remove_block_ids: list[str] | None = None,
        remove_omission_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Save/repair batches by block ID. Source text uses refs; no invented prose or HTML.

        Include visual_review_pages only after actually inspecting the page images.
        Get the document_schema resource for the strict block contract.
        """
        return library.append(
            job_id,
            blocks,
            omissions or [],
            visual_review_pages or [],
            remove_block_ids,
            remove_omission_refs,
        )

    @tool()
    def validate_view_build(job_id: str) -> dict[str, Any]:
        """Check schema/hash/text coverage/duplicates/visual regions and renderability."""
        return library.validate(job_id)

    @tool()
    def finalize_view_build(job_id: str) -> dict[str, Any]:
        """Revalidate and atomically publish a valid view. Invalid builds stay in staging."""
        return library.validate(job_id, finalize=True)

    @tool()
    def get_build_report(job_id: str) -> dict[str, Any]:
        """Read resumable staged document, errors, missing/duplicate refs and review warnings."""
        return library.report(job_id)

    @server.resource("pairper://document-schema")
    def document_schema() -> str:
        """Strict document JSON Schema. Use original IDs for text; source regions for visuals."""
        import json

        return json.dumps(Document.model_json_schema(), ensure_ascii=False)

    @server.prompt()
    def update_papers() -> str:
        """Update pending Pairper papers without rewriting source content."""
        return WORKFLOW

    return server


def main():
    # stdout is reserved for MCP protocol traffic. No GUI imports here.
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
