import base64
import os
import sys

from conftest import text_blocks
from mcp import Client, StdioServerParameters

from pairper.mcp.server import create_server


async def test_mcp_tools_images_and_build(library, pdf_path):
    paper = library.register(pdf_path)
    async with Client(create_server(library)) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 11
        result = await client.call_tool("list_pending_papers", {})
        assert not result.is_error
        image = await client.call_tool("get_page_image", {"paper_id": paper["id"], "page": 1})
        assert not image.is_error
        assert image.content[0].type == "image"
        assert base64.b64decode(image.content[0].data).startswith(b"\x89PNG")
        invalid = await client.call_tool(
            "get_page_content", {"paper_id": "../../escape", "page": 1}
        )
        assert invalid.is_error
        assert "Unknown paper" in str(invalid.content)
        region = await client.call_tool(
            "get_region_image",
            {
                "paper_id": paper["id"],
                "region": {"page": 1, "bbox": [30, 30, 300, 130]},
                "scale": 3,
            },
        )
        assert region.content[0].type == "image"
        begin = await client.call_tool(
            "begin_view_build", {"paper_id": paper["id"], "source_sha256": paper["sha256"]}
        )
        job = begin.structured_content["job_id"]
        blocks = [b.model_dump(mode="json") for b in text_blocks(library, paper)]
        saved = await client.call_tool(
            "append_document_blocks", {"job_id": job, "blocks": blocks, "visual_review_pages": [1]}
        )
        assert not saved.is_error
        final = await client.call_tool("finalize_view_build", {"job_id": job})
        assert final.structured_content["valid"]


async def test_real_stdio_subprocess(library, pdf_path):
    library.register(pdf_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pairper.mcp.server"],
        env={**os.environ, "PAIRPER_DATA_DIR": str(library.root)},
    )
    async with Client(params) as client:
        result = await client.call_tool("list_pending_papers", {})
        assert not result.is_error
        assert "Unit fixture" in str(result.content)
        image = await client.call_tool(
            "get_page_image", {"paper_id": library.list_papers()[0]["id"], "page": 1}
        )
        assert image.content[0].type == "image"
