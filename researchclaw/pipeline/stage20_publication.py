"""Deterministic Stage 20 state reconstructed from canonical evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Mapping

from researchclaw.config import RCConfig
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    canonical_decimal,
)
from researchclaw.pipeline.verified_registry import VerifiedRegistry


@dataclass(frozen=True)
class Stage20FabricationState:
    experiment_failed: bool
    real_metric_values: tuple[str, ...]
    verified_values_count: int
    verified_conditions: tuple[str, ...]
    has_real_data: bool
    fabrication_suspected: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_failed": self.experiment_failed,
            "real_metric_values": list(self.real_metric_values),
            "verified_values_count": self.verified_values_count,
            "verified_conditions": list(self.verified_conditions),
            "has_real_data": self.has_real_data,
            "fabrication_suspected": self.fabrication_suspected,
        }


def reconstruct_stage20_fabrication_state(
    evidence: CanonicalExperimentEvidence,
    canonical_config: RCConfig,
) -> Stage20FabricationState:
    """Rebuild every deterministic Stage 20 fabrication field from authority."""

    summary = _thaw_authority_value(evidence.summary)
    if not isinstance(summary, dict):
        raise ValueError("canonical experiment summary is not an object")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        registry = VerifiedRegistry.from_experiment(
            summary,
            metric_direction=canonical_config.experiment.metric_direction,
        )
        real_metric_values = tuple(
            sorted({canonical_decimal(value) for value in registry.values})
        )
    count = len(registry.canonical_values)
    has_real_data = count > 0
    experiment_failed = not has_real_data
    return Stage20FabricationState(
        experiment_failed=experiment_failed,
        real_metric_values=real_metric_values,
        verified_values_count=count,
        verified_conditions=tuple(sorted(registry.condition_names)),
        has_real_data=has_real_data,
        fabrication_suspected=experiment_failed,
    )


def thaw_canonical_summary(evidence: CanonicalExperimentEvidence) -> dict[str, object]:
    """Return a prompt-only mutable copy without changing Decimal authority."""

    value = _thaw_authority_value(evidence.summary)
    if not isinstance(value, dict):
        raise ValueError("canonical experiment summary is not an object")
    return value


def _thaw_authority_value(value: object) -> object:
    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return {str(key): _thaw_authority_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_authority_value(item) for item in value]
    return value
