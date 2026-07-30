"""Pure authority helpers for inactive structured Stage 25."""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from researchclaw.pipeline.stage25_publication import (
    STAGE25_PUBLICATION_POLICY_VERSION,
    _parse_suggestion,
    _suggestion_risk,
)


class StructuredStage25AuthorityError(RuntimeError):
    """A structured Stage 25 authority value is malformed."""


PUBLICATION_MODE = "structured-scientific-claim-v1"
STRUCTURED_STAGE25_ARTIFACTS = (
    "deai_audit.json",
    "stage25_deai_manifest.json",
)
STRUCTURED_STAGE25_EVIDENCE_REFS = tuple(
    f"stage-25/{name}" for name in STRUCTURED_STAGE25_ARTIFACTS
)
STRUCTURED_STAGE25_AUDIT_ROOTS = (
    "schema_version",
    "publication_policy_version",
    "recommend_only",
    "applied",
    "paper",
    "source_stage24_manifest",
    "suggestions",
    "counts",
    "rework_rule",
)
STRUCTURED_STAGE25_MANIFEST_ROOTS = (
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
    "source_stage21_manifest",
    "source_stage22_manifest",
    "source_stage23_manifest",
    "source_stage24_manifest",
    "source_paper",
    "quality_outcome",
    "degradation_signal",
    "claim_scope",
    "stage24_outcome",
    "stage24_output_count",
    "stage24_outputs",
    "deai_policy",
    "output_count",
    "outputs",
    "release_verdict",
    "generated",
)
RELEASE_BLOCK_REASONS = (
    "claim_scope_not_research_release",
    "compiler_not_success",
    "degradation_present",
    "pdf_missing_or_mismatched",
    "quality_not_passed",
    "stage23_not_passed",
    "stage24_not_passed",
)
_CAPABILITY = {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 0,
}
_OUTPUT_ROLES = (
    "obligation_inventory",
    "claims",
    "citations",
    "citation_support",
    "critique_resolution",
    "truth_audit",
    "citation_assessment",
    "generic_support_assessment",
    "resolution_assessment",
)
_DIRECT_STAGE24 = (
    ("obligation_inventory", "obligation_inventory.json"),
    ("claims", "claims.json"),
    ("citations", "citations.json"),
    ("citation_support", "citation_support.json"),
    ("critique_resolution", "critique_resolution.json"),
    ("truth_audit", "truth_audit.json"),
)


def derive_release_verdict(
    *,
    stage24_outcome: str,
    quality_outcome: str,
    stage23_outcome: str,
    degradation_signal: object,
    claim_scope: str,
    compiler_outcome: str,
    pdf_valid: bool,
) -> dict[str, object]:
    reasons: list[str] = []
    if claim_scope != "research_release":
        reasons.append("claim_scope_not_research_release")
    if compiler_outcome != "compiler-success":
        reasons.append("compiler_not_success")
    if degradation_signal is not None:
        reasons.append("degradation_present")
    if pdf_valid is not True:
        reasons.append("pdf_missing_or_mismatched")
    if quality_outcome != "passed":
        reasons.append("quality_not_passed")
    if stage23_outcome != "passed":
        reasons.append("stage23_not_passed")
    if stage24_outcome != "passed":
        reasons.append("stage24_not_passed")
    reasons.sort(key=lambda value: value.encode("utf-8"))
    return {
        "verdict": "eligible" if not reasons else "blocked",
        "reasons": reasons,
    }


def validate_stage25_audit_v2(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    if (
        type(value) is not dict
        or set(value) != set(STRUCTURED_STAGE25_AUDIT_ROOTS)
        or len(value) != len(STRUCTURED_STAGE25_AUDIT_ROOTS)
    ):
        raise StructuredStage25AuthorityError("Stage 25 audit roots mismatch")
    _true_int(value["schema_version"], 2, "audit schema")
    if (
        value["publication_policy_version"]
        != STAGE25_PUBLICATION_POLICY_VERSION
        or value["recommend_only"] is not True
        or value["applied"] is not False
    ):
        raise StructuredStage25AuthorityError("Stage 25 audit policy mismatch")
    _file_ref(value["paper"], expected_path="stage-23/paper_final_verified.md")
    _file_ref(
        value["source_stage24_manifest"],
        expected_path="stage-24/stage24_truth_manifest.json",
    )
    suggestions = value["suggestions"]
    if type(suggestions) is not list or len(suggestions) > 100:
        raise StructuredStage25AuthorityError("Stage 25 suggestions mismatch")
    try:
        parsed = [_parse_suggestion(item) for item in suggestions]
    except Exception as exc:
        raise StructuredStage25AuthorityError(
            "Stage 25 suggestion schema mismatch"
        ) from exc
    for item in parsed:
        if item["risk"] != _suggestion_risk(item["span"]):
            raise StructuredStage25AuthorityError(
                "Stage 25 suggestion risk mismatch"
            )
    counts = value["counts"]
    if (
        type(counts) is not dict
        or set(counts) != {"total", "touches_claim"}
        or len(counts) != 2
    ):
        raise StructuredStage25AuthorityError("Stage 25 counts roots mismatch")
    _nonnegative_int(counts["total"], "suggestion total")
    _nonnegative_int(counts["touches_claim"], "touches-claim total")
    expected = {
        "total": len(parsed),
        "touches_claim": sum(
            item["risk"] == "touches_claim" for item in parsed
        ),
    }
    if counts != expected:
        raise StructuredStage25AuthorityError("Stage 25 counts mismatch")
    if type(value["rework_rule"]) is not str or not value["rework_rule"].strip():
        raise StructuredStage25AuthorityError("Stage 25 rework rule mismatch")
    return value


def validate_stage25_manifest_v2(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    if (
        type(value) is not dict
        or set(value) != set(STRUCTURED_STAGE25_MANIFEST_ROOTS)
        or len(value) != len(STRUCTURED_STAGE25_MANIFEST_ROOTS)
    ):
        raise StructuredStage25AuthorityError("Stage 25 manifest roots mismatch")
    _true_int(value["schema_version"], 2, "manifest schema")
    if (
        value["publication_stage_id"] != "stage25"
        or value["publication_mode"] != PUBLICATION_MODE
    ):
        raise StructuredStage25AuthorityError(
            "Stage 25 publication identity mismatch"
        )
    _true_int(
        value["structured_capability_schema_version"],
        1,
        "capability schema",
    )
    if value["structured_capability_snapshot"] != _CAPABILITY:
        raise StructuredStage25AuthorityError(
            "Stage 25 capability is not exact 1110"
        )
    _sha(value["generation_binding_sha256"], "generation binding")
    paths = {
        "canonical_experiment_evidence": "canonical_experiment_evidence.json",
        "source_stage19_manifest": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "source_stage20_manifest": "stage-20/quality_gate_manifest.json",
        "source_stage21_manifest": "stage-21/bundle_index.json",
        "source_stage22_manifest": "stage-22/stage22_export_manifest.json",
        "source_stage23_manifest": "stage-23/stage23_verification_manifest.json",
        "source_stage24_manifest": "stage-24/stage24_truth_manifest.json",
        "source_paper": "stage-23/paper_final_verified.md",
    }
    for field, path in paths.items():
        _file_ref(value[field], expected_path=path)
    _cfs_ref(value["cfs"])
    if value["quality_outcome"] not in {"passed", "degraded"}:
        raise StructuredStage25AuthorityError("quality outcome mismatch")
    if value["degradation_signal"] is not None:
        _file_ref(value["degradation_signal"])
    if (
        (value["quality_outcome"] == "passed")
        != (value["degradation_signal"] is None)
    ):
        raise StructuredStage25AuthorityError(
            "quality and degradation signal mismatch"
        )
    if value["claim_scope"] not in {
        "research_release",
        "pipeline_validation",
        "exploratory",
    }:
        raise StructuredStage25AuthorityError("claim scope mismatch")
    if value["stage24_outcome"] not in {"passed", "degraded"}:
        raise StructuredStage25AuthorityError("Stage 24 outcome mismatch")
    if value["stage24_outcome"] != value["quality_outcome"]:
        raise StructuredStage25AuthorityError(
            "Stage 24 and quality outcomes diverge"
        )
    count = value["stage24_output_count"]
    _nonnegative_int(count, "Stage 24 output count")
    if not 6 <= count <= 274:
        raise StructuredStage25AuthorityError(
            "Stage 24 output count is outside the frozen closure"
        )
    rows = value["stage24_outputs"]
    if type(rows) is not list or len(rows) != count:
        raise StructuredStage25AuthorityError(
            "Stage 24 output count mismatch"
        )
    _stage24_rows(rows)
    if value["deai_policy"] != {
        "policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
        "recommend_only": True,
        "applied": False,
        "provider_calls": 0,
    }:
        raise StructuredStage25AuthorityError("Stage 25 de-AI policy mismatch")
    _true_int(value["output_count"], 1, "Stage 25 output count")
    outputs = value["outputs"]
    if type(outputs) is not list or len(outputs) != 1:
        raise StructuredStage25AuthorityError("Stage 25 outputs mismatch")
    _output_row(
        outputs[0],
        expected_role="deai_audit",
        expected_path="stage-25/deai_audit.json",
        logical_required=False,
    )
    reasons = _verdict(value["release_verdict"])
    locally_required = {
        *(
            ("claim_scope_not_research_release",)
            if value["claim_scope"] != "research_release"
            else ()
        ),
        *(
            ("degradation_present",)
            if value["degradation_signal"] is not None
            else ()
        ),
        *(
            ("quality_not_passed",)
            if value["quality_outcome"] != "passed"
            else ()
        ),
        *(
            ("stage24_not_passed",)
            if value["stage24_outcome"] != "passed"
            else ()
        ),
    }
    local_reason_set = {
        "claim_scope_not_research_release",
        "degradation_present",
        "quality_not_passed",
        "stage24_not_passed",
    }
    if reasons & local_reason_set != locally_required:
        raise StructuredStage25AuthorityError(
            "release verdict local predicates mismatch"
        )
    if type(value["generated"]) is not str or not value["generated"]:
        raise StructuredStage25AuthorityError("generated value mismatch")
    return value


def example_audit_v2() -> dict[str, object]:
    return {
        "schema_version": 2,
        "publication_policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
        "recommend_only": True,
        "applied": False,
        "paper": _example_ref("stage-23/paper_final_verified.md"),
        "source_stage24_manifest": _example_ref(
            "stage-24/stage24_truth_manifest.json"
        ),
        "suggestions": [],
        "counts": {"total": 0, "touches_claim": 0},
        "rework_rule": (
            "If suggestions are adopted, rerun the canonical citation and truth "
            "stages required by the affected claim spans; never edit automatically."
        ),
    }


def example_manifest_v2() -> dict[str, object]:
    stage24_outputs = [
        {
            "role": role,
            "logical_name": None,
            **_example_ref(f"stage-24/{name}"),
        }
        for role, name in _DIRECT_STAGE24
    ]
    return {
        "schema_version": 2,
        "publication_stage_id": "stage25",
        "publication_mode": PUBLICATION_MODE,
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": dict(_CAPABILITY),
        "generation_binding_sha256": "a" * 64,
        "canonical_experiment_evidence": _example_ref(
            "canonical_experiment_evidence.json"
        ),
        "cfs": {"schema_version": 1, "sha256": "a" * 64},
        "source_stage19_manifest": _example_ref(
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "source_stage20_manifest": _example_ref(
            "stage-20/quality_gate_manifest.json"
        ),
        "source_stage21_manifest": _example_ref("stage-21/bundle_index.json"),
        "source_stage22_manifest": _example_ref(
            "stage-22/stage22_export_manifest.json"
        ),
        "source_stage23_manifest": _example_ref(
            "stage-23/stage23_verification_manifest.json"
        ),
        "source_stage24_manifest": _example_ref(
            "stage-24/stage24_truth_manifest.json"
        ),
        "source_paper": _example_ref("stage-23/paper_final_verified.md"),
        "quality_outcome": "passed",
        "degradation_signal": None,
        "claim_scope": "research_release",
        "stage24_outcome": "passed",
        "stage24_output_count": len(stage24_outputs),
        "stage24_outputs": stage24_outputs,
        "deai_policy": {
            "policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
            "recommend_only": True,
            "applied": False,
            "provider_calls": 0,
        },
        "output_count": 1,
        "outputs": [
            {
                "role": "deai_audit",
                "logical_name": None,
                **_example_ref("stage-25/deai_audit.json"),
            }
        ],
        "release_verdict": {"verdict": "eligible", "reasons": []},
        "generated": "2026-07-28T00:00:00+00:00",
    }


def _stage24_rows(rows: Sequence[object]) -> None:
    if len(rows) < len(_DIRECT_STAGE24):
        raise StructuredStage25AuthorityError(
            "Stage 24 fixed output closure is incomplete"
        )
    previous_group = -1
    previous_logical = b""
    seen_paths: set[str] = set()
    seen_pairs: set[tuple[str, object]] = set()
    for index, row in enumerate(rows):
        if type(row) is not dict:
            raise StructuredStage25AuthorityError(
                "Stage 24 output row mismatch"
            )
        role = row.get("role")
        if role not in _OUTPUT_ROLES:
            raise StructuredStage25AuthorityError(
                "Stage 24 output role mismatch"
            )
        group = _OUTPUT_ROLES.index(role)
        if group < previous_group:
            raise StructuredStage25AuthorityError(
                "Stage 24 output role order mismatch"
            )
        logical_required = group >= 6
        _output_row(
            row,
            expected_role=role,
            expected_path=None,
            logical_required=logical_required,
        )
        if group < 6:
            expected_role, expected_name = _DIRECT_STAGE24[group]
            if (
                index != group
                or role != expected_role
                or row["path"] != f"stage-24/{expected_name}"
            ):
                raise StructuredStage25AuthorityError(
                    "Stage 24 fixed output order mismatch"
                )
        else:
            if re.fullmatch(r"[0-9a-f]{64}", row["logical_name"]) is None:
                raise StructuredStage25AuthorityError(
                    "Stage 24 dynamic logical name mismatch"
                )
            directory = {
                "citation_assessment": "citation-assessments",
                "generic_support_assessment": "generic-support-assessments",
                "resolution_assessment": "resolution-assessments",
            }[role]
            if row["path"] != (
                f"stage-24/{directory}/{row['logical_name']}.json"
            ):
                raise StructuredStage25AuthorityError(
                    "Stage 24 dynamic output path mismatch"
                )
            logical = row["logical_name"].encode("utf-8")
            if group == previous_group and logical <= previous_logical:
                raise StructuredStage25AuthorityError(
                    "Stage 24 dynamic output order mismatch"
                )
            previous_logical = logical
        pair = (role, row["logical_name"])
        if row["path"] in seen_paths or pair in seen_pairs:
            raise StructuredStage25AuthorityError(
                "Stage 24 output closure contains a duplicate"
            )
        seen_paths.add(row["path"])
        seen_pairs.add(pair)
        previous_group = group


def _output_row(
    value: object,
    *,
    expected_role: str,
    expected_path: str | None,
    logical_required: bool,
) -> None:
    if (
        type(value) is not dict
        or set(value)
        != {"role", "logical_name", "path", "sha256", "size"}
        or len(value) != 5
    ):
        raise StructuredStage25AuthorityError("output row roots mismatch")
    if value["role"] != expected_role:
        raise StructuredStage25AuthorityError("output role mismatch")
    if logical_required:
        if type(value["logical_name"]) is not str or not value["logical_name"]:
            raise StructuredStage25AuthorityError(
                "output logical name mismatch"
            )
    elif value["logical_name"] is not None:
        raise StructuredStage25AuthorityError("output logical name mismatch")
    if expected_path is not None and value["path"] != expected_path:
        raise StructuredStage25AuthorityError("output path mismatch")
    _path(value["path"], "output path")
    _sha(value["sha256"], "output sha256")
    _nonnegative_int(value["size"], "output size")


def _verdict(value: object) -> set[str]:
    if (
        type(value) is not dict
        or set(value) != {"verdict", "reasons"}
        or len(value) != 2
    ):
        raise StructuredStage25AuthorityError("release verdict roots mismatch")
    reasons = value["reasons"]
    if (
        type(reasons) is not list
        or any(reason not in RELEASE_BLOCK_REASONS for reason in reasons)
        or reasons
        != sorted(set(reasons), key=lambda reason: reason.encode("utf-8"))
    ):
        raise StructuredStage25AuthorityError("release reasons mismatch")
    if (
        value["verdict"] == "eligible"
        and reasons == []
        or value["verdict"] == "blocked"
        and bool(reasons)
    ):
        return set(reasons)
    raise StructuredStage25AuthorityError("release verdict mismatch")


def _file_ref(value: object, *, expected_path: str | None = None) -> None:
    if (
        type(value) is not dict
        or set(value) != {"path", "sha256", "size"}
        or len(value) != 3
    ):
        raise StructuredStage25AuthorityError("FileRef roots mismatch")
    _path(value["path"], "FileRef path")
    if expected_path is not None and value["path"] != expected_path:
        raise StructuredStage25AuthorityError("FileRef path mismatch")
    _sha(value["sha256"], "FileRef sha256")
    _nonnegative_int(value["size"], "FileRef size")


def _cfs_ref(value: object) -> None:
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "sha256"}
        or len(value) != 2
    ):
        raise StructuredStage25AuthorityError("CFS roots mismatch")
    _true_int(value["schema_version"], 1, "CFS schema")
    _sha(value["sha256"], "CFS sha256")


def _true_int(value: object, expected: int, label: str) -> None:
    if type(value) is not int or value != expected:
        raise StructuredStage25AuthorityError(f"{label} mismatch")


def _nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise StructuredStage25AuthorityError(f"{label} mismatch")


def _sha(value: object, label: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise StructuredStage25AuthorityError(f"{label} mismatch")


def _path(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise StructuredStage25AuthorityError(f"{label} mismatch")


def _example_ref(path: str) -> dict[str, object]:
    return {"path": path, "sha256": "a" * 64, "size": 1}
