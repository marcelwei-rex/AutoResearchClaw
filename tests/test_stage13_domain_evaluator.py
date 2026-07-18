from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    validate_refinement_result_set,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline import stage13_domain_evaluator
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.stage13_domain_evaluator import (
    Stage13DomainEvaluatorError,
    parse_domain_evaluator_refinement_result_set,
)
from researchclaw.pipeline.stage_impls._execution import (
    _execute_experiment_run,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage10_evaluator_capture import _execute_capture, _prepare_run


def test_stage13_domain_replay_checks_capability_before_opening_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(CANONICAL_EVIDENCE_CAPABILITIES, "domain_evaluator_authority", 0)

    def unexpected_namespace_access(*_args, **_kwargs):
        raise AssertionError("capability guard must run before Stage 13 namespace access")

    monkeypatch.setattr(
        stage13_domain_evaluator.ReleaseGraphLock,
        "acquire",
        unexpected_namespace_access,
    )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        stage13_domain_evaluator.validate_domain_evaluator_refinement_result_set(
            tmp_path / "missing-run", object()  # type: ignore[arg-type]
        )


def test_stage13_domain_evaluator_publishes_fixed_baseline_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_run(tmp_path)
    resumed = run / "config.resumed-20260718-120000.yaml"
    resumed.write_bytes((run / "config.yaml").read_bytes())
    write_active_config_binding(run, resumed)
    assert _execute_capture(run, config).status == StageStatus.DONE
    assert _execute_experiment_run(
        run / "stage-12", run, config, AdapterBundle()
    ).status == StageStatus.DONE

    llm_factory_calls = 0

    def counted_llm_factory(*_args, **_kwargs):
        nonlocal llm_factory_calls
        llm_factory_calls += 1
        return None

    def unexpected_sandbox_factory(*_args, **_kwargs):
        raise AssertionError("fixed Stage 13 evaluator must not construct a sandbox")

    monkeypatch.setattr(
        "researchclaw.pipeline.executor._create_configured_llm",
        counted_llm_factory,
    )
    monkeypatch.setattr(
        "researchclaw.experiment.factory.create_sandbox",
        unexpected_sandbox_factory,
    )
    result = execute_stage(
        stage=Stage.ITERATIVE_REFINE,
        run_dir=run,
        run_id="stage13-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status == StageStatus.DONE, result.error
    assert llm_factory_calls == 0
    assert result.artifacts == ("refinement_result_set.json",)
    assert result.evidence_refs == ("stage-13/refinement_result_set.json",)
    assert sorted(path.name for path in (run / "stage-13").iterdir()) == [
        "refinement_result_set.json"
    ]
    manifest = validate_refinement_result_set(run, config)
    assert manifest["schema_version"] == 2
    assert manifest["refinement_mode"] == "fixed_evaluator_no_refine"
    assert manifest["run_config"]["path"] == resumed.name
    assert manifest["iterations"] == []
    assert manifest["selected_result"] == {"type": "baseline", "iteration_id": None}

    stage13 = run / "stage-13"
    manifest_path = stage13 / "refinement_result_set.json"
    manifest_bytes = manifest_path.read_bytes()
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="does not accept a manifest override",
    ):
        validate_refinement_result_set(run, config, manifest_bytes.decode("utf-8"))

    def assert_manifest_rejected(mutator) -> None:
        candidate = deepcopy(manifest)
        mutator(candidate)
        manifest_path.write_text(
            canonical_authority_json_text(candidate), encoding="utf-8"
        )
        try:
            with pytest.raises(
                (CanonicalExperimentEvidenceError, Stage13DomainEvaluatorError)
            ):
                validate_refinement_result_set(run, config)
        finally:
            manifest_path.write_bytes(manifest_bytes)

    assert_manifest_rejected(lambda payload: payload.__setitem__("schema_version", True))
    assert_manifest_rejected(
        lambda payload: payload.__setitem__("legacy_refinement_log", {})
    )
    assert_manifest_rejected(
        lambda payload: payload.__setitem__("iterations", [{"iteration_id": "x"}])
    )
    assert_manifest_rejected(
        lambda payload: payload["observations"].__setitem__("size", True)
    )
    assert_manifest_rejected(
        lambda payload: payload.__setitem__("metric_authority", {"forged": True})
    )
    assert_manifest_rejected(
        lambda payload: payload["primary_metric"].__setitem__("value", 0)
    )
    assert_manifest_rejected(
        lambda payload: payload["run_config"].__setitem__("path", "config.yaml")
    )
    for field in (
        "baseline_manifest",
        "experiment_contract",
        "sealed_candidate_manifest",
        "capture_manifest",
        "execution_policy",
        "observations",
        "run_config",
    ):
        assert_manifest_rejected(
            lambda payload, field=field: payload[field].__setitem__("sha256", "0" * 64)
        )
        assert_manifest_rejected(
            lambda payload, field=field: payload[field].__setitem__(
                "size", payload[field]["size"] + 1
            )
        )
    duplicate_schema = manifest_bytes.decode("utf-8").replace(
        '"schema_version":2', '"schema_version":2,"schema_version":2', 1
    )
    with pytest.raises(CanonicalExperimentEvidenceError, match="duplicate"):
        parse_domain_evaluator_refinement_result_set(duplicate_schema)

    shadow = stage13 / "SHADOW_POISON"
    shadow.write_text("poison\n", encoding="utf-8")
    with pytest.raises(Stage13DomainEvaluatorError, match="namespace is not exact"):
        validate_refinement_result_set(run, config)
    shadow.unlink()

    original_validate = stage13_domain_evaluator.validate_experiment_result_set
    mutated = False

    def mutate_after_stage12_replay(*args, **kwargs):
        nonlocal mutated
        result = original_validate(*args, **kwargs)
        if not mutated:
            mutated = True
            manifest_path.write_text("{}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        stage13_domain_evaluator,
        "validate_experiment_result_set",
        mutate_after_stage12_replay,
    )
    try:
        with pytest.raises(Stage13DomainEvaluatorError, match="authority changed"):
            validate_refinement_result_set(run, config)
    finally:
        manifest_path.write_bytes(manifest_bytes)
