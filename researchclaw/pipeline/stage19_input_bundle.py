"""Immutable, single-read Stage 17/18 inputs for Stage 19 consumers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping

from researchclaw.literature.citation_plan import CitationPlanReplayInputs
from researchclaw.literature.citation_policy import ActiveConfigSnapshotInputs
from researchclaw.literature.evidence_cards import (
    EvidenceCardContractError,
    parse_cards_manifest,
)
from researchclaw.pipeline.sectional_revision import (
    SectionalRevisionContractError,
    extract_review_ledger,
)


class Stage19InputBundleError(ValueError):
    """Raised when a Stage 19 authority input is unsafe or changes in flight."""


@dataclass(frozen=True)
class BoundArtifact:
    path: str
    sha256: str
    content: bytes

    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stage19InputBundleError(f"{self.path} is not UTF-8") from exc


@dataclass(frozen=True)
class Stage19InputBundle:
    paper: BoundArtifact
    paper_structure_report: BoundArtifact
    experiment_fact_closure_report: BoundArtifact
    citation_closure_report: BoundArtifact
    reviews: BoundArtifact
    review_structure_report: BoundArtifact
    citation_plan: BoundArtifact
    effective_policy: BoundArtifact
    citation_allowlist: BoundArtifact
    candidates: BoundArtifact
    registry: BoundArtifact
    bibliography: BoundArtifact
    shortlist: BoundArtifact
    screening_report: BoundArtifact
    cards_manifest: BoundArtifact
    card_artifacts: tuple[BoundArtifact, ...]
    active_config_pointer: BoundArtifact | None
    config_snapshot_history: BoundArtifact | None
    resumed_config_snapshots: tuple[BoundArtifact, ...]
    active_config_snapshot: BoundArtifact
    checkpoint: BoundArtifact | None

    @property
    def artifacts(self) -> tuple[BoundArtifact, ...]:
        return (
            self.paper,
            self.paper_structure_report,
            self.experiment_fact_closure_report,
            self.citation_closure_report,
            self.reviews,
            self.review_structure_report,
            self.citation_plan,
            self.effective_policy,
            self.citation_allowlist,
            self.candidates,
            self.registry,
            self.bibliography,
            self.shortlist,
            self.screening_report,
            self.cards_manifest,
            *self.card_artifacts,
            *self.resumed_config_snapshots,
            *(
                artifact
                for artifact in (
                    self.active_config_pointer,
                    self.config_snapshot_history,
                    self.checkpoint,
                )
                if artifact is not None
            ),
            self.active_config_snapshot,
        )

    def citation_replay_inputs(self) -> CitationPlanReplayInputs:
        """Return Stage 4-6 / Stage 16 provenance as immutable captured text."""
        return CitationPlanReplayInputs(
            candidates_text=self.candidates.text(),
            registry_text=self.registry.text(),
            bibliography_text=self.bibliography.text(),
            shortlist_text=self.shortlist.text(),
            screening_report_text=self.screening_report.text(),
            cards_manifest_text=self.cards_manifest.text(),
            card_texts=MappingProxyType(
                {artifact.path: artifact.text() for artifact in self.card_artifacts}
            ),
            citation_allowlist_text=self.citation_allowlist.text(),
            effective_policy_text=self.effective_policy.text(),
            citation_plan_text=self.citation_plan.text(),
            active_config=ActiveConfigSnapshotInputs(
                config_source_path=self.active_config_snapshot.path,
                config_source_text=self.active_config_snapshot.text(),
                pointer_text=(
                    self.active_config_pointer.text()
                    if self.active_config_pointer is not None
                    else None
                ),
                history_text=(
                    self.config_snapshot_history.text()
                    if self.config_snapshot_history is not None
                    else None
                ),
                checkpoint_text=self.checkpoint.text() if self.checkpoint is not None else None,
            ),
        )


_PATHS = {
    "paper": "stage-17/paper_draft.md",
    "paper_structure_report": "stage-17/paper_structure_report.json",
    "experiment_fact_closure_report": "stage-17/experiment_fact_closure_report.json",
    "citation_closure_report": "stage-17/citation_closure_report.json",
    "reviews": "stage-18/reviews.md",
    "review_structure_report": "stage-18/review_structure_report.json",
    "citation_plan": "stage-16/citation_plan.json",
    "effective_policy": "stage-16/citation_policy_effective.json",
    "citation_allowlist": "stage-06/citation_allowlist.json",
    "candidates": "stage-04/candidates.jsonl",
    "registry": "stage-04/cite_key_registry.json",
    "bibliography": "stage-04/references.bib",
    "shortlist": "stage-05/shortlist.jsonl",
    "screening_report": "stage-05/screening_report.json",
    "cards_manifest": "stage-06/cards_manifest.json",
}


def load_stage19_input_bundle(run_dir: Path) -> Stage19InputBundle:
    """Capture every Stage 19 authority file once as immutable bytes."""

    values = {name: _read_regular_file(run_dir, path) for name, path in _PATHS.items()}
    card_artifacts = _read_card_artifacts(run_dir, values["cards_manifest"])
    pointer = _read_optional_regular_file(run_dir, "active_config_snapshot.json")
    history = _read_optional_regular_file(run_dir, "config_snapshot_history.jsonl")
    checkpoint = _read_optional_regular_file(run_dir, "checkpoint.json")
    resumed_snapshots = _read_resumed_config_snapshots(run_dir)
    config_path = _selected_config_path(
        pointer,
        history,
        checkpoint,
        resumed_snapshots,
    )
    return Stage19InputBundle(
        **values,
        card_artifacts=card_artifacts,
        active_config_pointer=pointer,
        config_snapshot_history=history,
        resumed_config_snapshots=resumed_snapshots,
        active_config_snapshot=_read_regular_file(run_dir, config_path),
        checkpoint=checkpoint,
    )


def verify_stage19_input_bundle_unchanged(
    run_dir: Path, bundle: Stage19InputBundle
) -> None:
    """Rediscover the namespace and compare it without consuming fresh bytes."""

    try:
        current = load_stage19_input_bundle(run_dir)
    except Stage19InputBundleError as exc:
        raise Stage19InputBundleError(
            f"Stage 19 input namespace changed after capture: {exc}"
        ) from exc
    if current != bundle:
        raise Stage19InputBundleError("Stage 19 input bundle changed after capture")


def parse_stage18_review_structure_report(
    text: str,
    *,
    bundle: Stage19InputBundle,
    canonical_evidence_path: str,
    canonical_evidence_sha256: str,
) -> dict[str, object]:
    """Strictly bind Stage 18 reviews to the captured Stage 17 generation."""

    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise Stage19InputBundleError("invalid Stage 18 review structure JSON") from exc
    if not isinstance(value, dict):
        raise Stage19InputBundleError("Stage 18 review structure report must be an object")
    expected = {
        "schema_version",
        "valid",
        "source_reviews_path",
        "source_reviews_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "paper_structure_report_path",
        "paper_structure_report_sha256",
        "experiment_fact_closure_report_path",
        "experiment_fact_closure_report_sha256",
        "citation_closure_report_path",
        "citation_closure_report_sha256",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "comment_count",
        "issues",
    }
    if set(value) != expected:
        raise Stage19InputBundleError("Stage 18 review structure fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise Stage19InputBundleError("unsupported Stage 18 review structure schema")
    if not isinstance(value["valid"], bool):
        raise Stage19InputBundleError("Stage 18 review validity is invalid")
    if type(value["comment_count"]) is not int or value["comment_count"] < 0:
        raise Stage19InputBundleError("Stage 18 review comment count is invalid")
    if not isinstance(value["issues"], list):
        raise Stage19InputBundleError("Stage 18 review issues are invalid")
    if value["valid"] and value["issues"]:
        raise Stage19InputBundleError("valid Stage 18 review report has issues")
    if not value["valid"] and not value["issues"]:
        raise Stage19InputBundleError("invalid Stage 18 review report has no issues")
    for issue in value["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"code", "message", "line"}:
            raise Stage19InputBundleError("Stage 18 review issue fields mismatch")
        if not isinstance(issue["code"], str) or not issue["code"].strip():
            raise Stage19InputBundleError("Stage 18 review issue code is invalid")
        if not isinstance(issue["message"], str) or not issue["message"].strip():
            raise Stage19InputBundleError("Stage 18 review issue message is invalid")
        line = issue["line"]
        if line is not None and (
            isinstance(line, bool) or not isinstance(line, int) or line < 1
        ):
            raise Stage19InputBundleError("Stage 18 review issue line is invalid")
    expected_bindings = {
        "source_reviews_path": bundle.reviews,
        "source_paper_path": bundle.paper,
        "paper_structure_report_path": bundle.paper_structure_report,
        "experiment_fact_closure_report_path": bundle.experiment_fact_closure_report,
        "citation_closure_report_path": bundle.citation_closure_report,
    }
    for path_field, artifact in expected_bindings.items():
        hash_field = path_field.replace("_path", "_sha256")
        if value[path_field] != artifact.path or value[hash_field] != artifact.sha256:
            raise Stage19InputBundleError(
                f"Stage 18 review structure does not bind {artifact.path}"
            )
    if (
        value["canonical_experiment_evidence_path"] != canonical_evidence_path
        or value["canonical_experiment_evidence_sha256"] != canonical_evidence_sha256
    ):
        raise Stage19InputBundleError("Stage 18 review structure canonical evidence mismatch")
    if value["valid"]:
        try:
            ledger = extract_review_ledger(
                bundle.reviews.text(), source_path="stage-18/reviews.md"
            )
        except SectionalRevisionContractError as exc:
            raise Stage19InputBundleError(
                f"valid Stage 18 reviews cannot produce a strict ledger: {exc}"
            ) from exc
        if value["comment_count"] != len(ledger.comments):
            raise Stage19InputBundleError("Stage 18 review comment count mismatch")
    return value


def _read_regular_file(run_dir: Path, relative_path: str) -> BoundArtifact:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise Stage19InputBundleError("Stage 19 run root is unsafe")
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise Stage19InputBundleError("noncanonical Stage 19 input path")
    path = run_dir.joinpath(*relative.parts)
    parent = run_dir
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or not parent.is_dir():
            raise Stage19InputBundleError(f"unsafe Stage 19 input parent: {relative_path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Stage19InputBundleError(f"cannot open Stage 19 input {relative_path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise Stage19InputBundleError(f"Stage 19 input is not a regular file: {relative_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    return BoundArtifact(
        path=relative.as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _read_optional_regular_file(run_dir: Path, relative_path: str) -> BoundArtifact | None:
    path = run_dir / relative_path
    if path.is_symlink():
        raise Stage19InputBundleError(f"unsafe optional Stage 19 input: {relative_path}")
    if not path.exists():
        return None
    return _read_regular_file(run_dir, relative_path)


def _selected_config_path(
    pointer: BoundArtifact | None,
    history: BoundArtifact | None,
    checkpoint: BoundArtifact | None,
    resumed_snapshots: tuple[BoundArtifact, ...],
) -> str:
    if pointer is None:
        if history is not None or checkpoint is not None or resumed_snapshots:
            raise Stage19InputBundleError("resume state exists without active config pointer")
        return "config.yaml"
    try:
        value = json.loads(pointer.text(), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise Stage19InputBundleError("invalid active config pointer JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("config_source_path"), str):
        raise Stage19InputBundleError("active config pointer has no config source path")
    relative = value["config_source_path"]
    if relative != "config.yaml" and re.fullmatch(
        r"config\.resumed-\d{8}-\d{6}\.yaml", relative
    ) is None:
        raise Stage19InputBundleError("active config pointer has unsafe config source path")
    if relative.startswith("config.resumed-") and relative not in {
        artifact.path for artifact in resumed_snapshots
    }:
        raise Stage19InputBundleError("active resumed config snapshot is missing")
    return relative


def _read_resumed_config_snapshots(run_dir: Path) -> tuple[BoundArtifact, ...]:
    """Capture the exact run-root resumed-config namespace and bytes."""

    paths: list[str] = []
    try:
        entries = tuple(run_dir.iterdir())
    except OSError as exc:
        raise Stage19InputBundleError("cannot enumerate Stage 19 run root") from exc
    for entry in entries:
        name = entry.name
        if not (name.startswith("config.resumed-") and name.endswith(".yaml")):
            continue
        if re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name) is None:
            raise Stage19InputBundleError("noncanonical resumed config snapshot name")
        paths.append(name)
    return tuple(_read_regular_file(run_dir, path) for path in sorted(paths))


def _read_card_artifacts(
    run_dir: Path, manifest_artifact: BoundArtifact
) -> tuple[BoundArtifact, ...]:
    try:
        manifest = parse_cards_manifest(manifest_artifact.text())
    except EvidenceCardContractError as exc:
        raise Stage19InputBundleError(f"invalid cards manifest: {exc}") from exc
    expected_paths = {
        str(entry[field])
        for entry in manifest["cards"]
        for field in ("json_path", "markdown_path")
    }
    cards_dir = run_dir / "stage-06" / "cards"
    if cards_dir.is_symlink() or not cards_dir.is_dir():
        raise Stage19InputBundleError("Stage 19 cards directory is missing or unsafe")
    actual_paths: set[str] = set()
    for path in cards_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise Stage19InputBundleError("Stage 19 cards directory must be flat files")
        actual_paths.add(f"stage-06/cards/{path.name}")
    if actual_paths != expected_paths:
        raise Stage19InputBundleError("Stage 19 cards manifest closure mismatch")
    return tuple(_read_regular_file(run_dir, path) for path in sorted(expected_paths))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stage19InputBundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
