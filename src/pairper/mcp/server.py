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
1. list_pending_papers로 새 대상만 찾는다. 유효한 기존 뷰는 재생성하지 않는다.
2. get_paper_manifest, 모든 get_page_content와 get_page_image를 읽는다.
이미지가 모델에 전달되지 않으면 중단하고 연결 문제를 보고한다.
3. document_schema 리소스를 읽는다. 원문을 요약/번역/의역하지 않는다.
텍스트는 runs의 refs로만 연결한다. 문단을 단과 페이지 경계를 넘어 재구성한다.
그림/벡터 도식/표/수식의 원본 영역을 반드시 포함한다. 불확실한 것은 source_region
또는 source_verified=false인 수식/표로 보존한다. 이미지의 전체 페이지 대체는 스캔
페이지 외에는 사용하지 않는다. 작은 영역을 요청하여 기호와 숫자를 대조한다.
4. begin_view_build 후 append_document_blocks로 배치 저장한다. 같은 블록 ID는 수정한다.
source_refs는 실제 표시 텍스트 ID와 원본 영역에 완전히 포함된 텍스트 ID의 합집합이다.
각 페이지를 시각 검토한 후 visual_review_pages에 기록한다. 이는 검토자의 진술이다.
생략은 장식적 페이지 번호/반복 머리말/꼬리말만 허용하며 omissions에 근거를 기록한다.
5. validate_view_build와 get_build_report에서 오류를 확인하고 같은 ID로 수정한다.
finalize_view_build가 성공할 때만 완료를 보고한다. 미검증 복원은 원본 이미지로 표시된다.
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
    def begin_view_build(paper_id: str, source_sha256: str) -> dict[str, Any]:
        """Begin or resume one exclusive build. Already-complete papers are protected."""
        return library.begin(paper_id, source_sha256)

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
