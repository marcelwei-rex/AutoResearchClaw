"""C0 interface boundary for the future Stage 12 single-invocation controller."""

from __future__ import annotations

from dataclasses import dataclass

from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)


@dataclass(frozen=True)
class InvocationLease:
    """Opaque controller-issued lease required by the C1 sandbox wrapper."""

    ordinal: int
    invocation_token: str
    generation_binding_sha256: str


class CanonicalExecutionController:
    """Deny-only C0 controller interface; C1 owns journal persistence."""

    def acquire(self, *, generation_binding_sha256: str) -> InvocationLease:
        del generation_binding_sha256
        require_canonical_evidence_capabilities("CanonicalExecutionController.acquire")
        raise AssertionError("capability gate returned before C5 activation")


def require_controller_lease(lease: object) -> InvocationLease:
    """Reject direct canonical Stage 12 sandbox calls without a real lease."""
    if not isinstance(lease, InvocationLease):
        raise PermissionError("canonical_stage12_invocation_lease_required")
    if lease.ordinal != 1:
        raise PermissionError("canonical_stage12_invocation_ordinal_invalid")
    return lease
