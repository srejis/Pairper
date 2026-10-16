import hashlib
from pathlib import Path

import pymupdf

from pairper.document.models import Region

EXTRACTOR_VERSION = 7


class _VisualClips(pymupdf.mupdf.FzDevice2):
    """Record painting bounds under the actual PDF clip and soft-mask stack."""

    def __init__(self, page):
        super().__init__()
        self.clips = [page.rect]
        self.masks = []
        self.images = []
        self.paths = []
        for name in (
            "clip_path", "clip_stroke_path", "clip_text", "clip_stroke_text",
            "clip_image_mask", "pop_clip", "fill_image", "fill_image_mask",
            "begin_mask", "end_mask",
            "fill_path", "stroke_path",
        ):
            getattr(self, "use_virtual_" + name)()

    def push(self, rect):
        self.clips.append(self.clips[-1] & pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1))

    def clip_path(self, ctx, path, even_odd, ctm, scissor):
        self.push(pymupdf.mupdf.ll_fz_bound_path(path, None, ctm))

    def clip_stroke_path(self, ctx, path, stroke, ctm, scissor):
        self.push(pymupdf.mupdf.ll_fz_bound_path(path, stroke, ctm))

    def clip_text(self, ctx, text, ctm, scissor):
        self.push(pymupdf.mupdf.ll_fz_bound_text(text, None, ctm))

    def clip_stroke_text(self, ctx, text, stroke, ctm, scissor):
        self.push(pymupdf.mupdf.ll_fz_bound_text(text, stroke, ctm))

    def clip_image_mask(self, ctx, image, ctm, scissor):
        self.push(pymupdf.mupdf.ll_fz_transform_rect(pymupdf.mupdf.fz_unit_rect, ctm))

    def pop_clip(self, ctx):
        self.clips.pop()

    def begin_mask(self, ctx, rect, luminosity, colorspace, background, params):
        self.masks.append((len(self.clips), rect))

    def end_mask(self, ctx, function):
        depth, rect = self.masks.pop()
        self.clips = self.clips[:depth]
        self.push(rect)

    def record_path(self, path, ctm):
        rect = pymupdf.mupdf.ll_fz_bound_path(path, None, ctm)
        raw = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
        self.paths.append((raw, None if self.masks else raw & self.clips[-1]))

    def fill_path(self, ctx, path, even_odd, ctm, colorspace, color, alpha, params):
        self.record_path(path, ctm)

    def stroke_path(self, ctx, path, stroke, ctm, colorspace, color, alpha, params):
        self.record_path(path, ctm)

    def fill_image(self, ctx, image, ctm, alpha, params):
        rect = pymupdf.mupdf.ll_fz_transform_rect(pymupdf.mupdf.fz_unit_rect, ctm)
        raw = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
        # Mask-building images affect opacity; they are not independent page content.
        visible = None if self.masks else raw & self.clips[-1]
        self.images.append((raw, visible))

    def fill_image_mask(self, ctx, image, ctm, colorspace, color, alpha, params):
        self.fill_image(ctx, image, ctm, alpha, params)


def extract_visuals(page):
    """Keep original object bounds and separately record the visible coverage bounds."""
    original_drawings = page.get_drawings()
    images = page.get_image_info(xrefs=True)
    device = _VisualClips(page)
    if original_drawings or images:
        mupdf = pymupdf.mupdf
        mupdf.fz_run_page(page.this, device, mupdf.FzMatrix(), mupdf.FzCookie())
    def bounds_key(rect):
        return tuple(round(value, 2) for value in rect)

    def index_records(records):
        index = {}
        for raw, visible in records:
            index.setdefault(bounds_key(raw), []).append(visible)
        return index

    paths = index_records(device.paths)
    image_bounds = index_records(device.images)

    def visible_bounds(raw, records):
        matches = records.get(bounds_key(raw))
        if matches is None:
            # Keep unmatched extraction records conservative; never discard source content.
            visible = raw & page.rect
        else:
            visible = pymupdf.Rect()
            for box in matches:
                if box is not None and not box.is_empty:
                    visible |= box
        return None if visible.is_empty else list(visible)

    drawings = [{
        "bbox": list(drawing["rect"]), "type": drawing["type"],
        "visible_bbox": visible_bounds(drawing["rect"], paths),
        # Deja Vu Figure 7 has a white plot background extending into its caption.
        # Preserve the raw drawing, but blank paper padding is not independent ink.
        "background_only": (drawing["type"] == "f"
                            and drawing.get("fill") == (1.0, 1.0, 1.0)
                            and drawing.get("fill_opacity") == 1.0
                            and len(drawing["items"]) == 1
                            and drawing["items"][0][0] == "re"),
    } for drawing in original_drawings]
    for image in images:
        image["visible_bbox"] = visible_bounds(pymupdf.Rect(image["bbox"]), image_bounds)

    return {
        "images": [{key: image[key] for key in ("bbox", "width", "height", "xref", "visible_bbox")}
                   for image in images],
        "drawings": drawings,
    }



def text_paint_bounds(page, spans):
    """Exact trace matches for figure labels whose font boxes include blank padding."""
    traces, painted = page.get_texttrace(), page.get_bboxlog()
    by_text = {}
    for trace in traces:
        key = ("".join(chr(c[0]) for c in trace["chars"]), trace["font"], tuple(trace["dir"]))
        by_text.setdefault(key, []).append(trace)
    result = {}
    for span in spans:
        if not span["text"].strip():
            continue
        candidates = by_text.get((span["text"], span["font"], tuple(span["direction"])), [])
        matches = [t for t in candidates
                   if pymupdf.Rect(t["bbox"]).intersects(pymupdf.Rect(span["bbox"]))]
        if len(matches) == 1:
            kind, box = painted[matches[0]["seqno"]][:2]
            if kind in {"fill-text", "stroke-text"}:
                result[span["id"]] = list(box)
    return result


def rotated_glyph_bounds(page, spans):
    """Actual painted bounds for rotated singleton glyphs (OFA Figure 2 arrows).

    Font ascender boxes can extend into the following caption. Keep those original
    boxes and IDs intact; only use an unambiguous text-paint match for figure coverage.
    """
    targets = [s for s in spans if len(s["text"]) == 1
               and tuple(s["direction"]) != (1.0, 0.0)]
    if not targets:
        return {}
    traces, painted = page.get_texttrace(), page.get_bboxlog()
    result = {}
    for span in targets:
        matches = [t for t in traces if len(t["chars"]) == 1
                   and t["chars"][0][0] == ord(span["text"])
                   and t["font"] == span["font"]
                   and tuple(t["dir"]) == tuple(span["direction"])
                   and pymupdf.Rect(t["bbox"]).intersects(pymupdf.Rect(span["bbox"]))]
        if len(matches) == 1:
            kind, box = painted[matches[0]["seqno"]][:2]
            if kind in {"fill-text", "stroke-text"}:
                result[span["id"]] = list(box)
    return result


def refresh_visuals(path: Path, source: dict):
    """Refresh old coverage metadata without re-extracting or changing stable text IDs."""
    with pymupdf.open(path) as pdf:
        for page, original in zip(pdf, source["pages"], strict=True):
            page.set_rotation(0)
            original.update(extract_visuals(page))
            original["rotated_glyph_bounds"] = rotated_glyph_bounds(page, original["spans"])
            original["text_paint_bounds"] = text_paint_bounds(page, original["spans"])
    source["extractor_version"] = EXTRACTOR_VERSION


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
                    "rotated_glyph_bounds": rotated_glyph_bounds(page, spans),
                    "text_paint_bounds": text_paint_bounds(page, spans),
                    **extract_visuals(page),
                }
            )
    return {
        "extractor_version": EXTRACTOR_VERSION,
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
