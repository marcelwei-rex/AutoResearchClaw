from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.config import RCConfig
from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalProjectArtifact,
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage22_export import (
    Stage22ExportError,
    export_canonical_stage22,
)
from researchclaw.pipeline.stage22_publication import (
    Stage22PublicationError,
    load_stage22_export_publication,
    parse_stage22_export_manifest,
    publish_stage22_outputs,
    validate_stage22_export_publication,
)
from researchclaw.pipeline.stage22_semantics import (
    build_stage22_deterministic_outputs,
)
from researchclaw.pipeline.stage23_input_bundle import load_stage23_input_bundle
from researchclaw.pipeline import stage22_export as export_module
from researchclaw.pipeline.stage_impls._review_publish import (
    _execute_export_publish_legacy_disabled,
)


@pytest.fixture
def canonical_config() -> RCConfig:
    return RCConfig.load(
        Path(__file__).parents[1] / "config.deepseek.sectional-dry-run.yaml",
        check_paths=False,
    )


def _bound(path: str, content: str) -> BoundArtifact:
    raw = content.encode("utf-8")
    return BoundArtifact(path, hashlib.sha256(raw).hexdigest(), raw)


def _bundle(config: RCConfig) -> SimpleNamespace:
    project = (
        CanonicalProjectArtifact(
            logical_name="detector_plugin.py",
            source_path="stage-10/selected_candidate/detector_plugin.py",
            sha256=hashlib.sha256(b"import numpy\n").hexdigest(),
            content=b"import numpy\n",
        ),
        CanonicalProjectArtifact(
            logical_name="main.py",
            source_path="stage-10/selected_candidate/main.py",
            sha256=hashlib.sha256(b"print('canonical')\n").hexdigest(),
            content=b"print('canonical')\n",
        ),
    )
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
        selected_result_manifest_path="stage-13/refinement_result_set.json",
        selected_result_manifest_sha256="b" * 64,
        project_artifacts=project,
        summary=MappingProxyType(
            {
                "best_run": MappingProxyType(
                    {
                        "status": "success",
                        "metrics": MappingProxyType({"detection_f1": 0.5}),
                    }
                ),
                "condition_summaries": MappingProxyType({}),
                "metrics_summary": MappingProxyType({}),
            }
        ),
    )
    bibliography = _bound(
        "stage-04/references.bib",
        "@article{smith2024test,\n  title={Test},\n  year={2024}\n}\n",
    )
    stage19_inputs = SimpleNamespace(bibliography=bibliography)
    revised = _bound(
        "stage-19/paper_revised.md",
        "## Canonical Paper\n\n## Abstract\nText.\n\n## Conclusion\nDone.\n",
    )
    stage20 = SimpleNamespace(
        revised_paper=revised,
        publication_mode="legacy",
        publication_binding=_bound(
            "stage-19/revision_evidence_binding.json", "binding\n"
        ),
    )
    stage21 = SimpleNamespace(
        quality_gate_manifest=_bound(
            "stage-20/quality_gate_manifest.json", "gate\n"
        ),
        quality_gate_outcome="passed",
    )
    return SimpleNamespace(
        evidence=evidence,
        canonical_config=config,
        stage19_inputs=stage19_inputs,
        stage20_inputs=stage20,
        stage21_inputs=stage21,
        claim_scope=config.experiment.claim_scope,
    )


_GENERATED = "2026-07-15T00:00:00+00:00"


def _publication_payload(
    bundle: SimpleNamespace,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    deterministic = build_stage22_deterministic_outputs(
        bundle, generated=_GENERATED
    )
    direct = dict(deterministic.direct_files)
    direct["compile_status.json"] = canonical_authority_json_text(
        {
            "schema_version": 1,
            "success": False,
            "attempts": 0,
            "errors": ["pdflatex not installed"],
            "status": "toolchain_missing",
            "tooling_available": False,
            "generated": _GENERATED,
        }
    ).encode("utf-8")
    return direct, dict(deterministic.code_files)


def _nested_code_bundle(config: RCConfig) -> SimpleNamespace:
    bundle = _bundle(config)
    files = {
        "main.py": b"print('canonical')\n",
        "trojnet/anomaly.py": b"def score():\n    return 1\n",
        "data/c1355/c1355_ht1.bench": b"INPUT(a)\nOUTPUT(z)\nz = a\n",
    }
    bundle.evidence.project_artifacts = tuple(
        CanonicalProjectArtifact(
            logical_name=name,
            source_path=f"stage-10/evaluator-capture-v1/{name}",
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        for name, content in sorted(files.items())
    )
    return bundle


def test_stage22_publication_writes_manifest_last_and_binds_code(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage22_export_manifest.json").write_text(
        "stale\n", encoding="utf-8"
    )
    (stage_dir / "paper_final.md").write_text("stale paper\n", encoding="utf-8")
    bundle = _nested_code_bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    checks: list[str] = []

    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: checks.append("checked"),
    )

    assert checks == ["checked", "checked"]
    manifest = parse_stage22_export_manifest(
        (stage_dir / "stage22_export_manifest.json").read_text(encoding="utf-8")
    )
    output_paths = {entry["path"] for entry in manifest["outputs"]}
    assert "paper.tex" in output_paths
    assert "references.bib" in output_paths
    assert "code/main.py" in output_paths
    assert manifest["template_name"]
    assert validate_stage22_export_publication(run_dir, bundle) == manifest
    snapshot = load_stage22_export_publication(run_dir, bundle)
    assert snapshot.require_output("paper_final.md").content == direct_files[
        "paper_final.md"
    ]
    assert snapshot.require_output("references.bib").content == direct_files[
        "references.bib"
    ]
    assert snapshot.require_output("paper.tex").content == direct_files["paper.tex"]


def test_stage22_nested_code_tree_can_be_republished(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _nested_code_bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)

    for _attempt in range(2):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )

    snapshot = load_stage22_export_publication(run_dir, bundle)
    assert snapshot.require_output("code/trojnet/anomaly.py").content == code_files[
        "trojnet/anomaly.py"
    ]
    assert snapshot.require_output(
        "code/data/c1355/c1355_ht1.bench"
    ).content == code_files["data/c1355/c1355_ht1.bench"]


def test_stage22_late_failure_removes_nested_success_namespace(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _nested_code_bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    checks = 0

    def fail_after_manifest() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("injected post-manifest source fixpoint failure")

    with pytest.raises(RuntimeError, match="post-manifest"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=fail_after_manifest,
        )

    assert tuple(stage_dir.iterdir()) == ()


@pytest.mark.parametrize("poison", ("symlink", "fifo"))
def test_stage22_recursive_cleanup_rejects_nested_special_files_without_following(
    tmp_path: Path,
    canonical_config: RCConfig,
    poison: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    nested = stage_dir / "code/nested"
    nested.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_text("keep\n", encoding="utf-8")
    if poison == "symlink":
        (nested / "poison").symlink_to(external)
    else:
        os.mkfifo(nested / "poison")
    (stage_dir / "stage22_export_manifest.json").write_text(
        "stale\n", encoding="utf-8"
    )
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)

    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )

    assert external.read_text(encoding="utf-8") == "keep\n"
    assert validate_stage22_export_publication(run_dir, bundle)


def test_stage22_recursive_cleanup_collision_invalidates_success_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import bound_output_namespace as bound_module

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    (stage_dir / "code/nested").mkdir(parents=True)
    (stage_dir / "code/nested/payload.py").write_text(
        "value = 1\n", encoding="utf-8"
    )
    (stage_dir / "stage22_export_manifest.json").write_text(
        "stale\n", encoding="utf-8"
    )
    original = bound_module._remove_tree_at

    def fail_code_cleanup(parent_fd: int, name: str) -> None:
        if name.startswith(".code.rejected-"):
            raise OSError("injected recursive cleanup collision")
        original(parent_fd, name)

    monkeypatch.setattr(bound_module, "_remove_tree_at", fail_code_cleanup)
    with pytest.raises(OSError, match="recursive cleanup collision"):
        export_module._reset_stage22_namespace(run_dir, stage_dir)

    assert not (stage_dir / "stage22_export_manifest.json").exists()
    assert not (stage_dir / "code").exists()
    monkeypatch.setattr(bound_module, "_remove_tree_at", original)
    export_module._reset_stage22_namespace(run_dir, stage_dir)
    assert tuple(stage_dir.iterdir()) == ()


def test_stage23_input_loader_captures_only_replayed_stage22_publication(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_input_bundle.load_stage22_input_bundle",
        lambda *_args, **_kwargs: bundle,
    )

    captured = load_stage23_input_bundle(run_dir, canonical_config)

    assert captured.paper.content == direct_files["paper_final.md"]
    assert captured.bibliography.content == direct_files["references.bib"]
    assert captured.latex.content == direct_files["paper.tex"]
    assert captured.cited_keys == ()


def test_stage22_publication_cleans_outputs_when_precommit_fixpoint_fails(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)

    with pytest.raises(RuntimeError, match="changed"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: (_ for _ in ()).throw(RuntimeError("changed")),
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_replaces_stale_nested_output_tree_before_publication(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    (stage_dir / "code/nested").mkdir(parents=True)
    (stage_dir / "stage22_export_manifest.json").write_text(
        "stale\n", encoding="utf-8"
    )
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)

    publication = publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )

    assert publication["schema_version"] == 1
    assert (stage_dir / "stage22_export_manifest.json").read_text(
        encoding="utf-8"
    ) != "stale\n"
    assert not (stage_dir / "code/nested").exists()


def test_stage22_postcommit_fixpoint_failure_removes_manifest_and_outputs(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("late input change")

    with pytest.raises(RuntimeError, match="late input change"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=check,
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_replays_outputs_after_postcommit_input_fixpoint(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    calls = 0

    def mutate_after_manifest() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            (stage_dir / "code/main.py").write_text(
                "print('tampered')\n", encoding="utf-8"
            )

    with pytest.raises(Stage22PublicationError, match="output hash mismatch"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=mutate_after_manifest,
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_replaces_code_symlink_without_touching_external_directory(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    (stage_dir / "code").symlink_to(outside, target_is_directory=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)

    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (stage_dir / "code").is_symlink()
    assert (stage_dir / "code/main.py").is_file()


def test_stage22_export_ignores_legacy_chart_and_experiment_shadows(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    (run_dir / "stage-14_v99/charts").mkdir(parents=True)
    (run_dir / "stage-14_v99/charts/shadow.png").write_bytes(b"shadow")
    (run_dir / "stage-13").mkdir()
    (run_dir / "stage-13/experiment_final.py").write_text(
        "print('shadow')\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        export_module,
        "compile_latex",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=False,
            attempts=0,
            errors=("pdflatex not installed",),
        ),
    )
    monkeypatch.setattr(
        export_module,
        "verify_stage22_input_bundle_unchanged",
        lambda *_args, **_kwargs: None,
    )

    artifacts = export_canonical_stage22(
        run_dir,
        stage_dir,
        bundle=_bundle(canonical_config),
        runtime_config=canonical_config,
        generated=_GENERATED,
    )

    assert "stage22_export_manifest.json" in artifacts
    assert (stage_dir / "code/main.py").read_text(encoding="utf-8") == (
        "print('canonical')\n"
    )
    assert not (stage_dir / "charts").exists()
    manifest = json.loads(
        (stage_dir / "stage22_export_manifest.json").read_text(encoding="utf-8")
    )
    assert all("shadow" not in entry["path"] for entry in manifest["outputs"])


@pytest.mark.parametrize(
    "results_body",
    (
        "Unsupported detection F1 was 0.777777.\n",
        "| Method | F1 |\n|---|---:|\n| Proposed | 0.777777 |\n",
    ),
)
def test_stage22_verifies_rendered_latex_results_before_publication(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    results_body: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage22_export_manifest.json").write_text(
        "stale\n", encoding="utf-8"
    )
    (stage_dir / "paper_final.md").write_text("stale paper\n", encoding="utf-8")
    bundle = _bundle(canonical_config)
    bundle.stage20_inputs.revised_paper = _bound(
        "stage-19/paper_revised.md",
        "## Canonical Paper\n\n## Abstract\nText.\n\n## Results\n"
        + results_body
        + "\n## Conclusion\nDone.\n",
    )
    monkeypatch.setattr(
        export_module,
        "compile_latex",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=False,
            attempts=0,
            errors=("pdflatex not installed",),
        ),
    )

    with pytest.raises(Stage22ExportError, match="LaTeX verification rejected"):
        export_canonical_stage22(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=canonical_config,
            generated=_GENERATED,
        )

    assert list(stage_dir.iterdir()) == []


@pytest.mark.parametrize(
    "name,mutation",
    (
        ("paper_final.md", b"different paper\n"),
        ("references.bib", b"@article{forged2026, title={Forged}}\n"),
        ("paper_verification.json", b"{}\n"),
        ("sanitization_report.json", b"{}\n"),
        ("canonical_source.json", b"{}\n"),
    ),
)
def test_stage22_publication_rejects_semantically_forged_direct_outputs(
    tmp_path: Path,
    canonical_config: RCConfig,
    name: str,
    mutation: bytes,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    direct_files[name] = mutation

    with pytest.raises(Stage22PublicationError, match="semantic output mismatch"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_invalidates_old_commit_before_semantic_reconstruction(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    manifest = stage_dir / "stage22_export_manifest.json"
    manifest.write_text("stale\n", encoding="utf-8")
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    direct_files["paper_final.md"] = b"forged\n"

    with pytest.raises(Stage22PublicationError, match="semantic output mismatch"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )

    assert not manifest.exists()


def test_stage22_disk_replay_rejects_output_and_manifest_hash_forgery(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )
    forged = b"forged but hash-bound\n"
    (stage_dir / "paper_final.md").write_bytes(forged)
    manifest_path = stage_dir / "stage22_export_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["outputs"]:
        if entry["path"] == "paper_final.md":
            entry["sha256"] = hashlib.sha256(forged).hexdigest()
    manifest_path.write_text(
        canonical_authority_json_text(manifest), encoding="utf-8"
    )

    with pytest.raises(Stage22PublicationError, match="semantic output mismatch"):
        validate_stage22_export_publication(run_dir, bundle)


@pytest.mark.parametrize(
    "missing",
    (
        "paper_final.md",
        "paper_final_latex.md",
        "references.bib",
        "paper.tex",
        "paper_verification.json",
        "sanitization_report.json",
        "canonical_source.json",
        "compile_status.json",
    ),
)
def test_stage22_publication_requires_every_mandatory_output(
    tmp_path: Path,
    canonical_config: RCConfig,
    missing: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    del direct_files[missing]

    with pytest.raises(Stage22PublicationError):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_compile_success_requires_nonempty_pdf_header(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    direct_files["compile_status.json"] = canonical_authority_json_text(
        {
            "schema_version": 1,
            "success": True,
            "attempts": 1,
            "errors": [],
            "status": "success",
            "tooling_available": True,
            "generated": _GENERATED,
        }
    ).encode("utf-8")

    with pytest.raises(Stage22PublicationError, match="invalid PDF"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )

    direct_files["paper.pdf"] = b"%PDF-1.7\nbody\n%%EOF\n"
    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )
    assert (stage_dir / "paper.pdf").read_bytes().endswith(b"%%EOF\n")

    direct_files["paper.pdf"] = b"%PDF-1.7\nbody without trailer"
    with pytest.raises(Stage22PublicationError, match="invalid PDF"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )


def test_stage22_failed_compile_rejects_published_pdf(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    direct_files["paper.pdf"] = b"%PDF-1.7\nbody"

    with pytest.raises(Stage22PublicationError, match="must not publish a PDF"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=lambda: None,
        )


@pytest.mark.parametrize(
    "unsafe",
    ("./paper_final.md", "nested//paper.tex", "paper%2Etex", "papér.tex"),
)
def test_stage22_manifest_rejects_noncanonical_output_paths(
    tmp_path: Path,
    canonical_config: RCConfig,
    unsafe: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=_GENERATED,
        precommit_check=lambda: None,
    )
    value = json.loads(
        (stage_dir / "stage22_export_manifest.json").read_text(encoding="utf-8")
    )
    value["outputs"][0]["path"] = unsafe

    with pytest.raises(Stage22PublicationError):
        parse_stage22_export_manifest(json.dumps(value))


def test_stage22_project_python_files_are_parsed_independently(
    canonical_config: RCConfig,
) -> None:
    bundle = _bundle(canonical_config)
    first = b"from __future__ import annotations\nimport numpy\n"
    second = b"from __future__ import annotations\nimport pandas\n"
    bundle.evidence.project_artifacts = (
        CanonicalProjectArtifact(
            "main.py", "stage-10/selected_candidate/main.py", hashlib.sha256(first).hexdigest(), first
        ),
        CanonicalProjectArtifact(
            "helper.py", "stage-10/selected_candidate/helper.py", hashlib.sha256(second).hexdigest(), second
        ),
    )

    deterministic = build_stage22_deterministic_outputs(bundle, generated=_GENERATED)

    assert deterministic.code_files["requirements.txt"] == b"numpy\npandas\n"


def test_legacy_stage22_producer_is_mechanically_disabled(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "run/stage-22"
    stage_dir.mkdir(parents=True)

    with pytest.raises(PermissionError, match="permanently disabled"):
        _execute_export_publish_legacy_disabled(
            stage_dir,
            tmp_path / "run",
            None,  # type: ignore[arg-type]
            AdapterBundle(),
        )

    assert list(stage_dir.iterdir()) == []


def test_stage22_parent_replacement_cannot_touch_external_directory(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-22"
    stage_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    bundle = _nested_code_bundle(canonical_config)
    direct_files, code_files = _publication_payload(bundle)
    moved = run_dir / "stage-22-moved"

    def replace_parent() -> None:
        if not moved.exists():
            os.rename(stage_dir, moved)
            stage_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="directory changed"):
        publish_stage22_outputs(
            run_dir,
            stage_dir,
            bundle=bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=_GENERATED,
            precommit_check=replace_parent,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (outside / "stage22_export_manifest.json").exists()
    assert list(moved.iterdir()) == []
