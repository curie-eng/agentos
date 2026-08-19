"""Validated trajectory sidecar models for deterministic eval scoring.

The sidecar lives beside ``evals/cases.json`` and is owned by the run layer.
It deliberately does not extend the frozen eval case format.
"""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from .scorer import TrajectoryMode, TrajectorySpec


class TrajectoryCaseSpec(BaseModel):
    """One case id and its deterministic trajectory expectation."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    case_id: str
    expected: list[str]
    mode: TrajectoryMode
    threshold: float = Field(default=1.0, strict=True, ge=0.0, le=1.0)

    def to_domain(self) -> TrajectorySpec:
        """Convert the transport model to the scorer domain model."""

        return TrajectorySpec(
            expected=tuple(self.expected),
            mode=self.mode,
            threshold=self.threshold,
        )


class TrajectorySidecar(BaseModel):
    """Trajectory specs supplied by the run layer above the frozen case port."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    specs: list[TrajectoryCaseSpec]

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> TrajectorySidecar:
        case_ids = [spec.case_id for spec in self.specs]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("trajectory spec case ids must be unique")
        return self

    def to_spec_map(self) -> dict[str, TrajectorySpec]:
        """Return the case id map consumed by :class:`TrajectoryScorer`."""

        return {spec.case_id: spec.to_domain() for spec in self.specs}
