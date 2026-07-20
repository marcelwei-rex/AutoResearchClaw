from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalEvidenceArtifact,
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage15_decision_projection import (
    DECISION_POLICY_VERSION,
    build_stage15_decision_projection,
    fixed_domain_decision_prompt,
)
from researchclaw.pipeline.stage_impls._paper_writing import (
    _execute_paper_outline,
    _load_bound_stage15_decision,
)
from researchclaw.adapters import AdapterBundle


_METRIC_KEYS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)
_CONDITIONS = ("baseline_a", "baseline_b", "proposed")


def _metric_values(condition_index: int, seed: int, variant: int) -> dict[str, Decimal]:
    base = Decimal(condition_index + 1) / Decimal("10")
    delta = Decimal(seed * 18 + variant + 1) / Decimal("10000")
    return {
        key: base + delta + Decimal(index) / Decimal("1000")
        for index, key in enumerate(_METRIC_KEYS)
    }


def _evidence() -> SimpleNamespace:
    rows = []
    per_seed = []
    aggregates = []
    variants = tuple(
        (f"family-{family}", f"family-{family}-variant-{variant}")
        for family in range(6)
        for variant in range(3)
    )
    for condition_index, condition in enumerate(_CONDITIONS):
        for seed in (0, 1, 2):
            for variant_index, (family, variant) in enumerate(variants):
                rows.append(
                    {
                        "circuit_family": family,
                        "circuit_variant": variant,
                        "condition": condition,
                        "metrics": _metric_values(condition_index, seed, variant_index),
                        "n_total": 100,
                        "n_trojan": 10,
                        "seed": seed,
                    }
                )
            per_seed.append(
                {
                    "condition": condition,
                    "seed": seed,
                    "n_variants": 18,
                    "metrics": _metric_values(condition_index, seed, 0),
                }
            )
        aggregates.append(
            {
                "condition": condition,
                "n_seeds": 3,
                "metrics": {
                    key: {
                        "mean": Decimal(condition_index + 1) / Decimal("10"),
                        "std": Decimal("0.01"),
                        "min": Decimal(condition_index + 1) / Decimal("10") - Decimal("0.01"),
                        "max": Decimal(condition_index + 1) / Decimal("10") + Decimal("0.01"),
                    }
                    for key in _METRIC_KEYS
                },
            }
        )
    observations = {
        "schema_version": 2,
        "observation_policy_version": 1,
        "dataset_capture_sha256": "1" * 64,
        "score_evidence_sha256": "2" * 64,
        "metric_keys": list(_METRIC_KEYS),
        "observations": rows,
        "per_seed": per_seed,
        "aggregate": aggregates,
        "primary_metric": {
            "aggregation": "mean_variants_then_mean_seeds_v1",
            "condition": "proposed",
            "key": "auprc",
            "observation_set": "exact_18_variants_per_seed",
            "value": Decimal("0.3"),
        },
    }
    observation_bytes = canonical_authority_json_text(observations).encode("utf-8")
    contract_bytes = (
        "claim_scope: pipeline_validation\n"
        "dataset_origin: synthetic\n"
        "primary_metric:\n"
        "  key: auprc\n"
        "  direction: maximize\n"
        "metric_units:\n"
        + "".join(f"  {key}: ratio\n" for key in _METRIC_KEYS)
    ).encode("utf-8")
    manifest_sha256 = "3" * 64
    return SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256=manifest_sha256,
        manifest={
            "schema_version": 2,
            "generation_kind": "domain_evaluator",
            "bindings": {
                "claim_scope": "pipeline_validation",
                "dataset_origin": "synthetic",
                "evaluator_schema": "test_domain_v1",
            },
            "primary_metric": observations["primary_metric"],
        },
        experiment_contract_bytes=contract_bytes,
        selected_execution_artifact=CanonicalEvidenceArtifact(
            role="observations",
            path="stage-12/evidence-v2/observations.json",
            sha256=hashlib.sha256(observation_bytes).hexdigest(),
            content=observation_bytes,
        ),
    )


def test_projection_is_decimal_context_independent_and_bounded() -> None:
    evidence = _evidence()
    outputs = []
    for precision in (7, 28, 80):
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUND_DOWN if precision == 7 else ROUND_HALF_EVEN
            outputs.append(build_stage15_decision_projection(evidence))

    assert len({item.content for item in outputs}) == 1
    projection = outputs[0]
    assert projection.policy_version == DECISION_POLICY_VERSION
    assert projection.sha256 == hashlib.sha256(projection.content).hexdigest()
    text = projection.prompt_text
    assert text.count('"condition_id"') == 3
    assert text.count('"n_variants":18') == 9
    assert text.count('"n_seeds":3') == 3
    assert '"observation_count":162' in text
    assert '"rows_per_group":18' in text
    payload = json.loads(text, parse_float=Decimal)
    assert all(set(item) == {"key", "unit"} for item in payload["metric_policy"])
    assert payload["primary_metric"]["direction"] == "maximize"


def test_fixed_prompt_uses_deterministic_gates_not_subjective_rating() -> None:
    projection = build_stage15_decision_projection(_evidence())
    system, user = fixed_domain_decision_prompt(projection)

    assert "fixed, independently replayed" in system
    assert "deterministic_gates" in user
    assert "subjective analysis-quality score" in user
    assert "rating is" not in user.lower()
    assert projection.prompt_text in user


def test_stage16_rebuilds_projection_before_accepting_proceed(tmp_path: Path) -> None:
    evidence = _evidence()
    projection = build_stage15_decision_projection(evidence)
    stage15 = tmp_path / "stage-15"
    stage15.mkdir()
    decision = "## Decision\nPROCEED\n\n## Justification\nBound evidence is complete.\n"
    (stage15 / "decision.md").write_text(decision, encoding="utf-8")
    payload = {
        "decision": "proceed",
        "raw_text_excerpt": decision[:500],
        "quality_warnings": [],
        "generated": "2026-07-20T00:00:00+00:00",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "decision_path": "stage-15/decision.md",
        "decision_sha256": hashlib.sha256(decision.encode("utf-8")).hexdigest(),
        "decision_policy_version": projection.policy_version,
        "decision_projection_schema_version": projection.schema_version,
        "decision_projection_sha256": projection.sha256,
    }
    binding = stage15 / "decision_structured.json"
    binding.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_bound_stage15_decision(tmp_path, evidence)[0] == decision

    payload["decision_projection_sha256"] = "0" * 64
    binding.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="decision projection binding mismatch"):
        _load_bound_stage15_decision(tmp_path, evidence)


@pytest.mark.parametrize("invalid_version", (True, 1.0, "1", None))
def test_stage16_rejects_non_integer_projection_version_before_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_version: object,
) -> None:
    evidence = _evidence()
    projection = build_stage15_decision_projection(evidence)
    stage15 = tmp_path / "stage-15"
    stage16 = tmp_path / "stage-16"
    stage15.mkdir()
    stage16.mkdir()
    decision = "## Decision\nPROCEED\n\n## Justification\nComplete.\n"
    payload = {
        "decision": "proceed",
        "raw_text_excerpt": decision[:500],
        "quality_warnings": [],
        "generated": "2026-07-20T00:00:00+00:00",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "decision_path": "stage-15/decision.md",
        "decision_sha256": hashlib.sha256(decision.encode("utf-8")).hexdigest(),
        "decision_policy_version": projection.policy_version,
        "decision_projection_schema_version": invalid_version,
        "decision_projection_sha256": projection.sha256,
    }
    (stage15 / "decision.md").write_text(decision, encoding="utf-8")
    (stage15 / "decision_structured.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._paper_writing.load_canonical_experiment_evidence",
        lambda _run_dir: evidence,
    )

    class _UnexpectedLLM:
        calls = 0

        def chat(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("Stage 16 must reject before invoking the LLM")

    llm = _UnexpectedLLM()
    result = _execute_paper_outline(
        stage16,
        tmp_path,
        None,  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status.value == "failed"
    assert llm.calls == 0


def test_primary_direction_is_projected_from_bound_contract() -> None:
    evidence = _evidence()
    evidence.experiment_contract_bytes = evidence.experiment_contract_bytes.replace(
        b"direction: maximize", b"direction: minimize"
    )
    projection = build_stage15_decision_projection(evidence)
    payload = json.loads(projection.content, parse_float=Decimal)

    assert payload["primary_metric"]["direction"] == "minimize"
    assert all("direction" not in item for item in payload["metric_policy"])
