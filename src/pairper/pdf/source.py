import hashlib
from pathlib import Path

import pymupdf

from pairper.document.models import Region


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract(path: Path, digest: str) -> dict:
    pages = []
    # Open from bytes so malformed inputs cannot leave a Windows file handle locked.
    with pymupdf.open(stream=path.read_bytes(), filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        if not pdf.is_pdf or not len(pdf):
            raise ValueError("A nonempty PDF is required")
        metadata = pdf.metadata
        for index, page in enumerate(pdf):
            original_rotation = page.rotation
            page.set_rotation(0)
            spans = []
            for bi, block in enumerate(page.get_text("dict", sort=False)["blocks"]):
                for li, line in enumerate(block.get("lines", [])):
                    for si, span in enumerate(line["spans"]):
                        if not span["text"]:
                            continue
                        spans.append(
                            {
                                "id": f"{digest[:16]}-p{index + 1}-b{bi}-l{li}-s{si}",
                                "text": span["text"],
                                "bbox": span["bbox"],
                                "font": span["font"],
                                "size": span["size"],
                                "flags": span["flags"],
                                "direction": line["dir"],
                                "order": len(spans),
                                "block": bi,
                                "line": li,
                            }
                        )
            pages.append(
                {
                    "page": index + 1,
                    "width": page.rect.width,
                    "height": page.rect.height,
                    "rotation": original_rotation,
                    "coordinate_system": "unrotated_pdf_points_top_left",
                    "spans": spans,
                    "images": [
                        {
                            "bbox": i["bbox"],
                            "width": i["width"],
                            "height": i["height"],
                            "xref": i["xref"],
                        }
                        for i in page.get_image_info(xrefs=True)
                    ],
                    "drawings": [
                        {"bbox": list(d["rect"]), "type": d["type"]} for d in page.get_drawings()
                    ],
                }
            )
    return {
        "extractor_version": 1,
        "pymupdf_version": pymupdf.VersionBind,
        "sha256": digest,
        "metadata": metadata,
        "pages": pages,
    }


def extract_embedded_images(path: Path, destination: Path) -> list[dict]:
    """Preserve embedded bytes; region rendering separately covers vector graphics."""
    result = []
    with pymupdf.open(stream=path.read_bytes(), filetype="pdf") as pdf:
        seen = set()
        for page in pdf:
            for item in page.get_images(full=True):
                xref = item[0]
                if xref in seen:
                    continue
                seen.add(xref)
                data = pdf.extract_image(xref)
                if not data:
                    continue
                digest = hashlib.sha256(data["image"]).hexdigest()
                asset = destination / f"{digest}.{data['ext']}"
                if not asset.exists():
                    asset.write_bytes(data["image"])
                result.append(
                    {
                        "xref": xref,
                        "asset_id": digest,
                        "filename": asset.name,
                        "width": data["width"],
                        "height": data["height"],
                    }
                )
    return result


def check_region(source: dict, region: Region):
    if region.page > len(source["pages"]):
        raise ValueError("Page outside document")
    page = source["pages"][region.page - 1]
    if region.bbox[2] > page["width"] + 0.01 or region.bbox[3] > page["height"] + 0.01:
        raise ValueError("Region outside page")


def render(
    path: Path, source: dict, page: int, region: Region | None = None, scale: float = 1.5
) -> bytes:
    if page < 1 or page > len(source["pages"]):
        raise ValueError("Page outside document")
    if not 0.5 <= scale <= 4:
        raise ValueError("Scale must be between 0.5 and 4")
    if region:
        check_region(source, region)
    info = source["pages"][page - 1]
    rect = pymupdf.Rect(region.bbox if region else (0, 0, info["width"], info["height"]))
    if rect.width * rect.height * scale * scale > 24_000_000:
        raise ValueError("Image exceeds 24 megapixel limit; request a smaller region")
    with pymupdf.open(path) as pdf:
        p = pdf[page - 1]
        # Extraction coordinates are unrotated; normalize the page before clipping.
        p.set_rotation(0)
        matrix = pymupdf.Matrix(scale, scale)
        if region:
            matrix.prerotate(region.rotation)
        pix = p.get_pixmap(matrix=matrix, clip=rect, alpha=False)
        return pix.tobytes("png")
