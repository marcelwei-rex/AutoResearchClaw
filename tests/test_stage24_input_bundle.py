from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.config import RCConfig
from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.stage15_critique import Stage15CritiquePublication
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage22_publication import Stage22PublicationSnapshot
from researchclaw.pipeline.stage23_input_bundle import Stage23InputBundle
from researchclaw.pipeline.stage23_verification import Stage23PublicationSnapshot
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundleError,
    load_stage24_input_bundle,
    verify_stage24_input_bundle_unchanged,
)
from researchclaw.pipeline.stage_impls._release_audit import _execute_truth_audit
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationError,
    Stage24PublicationSnapshot,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CAPABILITY_SCHEMA_VERSION,
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.stages import StageStatus


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


@pytest.fixture(autouse=True)
def _enable_complete_capability_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: CAPABILITY_SCHEMA_VERSION
            for name in CANONICAL_EVIDENCE_CAPABILITIES
        },
    )


def test_stage24_bundle_guard_precedes_stage23_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {**CANONICAL_EVIDENCE_CAPABILITIES, "independent_release_reconstruction": 0},
    )
    calls = 0

    def read_stage23(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("Stage 23 must not be read before the capability guard")

    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage23_input_bundle",
        read_stage23,
    )
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        load_stage24_input_bundle(tmp_path / "run", config)

    assert calls == 0


def _fixture_graph(tmp_path: Path, config: RCConfig):
    run_dir = tmp_path / "run"
    stage15 = run_dir / "stage-15"
    stage15.mkdir(parents=True)
    canonical = _bound("canonical_experiment_evidence.json", b'{"root":1}\n')
    contract = _bound("stage-09/experiment_contract.yaml", b"schema_version: 2\n")
    (run_dir / canonical.path).write_bytes(canonical.content)
    critique = _bound("stage-15/critique.json", b'{"state":"model_final"}\n')
    critique_manifest = _bound(
        "stage-15/stage15_critique_manifest.json", b'{"sealed":true}\n'
    )
    (run_dir / critique.path).write_bytes(critique.content)
    (run_dir / critique_manifest.path).write_bytes(critique_manifest.content)
    evidence = SimpleNamespace(
        manifest_path=canonical.path,
        manifest_sha256=canonical.sha256,
        experiment_contract_path=contract.path,
        experiment_contract_sha256=contract.sha256,
        experiment_contract_bytes=contract.content,
        selected_execution_artifact=SimpleNamespace(
            path="stage-12/evidence-v1/run-1.json",
            sha256="1" * 64,
            content=b'{"metric_observations":{}}\n',
        ),
    )
    citation_plan = _bound("stage-16/citation_plan.json", b'{"claims":[]}\n')
    card = _bound("stage-06/cards/card-001.json", b'{"card_id":"card-001"}\n')
    active_config = _bound("config.yaml", b"project: test\n")
    stage19 = SimpleNamespace(
        artifacts=(citation_plan, card, active_config),
        card_artifacts=(card,),
    )
    revised = _bound("stage-19/paper_revised.md", b"Revised.\n")
    revision_binding = _bound("stage-19/revision_evidence_binding.json", b"{}\n")
    stage20 = SimpleNamespace(
        revised_paper=revised,
        publication_binding=revision_binding,
    )
    stage21 = SimpleNamespace(
        quality_report=_bound("stage-20/quality_report.json", b"{}\n"),
        fabrication_flags=_bound("stage-20/fabrication_flags.json", b"{}\n"),
        quality_gate_manifest=_bound("stage-20/quality_gate_manifest.json", b"{}\n"),
    )
    stage22_inputs = SimpleNamespace(
        evidence=evidence,
        canonical_config=config,
        stage19_inputs=stage19,
        stage20_inputs=stage20,
        stage21_inputs=stage21,
        citation_authority=SimpleNamespace(
            plan=MappingProxyType({"claims": ()}),
            effective_policy=MappingProxyType({"effective_min_unique_sources": 0}),
        ),
    )
    stage22_publication = Stage22PublicationSnapshot(
        manifest=_bound("stage-22/stage22_export_manifest.json", b"{}\n"),
        outputs=(_bound("stage-22/paper_final.md", b"Paper.\n"),),
    )
    stage23_inputs = Stage23InputBundle(
        stage22_inputs=stage22_inputs,  # type: ignore[arg-type]
        publication=stage22_publication,
        paper=stage22_publication.outputs[0],
        bibliography=_bound("stage-22/references.bib", b"% none\n"),
        latex=_bound("stage-22/paper.tex", b"Paper.\n"),
        cited_keys=(),
        claim_scope="pipeline_validation",
    )
    stage23_publication = Stage23PublicationSnapshot(
        manifest=_bound("stage-23/stage23_verification_manifest.json", b"{}\n"),
        outputs=(
            _bound("stage-23/paper_final_verified.md", b"Paper.\n"),
            _bound("stage-23/references_verified.bib", b"% none\n"),
            _bound("stage-23/verification_report.json", b'{"results":[]}\n'),
        ),
    )
    critique_publication = Stage15CritiquePublication(
        state="model_final",
        critique=MappingProxyType({"state": "model_final"}),
        manifest=MappingProxyType({"critique_sha256": critique.sha256}),
        artifacts=(critique.path, critique_manifest.path),
    )
    return (
        run_dir,
        stage23_inputs,
        stage23_publication,
        critique_publication,
    )


def _patch_graph(monkeypatch, stage23_inputs, stage23_publication, critique):
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage23_input_bundle",
        lambda *_args: stage23_inputs,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage23_verification_publication",
        lambda *_args: stage23_publication,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.verify_stage23_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage15_critique_publication",
        lambda *_args: critique,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.parse_stage23_verification_report",
        lambda *_args: {"summary": {"fatal": False}, "results": []},
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.validate_contract_dict",
        lambda *_args: SimpleNamespace(dataset_origin="synthetic"),
    )


def test_stage24_bundle_binds_raw_stage23_paper_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    _patch_graph(monkeypatch, inputs, publication, critique)

    bundle = load_stage24_input_bundle(run_dir, config)

    assert bundle.paper.content == b"Paper.\n"
    assert bundle.paper.sha256 == hashlib.sha256(b"Paper.\n").hexdigest()
    assert bundle.model_projection.writer_model == config.llm.primary_model
    assert len(bundle.identity_sha256) == 64


def test_stage24_bundle_identity_changes_for_whitespace_distinct_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    _patch_graph(monkeypatch, inputs, publication, critique)
    first = load_stage24_input_bundle(run_dir, config)
    changed = replace(
        publication,
        outputs=tuple(
            _bound(item.path, b"Paper.  \n")
            if item.path.endswith("paper_final_verified.md")
            else item
            for item in publication.outputs
        ),
    )
    _patch_graph(monkeypatch, inputs, changed, critique)
    second = load_stage24_input_bundle(run_dir, config)

    assert first.paper.text().strip() == second.paper.text().strip()
    assert first.paper.sha256 != second.paper.sha256
    assert first.identity_sha256 != second.identity_sha256


def test_stage24_bundle_rejects_writer_equal_assessment_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    bad = replace(
        config,
        paper_revision=replace(
            config.paper_revision, critic_model=config.llm.primary_model
        ),
    )
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, bad)
    _patch_graph(monkeypatch, inputs, publication, critique)

    with pytest.raises(Stage24InputBundleError, match="must differ"):
        load_stage24_input_bundle(run_dir, bad)


def test_stage24_bundle_rejects_same_path_under_multiple_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    duplicate = inputs.stage22_inputs.stage19_inputs.artifacts[0]
    inputs.stage22_inputs.stage20_inputs.revised_paper = duplicate
    _patch_graph(monkeypatch, inputs, publication, critique)

    with pytest.raises(Stage24InputBundleError, match="multiple logical roles"):
        load_stage24_input_bundle(run_dir, config)


def test_stage24_bundle_values_are_recursively_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    _patch_graph(monkeypatch, inputs, publication, critique)
    bundle = load_stage24_input_bundle(run_dir, config)

    with pytest.raises(TypeError):
        bundle.citation_plan["claims"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.verification["results"] = ()  # type: ignore[index]


def test_stage24_bundle_final_fixpoint_rejects_changed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    _patch_graph(monkeypatch, inputs, publication, critique)
    bundle = load_stage24_input_bundle(run_dir, config)
    changed = replace(bundle, identity_sha256="f" * 64)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage24_input_bundle",
        lambda *_args: changed,
    )

    with pytest.raises(Stage24InputBundleError, match="changed after capture"):
        verify_stage24_input_bundle_unchanged(run_dir, config, bundle)


def test_stage24_bundle_rejects_real_critique_mutation_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir, inputs, publication, critique = _fixture_graph(tmp_path, config)
    _patch_graph(monkeypatch, inputs, publication, critique)

    def mutate_after_replay(*_args):
        (run_dir / "stage-15" / "critique.json").write_bytes(b'{"changed":true}\n')
        return critique

    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_input_bundle.load_stage15_critique_publication",
        mutate_after_replay,
    )

    with pytest.raises(Stage24InputBundleError, match="changed during capture"):
        load_stage24_input_bundle(run_dir, config)


def test_truth_audit_rejects_bundle_before_first_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)

    class SpyLLM:
        calls = 0

        def chat(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("LLM must not be called")

    llm = SpyLLM()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.load_stage24_input_bundle",
        lambda *_args: (_ for _ in ()).throw(
            Stage24InputBundleError("invalid canonical graph")
        ),
    )

    result = _execute_truth_audit(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert llm.calls == 0
    assert tuple(stage_dir.iterdir()) == ()


def test_truth_audit_maps_canonical_publisher_snapshot_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)

    snapshot = Stage24PublicationSnapshot(
        manifest=_bound("stage-24/stage24_truth_manifest.json", b"{}\n"),
        paper=_bound("stage-23/paper_final_verified.md", b"Paper.\n"),
        outputs=(_bound("stage-24/truth_audit.json", b"{}\n"),),
        assessment_files=(),
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._release_audit.execute_stage24_truth",
        lambda *_args, **_kwargs: snapshot,
    )

    result = _execute_truth_audit(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=None,
    )

    assert result.status is StageStatus.DONE
    assert result.artifacts == (
        "truth_audit.json",
        "stage24_truth_manifest.json",
    )


def test_truth_audit_cleanup_does_not_short_circuit_on_unsafe_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    (stage_dir / "claims.json" / "nested").mkdir(parents=True)
    for name in (
        "citations.json",
        "citation_support.json",
        "critique_resolution.json",
        "truth_audit.json",
    ):
        (stage_dir / name).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.load_stage24_input_bundle",
        lambda *_args: object(),
    )

    result = _execute_truth_audit(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert "authority cleanup was incomplete" in (result.error or "")


def test_truth_audit_rejects_stage24_parent_symlink_without_external_deletion(
    tmp_path: Path,
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    owned = (
        "truth_audit.json",
        "critique_resolution.json",
        "citation_support.json",
        "citations.json",
        "claims.json",
    )
    for name in owned:
        (external / name).write_text(f"external-{name}", encoding="utf-8")
    stage_dir = run_dir / "stage-24"
    stage_dir.symlink_to(external, target_is_directory=True)

    result = _execute_truth_audit(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert "Truth audit failed" in (result.error or "")
    assert {
        name: (external / name).read_text(encoding="utf-8") for name in owned
    } == {name: f"external-{name}" for name in owned}


def test_truth_audit_parent_replacement_only_cleans_held_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    (stage_dir / "truth_audit.json").write_text("stale", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "truth_audit.json"
    sentinel.write_text("external", encoding="utf-8")
    moved = run_dir / "stage-24-moved"

    from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace

    original = BoundOutputNamespace.remove_flat_entries
    replaced = False

    def replace_parent(namespace, names):
        nonlocal replaced
        if not replaced:
            stage_dir.rename(moved)
            stage_dir.symlink_to(external, target_is_directory=True)
            replaced = True
        return original(namespace, names)

    monkeypatch.setattr(BoundOutputNamespace, "remove_flat_entries", replace_parent)

    result = _execute_truth_audit(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert tuple(moved.iterdir()) == ()


def test_truth_audit_maps_capability_failure_to_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.load_stage24_input_bundle",
        lambda *_args: (_ for _ in ()).throw(
            CanonicalEvidenceMigrationIncomplete("stage24", ("remaining",))
        ),
    )

    result = _execute_truth_audit(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert "canonical_evidence_migration_incomplete" in (result.error or "")
