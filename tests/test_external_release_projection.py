from __future__ import annotations

from decimal import Decimal
import inspect

import pytest

from researchclaw.pipeline.external_release_projection import (
    ExternalReleaseProjectionError,
    _required_mapping,
    external_json_value,
)
from researchclaw.collaboration.publisher import ArtifactPublisher
from researchclaw.mcp.server import ResearchClawMCPServer


def test_external_projection_recursively_freezes_parsed_authority() -> None:
    frozen = _required_mapping(
        {"summary": {"total": 1}, "results": [{"status": "verified"}]},
        "verification",
    )
    with pytest.raises(TypeError):
        frozen["summary"]["total"] = 2
    with pytest.raises(TypeError):
        frozen["results"][0]["status"] = "hallucinated"
    assert isinstance(frozen["results"], tuple)


def test_external_json_value_preserves_decimal_as_exact_string() -> None:
    assert external_json_value(
        {"metric": Decimal("0.123456789012345678"), "rows": (Decimal("1"),)}
    ) == {"metric": "0.123456789012345678", "rows": ["1"]}


@pytest.mark.parametrize("value", [1.0, object()])
def test_external_json_value_rejects_non_authority_types(value: object) -> None:
    with pytest.raises(ExternalReleaseProjectionError):
        external_json_value(value)


def test_stateless_external_consumers_have_no_legacy_result_reads() -> None:
    sources = "\n".join(
        (
            inspect.getsource(ArtifactPublisher),
            inspect.getsource(ResearchClawMCPServer._handle_get_results),
        )
    )
    for forbidden in (
        "stage-14*",
        "experiment_results.json",
        ".glob(",
        ".read_text(",
    ):
        assert forbidden not in sources
