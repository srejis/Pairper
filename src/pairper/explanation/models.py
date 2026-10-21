"""Strict explanation payloads. Prose and mathematical symbols stay separate."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TextSegment(Strict):
    kind: Literal['text'] = 'text'
    text: str = Field(min_length=1)


class MathSegment(Strict):
    kind: Literal['math'] = 'math'
    latex: str = Field(min_length=1)


Segment = Annotated[TextSegment | MathSegment, Field(discriminator='kind')]


class Symbol(Strict):
    symbol: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class Explanation(Strict):
    target_id: str = Field(min_length=1)
    kind: Literal['term', 'math']
    math_role: Literal['symbol', 'formula', 'literal', 'notation'] = 'formula'
    exclusion_reason: str = ''
    category: str | None = None
    description: list[Segment] = Field(default_factory=list)
    symbols: list[Symbol] = Field(default_factory=list)
    meaning: list[Segment] = Field(default_factory=list)
    process: list[Segment] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reviewed: bool = False

    @model_validator(mode='after')
    def coherent(self):
        if self.kind == 'term':
            if not self.category or not self.category.strip() or not self.description:
                raise ValueError('Term needs a category followed by a direct description')
            if self.symbols or self.meaning or self.process:
                raise ValueError('Formula fields do not belong to term explanations')
        elif self.math_role == 'notation':
            if not self.exclusion_reason.strip() or self.symbols or self.meaning or self.process:
                raise ValueError('Notation needs a reviewed exclusion reason and no fabricated explanation')
        elif not self.meaning or (self.math_role == 'symbol' and not self.symbols) or (
                self.math_role == 'formula' and not self.process):
            raise ValueError('Formula needs symbol meanings, overall meaning and evaluation process')
        if self.kind == 'math' and (self.category or self.description):
            raise ValueError('Term fields do not belong to formula explanations')
        if self.math_role != 'notation' and self.exclusion_reason:
            raise ValueError('Only non-content notation has an exclusion reason')
        for prose in (self.description, self.meaning, self.process):
            if prose and prose[0].kind != 'text':
                raise ValueError('Write human meaning before inline mathematical symbols')
        for name in ('description', 'meaning', 'process'):
            if any(s.kind == 'text' and not s.text.strip() for s in getattr(self, name)):
                raise ValueError('Explanation prose must not be whitespace')
        if len({s.symbol for s in self.symbols}) != len(self.symbols):
            raise ValueError('Duplicate formula symbol')
        return self
