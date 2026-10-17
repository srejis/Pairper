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

WORKFLOW = """Pairper 원본 기반 논문 재구성 절차:
1. list_pending_papers로 대상을 찾는다. 명시적 교정은 begin_view_build(revise_existing=true)를
사용하며 원본과 이전 뷰를 보존한다. 먼저 get_reconstruction_guide와 document_schema를 읽는다.
2. get_paper_manifest의 pdf_evidence에서 원본 목차·링크 목적지·논리 구조 존재 여부를 확인한다.
모든 페이지의 get_page_content, get_page_evidence(summary/lines), get_page_image를 확인한다.
원본 이미지가 전달되지 않으면 완료를 주장하지 않고 연결 문제를 보고한다. 모호한 문자·공백·
그림 경계는 characters/paths/links와 get_region_image로 확인한다. table_candidates는 추론
후보이지 원본에 명시된 셀이나 검증된 표가 아니다. next_offset이 있으면 끝까지 페이지네이션한다.
3. 프로그램은 문자·배치·도형·목차·링크 추출, 선택 구간 확장, 출처 계산, 검증·표시를 맡는다.
AI는 원본 근거로 읽기 순서, 문단/단/페이지 연결, 영역의 역할·포함 관계를 결정한다.
추출 block/line을 의미적 문단으로 간주하지 않는다. PDF에 없는 역할을 억지로 추정하지 않는다.
4. 텍스트는 runs.refs 또는 명시적 ref_ranges(first_ref,last_ref)로 선택한다. 범위 하나는
같은 페이지·추출 블록 내부의 정방향 구간이다. 페이지/단을 넘으면 선택 구간을 순서대로 나열한다.
source_refs를 생략하면 프로그램이 실제 표시 refs와 영역에서 계산한다. 단어·문단을 요약/의역하지
않는다. 원본 추출이 실제로 손실한 문자만 원본 대조 후 text_override/source_verified로 전사한다.
5. group과 parent_id로 정리·정의·증명·프롬프트·예시·체크리스트·목록 항목을 묶는다.
그룹은 자식 내용을 source_refs에 재선언하지 않는다. order는 부모 먼저, 자손이 연속하는 읽기
순서다. relations로 증명 대상·캡션 대상·각주를 연결한다. group 자체는 이미지 대체 수단이 아니다.
6. 실제 도식·사진·그래프만 figure로 보존한다. Figure 번호가 붙은 프롬프트도 읽을 수 있는
텍스트/코드로 구조화한다. 수식은 원본과 대조한 LaTeX, 표는 행·열·병합 셀과 refs/수식으로 작성한다.
알고리즘은 algorithm 그룹 안에 코드 행·문단 등을 두고, 실제 코드는 code_lines로 들여쓰기·빈 줄을
보존한다. 프롬프트/코드의 원문 기호는 literal=true로 보존한다. 하이픈은 일괄 삭제하지 않는다.
7. begin_view_build 후 append_document_blocks로 배치 저장한다. 같은 ID는 수정된다.
시각 검토 후에만 visual_review_pages를 기록한다. source_verified는 원본 대조 진술이지 프로그램의
문법 통과 결과가 아니다. 생략은 장식적 페이지 번호/반복 머리말/꼬리말만 근거와 함께 기록한다.
8. validate_view_build/get_build_report로 refs·좌표·그룹/연결·표 셀과 실제 KaTeX 문법 검사를
확인하고 오류를 교정한다. 프로그램의 검사는 읽기 순서·문단 경계·수식 의미 동치를 증명하지 않는다.
finalize 성공과 실제 읽기 화면의 구조·표시 확인 후에만 완료를 보고한다. 새 완료/기존 완료/검토 필요를
구분한다. 수식·표·본문을 이미지로 우회하거나 불확실한 부분을 추측해서 완료하지 않는다.
"""

RECONSTRUCTION_GUIDE = """원본 위치와 읽기 단위는 다르다.
프로그램 근거: pdf_evidence의 목차·태그 존재 여부는 원본 정보다. page_evidence의 lines와
characters는 추출기의 기하 정보이고 의미 문단이 아니다. paths는 PDF 그리기 연산이다.
table_candidates는 라이브러리 추론이며 source_verified=false다. 논리 태그가 없다고 문서 구조가
없는 것은 아니며, 태그가 있더라도 지원되지 않는 구조를 읽었다고 주장하지 않는다.
읽기 단위: title/authors/heading/paragraph/list/equation/figure/table/caption/footnote/reference/code.
복합 group_role: section/front_matter/theorem/definition/lemma/fact/remark/proof/prompt/example/
checklist/checklist_item/algorithm/figure_group/table_group/list_item.
그룹은 c.runs로 원본 제목을 선택할 수 있고 내용은 parent_id를 가진 자식 블록에 둔다.
그룹의 라벨에 쓴 refs를 자식에서 다시 쓰지 않는다. 자식이 없거나 순환·비연속인 그룹은 실패한다.
본문·표 셀·목록 속 긴 문단/수식은 group(list_item 등) 아래에 필요한 블록들을 순서대로 둔다.
동일 ID 블록을 두 부모 밑에 중복 렌더하지 않는다. 표는 셀의 행/열/병합 관계를 보존한다.
프롬프트/예시/알고리즘의 테두리 영역은 group.content.source_regions에 둘 수 있으나
source_verified=true와 모든 내부 텍스트의 자식 표시를 요구한다. 이 영역으로 이미지를 가릴 수 없다.
relations는 {kind: caption_of|proof_of|footnote_for|refers_to, target_id: 블록ID} 배열이다.
옛 caption_id도 읽을 수 있다. Figure/Table 원래 번호·캡션 위치는 원문대로 유지한다.
여러 페이지의 프롬프트·코드·증명은 그룹 하나와 여러 원본 기반 자식으로 표현한다.
일반 강조·각주 표식은 TextRun.bold/italic/underline/superscript, literal은 문자 정규화를 끈다.
ref_ranges는 추출 순서를 새 읽기 순서라고 추정하지 않고 AI가 선택한 구간만 확장한다.
source_refs 생략은 편의 기능이며 실제 표시 원문 누락/중복 검사를 약화하지 않는다.
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
    def get_reconstruction_guide() -> str:
        """Read program/AI responsibilities and source-backed group/selection contracts."""
        return RECONSTRUCTION_GUIDE

    @tool()
    def get_page_evidence(
        paper_id: str, page: int, section: str = "summary", offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Read bounded PDF evidence: summary/lines/characters/paths/links/annotations/table_candidates.

        Follow next_offset until null. Tables are inferred proposals requiring source review.
        Source text IDs and previous views are never rewritten by evidence extraction.
        """
        return library.page_evidence(paper_id, page, section, offset, limit)

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
