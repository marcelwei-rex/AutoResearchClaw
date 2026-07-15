from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.stage15_critique import (
    CRITIQUE_POLICY_VERSION,
    CRITIQUE_SCHEMA_VERSION,
    Stage15CritiqueError,
    canonical_json_text,
    load_stage15_critique_publication_from_namespace,
    parse_model_findings_response,
    prepare_stage15_critique_namespace,
    publish_external_critique_or_request,
    publish_model_or_none_critique,
)


SHA = "1" * 64
CANONICAL = {"path": "canonical_experiment_evidence.json", "sha256": SHA}
DECISION_TEXT = b"PROCEED\n"
DECISION_STRUCTURED = json.dumps(
    {
        "canonical_experiment_evidence_path": CANONICAL["path"],
        "canonical_experiment_evidence_sha256": CANONICAL["sha256"],
        "decision_path": "stage-15/decision.md",
        "decision_sha256": hashlib.sha256(DECISION_TEXT).hexdigest(),
    },
    sort_keys=True,
).encode("utf-8")
DECISION = {
    "text_path": "stage-15/decision.md",
    "text_sha256": hashlib.sha256(DECISION_TEXT).hexdigest(),
    "structured_path": "stage-15/decision_structured.json",
    "structured_sha256": hashlib.sha256(DECISION_STRUCTURED).hexdigest(),
}
FINDING = {
    "id": "sc-01",
    "severity": "P1",
    "category": "evidence",
    "question": "Which observation supports this claim?",
    "finding": "The claim lacks a bound observation.",
    "falsification_criterion": "A preregistered replicate contradicts it.",
}


@pytest.fixture(autouse=True)
def _canonical_evidence_fixpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.stage15_critique.load_canonical_experiment_evidence",
        lambda _run_dir: SimpleNamespace(
            manifest_path=CANONICAL["path"],
            manifest_sha256=CANONICAL["sha256"],
        ),
    )


def _write_decision(stage15: Path) -> None:
    (stage15 / "decision.md").write_bytes(DECISION_TEXT)
    (stage15 / "decision_structured.json").write_bytes(DECISION_STRUCTURED)


def _namespace(tmp_path: Path) -> BoundOutputNamespace:
    stage15 = tmp_path / "stage-15"
    stage15.mkdir()
    _write_decision(stage15)
    return BoundOutputNamespace.open(tmp_path, stage15, "stage-15")


def test_model_final_is_manifest_last_and_strictly_replayable(tmp_path: Path) -> None:
    with _namespace(tmp_path) as namespace:
        publication = publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
            critic_model="critic",
            findings=[FINDING],
            unavailability_reason=None,
        )
        assert publication.state == "model_final"
        replayed = load_stage15_critique_publication_from_namespace(namespace)
        assert replayed.manifest["finding_count"] == 1
        assert set(namespace.direct_entries()) == {
            "critique.json",
            "decision.md",
            "decision_structured.json",
            "stage15_critique_manifest.json",
        }


def test_manifest_write_failure_removes_success_named_critique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = BoundOutputNamespace.write_text_atomic

    def reject_manifest(namespace, name, text):
        if name == "stage15_critique_manifest.json":
            raise OSError("injected manifest failure")
        return original(namespace, name, text)

    monkeypatch.setattr(BoundOutputNamespace, "write_text_atomic", reject_manifest)
    with _namespace(tmp_path) as namespace:
        with pytest.raises(OSError, match="injected manifest failure"):
            publish_model_or_none_critique(
                namespace=namespace,
                canonical_evidence=CANONICAL,
                decision=DECISION,
                writer_model="writer",
                critic_model="critic",
                findings=[FINDING],
                unavailability_reason=None,
            )
        assert "critique.json" not in namespace.direct_entries()
        assert "stage15_critique_manifest.json" not in namespace.direct_entries()


def test_none_final_has_no_critic_or_external_compatibility_fields(
    tmp_path: Path,
) -> None:
    with _namespace(tmp_path) as namespace:
        publication = publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
            critic_model="",
            findings=None,
            unavailability_reason="critic_not_configured",
        )
        assert publication.state == "none_final"
        assert set(publication.critique) == {
            "schema_version",
            "policy_version",
            "state",
            "recommend_only",
            "canonical_evidence",
            "decision",
            "writer_model",
            "unavailability_reason",
            "findings",
        }


def test_external_pending_has_no_final_authority(tmp_path: Path) -> None:
    with _namespace(tmp_path) as namespace:
        publication = publish_external_critique_or_request(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
        )
        assert publication.state == "external_pending"
        assert set(namespace.direct_entries()) == {
            "decision.md",
            "decision_structured.json",
            "critique-pending",
        }
        request = namespace.read_flat_directory("critique-pending")
        assert set(request) == {"external_review_request.json"}


def test_external_final_copies_strict_findings_and_binds_raw_inputs(
    tmp_path: Path,
) -> None:
    stage15 = tmp_path / "stage-15"
    external = stage15 / "external-review"
    external.mkdir(parents=True)
    _write_decision(stage15)
    structured = {
        "schema_version": CRITIQUE_SCHEMA_VERSION,
        "policy_version": CRITIQUE_POLICY_VERSION,
        "target_canonical_evidence": CANONICAL,
        "target_decision": DECISION,
        "reviewer": {
            "reviewer_id": "reviewer-1",
            "reviewer_kind": "human",
            "organization": "independent",
        },
        "findings": [FINDING],
    }
    (external / "structured.json").write_text(
        canonical_json_text(structured), encoding="utf-8"
    )
    (external / "review.md").write_text("Independent prose review.\n", encoding="utf-8")

    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        publication = publish_external_critique_or_request(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
        )
        assert publication.state == "external_final"
        assert publication.critique["findings"] == (FINDING,)
        assert len(publication.manifest["external_inputs"]) == 2

        (external / "structured.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(Stage15CritiqueError, match="hash mismatch"):
            load_stage15_critique_publication_from_namespace(namespace)


def test_external_replay_reconstructs_semantics_from_structured_source(
    tmp_path: Path,
) -> None:
    stage15 = tmp_path / "stage-15"
    external = stage15 / "external-review"
    external.mkdir(parents=True)
    _write_decision(stage15)
    structured = {
        "schema_version": CRITIQUE_SCHEMA_VERSION,
        "policy_version": CRITIQUE_POLICY_VERSION,
        "target_canonical_evidence": CANONICAL,
        "target_decision": DECISION,
        "reviewer": {
            "reviewer_id": "reviewer-1",
            "reviewer_kind": "human",
            "organization": "independent",
        },
        "findings": [FINDING],
    }
    structured_path = external / "structured.json"
    structured_path.write_text(canonical_json_text(structured), encoding="utf-8")

    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        publish_external_critique_or_request(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
        )
        changed = json.loads(structured_path.read_text(encoding="utf-8"))
        changed["findings"][0]["finding"] = "Different source finding."
        changed_bytes = canonical_json_text(changed).encode("utf-8")
        structured_path.write_bytes(changed_bytes)

        critique_path = stage15 / "critique.json"
        critique = json.loads(critique_path.read_text(encoding="utf-8"))
        critique["external_structured"]["sha256"] = hashlib.sha256(
            changed_bytes
        ).hexdigest()
        critique_bytes = canonical_json_text(critique).encode("utf-8")
        critique_path.write_bytes(critique_bytes)

        manifest_path = stage15 / "stage15_critique_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["critique_sha256"] = hashlib.sha256(critique_bytes).hexdigest()
        manifest["external_inputs"][0]["sha256"] = hashlib.sha256(
            changed_bytes
        ).hexdigest()
        manifest_path.write_text(canonical_json_text(manifest), encoding="utf-8")

        with pytest.raises(Stage15CritiqueError, match="findings mismatch"):
            load_stage15_critique_publication_from_namespace(namespace)


def test_external_fixpoint_read_failure_removes_final_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage15 = tmp_path / "stage-15"
    external = stage15 / "external-review"
    external.mkdir(parents=True)
    _write_decision(stage15)
    structured = {
        "schema_version": CRITIQUE_SCHEMA_VERSION,
        "policy_version": CRITIQUE_POLICY_VERSION,
        "target_canonical_evidence": CANONICAL,
        "target_decision": DECISION,
        "reviewer": {
            "reviewer_id": "reviewer-1",
            "reviewer_kind": "human",
            "organization": "independent",
        },
        "findings": [FINDING],
    }
    (external / "structured.json").write_text(
        canonical_json_text(structured), encoding="utf-8"
    )
    original = BoundOutputNamespace.read_flat_directory
    reads = 0

    def fail_second_read(namespace, name):
        nonlocal reads
        result = original(namespace, name)
        if name == "external-review":
            reads += 1
            if reads == 2:
                raise OSError("injected external fixpoint failure")
        return result

    monkeypatch.setattr(
        BoundOutputNamespace, "read_flat_directory", fail_second_read
    )
    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        with pytest.raises(Stage15CritiqueError, match="fixpoint failure"):
            publish_external_critique_or_request(
                namespace=namespace,
                canonical_evidence=CANONICAL,
                decision=DECISION,
                writer_model="writer",
            )
        assert "critique.json" not in namespace.direct_entries()
        assert "stage15_critique_manifest.json" not in namespace.direct_entries()


def test_shadow_critique_entry_invalidates_output_namespace_replay(
    tmp_path: Path,
) -> None:
    with _namespace(tmp_path) as namespace:
        publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
            critic_model="critic",
            findings=[FINDING],
            unavailability_reason=None,
        )
        namespace.write_text_atomic("critique-shadow.json", "{}\n")
        with pytest.raises(Stage15CritiqueError, match="namespace"):
            load_stage15_critique_publication_from_namespace(namespace)


@pytest.mark.parametrize("name", ["old_critique.json", "shadow_critique.json"])
def test_any_extra_direct_entry_invalidates_final_namespace(
    tmp_path: Path, name: str
) -> None:
    with _namespace(tmp_path) as namespace:
        publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
            critic_model="critic",
            findings=[FINDING],
            unavailability_reason=None,
        )
        namespace.write_text_atomic(name, "{}\n")
        with pytest.raises(Stage15CritiqueError, match="namespace"):
            load_stage15_critique_publication_from_namespace(namespace)


def test_nested_and_symlink_shadow_namespaces_are_rejected(tmp_path: Path) -> None:
    stage15 = tmp_path / "stage-15"
    stage15.mkdir()
    _write_decision(stage15)
    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence=CANONICAL,
            decision=DECISION,
            writer_model="writer",
            critic_model="critic",
            findings=[FINDING],
            unavailability_reason=None,
        )
        legacy = stage15 / "legacy"
        legacy.mkdir()
        (legacy / "critique.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(Stage15CritiqueError, match="namespace"):
            load_stage15_critique_publication_from_namespace(namespace)
        (legacy / "critique.json").unlink()
        legacy.rmdir()
        (stage15 / "shadow").symlink_to(stage15 / "critique.json")
        with pytest.raises(Stage15CritiqueError, match="namespace"):
            load_stage15_critique_publication_from_namespace(namespace)


def test_parent_replacement_cannot_publish_to_external_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage15 = tmp_path / "stage-15"
    stage15.mkdir()
    _write_decision(stage15)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    moved = tmp_path / "stage-15-moved"
    original = BoundOutputNamespace.write_text_atomic

    def replace_before_manifest(namespace, name, text):
        if name == "stage15_critique_manifest.json" and not moved.exists():
            os.rename(stage15, moved)
            stage15.symlink_to(outside, target_is_directory=True)
        return original(namespace, name, text)

    monkeypatch.setattr(
        BoundOutputNamespace, "write_text_atomic", replace_before_manifest
    )
    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        with pytest.raises(OSError, match="directory changed"):
            publish_model_or_none_critique(
                namespace=namespace,
                canonical_evidence=CANONICAL,
                decision=DECISION,
                writer_model="writer",
                critic_model="critic",
                findings=[FINDING],
                unavailability_reason=None,
            )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (outside / "critique.json").exists()
    assert not (outside / "stage15_critique_manifest.json").exists()
    assert not (moved / "critique.json").exists()
    assert not (moved / "stage15_critique_manifest.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(extra="forbidden"),
        lambda item: item["findings"][0].update(category="counterexample"),
        lambda item: item["findings"].append(dict(FINDING)),
    ],
)
def test_model_response_rejects_unknown_category_duplicate_id_and_extra_fields(
    mutation,
) -> None:
    payload = {"findings": [dict(FINDING)]}
    mutation(payload)
    with pytest.raises(Stage15CritiqueError):
        parse_model_findings_response(json.dumps(payload))


def test_duplicate_json_keys_are_rejected() -> None:
    content = '{"findings":[],"findings":[]}'
    with pytest.raises(Stage15CritiqueError, match="duplicate key"):
        parse_model_findings_response(content)


def test_prepare_invalidates_authority_but_preserves_external_inputs(
    tmp_path: Path,
) -> None:
    stage15 = tmp_path / "stage-15"
    external = stage15 / "external-review"
    external.mkdir(parents=True)
    (external / "structured.json").write_text("external", encoding="utf-8")
    (stage15 / "critique.json").write_text("stale", encoding="utf-8")
    (stage15 / "stage15_critique_manifest.json").write_text(
        "stale", encoding="utf-8"
    )

    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        prepare_stage15_critique_namespace(namespace)
        assert namespace.direct_entries() == ("external-review",)
        assert namespace.read_flat_directory("external-review") == {
            "structured.json": b"external"
        }


def test_pending_collision_cannot_preserve_old_final_authority(
    tmp_path: Path,
) -> None:
    stage15 = tmp_path / "stage-15"
    (stage15 / "critique-pending" / "nested").mkdir(parents=True)
    (stage15 / "critique.json").write_text("stale", encoding="utf-8")
    (stage15 / "stage15_critique_manifest.json").write_text(
        "stale", encoding="utf-8"
    )

    with BoundOutputNamespace.open(tmp_path, stage15, "stage-15") as namespace:
        with pytest.raises(Stage15CritiqueError, match="cleanup was incomplete"):
            prepare_stage15_critique_namespace(namespace)
        assert "critique.json" not in namespace.direct_entries()
        assert "stage15_critique_manifest.json" not in namespace.direct_entries()
