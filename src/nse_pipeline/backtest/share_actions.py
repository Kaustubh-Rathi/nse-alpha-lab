"""Exact rational share transformations, scoped to a proven security key."""
from dataclasses import dataclass
from fractions import Fraction

from .accounting_types import Evidence, identifier, integer


@dataclass(frozen=True)
class ShareAction:
    action_id: str
    kind: str
    numerator: int
    denominator: int
    evidence: Evidence

    def __post_init__(self):
        identifier(self.action_id)
        integer(self.numerator, minimum=1)
        integer(self.denominator, minimum=1)
        if self.kind not in ("split", "bonus") or not isinstance(self.evidence, Evidence):
            raise ValueError("Only evidence-scoped split/bonus terms are supported")

    @property
    def factor(self):
        ratio = Fraction(self.numerator, self.denominator)
        return 1 + ratio if self.kind == "bonus" else ratio
