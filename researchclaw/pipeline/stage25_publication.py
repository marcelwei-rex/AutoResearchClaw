"""Canonical Stage 25 recommend-only prose audit publication."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationSnapshot,
    load_stage24_publication_snapshot,
)


STAGE25_PUBLICATION_POLICY_VERSION = "stage25_deai_v1"
STAGE25_MANIFEST_PATH = "stage25_deai_manifest.json"
STAGE25_OUTPUT_PATH = "deai_audit.json"
_STAGING_NAME = ".stage25-publication.staging"

_DEAI_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bdelve(?:s|d)?\b", "'delve' is a well-known AI-generated tic"),
    (r"\bIt is worth noting that\b", "hedging filler common in AI prose"),
    (r"\bIn conclusion,", "formulaic closer"),
    (r"\bFurthermore,\s", "chained formal connectives read as generated"),
    (r"\bMoreover,\s", "chained formal connectives read as generated"),
    (r"\bplays a (?:crucial|pivotal|vital) role\b", "stock intensifier phrase"),
    (r"\bunderscore(?:s|d)? the importance\b", "stock emphasis phrase"),
    (r"\bcomprehensive(?:ly)?\b", "overused breadth adjective"),
)


class Stage25PublicationError(ValueError):
    """Raised when Stage 25 cannot publish or replay one generation."""


@dataclass(frozen=True)
class Stage25PublicationSnapshot:
    manifest: BoundArtifact
    audit: BoundArtifact


def execute_stage25_deai(
    run_dir: Path,
    stage_dir: Path,
    *,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage25PublicationSnapshot:
    """Invalidate old authority, capture Stage 24, and publish Stage 25."""

    require_canonical_evidence_capabilities("execute_stage25_deai")
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-25") as namespace:
        try:
            _reset_namespace(namespace)
            source = load_stage24_publication_snapshot(run_dir, runtime_config)
            return _publish_after_invalidation(
                namespace,
                source=source,
                runtime_config=runtime_config,
                llm=llm,
            )
        except Exception as exc:
            try:
                _reset_namespace(namespace)
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 25 cleanup also failed: {cleanup_exc}")
            raise


def _publish_stage25_deai(
    run_dir: Path,
    stage_dir: Path,
    *,
    source: Stage24PublicationSnapshot,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage25PublicationSnapshot:
    """Testable producer entry accepting only an immutable Stage 24 snapshot."""

    require_canonical_evidence_capabilities("_publish_stage25_deai_test_fixture")
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-25") as namespace:
        try:
            _reset_namespace(namespace)
            return _publish_after_invalidation(
                namespace,
                source=source,
                runtime_config=runtime_config,
                llm=llm,
            )
        except Exception as exc:
            try:
                _reset_namespace(namespace)
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 25 cleanup also failed: {cleanup_exc}")
            raise


def _publish_after_invalidation(
    namespace: BoundOutputNamespace,
    *,
    source: Stage24PublicationSnapshot,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage25PublicationSnapshot:
    require_canonical_evidence_capabilities("_stage25_publish_after_invalidation")
    del llm
    output = _derive_audit(source)
    _replay_output(source, output)
    namespace.stage_flat_tree(
        _STAGING_NAME,
        direct_files={STAGE25_OUTPUT_PATH: output},
        flat_directories={},
    )
    staged, directories = namespace.read_flat_tree(_STAGING_NAME)
    if directories or set(staged) != {STAGE25_OUTPUT_PATH}:
        raise Stage25PublicationError("Stage 25 staged namespace mismatch")
    _replay_output(source, staged[STAGE25_OUTPUT_PATH])
    _verify_stage24_unchanged(namespace.run_dir, runtime_config, source)
    namespace.publish_staged_tree(
        _STAGING_NAME,
        direct_names=(STAGE25_OUTPUT_PATH,),
        directory_names=(),
    )
    manifest = _build_manifest(source, output)
    namespace.write_text_atomic(
        STAGE25_MANIFEST_PATH, canonical_authority_json_text(manifest)
    )
    snapshot = _load_stage25_from_namespace(namespace, source=source)
    _verify_stage24_unchanged(namespace.run_dir, runtime_config, source)
    final = _load_stage25_from_namespace(namespace, source=source)
    if final != snapshot:
        raise Stage25PublicationError("Stage 25 publication changed after fixpoint")
    _verify_stage24_unchanged(namespace.run_dir, runtime_config, source)
    return snapshot


def load_stage25_publication(
    run_dir: Path,
    runtime_config: RCConfig,
) -> Stage25PublicationSnapshot:
    """Replay Stage 24 and Stage 25 from their canonical disk namespaces."""

    require_canonical_evidence_capabilities("load_stage25_publication")
    source = load_stage24_publication_snapshot(run_dir, runtime_config)
    with BoundOutputNamespace.open(
        run_dir, run_dir / "stage-25", "stage-25"
    ) as namespace:
        snapshot = _load_stage25_from_namespace(namespace, source=source)
        _verify_stage24_unchanged(run_dir, runtime_config, source)
        final = _load_stage25_from_namespace(namespace, source=source)
        if final != snapshot:
            raise Stage25PublicationError("Stage 25 changed during consumer replay")
        _verify_stage24_unchanged(run_dir, runtime_config, source)
        return snapshot


def _load_stage25_from_namespace(
    namespace: BoundOutputNamespace,
    *,
    source: Stage24PublicationSnapshot,
) -> Stage25PublicationSnapshot:
    require_canonical_evidence_capabilities("_load_stage25_from_namespace")
    if set(namespace.direct_entries()) != {
        STAGE25_OUTPUT_PATH,
        STAGE25_MANIFEST_PATH,
    }:
        raise Stage25PublicationError("Stage 25 output namespace mismatch")
    manifest_bytes = namespace.read_bytes(STAGE25_MANIFEST_PATH)
    output = namespace.read_bytes(STAGE25_OUTPUT_PATH)
    manifest = parse_stage25_manifest(manifest_bytes)
    _verify_manifest(manifest, source, output)
    _replay_output(source, output)
    if namespace.read_bytes(STAGE25_MANIFEST_PATH) != manifest_bytes:
        raise Stage25PublicationError("Stage 25 manifest changed during replay")
    if namespace.read_bytes(STAGE25_OUTPUT_PATH) != output:
        raise Stage25PublicationError("Stage 25 audit changed during replay")
    namespace.assert_canonical()
    return Stage25PublicationSnapshot(
        manifest=_bound(f"stage-25/{STAGE25_MANIFEST_PATH}", manifest_bytes),
        audit=_bound(f"stage-25/{STAGE25_OUTPUT_PATH}", output),
    )


def _derive_audit(source: Stage24PublicationSnapshot) -> bytes:
    try:
        paper_text = source.paper.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage25PublicationError("Stage 24 paper is not UTF-8") from exc
    suggestions = _heuristic_suggestions(paper_text)
    payload = {
        "schema_version": 1,
        "publication_policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
        "recommend_only": True,
        "applied": False,
        "paper_path": source.paper.path,
        "paper_sha256": source.paper.sha256,
        "stage24_manifest_path": source.manifest.path,
        "stage24_manifest_sha256": source.manifest.sha256,
        "suggestions": suggestions,
        "counts": {
            "total": len(suggestions),
            "touches_claim": sum(
                item["risk"] == "touches_claim" for item in suggestions
            ),
        },
        "rework_rule": (
            "If suggestions are adopted, rerun the canonical citation and truth "
            "stages required by the affected claim spans; never edit automatically."
        ),
    }
    return canonical_authority_json_text(payload).encode("utf-8")


def _heuristic_suggestions(paper_text: str) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    stripped = re.sub(r"```.*?```", "", paper_text, flags=re.DOTALL)
    for pattern, issue in _DEAI_PATTERNS:
        for match in re.finditer(pattern, stripped, flags=re.IGNORECASE):
            lo = max(0, match.start() - 80)
            hi = min(len(stripped), match.end() + 80)
            span = " ".join(stripped[lo:hi].split())[:300]
            suggestions.append(
                {
                    "source": "heuristic",
                    "span": span,
                    "issue": issue,
                    "suggested_rewrite": "",
                    "risk": (
                        _suggestion_risk(span)
                    ),
                }
            )
            if len(suggestions) >= 60:
                return suggestions
    return suggestions


def parse_stage25_audit(content: bytes) -> dict[str, Any]:
    value = _parse_json_object(content, "Stage 25 audit")
    fields = {
        "schema_version",
        "publication_policy_version",
        "recommend_only",
        "applied",
        "paper_path",
        "paper_sha256",
        "stage24_manifest_path",
        "stage24_manifest_sha256",
        "suggestions",
        "counts",
        "rework_rule",
    }
    if set(value) != fields:
        raise Stage25PublicationError("Stage 25 audit fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage25PublicationError("Stage 25 audit schema is invalid")
    if value["publication_policy_version"] != STAGE25_PUBLICATION_POLICY_VERSION:
        raise Stage25PublicationError("Stage 25 audit policy is invalid")
    if value["recommend_only"] is not True or value["applied"] is not False:
        raise Stage25PublicationError("Stage 25 audit is not recommend-only")
    _require_path(value["paper_path"], "paper_path")
    _require_hash(value["paper_sha256"], "paper_sha256")
    _require_path(value["stage24_manifest_path"], "stage24_manifest_path")
    _require_hash(value["stage24_manifest_sha256"], "stage24_manifest_sha256")
    suggestions = value["suggestions"]
    if not isinstance(suggestions, list) or len(suggestions) > 100:
        raise Stage25PublicationError("Stage 25 suggestions are invalid")
    parsed = [_parse_suggestion(item) for item in suggestions]
    counts = value["counts"]
    if not isinstance(counts, dict) or set(counts) != {"total", "touches_claim"}:
        raise Stage25PublicationError("Stage 25 counts are invalid")
    if any(type(counts[key]) is not int or counts[key] < 0 for key in counts):
        raise Stage25PublicationError("Stage 25 counts are invalid")
    expected_touches = sum(item["risk"] == "touches_claim" for item in parsed)
    if counts != {"total": len(parsed), "touches_claim": expected_touches}:
        raise Stage25PublicationError("Stage 25 counts mismatch")
    if not isinstance(value["rework_rule"], str) or not value["rework_rule"].strip():
        raise Stage25PublicationError("Stage 25 rework rule is invalid")
    return value


def parse_stage25_manifest(content: bytes) -> dict[str, Any]:
    value = _parse_json_object(content, "Stage 25 manifest")
    fields = {
        "schema_version",
        "publication_policy_version",
        "stage24_manifest_path",
        "stage24_manifest_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage24_artifacts",
        "output_path",
        "output_sha256",
    }
    if set(value) != fields:
        raise Stage25PublicationError("Stage 25 manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage25PublicationError("Stage 25 manifest schema is invalid")
    if value["publication_policy_version"] != STAGE25_PUBLICATION_POLICY_VERSION:
        raise Stage25PublicationError("Stage 25 manifest policy is invalid")
    for field in ("stage24_manifest_path", "source_paper_path", "output_path"):
        _require_path(value[field], field)
    for field in ("stage24_manifest_sha256", "source_paper_sha256", "output_sha256"):
        _require_hash(value[field], field)
    artifacts = value["stage24_artifacts"]
    if not isinstance(artifacts, list):
        raise Stage25PublicationError("Stage 25 artifact closure is invalid")
    parsed: list[tuple[str, str]] = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise Stage25PublicationError("Stage 25 artifact entry is invalid")
        _require_path(item["path"], "artifact.path")
        _require_hash(item["sha256"], "artifact.sha256")
        parsed.append((item["path"], item["sha256"]))
    if parsed != sorted(set(parsed)):
        raise Stage25PublicationError("Stage 25 artifact closure is not canonical")
    return value


def _build_manifest(
    source: Stage24PublicationSnapshot,
    output: bytes,
) -> dict[str, Any]:
    artifacts = sorted(
        (item.path, item.sha256)
        for item in (*source.outputs, *source.assessment_files)
    )
    return {
        "schema_version": 1,
        "publication_policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
        "stage24_manifest_path": source.manifest.path,
        "stage24_manifest_sha256": source.manifest.sha256,
        "source_paper_path": source.paper.path,
        "source_paper_sha256": source.paper.sha256,
        "stage24_artifacts": [
            {"path": path, "sha256": sha256} for path, sha256 in artifacts
        ],
        "output_path": f"stage-25/{STAGE25_OUTPUT_PATH}",
        "output_sha256": _sha256(output),
    }


def _verify_manifest(
    manifest: Mapping[str, Any],
    source: Stage24PublicationSnapshot,
    output: bytes,
) -> None:
    if manifest != _build_manifest(source, output):
        raise Stage25PublicationError("Stage 25 manifest replay mismatch")


def _replay_output(source: Stage24PublicationSnapshot, output: bytes) -> None:
    parsed = parse_stage25_audit(output)
    if parsed["paper_path"] != source.paper.path:
        raise Stage25PublicationError("Stage 25 paper path binding mismatch")
    if parsed["paper_sha256"] != source.paper.sha256:
        raise Stage25PublicationError("Stage 25 paper hash binding mismatch")
    if parsed["stage24_manifest_path"] != source.manifest.path:
        raise Stage25PublicationError("Stage 25 source manifest path mismatch")
    if parsed["stage24_manifest_sha256"] != source.manifest.sha256:
        raise Stage25PublicationError("Stage 25 source manifest hash mismatch")
    for suggestion in parsed["suggestions"]:
        if suggestion["risk"] != _suggestion_risk(suggestion["span"]):
            raise Stage25PublicationError(
                "Stage 25 suggestion risk does not match deterministic policy"
            )
    expected = _derive_audit(source)
    if output != expected:
        raise Stage25PublicationError("Stage 25 audit semantic replay mismatch")


def _verify_stage24_unchanged(
    run_dir: Path,
    runtime_config: RCConfig,
    source: Stage24PublicationSnapshot,
) -> None:
    if load_stage24_publication_snapshot(run_dir, runtime_config) != source:
        raise Stage25PublicationError("Stage 24 publication changed during Stage 25")


def _reset_namespace(namespace: BoundOutputNamespace) -> None:
    errors: list[str] = []
    for name in (STAGE25_MANIFEST_PATH, STAGE25_OUTPUT_PATH, _STAGING_NAME):
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    namespace.assert_canonical()
    if errors:
        raise OSError("Stage 25 cleanup failed: " + "; ".join(errors))


def _parse_suggestion(item: object, *, source: str | None = None) -> dict[str, str]:
    fields = {"source", "span", "issue", "suggested_rewrite", "risk"}
    expected = fields - ({"source"} if source is not None else set())
    if not isinstance(item, dict) or set(item) != expected:
        raise Stage25PublicationError("Stage 25 suggestion fields mismatch")
    value = dict(item)
    if source is not None:
        value["source"] = source
    if value["source"] != "heuristic":
        raise Stage25PublicationError("Stage 25 suggestion source is invalid")
    for field, maximum in (("span", 300), ("issue", 300), ("suggested_rewrite", 500)):
        text = value[field]
        if not isinstance(text, str) or len(text) > maximum:
            raise Stage25PublicationError(f"Stage 25 suggestion {field} is invalid")
        if field != "suggested_rewrite" and not text.strip():
            raise Stage25PublicationError(f"Stage 25 suggestion {field} is empty")
    if value["risk"] not in {"style_only", "touches_claim"}:
        raise Stage25PublicationError("Stage 25 suggestion risk is invalid")
    return value


def _suggestion_risk(span: str) -> str:
    return (
        "touches_claim"
        if re.search(r"\d|\\cite|\[[A-Za-z]+\d{4}", span)
        else "style_only"
    )


def _parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage25PublicationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise Stage25PublicationError(f"{label} root is not an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_path(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise Stage25PublicationError(f"Stage 25 {field} is invalid")


def _require_hash(value: object, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Stage25PublicationError(f"Stage 25 {field} is invalid")


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(path, _sha256(content), content)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
