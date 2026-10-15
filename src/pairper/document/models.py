"""Strict, source-referencing interchange contract. Pages are one-based PDF coordinates."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Region(StrictModel):
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    rotation: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def valid_box(self):
        x0, y0, x1, y1 = self.bbox
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
            raise ValueError("Region must have positive area and nonnegative coordinates")
        return self


class TextRun(StrictModel):
    kind: Literal["text"] = "text"
    refs: list[str] = Field(min_length=1)
    join: Literal["space", "none", "newline", "dehyphenate"] = "space"
    before: Literal["auto", "none", "space", "newline"] = "auto"


class MathRun(StrictModel):
    kind: Literal["math"] = "math"
    latex: str
    region: Region
    refs: list[str] = Field(default_factory=list)
    # Human/AI review is recorded; it is never inferred from valid LaTeX syntax.
    source_verified: bool = False
    before: Literal["auto", "none", "space", "newline"] = "auto"


Run = Annotated[TextRun | MathRun, Field(discriminator="kind")]


class Cell(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    rowspan: int = Field(default=1, ge=1, le=100)
    colspan: int = Field(default=1, ge=1, le=100)
    header: bool = False
    runs: list[Run] = Field(default_factory=list)


class Content(StrictModel):
    runs: list[Run] = Field(default_factory=list)
    level: int = Field(default=2, ge=2, le=6)
    items: list[list[Run]] = Field(default_factory=list)
    ordered: bool = False
    region: Region | None = None
    latex: str | None = None
    number_refs: list[str] = Field(default_factory=list)
    cells: list[Cell] = Field(default_factory=list)
    source_verified: bool = False
    caption_id: str | None = None
    figure_kind: str | None = None
    asset_id: str | None = None


BlockType = Literal[
    "title",
    "authors",
    "heading",
    "paragraph",
    "list",
    "equation",
    "figure",
    "table",
    "caption",
    "footnote",
    "reference",
    "source_region",
]


class Block(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    type: BlockType
    order: int = Field(ge=0)
    source_refs: list[str] = Field(default_factory=list)
    content: Content
    validation_status: Literal["unverified", "source_verified", "fallback"] = "unverified"

    @model_validator(mode="after")
    def structure(self):
        c = self.content
        if self.type in {"equation", "figure", "table", "source_region"} and c.region is None:
            raise ValueError("Visual blocks require a source region")
        if self.type not in {"equation", "figure", "table", "source_region"} and c.region:
            raise ValueError("Use inline math regions or a separate visual block")
        if self.type == "list":
            if not c.items or c.runs or c.cells:
                raise ValueError("List blocks require items only")
        elif self.type in {"equation", "figure", "table", "source_region"}:
            if c.runs or c.items:
                raise ValueError("Visual blocks cannot hide text runs")
        elif not c.runs or c.items or c.cells:
            raise ValueError("Text blocks require runs only")
        if self.type != "table" and c.cells:
            raise ValueError("Cells are only valid for table blocks")
        if self.type != "equation" and (c.latex or c.number_refs):
            raise ValueError("Block LaTeX and number references are equation-only")
        return self


class Omission(StrictModel):
    ref: str
    category: Literal["page_number", "repeated_header", "repeated_footer"]
    reason: str = Field(min_length=10)


class Document(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    paper_id: str
    source_sha256: str
    blocks: list[Block] = Field(default_factory=list)
    omissions: list[Omission] = Field(default_factory=list)
    visual_review_pages: list[int] = Field(default_factory=list)


def rendered_refs(block: Block) -> list[str]:
    """Only count text that will actually be displayed, never decorative declarations."""
    c = block.content
    runs = list(c.runs) + [r for item in c.items for r in item]
    if block.type == "table" and c.source_verified and c.cells:
        runs += [r for cell in c.cells for r in cell.runs]
    return [ref for run in runs for ref in run.refs] + list(c.number_refs)
