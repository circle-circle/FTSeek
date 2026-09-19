"""Schema and evidence contract for the review bundle.

Every metric in a review bundle must be independently reproducible.  A metric
without evidence is not a missing cosmetic field: it is unusable for a trading
decision and must fail validation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, Optional, Tuple


SCHEMA_VERSION = "review_bundle/1"
ALLOWED_KINDS = ("sql", "artifact", "log", "derived")
ALLOWED_UNITS = ("usd", "pct", "pp", "count", "ratio", "text", "bool")
REQUIRED_INTEGRITY = ("n", "window", "universe", "truncation_checked")


class EvidenceError(ValueError):
    """Raised when a review metric cannot be reproduced or audited."""


@dataclass(frozen=True)
class Evidence:
    kind: str
    ref: str
    params: Tuple[Any, ...] = ()
    note: str = ""
    integrity: Dict[str, Any] = field(default_factory=dict)

    def validate(self, owner: str) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise EvidenceError(f"{owner}: evidence.kind is invalid: {self.kind!r}")
        if not str(self.ref or "").strip():
            raise EvidenceError(f"{owner}: evidence.ref is empty")

        if self.kind not in ("artifact", "derived"):
            return

        missing = [key for key in REQUIRED_INTEGRITY if key not in self.integrity]
        if missing:
            raise EvidenceError(f"{owner}: evidence.integrity is missing {', '.join(missing)}")

        sample_n = self.integrity["n"]
        if not isinstance(sample_n, int) or isinstance(sample_n, bool) or sample_n < 0:
            raise EvidenceError(f"{owner}: evidence.integrity.n must be a non-negative integer")
        if not str(self.integrity["window"] or "").strip():
            raise EvidenceError(f"{owner}: evidence.integrity.window is empty")
        if not str(self.integrity["universe"] or "").strip():
            raise EvidenceError(f"{owner}: evidence.integrity.universe is empty")
        if not isinstance(self.integrity["truncation_checked"], bool):
            raise EvidenceError(f"{owner}: evidence.integrity.truncation_checked must be bool")


@dataclass
class Metric:
    name: str
    value: Any
    n: int
    unit: str
    evidence: Optional[Evidence] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def validate(self, path: str = "") -> None:
        owner = path or self.name
        if not str(self.name or "").strip():
            raise EvidenceError(f"{owner}: metric.name is empty")
        if self.unit not in ALLOWED_UNITS:
            raise EvidenceError(f"{owner}: metric.unit is invalid: {self.unit!r}")
        if not isinstance(self.n, int) or isinstance(self.n, bool) or self.n < 0:
            raise EvidenceError(f"{owner}: metric.n must be a non-negative integer")
        if self.evidence is None:
            raise EvidenceError(f"{owner}: evidence is required")
        self.evidence.validate(owner)


@dataclass
class Bundle:
    report_date: str
    headline: Dict[str, Metric]
    schema_version: str = SCHEMA_VERSION
    sections: Dict[str, Any] = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    pending_adjudications: list = field(default_factory=list)
    self_audit: Dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    git: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EvidenceError(f"bundle.schema_version must be {SCHEMA_VERSION!r}")
        try:
            date.fromisoformat(self.report_date)
        except (TypeError, ValueError) as exc:
            raise EvidenceError("bundle.report_date must be YYYY-MM-DD") from exc
        if not isinstance(self.headline, dict) or not self.headline:
            raise EvidenceError("bundle.headline must be a non-empty mapping")

        for path, metric in _iter_metrics(self.headline, "headline"):
            metric.validate(path)
        for path, metric in _iter_metrics(self.sections, "sections"):
            metric.validate(path)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


def _iter_metrics(node: Any, path: str) -> Iterable[Tuple[str, Metric]]:
    if isinstance(node, Metric):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_metrics(value, child_path)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _iter_metrics(value, f"{path}[{index}]")
