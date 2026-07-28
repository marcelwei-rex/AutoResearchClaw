"""Deterministic schema-v2 authority for private structured Stage 21."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)


BUNDLE_INDEX_SCHEMA_VERSION = 2
ARCHIVE_SCHEMA_VERSION = 1
PUBLICATION_STAGE_ID = "stage21"
PUBLICATION_MODE = "structured-scientific-claim-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\Z"
)
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "publication_stage_id",
        "publication_mode",
        "structured_capability_schema_version",
        "structured_capability_snapshot",
        "generation_binding_sha256",
        "canonical_experiment_evidence",
        "cfs",
        "source_stage19_manifest",
        "source_stage20_manifest",
        "source_paper",
        "quality_outcome",
        "degradation_signal",
        "archive",
        "artifact_count",
        "artifacts",
        "generated",
    }
)
_PASSED_ROLES = (
    "canonical_experiment_evidence",
    "source_stage19_manifest",
    "source_paper",
    "stage20_quality_report",
    "stage20_fabrication_flags",
    "source_stage20_manifest",
    "stage21_archive",
)
_DEGRADED_ROLES = (
    "canonical_experiment_evidence",
    "source_stage19_manifest",
    "source_paper",
    "stage20_quality_report",
    "stage20_fabrication_flags",
    "stage20_degradation_signal",
    "source_stage20_manifest",
    "stage21_archive",
)
_ROLE_PATHS = {
    "canonical_experiment_evidence": "canonical_experiment_evidence.json",
    "source_stage19_manifest": (
        "stage-19/scientific_claim_authority_manifest.json"
    ),
    "source_paper": "stage-19/scientific_claim_paper_revised.md",
    "stage20_quality_report": "stage-20/quality_report.json",
    "stage20_fabrication_flags": "stage-20/fabrication_flags.json",
    "stage20_degradation_signal": "degradation_signal.json",
    "source_stage20_manifest": "stage-20/quality_gate_manifest.json",
    "stage21_archive": "stage-21/archive.md",
}
_AUTHORITY_BOUNDARY = (
    "This archive is a deterministic operational projection only; it is not "
    "scientific claim, numeric, or citation authority."
)


class StructuredStage21AuthorityError(ValueError):
    """Schema-v2 bytes are malformed or do not independently replay."""


@dataclass(frozen=True)
class BoundSource:
    role: str
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Stage21AuthorityInputs:
    generation_binding_sha256: str
    cfs_sha256: str
    quality_outcome: str
    generated: str
    sources: tuple[BoundSource, ...]

    def source(self, role: str) -> BoundSource:
        matches = tuple(item for item in self.sources if item.role == role)
        if len(matches) != 1:
            raise StructuredStage21AuthorityError(
                f"exact Stage 21 source role missing: {role}"
            )
        return matches[0]


def render_archive(inputs: Stage21AuthorityInputs) -> bytes:
    """Render the exact frozen ASCII archive grammar."""

    _validate_inputs(inputs)
    canonical = inputs.source("canonical_experiment_evidence")
    paper = inputs.source("source_paper")
    stage19 = inputs.source("source_stage19_manifest")
    stage20 = inputs.source("source_stage20_manifest")
    if inputs.quality_outcome == "passed":
        signal_path = "null"
        signal_sha = "null"
        count = 7
    else:
        signal = inputs.source("stage20_degradation_signal")
        signal_path = signal.path
        signal_sha = signal.sha256
        count = 8
    text = (
        "# Structured Stage 21 Archive\n"
        "\n"
        "## Policy\n"
        "Archive-Schema-Version: 1\n"
        "Publication-Stage-ID: stage21\n"
        "Publication-Mode: structured-scientific-claim-v1\n"
        "\n"
        "## Stage 20 Outcome\n"
        f"Quality-Outcome: {inputs.quality_outcome}\n"
        f"Degradation-Signal-Path: {signal_path}\n"
        f"Degradation-Signal-SHA256: {signal_sha}\n"
        "\n"
        "## Source Bindings\n"
        f"Canonical-Evidence-Path: {canonical.path}\n"
        f"Canonical-Evidence-SHA256: {canonical.sha256}\n"
        f"Source-Paper-Path: {paper.path}\n"
        f"Source-Paper-SHA256: {paper.sha256}\n"
        f"Stage19-Manifest-Path: {stage19.path}\n"
        f"Stage19-Manifest-SHA256: {stage19.sha256}\n"
        f"Stage20-Manifest-Path: {stage20.path}\n"
        f"Stage20-Manifest-SHA256: {stage20.sha256}\n"
        f"Generation-Binding-SHA256: {inputs.generation_binding_sha256}\n"
        "CFS-Path: null\n"
        "CFS-Schema-Version: 1\n"
        f"CFS-SHA256: {inputs.cfs_sha256}\n"
        "\n"
        "## Artifact Inventory\n"
        f"Artifact-Count: {count}\n"
        "\n"
        "## Authority Boundary\n"
        f"{_AUTHORITY_BOUNDARY}\n"
    )
    try:
        content = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StructuredStage21AuthorityError(
            "structured Stage 21 archive is not ASCII"
        ) from exc
    _require_archive_bytes(content)
    return content


def build_bundle_index_v2(
    inputs: Stage21AuthorityInputs,
    *,
    archive_bytes: bytes,
) -> bytes:
    """Build exact canonical schema-v2 bytes from held source snapshots."""

    expected_archive = render_archive(inputs)
    if archive_bytes != expected_archive:
        raise StructuredStage21AuthorityError(
            "archive bytes do not match independent renderer"
        )
    roles = (
        _PASSED_ROLES
        if inputs.quality_outcome == "passed"
        else _DEGRADED_ROLES
    )
    source_by_role = {item.role: item for item in inputs.sources}
    archive = BoundSource(
        "stage21_archive",
        "stage-21/archive.md",
        hashlib.sha256(archive_bytes).hexdigest(),
        len(archive_bytes),
    )
    inventory_sources = {**source_by_role, "stage21_archive": archive}
    artifacts = [
        {
            "role": role,
            "path": inventory_sources[role].path,
            "sha256": inventory_sources[role].sha256,
            "size": inventory_sources[role].size,
        }
        for role in roles
    ]
    canonical = inputs.source("canonical_experiment_evidence")
    stage19 = inputs.source("source_stage19_manifest")
    stage20 = inputs.source("source_stage20_manifest")
    paper = inputs.source("source_paper")
    signal = (
        None
        if inputs.quality_outcome == "passed"
        else _file_ref(inputs.source("stage20_degradation_signal"))
    )
    payload: dict[str, object] = {
        "schema_version": BUNDLE_INDEX_SCHEMA_VERSION,
        "publication_stage_id": PUBLICATION_STAGE_ID,
        "publication_mode": PUBLICATION_MODE,
        "structured_capability_schema_version": (
            capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION
        ),
        "structured_capability_snapshot": (
            capability.code_owned_structured_capability_snapshot()
        ),
        "generation_binding_sha256": inputs.generation_binding_sha256,
        "canonical_experiment_evidence": _file_ref(canonical),
        "cfs": {"schema_version": 1, "sha256": inputs.cfs_sha256},
        "source_stage19_manifest": _file_ref(stage19),
        "source_stage20_manifest": _file_ref(stage20),
        "source_paper": _file_ref(paper),
        "quality_outcome": inputs.quality_outcome,
        "degradation_signal": signal,
        "archive": _file_ref(archive),
        "artifact_count": len(roles),
        "artifacts": artifacts,
        "generated": inputs.generated,
    }
    content = _canonical_json(payload)
    parsed = parse_bundle_index_v2(content)
    _cross_match_manifest(parsed, archive_bytes=archive_bytes)
    return content


def parse_bundle_index_v2(content: bytes) -> dict[str, object]:
    """Strictly parse canonical schema-v2 authority bytes."""

    if type(content) is not bytes or not content:
        raise StructuredStage21AuthorityError(
            "bundle index must be nonempty bytes"
        )
    if content.startswith(b"\xef\xbb\xbf") or b"\r" in content:
        raise StructuredStage21AuthorityError(
            "bundle index encoding is not canonical"
        )
    try:
        text = content.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except StructuredStage21AuthorityError:
        raise
    except Exception as exc:
        raise StructuredStage21AuthorityError(
            "bundle index is not strict JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _ROOT_KEYS:
        raise StructuredStage21AuthorityError(
            "bundle index root fields mismatch"
        )
    if _canonical_json(payload) != content:
        raise StructuredStage21AuthorityError(
            "bundle index JSON bytes are not canonical"
        )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 2
        or payload["publication_stage_id"] != PUBLICATION_STAGE_ID
        or payload["publication_mode"] != PUBLICATION_MODE
        or type(payload["structured_capability_schema_version"]) is not int
        or payload["structured_capability_schema_version"] != 1
    ):
        raise StructuredStage21AuthorityError(
            "bundle index policy fields mismatch"
        )
    expected_capability = (
        capability.code_owned_structured_capability_snapshot()
    )
    stored_capability = payload["structured_capability_snapshot"]
    if (
        not isinstance(stored_capability, dict)
        or set(stored_capability) != set(expected_capability)
        or any(
            type(stored_capability[name]) is not int
            or stored_capability[name] != expected_capability[name]
            for name in expected_capability
        )
    ):
        raise StructuredStage21AuthorityError(
            "bundle index capability snapshot mismatch"
        )
    if expected_capability != {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }:
        raise StructuredStage21AuthorityError(
            "private Stage 21 requires exact capability 1110"
        )
    _require_sha(payload["generation_binding_sha256"], "generation binding")
    _require_file_ref(
        payload["canonical_experiment_evidence"],
        "canonical_experiment_evidence.json",
        "canonical evidence",
    )
    _require_cfs(payload["cfs"])
    _require_file_ref(
        payload["source_stage19_manifest"],
        "stage-19/scientific_claim_authority_manifest.json",
        "Stage 19 manifest",
    )
    _require_file_ref(
        payload["source_stage20_manifest"],
        "stage-20/quality_gate_manifest.json",
        "Stage 20 manifest",
    )
    _require_file_ref(
        payload["source_paper"],
        "stage-19/scientific_claim_paper_revised.md",
        "source paper",
    )
    _require_file_ref(
        payload["archive"], "stage-21/archive.md", "archive"
    )
    outcome = payload["quality_outcome"]
    if outcome not in {"passed", "degraded"}:
        raise StructuredStage21AuthorityError(
            "bundle index quality outcome is invalid"
        )
    if outcome == "passed":
        if payload["degradation_signal"] is not None:
            raise StructuredStage21AuthorityError(
                "passed bundle has a degradation signal"
            )
        roles = _PASSED_ROLES
    else:
        _require_file_ref(
            payload["degradation_signal"],
            "degradation_signal.json",
            "degradation signal",
        )
        roles = _DEGRADED_ROLES
    count = payload["artifact_count"]
    if type(count) is not int or count != len(roles):
        raise StructuredStage21AuthorityError(
            "bundle index artifact count mismatch"
        )
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != len(roles):
        raise StructuredStage21AuthorityError(
            "bundle index artifact inventory mismatch"
        )
    for row, role in zip(artifacts, roles, strict=True):
        if not isinstance(row, dict) or set(row) != {
            "role",
            "path",
            "sha256",
            "size",
        }:
            raise StructuredStage21AuthorityError(
                "bundle index artifact fields mismatch"
            )
        if row["role"] != role or row["path"] != _ROLE_PATHS[role]:
            raise StructuredStage21AuthorityError(
                "bundle index artifact order or path mismatch"
            )
        _require_sha(row["sha256"], f"artifact {role}")
        if type(row["size"]) is not int or row["size"] < 0:
            raise StructuredStage21AuthorityError(
                f"artifact {role} size is invalid"
            )
    _require_timestamp(payload["generated"])
    _cross_match_manifest(payload, archive_bytes=None)
    return payload


def render_archive_from_manifest(
    manifest: Mapping[str, object],
) -> bytes:
    """Independently rerender archive bytes from a parsed v2 manifest."""

    sources = (
        _bound_from_ref(
            "canonical_experiment_evidence",
            manifest["canonical_experiment_evidence"],
            manifest,
        ),
        _bound_from_ref(
            "source_stage19_manifest",
            manifest["source_stage19_manifest"],
            manifest,
        ),
        _bound_from_ref(
            "source_paper", manifest["source_paper"], manifest
        ),
        _bound_from_inventory("stage20_quality_report", manifest),
        _bound_from_inventory("stage20_fabrication_flags", manifest),
        *(
            ()
            if manifest["quality_outcome"] == "passed"
            else (
                _bound_from_ref(
                    "stage20_degradation_signal",
                    manifest["degradation_signal"],
                    manifest,
                ),
            )
        ),
        _bound_from_ref(
            "source_stage20_manifest",
            manifest["source_stage20_manifest"],
            manifest,
        ),
    )
    cfs = manifest["cfs"]
    assert isinstance(cfs, dict)
    inputs = Stage21AuthorityInputs(
        str(manifest["generation_binding_sha256"]),
        str(cfs["sha256"]),
        str(manifest["quality_outcome"]),
        str(manifest["generated"]),
        tuple(sources),
    )
    return render_archive(inputs)


def replay_bundle_index_v2(
    content: bytes,
    *,
    archive_bytes: bytes,
    expected: bytes,
) -> dict[str, object]:
    """Replay stored index and independently rerender its bound archive."""

    payload = parse_bundle_index_v2(content)
    _require_archive_bytes(archive_bytes)
    rerendered = render_archive_from_manifest(payload)
    if archive_bytes != rerendered:
        raise StructuredStage21AuthorityError(
            "archive does not independently rerender"
        )
    _cross_match_manifest(payload, archive_bytes=archive_bytes)
    if type(expected) is not bytes or content != expected:
        raise StructuredStage21AuthorityError(
            "bundle index differs from deterministic rebuild"
        )
    return payload


def _validate_inputs(inputs: Stage21AuthorityInputs) -> None:
    if not isinstance(inputs, Stage21AuthorityInputs):
        raise StructuredStage21AuthorityError(
            "Stage 21 authority inputs are invalid"
        )
    _require_sha(inputs.generation_binding_sha256, "generation binding")
    _require_sha(inputs.cfs_sha256, "CFS")
    _require_timestamp(inputs.generated)
    roles = (
        _PASSED_ROLES[:-1]
        if inputs.quality_outcome == "passed"
        else _DEGRADED_ROLES[:-1]
    )
    if tuple(item.role for item in inputs.sources) != roles:
        raise StructuredStage21AuthorityError(
            "Stage 21 source role inventory mismatch"
        )
    for item in inputs.sources:
        if (
            not isinstance(item, BoundSource)
            or item.path != _ROLE_PATHS[item.role]
            or type(item.size) is not int
            or item.size < 0
        ):
            raise StructuredStage21AuthorityError(
                f"Stage 21 source is invalid: {item.role}"
            )
        _require_sha(item.sha256, item.role)


def _file_ref(source: BoundSource) -> dict[str, str]:
    return {"path": source.path, "sha256": source.sha256}


def _canonical_json(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StructuredStage21AuthorityError(
            "bundle index cannot be canonically encoded"
        ) from exc


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredStage21AuthorityError(
                f"duplicate bundle index key: {key}"
            )
        result[key] = value
    return result


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise StructuredStage21AuthorityError(f"{label} SHA-256 is invalid")


def _require_timestamp(value: object) -> None:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise StructuredStage21AuthorityError(
            "bundle index generated timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StructuredStage21AuthorityError(
            "bundle index generated timestamp is invalid"
        ) from exc
    if parsed.microsecond != 0 or parsed.utcoffset() is None:
        raise StructuredStage21AuthorityError(
            "bundle index generated timestamp is invalid"
        )


def _require_file_ref(value: object, path: str, label: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or value["path"] != path
    ):
        raise StructuredStage21AuthorityError(f"{label} FileRef is invalid")
    _require_sha(value["sha256"], label)


def _require_cfs(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "sha256"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise StructuredStage21AuthorityError("CFS binding is invalid")
    _require_sha(value["sha256"], "CFS")


def _cross_match_manifest(
    payload: Mapping[str, object],
    *,
    archive_bytes: bytes | None,
) -> None:
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    rows = {str(row["role"]): row for row in artifacts}
    refs = {
        "canonical_experiment_evidence": payload[
            "canonical_experiment_evidence"
        ],
        "source_stage19_manifest": payload["source_stage19_manifest"],
        "source_paper": payload["source_paper"],
        "source_stage20_manifest": payload["source_stage20_manifest"],
        "stage21_archive": payload["archive"],
    }
    if payload["quality_outcome"] == "degraded":
        refs["stage20_degradation_signal"] = payload[
            "degradation_signal"
        ]
    for role, ref in refs.items():
        assert isinstance(ref, dict)
        row = rows.get(role)
        if (
            row is None
            or row["path"] != ref["path"]
            or row["sha256"] != ref["sha256"]
        ):
            raise StructuredStage21AuthorityError(
                f"bundle index FileRef inventory mismatch: {role}"
            )
    if archive_bytes is not None:
        archive_row = rows["stage21_archive"]
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if (
            archive_row["sha256"] != digest
            or archive_row["size"] != len(archive_bytes)
        ):
            raise StructuredStage21AuthorityError(
                "archive hash or size mismatch"
            )


def _bound_from_ref(
    role: str,
    ref: object,
    manifest: Mapping[str, object],
) -> BoundSource:
    assert isinstance(ref, dict)
    row = _inventory_row(role, manifest)
    return BoundSource(
        role,
        str(ref["path"]),
        str(ref["sha256"]),
        int(row["size"]),
    )


def _bound_from_inventory(
    role: str, manifest: Mapping[str, object]
) -> BoundSource:
    row = _inventory_row(role, manifest)
    return BoundSource(
        role,
        str(row["path"]),
        str(row["sha256"]),
        int(row["size"]),
    )


def _inventory_row(
    role: str, manifest: Mapping[str, object]
) -> Mapping[str, object]:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    matches = tuple(row for row in artifacts if row["role"] == role)
    if len(matches) != 1:
        raise StructuredStage21AuthorityError(
            f"bundle index inventory role mismatch: {role}"
        )
    return matches[0]


def _require_archive_bytes(content: bytes) -> None:
    if (
        type(content) is not bytes
        or not content
        or not content.endswith(b"\n")
        or content.endswith(b"\n\n")
        or b"\r" in content
        or content.startswith(b"\xef\xbb\xbf")
    ):
        raise StructuredStage21AuthorityError(
            "archive bytes are not canonical"
        )
    for line in content.splitlines():
        if line.endswith((b" ", b"\t")):
            raise StructuredStage21AuthorityError(
                "archive contains trailing whitespace"
            )
