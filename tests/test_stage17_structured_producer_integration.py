"""B3-B inactive structured Stage 17 producer and publication integration."""

from __future__ import annotations

import copy
import inspect
import hashlib
import json
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline import scientific_claim_authority as authority
from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.scientific_claim_authority import (
    build_scientific_claim_generation_binding,
)
from researchclaw.pipeline.stage_impls import _paper_writing
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline import stage17_structured_publication as structured
from researchclaw.pipeline.stage17_structured_publication import (
    STRUCTURED_STAGE17_MANIFEST,
    STRUCTURED_STAGE17_OUTPUTS,
    STRUCTURED_STAGE17_STAGING,
    execute_structured_stage17_publication,
)

from tests.test_scientific_claim_authority import _complete_cfs, _evidence


def test_b4_declaration_is_exactly_1110_and_structured_path_remains_inactive() -> None:
    assert capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }


def test_structured_stage17_owns_only_exact_eight_names() -> None:
    assert STRUCTURED_STAGE17_OUTPUTS == (
        "scientific_evidence_facts.json",
        "scientific_claim_registry.json",
        "scientific_claim_selection.json",
        "paper_draft.md",
        "paper_structure_report.json",
        "experiment_fact_closure_report.json",
        "citation_closure_report.json",
    )
    assert STRUCTURED_STAGE17_MANIFEST == "scientific_claim_authority_manifest.json"
    assert STRUCTURED_STAGE17_STAGING == ".stage17-structured-publication.staging"


def test_direct_structured_entry_has_no_caller_authority() -> None:
    parameters = inspect.signature(execute_structured_stage17_publication).parameters
    assert tuple(parameters) == ("run_dir", "stage_dir", "llm")
    assert "evidence" not in parameters
    assert "binding" not in parameters
    assert "capabilities" not in parameters


def test_structured_capture_constructor_is_private(active_capture) -> None:
    _run_dir, _stage_dir, capture = active_capture
    with pytest.raises(TypeError, match="construction is private"):
        structured.StructuredStage17SourceCapture(
            evidence=capture.evidence,
            binding=capture.binding,
            files=capture.files,
            citation_plan=capture.citation_plan,
            citation_allowlist=capture.citation_allowlist,
            writer_owner=capture._writer_owner,
            run_identity=capture._run_identity,
            run_path=capture._run_path,
            construction_token=object(),
            authority=object(),
        )


def test_direct_structured_guard_precedes_every_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_io(*_args: object, **_kwargs: object) -> None:
        nonlocal touched
        touched = True
        raise AssertionError("direct structured entry performed I/O")

    monkeypatch.setattr(_paper_writing.ReleaseGraphLock, "acquire", forbidden_io)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        execute_structured_stage17_publication(
            tmp_path / "missing-run",
            tmp_path / "missing-run" / "stage-17",
            None,
        )
    assert touched is False


def test_ordinary_dispatch_checks_eligibility_before_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def eligible() -> bool:
        order.append("eligibility")
        return True

    def guard(_entrypoint: str) -> None:
        order.append("guard")
        raise capability.StructuredScientificClaimCapabilityIncomplete(
            "ordinary", ("stage17_publication",)
        )

    def forbidden_lock(*_args: object, **_kwargs: object) -> None:
        order.append("lock")
        raise AssertionError("writer lock acquired before complete guard")

    monkeypatch.setattr(capability, "structured_publication_is_eligible", eligible)
    monkeypatch.setattr(capability, "require_complete_structured_capability", guard)
    monkeypatch.setattr(_paper_writing.ReleaseGraphLock, "acquire", forbidden_lock)

    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        _paper_writing._execute_paper_draft(
            tmp_path / "stage-17",
            tmp_path,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
    assert order == ["eligibility", "guard"]


class _SelectionLLM:
    def __init__(self, hook=None, response_transform=None) -> None:
        self.calls = 0
        self.hook = hook
        self.response_transform = response_transform

    def chat(self, messages, **kwargs):
        self.calls += 1
        prompt = json.loads(messages[0]["content"])
        ids = [item["claim_id"] for item in prompt["claims"]]
        payload = {
            "selected_claim_ids": ids,
            "ordered_claim_ids": ids,
            "connector_template_ids": ["NONE"] * max(0, len(ids) - 1),
        }
        if self.response_transform is not None:
            payload = self.response_transform(self.calls, prompt, payload)
        if self.hook is not None:
            self.hook(self.calls)
        return LLMResponse(
            content=canonical_authority_json_text(payload),
            model="fixture",
        )


def _citation_inputs() -> tuple[bytes, bytes]:
    allowlist = canonical_authority_json_text(
        {
            "schema_version": 1,
            "eligibility_policy_version": 1,
            "shortlist_path": "stage-05/shortlist.jsonl",
            "shortlist_sha256": "1" * 64,
            "references_path": "stage-04/references.bib",
            "references_sha256": "2" * 64,
            "cards_manifest_path": "stage-06/cards_manifest.json",
            "cards_manifest_sha256": "3" * 64,
            "eligible_keys": ["Smith2024"],
            "ineligible": [],
        }
    ).encode("utf-8")
    plan = canonical_authority_json_text(
        {
            "schema_version": 1,
            "plan_version": 2,
            "plan_status": "final",
            "claim_scope": "pipeline_validation",
            "citation_allowlist_path": "stage-06/citation_allowlist.json",
            "citation_allowlist_sha256": hashlib.sha256(allowlist).hexdigest(),
            "cards_manifest_path": "stage-06/cards_manifest.json",
            "cards_manifest_sha256": "3" * 64,
            "effective_policy_path": "stage-16/citation_policy_effective.json",
            "effective_policy_sha256": "4" * 64,
            "claims": [
                {
                    "claim_id": "planned-claim-001",
                    "section_path": ["Introduction"],
                    "claim_text": "Prior work motivates the governed evaluation.",
                    "claim_type": "background",
                    "planned_citations": [
                        {
                            "cite_key": "Smith2024",
                            "evidence_excerpt_ids": ["excerpt-1"],
                            "support_status": "abstract_sufficient",
                        }
                    ],
                }
            ],
        }
    ).encode("utf-8")
    return plan, allowlist


@pytest.fixture
def active_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    cfs = _complete_cfs()
    cfs["bound_labels"] = {
        "benchmark_tokens": (),
        "dataset": "Trust-HUB",
        "evaluator_schema": "fixture-evaluator",
        "evaluator_id": "fixture-evaluator",
    }
    cfs["runtime"] = {
        "python": "3.11",
        "device": "cpu",
        "packages": {},
    }
    monkeypatch.setattr(authority, "build_canonical_fact_sheet", lambda _value: cfs)
    from researchclaw.literature import experiment_fact_closure

    monkeypatch.setattr(
        experiment_fact_closure,
        "build_canonical_fact_sheet",
        lambda _value: cfs,
    )
    evidence = _evidence(
        structured_results=MappingProxyType({"primary": Decimal("1.25")})
    )
    binding = build_scientific_claim_generation_binding(evidence)
    plan, allowlist = _citation_inputs()
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    (run_dir / "stage-16").mkdir(parents=True)
    (run_dir / "stage-06").mkdir()
    stage_dir.mkdir()
    (run_dir / "stage-16" / "citation_plan.json").write_bytes(plan)
    (run_dir / "stage-06" / "citation_allowlist.json").write_bytes(allowlist)

    with ReleaseGraphLock.acquire(run_dir, "b3b-fixture", mode="write") as lease:
        with monkeypatch.context() as capture_patch:
            capture_patch.setattr(
                structured,
                "build_scientific_claim_generation_binding",
                lambda received: (
                    binding
                    if received is evidence
                    else pytest.fail("capture changed evidence generation")
                ),
            )
            capture_patch.setattr(
                structured,
                "_capture_evidence_files",
                lambda reader, received: (
                    ()
                    if reader is lease and received is evidence
                    else pytest.fail("capture changed writer epoch")
                ),
            )
            capture = structured.capture_structured_stage17_sources(
                lease, evidence
            )
        yield _ActiveCapture(run_dir, stage_dir, capture, lease)


@dataclass
class _ActiveCapture:
    run_dir: Path
    stage_dir: Path
    capture: structured.StructuredStage17SourceCapture
    lease: ReleaseGraphLock

    def __iter__(self):
        return iter((self.run_dir, self.stage_dir, self.capture))


def _publish(active_capture, llm):
    return structured.publish_structured_stage17_from_capture(
        active_capture.lease,
        stage_dir=active_capture.stage_dir,
        capture=active_capture.capture,
        llm=llm,
    )


def test_direct_source_capture_uses_held_run_fd_and_full_inventory(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _stage_dir, fixture_capture = active_capture
    original = fixture_capture.evidence
    manifest = MappingProxyType(
        {"schema_version": 2, "generation_kind": "domain_evaluator"}
    )
    candidate = MappingProxyType({"schema_version": 2})
    selected = MappingProxyType(
        {"schema_version": 2, "result_set_type": "stage13_refinement"}
    )
    evidence = replace(
        original,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(
            canonical_authority_json_text(manifest).encode("utf-8")
        ).hexdigest(),
        candidate=candidate,
        candidate_manifest_sha256=hashlib.sha256(
            canonical_authority_json_text(candidate).encode("utf-8")
        ).hexdigest(),
        selected_result=selected,
        selected_result_manifest_sha256=hashlib.sha256(
            canonical_authority_json_text(selected).encode("utf-8")
        ).hexdigest(),
    )
    monkeypatch.setattr(
        structured,
        "build_scientific_claim_generation_binding",
        lambda received: replace(fixture_capture.binding, evidence=received),
    )
    candidate_root = evidence.candidate_manifest_path.rsplit("/", 1)[0]
    files = {
        evidence.manifest_path: canonical_authority_json_text(
            manifest
        ).encode("utf-8"),
        evidence.candidate_manifest_path: canonical_authority_json_text(
            candidate
        ).encode("utf-8"),
        evidence.selected_result_manifest_path: canonical_authority_json_text(
            selected
        ).encode("utf-8"),
        evidence.experiment_contract_path: evidence.experiment_contract_bytes,
        evidence.run_config_path: evidence.run_config_bytes,
        evidence.selected_execution_artifact.path: (
            evidence.selected_execution_artifact.content
        ),
    }
    assert evidence.execution_policy_artifact is not None
    files[evidence.execution_policy_artifact.path] = (
        evidence.execution_policy_artifact.content
    )
    for artifact in evidence.artifacts:
        files[f"{candidate_root}/{artifact.path}"] = artifact.content
    for path, content in files.items():
        target = run_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    with ReleaseGraphLock.acquire(run_dir, "capture-inventory") as lease:
        captured = structured.capture_structured_stage17_sources(
            lease, evidence
        )
    captured_paths = {path for path, _digest, _content in captured.files}
    assert set(files) <= captured_paths
    assert captured.evidence is evidence
    assert captured.binding.evidence is evidence


def test_real_closures_five_calls_exact_namespace_and_disk_replay(
    active_capture,
) -> None:
    llm = _SelectionLLM()
    snapshot = _publish(active_capture, llm)
    _run_dir, stage_dir, _capture = active_capture
    assert llm.calls == 5
    assert set(stage_dir.iterdir()) == {
        stage_dir / name for name in (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST)
    }
    assert not (stage_dir / STRUCTURED_STAGE17_STAGING).exists()
    assert snapshot.namespace_identity[2] == tuple(
        sorted((*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST))
    )
    assert json.loads(
        snapshot.content("experiment_fact_closure_report.json")
    )["valid"] is True
    assert json.loads(snapshot.content("citation_closure_report.json"))["valid"] is True

    run_dir, _stage_dir, capture = active_capture
    with ReleaseGraphLock.acquire(run_dir, "public-replay-attacks") as lease:
        with lease.open_stage_namespace("stage-17") as namespace:
            extra = stage_dir / "extra.json"
            extra.write_text("{}\n", encoding="utf-8")
            with pytest.raises(Exception, match="final namespace"):
                structured._capture_final(namespace, capture)
            extra.unlink()
            paper = stage_dir / "paper_draft.md"
            paper_bytes = paper.read_bytes()
            paper.unlink()
            with pytest.raises(Exception, match="final namespace"):
                structured._capture_final(namespace, capture)
            paper.write_bytes(paper_bytes)

    alias = run_dir / "paper-alias"
    alias.write_bytes(snapshot.content("paper_draft.md"))
    paper = stage_dir / "paper_draft.md"
    paper.unlink()
    os.link(alias, paper)
    with ReleaseGraphLock.acquire(run_dir, "public-hardlink-attack") as lease:
        with lease.open_stage_namespace("stage-17") as namespace:
            with pytest.raises(Exception, match="hardlink alias"):
                structured._capture_final(namespace, capture)


def test_provider_prose_or_extra_authority_fails_and_leaves_no_manifest(
    active_capture,
) -> None:
    def add_prose(_call, _prompt, payload):
        return {**payload, "prose": "forbidden"}

    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM(response_transform=add_prose))
    _run_dir, stage_dir, _capture = active_capture
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


@pytest.mark.parametrize("failure", ("unknown", "missing", "duplicate"))
def test_provider_semantic_failures_are_terminal_and_bounded(
    active_capture,
    failure: str,
) -> None:
    def invalidate(call, _prompt, payload):
        if call != 2:
            return payload
        if failure == "unknown":
            payload["selected_claim_ids"] = ["f" * 64]
            payload["ordered_claim_ids"] = ["f" * 64]
        elif failure == "missing":
            payload["selected_claim_ids"] = []
            payload["ordered_claim_ids"] = []
            payload["connector_template_ids"] = []
        else:
            first = payload["ordered_claim_ids"][0]
            payload["ordered_claim_ids"] = [first, first]
            payload["connector_template_ids"] = ["NONE"]
        return payload

    llm = _SelectionLLM(response_transform=invalidate)
    with pytest.raises(Exception):
        _publish(active_capture, llm)
    assert llm.calls == 2
    _run_dir, stage_dir, _capture = active_capture
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()


def test_provider_exception_stops_at_failing_call(active_capture) -> None:
    class FailingLLM(_SelectionLLM):
        def chat(self, messages, **kwargs):
            if self.calls == 2:
                self.calls += 1
                raise RuntimeError("provider failed")
            return super().chat(messages, **kwargs)

    llm = FailingLLM()
    with pytest.raises(Exception, match="provider failed"):
        _publish(active_capture, llm)
    assert llm.calls == 3
    _run_dir, stage_dir, _capture = active_capture
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()


@pytest.mark.parametrize("kind", ("symlink", "fifo", "hardlink", "staging"))
def test_unsafe_prepublication_entries_are_rejected_and_invalidated(
    active_capture,
    kind: str,
) -> None:
    _run_dir, stage_dir, _capture = active_capture
    target = stage_dir / STRUCTURED_STAGE17_OUTPUTS[0]
    if kind == "symlink":
        target.symlink_to(stage_dir.parent / "external")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "hardlink":
        source = stage_dir / "alias-source"
        source.write_text("stale", encoding="utf-8")
        os.link(source, target)
    else:
        target = stage_dir / STRUCTURED_STAGE17_STAGING
        target.mkdir()
    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM())
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert not target.exists()


def test_cleanup_collision_does_not_short_circuit_other_invalidation(
    active_capture,
) -> None:
    run_dir, stage_dir, _capture = active_capture
    manifest = stage_dir / STRUCTURED_STAGE17_MANIFEST
    manifest.write_text("stale", encoding="utf-8")
    collision = stage_dir / STRUCTURED_STAGE17_OUTPUTS[0]
    collision.mkdir()
    (collision / "nested").mkdir()
    later = stage_dir / STRUCTURED_STAGE17_OUTPUTS[-1]
    later.write_text("stale", encoding="utf-8")
    with ReleaseGraphLock.acquire(run_dir, "cleanup-test") as lease:
        with lease.open_stage_namespace("stage-17") as namespace:
            with pytest.raises(
                structured.StructuredStage17PublicationError,
                match="cleanup incomplete",
            ):
                structured._cleanup_owned_namespace(namespace)
    assert not manifest.exists()
    assert not later.exists()
    assert collision.is_dir()


def test_late_source_mutation_fails_and_cleans_authority(active_capture) -> None:
    run_dir, stage_dir, _capture = active_capture

    def mutate(call: int) -> None:
        if call == 5:
            (run_dir / "stage-16" / "citation_plan.json").write_text(
                "mutated\n", encoding="utf-8"
            )

    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM(hook=mutate))
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


@pytest.mark.parametrize("target", ("paper", "manifest"))
def test_late_output_or_manifest_mutation_fails_double_final_replay(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _run_dir, stage_dir, _capture = active_capture
    original = structured._capture_final
    calls = 0

    def mutate_after_first(namespace, capture):
        nonlocal calls
        calls += 1
        snapshot = original(namespace, capture)
        if calls == 1:
            name = (
                "paper_draft.md"
                if target == "paper"
                else STRUCTURED_STAGE17_MANIFEST
            )
            (stage_dir / name).write_bytes((stage_dir / name).read_bytes() + b"x")
        return snapshot

    monkeypatch.setattr(structured, "_capture_final", mutate_after_first)
    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM())
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


def test_mutation_after_second_capture_is_rejected_before_success(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dir, stage_dir, _capture = active_capture
    original = structured._capture_final
    calls = 0

    def mutate_after_second(namespace, capture):
        nonlocal calls
        calls += 1
        snapshot = original(namespace, capture)
        if calls == 2:
            paper = stage_dir / "paper_draft.md"
            paper.write_bytes(paper.read_bytes() + b"x")
        return snapshot

    monkeypatch.setattr(structured, "_capture_final", mutate_after_second)
    with pytest.raises(
        Exception,
        match="changed after second capture|bytes differ from code rerender",
    ):
        _publish(active_capture, _SelectionLLM())
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


def test_last_output_read_source_mutation_is_rejected(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, stage_dir, _capture = active_capture
    original = structured.BoundOutputNamespace.read_regular_snapshot
    reads = 0

    def mutate_after_last_output_read(namespace, name):
        nonlocal reads
        snapshot = original(namespace, name)
        reads += 1
        if reads == 24:
            plan = run_dir / "stage-16" / "citation_plan.json"
            plan.write_bytes(plan.read_bytes() + b"x")
        return snapshot

    monkeypatch.setattr(
        structured.BoundOutputNamespace,
        "read_regular_snapshot",
        mutate_after_last_output_read,
    )
    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM())
    assert reads == 24
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


def test_same_bytes_hardlink_source_is_rejected(
    active_capture,
) -> None:
    run_dir, stage_dir, _capture = active_capture

    def replace_plan_with_hardlink(call: int) -> None:
        if call != 5:
            return
        plan = run_dir / "stage-16" / "citation_plan.json"
        alias = run_dir.parent / "citation-plan-alias"
        alias.write_bytes(plan.read_bytes())
        plan.unlink()
        os.link(alias, plan)

    with pytest.raises(Exception, match="hardlink|unaliased"):
        _publish(
            active_capture,
            _SelectionLLM(hook=replace_plan_with_hardlink),
        )
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


def test_capture_is_bound_to_same_writer_and_run_before_namespace_access(
    active_capture,
    tmp_path: Path,
) -> None:
    _run_a, _stage_a, capture = active_capture
    run_b = tmp_path / "run-b"
    stage_b = run_b / "stage-17"
    (run_b / "stage-16").mkdir(parents=True)
    (run_b / "stage-06").mkdir()
    stage_b.mkdir()
    (run_b / "stage-16" / "citation_plan.json").write_bytes(
        capture.citation_plan
    )
    (run_b / "stage-06" / "citation_allowlist.json").write_bytes(
        capture.citation_allowlist
    )
    stale_manifest = stage_b / STRUCTURED_STAGE17_MANIFEST
    stale_manifest.write_text("must remain untouched\n", encoding="utf-8")

    with ReleaseGraphLock.acquire(run_b, "cross-run-probe", mode="write") as lease_b:
        with pytest.raises(Exception, match="capture.*run|capture.*writer"):
            structured.publish_structured_stage17_from_capture(
                lease_b,
                stage_dir=stage_b,
                capture=capture,
                llm=_SelectionLLM(),
            )
    assert stale_manifest.read_text(encoding="utf-8") == "must remain untouched\n"
    assert tuple(stage_b.iterdir()) == (stale_manifest,)


def test_copied_capture_is_rejected_before_namespace_access(
    active_capture,
) -> None:
    _run_dir, stage_dir, capture = active_capture
    marker = stage_dir / STRUCTURED_STAGE17_MANIFEST
    marker.write_text("must remain untouched\n", encoding="utf-8")
    copied = copy.copy(capture)

    with pytest.raises(Exception, match="capture.*issued|capture.*construction"):
        structured.publish_structured_stage17_from_capture(
            active_capture.lease,
            stage_dir=stage_dir,
            capture=copied,
            llm=_SelectionLLM(),
        )
    assert marker.read_text(encoding="utf-8") == "must remain untouched\n"


@pytest.mark.parametrize("kind", ("fake", "reader", "inactive"))
def test_untrusted_or_inactive_capture_context_is_rejected_before_namespace_access(
    active_capture,
    kind: str,
) -> None:
    run_dir, stage_dir, capture = active_capture
    marker = stage_dir / STRUCTURED_STAGE17_MANIFEST
    marker.write_text("must remain untouched\n", encoding="utf-8")
    lease: object = active_capture.lease
    candidate: object = capture
    borrowed: ReleaseGraphLock | None = None
    if kind == "fake":
        candidate = object()
    elif kind == "reader":
        borrowed = ReleaseGraphLock.acquire(
            run_dir, "reader-capture-probe", mode="read"
        )
        lease = borrowed
    else:
        borrowed = ReleaseGraphLock.acquire(
            run_dir, "inactive-capture-probe", mode="write"
        )
        borrowed.close()
        lease = borrowed
    try:
        with pytest.raises(Exception):
            structured.publish_structured_stage17_from_capture(
                lease,  # type: ignore[arg-type]
                stage_dir=stage_dir,
                capture=candidate,  # type: ignore[arg-type]
                llm=_SelectionLLM(),
            )
    finally:
        if borrowed is not None:
            borrowed.close()
    assert marker.read_text(encoding="utf-8") == "must remain untouched\n"


def test_mixed_generation_capture_fails_before_provider(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dir, stage_dir, capture = active_capture
    other = _evidence(
        _summary_content=canonical_authority_json_text(
            {"flag": True, "label": "alpha", "value": Decimal("2.5")}
        ).encode("utf-8"),
        structured_results=MappingProxyType({"primary": Decimal("2.5")}),
    )
    monkeypatch.setattr(
        structured,
        "build_scientific_claim_generation_binding",
        lambda _received: capture.binding,
    )
    monkeypatch.setattr(
        structured,
        "_capture_evidence_files",
        lambda _reader, _received: (),
    )
    with pytest.raises(Exception, match="changed evidence generation"):
        structured.capture_structured_stage17_sources(
            active_capture.lease,
            other,
        )
    assert tuple(stage_dir.iterdir()) == ()


def test_stage_parent_replacement_never_writes_external(active_capture) -> None:
    run_dir, stage_dir, _capture = active_capture
    moved = run_dir / "stage-17-moved"
    external = run_dir / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("external", encoding="utf-8")

    def replace(call: int) -> None:
        if call == 1:
            stage_dir.rename(moved)
            stage_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(Exception):
        _publish(active_capture, _SelectionLLM(hook=replace))
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert set(external.iterdir()) == {sentinel}
    assert not (moved / STRUCTURED_STAGE17_MANIFEST).exists()


def test_run_parent_replacement_never_writes_external(active_capture) -> None:
    run_dir, _stage_dir, _capture = active_capture
    moved = run_dir.parent / "run-moved"
    sentinel: Path | None = None

    def replace(call: int) -> None:
        nonlocal sentinel
        if call == 1:
            run_dir.rename(moved)
            (run_dir / "stage-17").mkdir(parents=True)
            sentinel = run_dir / "stage-17" / "sentinel"
            sentinel.write_text("external", encoding="utf-8")

    with pytest.raises(Exception, match="run directory changed"):
        _publish(active_capture, _SelectionLLM(hook=replace))
    assert sentinel is not None
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert set(sentinel.parent.iterdir()) == {sentinel}
    assert not (moved / "stage-17" / STRUCTURED_STAGE17_MANIFEST).exists()


def test_detached_stage_revival_is_rejected(active_capture) -> None:
    run_dir, stage_dir, _capture = active_capture
    moved = run_dir / "stage-17-moved"

    def replace_and_restore(call: int) -> None:
        if call == 1:
            stage_dir.rename(moved)
            moved.rename(stage_dir)

    with pytest.raises(Exception, match="directory changed|run namespace changed"):
        _publish(active_capture, _SelectionLLM(hook=replace_and_restore))
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()


@pytest.mark.parametrize(
    "replacement",
    [
        {
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
            "stage24_and_release_integration": 0,
        },
        {"malformed": 1},
    ],
)
def test_partial_or_malformed_ordinary_map_stays_generic_without_structured_touch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(
        capability, "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES", replacement
    )
    evidence = SimpleNamespace(
        manifest=MappingProxyType({"schema_version": 1}),
        candidate=MappingProxyType({}),
        selected_result=MappingProxyType({}),
    )
    captures = 0

    def load(_run_dir):
        nonlocal captures
        captures += 1
        return evidence

    monkeypatch.setattr(_paper_writing, "load_canonical_experiment_evidence", load)
    expected = _paper_writing.StageResult(
        stage=Stage.PAPER_DRAFT,
        status=StageStatus.DONE,
        artifacts=("legacy",),
    )
    monkeypatch.setattr(
        _paper_writing,
        "_execute_paper_draft_under_release_epoch",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(
        _paper_writing,
        "publish_structured_stage17_from_capture",
        lambda *_args, **_kwargs: pytest.fail("structured path was touched"),
    )
    result = _paper_writing._execute_paper_draft(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result is expected
    assert captures == 1


def test_corrupt_domain_discriminator_fails_without_generic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(
        _paper_writing,
        "load_canonical_experiment_evidence",
        lambda _run_dir: SimpleNamespace(
            manifest=MappingProxyType(
                {"schema_version": 2, "generation_kind": "domain_evaluator"}
            ),
            candidate=MappingProxyType({}),
            selected_result=MappingProxyType({}),
        ),
    )
    monkeypatch.setattr(
        _paper_writing,
        "_execute_paper_draft_under_release_epoch",
        lambda *_args, **_kwargs: pytest.fail("generic fallback occurred"),
    )
    result = _paper_writing._execute_paper_draft(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert "discriminator" in (result.error or "")


def test_generic_capture_failure_preserves_baseline_cleanup_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    names = (
        "paper_draft.md",
        "paper_structure_report.json",
        "experiment_fact_closure_report.json",
        "citation_closure_report.json",
    )
    for name in names:
        (stage_dir / name).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        _paper_writing,
        "load_canonical_experiment_evidence",
        lambda _run_dir: (_ for _ in ()).throw(RuntimeError("replay failed")),
    )
    result = _paper_writing._execute_paper_draft(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert all(not (stage_dir / name).exists() for name in names)


def test_structured_capture_failure_invalidates_old_manifest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    for name in (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST):
        (stage_dir / name).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    evidence = SimpleNamespace(
        manifest=MappingProxyType(
            {"schema_version": 2, "generation_kind": "domain_evaluator"}
        ),
        candidate=MappingProxyType({"schema_version": 2}),
        selected_result=MappingProxyType(
            {"schema_version": 2, "result_set_type": "stage13_refinement"}
        ),
    )
    monkeypatch.setattr(
        _paper_writing,
        "load_canonical_experiment_evidence",
        lambda _run_dir: evidence,
    )
    monkeypatch.setattr(
        _paper_writing,
        "capture_structured_stage17_sources",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    result = _paper_writing._execute_paper_draft(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
    assert tuple(stage_dir.iterdir()) == ()


def test_exact_1111_ordinary_dispatch_uses_one_capture_for_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    evidence = SimpleNamespace(
        manifest=MappingProxyType(
            {"schema_version": 2, "generation_kind": "domain_evaluator"}
        ),
        candidate=MappingProxyType({"schema_version": 2}),
        selected_result=MappingProxyType(
            {"schema_version": 2, "result_set_type": "stage13_refinement"}
        ),
    )
    captures = 0
    immutable_capture = object()

    def load(_run_dir):
        nonlocal captures
        captures += 1
        return evidence

    def capture(_lease, received):
        assert received is evidence
        return immutable_capture

    def publish(_lease, *, stage_dir, capture, llm):
        assert stage_dir == run_dir / "stage-17"
        assert capture is immutable_capture
        assert llm is None
        return object()

    monkeypatch.setattr(_paper_writing, "load_canonical_experiment_evidence", load)
    monkeypatch.setattr(
        _paper_writing, "capture_structured_stage17_sources", capture
    )
    monkeypatch.setattr(
        _paper_writing, "publish_structured_stage17_from_capture", publish
    )
    result = _paper_writing._execute_paper_draft(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    assert captures == 1


@pytest.mark.parametrize("break_output", (False, True), ids=("stable", "missing"))
def test_execute_stage_structured_postcondition_is_terminal_and_fail_closed(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
    break_output: bool,
) -> None:
    run_dir, stage_dir, capture = active_capture
    stage16 = run_dir / "stage-16"
    for name in (
        "outline.md",
        "outline_binding.json",
        "citation_policy_effective.json",
    ):
        (stage16 / name).write_text("input\n", encoding="utf-8")
    artifacts = (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST)

    def publish_then_break(
        received_stage_dir: Path,
        received_run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
        prompts: object = None,
    ) -> _paper_writing.StageResult:
        _ = (llm, prompts)
        assert received_stage_dir == stage_dir
        assert received_run_dir == run_dir
        with ReleaseGraphLock.acquire(
            run_dir, "structured-postcondition-producer", mode="write"
        ) as lease:
            structured.publish_structured_stage17_from_capture(
                lease,
                stage_dir=stage_dir,
                capture=capture,
                llm=_SelectionLLM(),
            )
        if break_output:
            (stage_dir / "scientific_evidence_facts.json").unlink()
        return _paper_writing.StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.DONE,
            artifacts=artifacts,
            decision="structured-scientific-claim-v1",
            evidence_refs=tuple(f"stage-17/{name}" for name in artifacts),
        )

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS,
        Stage.PAPER_DRAFT,
        publish_then_break,
    )
    config = RCConfig.load(
        Path(__file__).parent.parent / "config.researchclaw.example.yaml",
        check_paths=False,
    )
    result = pipeline_executor.execute_stage(
        Stage.PAPER_DRAFT,
        run_dir=run_dir,
        run_id="structured-stage17-postcondition",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    if break_output:
        assert result.status is StageStatus.FAILED
        assert "postcondition" in (result.error or "").lower()
        assert not (stage_dir / STRUCTURED_STAGE17_MANIFEST).exists()
        assert all(
            not (stage_dir / name).exists() for name in STRUCTURED_STAGE17_OUTPUTS
        )
    else:
        assert result.status is StageStatus.DONE
        assert result.artifacts == artifacts
        assert set(stage_dir.iterdir()) == {
            stage_dir / name for name in artifacts
        }
