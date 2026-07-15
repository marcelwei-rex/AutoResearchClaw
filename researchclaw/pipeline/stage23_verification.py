"""Canonical Stage 23 citation verification and manifest-last publication."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Mapping, Sequence

from researchclaw.config import RCConfig
from researchclaw.literature.verify import (
    CitationResult,
    VerificationReport,
    VerifyStatus,
    parse_bibtex_entries,
    verify_citations,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage23_input_bundle import (
    Stage23InputBundle,
    load_stage23_input_bundle,
    verify_stage23_input_bundle_unchanged,
)


class Stage23VerificationError(ValueError):
    """Raised when Stage 23 cannot publish one closed verification result."""


@dataclass(frozen=True)
class Stage23VerificationOutcome:
    artifacts: tuple[str, ...]
    degraded: bool


_MANIFEST_NAME = "stage23_verification_manifest.json"
_OUTPUT_NAMES = (
    "paper_final_verified.md",
    "references_verified.bib",
    "verification_report.json",
)
_SUMMARY_FIELDS = {
    "total",
    "verified",
    "suspicious",
    "hallucinated",
    "skipped",
    "integrity_score",
    "claim_scope",
    "cited_keys",
    "verification_complete",
    "relevance_complete",
    "relevance_threshold",
    "hallucinated_keys",
    "suspicious_keys",
    "skipped_keys",
    "unscored_keys",
    "low_relevance_keys",
    "relevance_error",
    "degraded",
    "fatal",
}
_RESULT_REQUIRED_FIELDS = {
    "cite_key",
    "title",
    "status",
    "confidence",
    "method",
    "details",
}


def execute_canonical_stage23(
    run_dir: Path,
    stage_dir: Path,
    runtime_config: RCConfig,
    *,
    relevance_checker: Callable[[Sequence[CitationResult]], Mapping[str, Decimal]]
    | None,
) -> Stage23VerificationOutcome:
    """Verify only captured Stage 22 citations and publish a replayable result."""

    _reset_stage23_namespace(run_dir, stage_dir)
    try:
        bundle = load_stage23_input_bundle(run_dir, runtime_config)
        bounded_bib = _bounded_bibliography(
            bundle.bibliography.text(), set(bundle.cited_keys)
        )
        report = (
            verify_citations(
                bounded_bib,
                s2_api_key=(
                    getattr(
                        bundle.stage22_inputs.canonical_config.llm,
                        "s2_api_key",
                        "",
                    )
                    or ""
                ),
            )
            if bundle.cited_keys
            else VerificationReport()
        )
        _validate_external_report(report, bundle.cited_keys)
        relevance_error = ""
        relevance_scores: dict[str, Decimal] = {}
        if relevance_checker is not None and report.results:
            try:
                relevance = relevance_checker(tuple(report.results))
                if set(relevance) != set(bundle.cited_keys):
                    raise ValueError("relevance result key closure mismatch")
                for result in report.results:
                    score = relevance[result.cite_key]
                    if (
                        not isinstance(score, Decimal)
                        or not score.is_finite()
                        or not Decimal("0") <= score <= Decimal("1")
                    ):
                        raise ValueError("relevance score is invalid")
                    relevance_scores[result.cite_key] = score
            except (RuntimeError, ValueError) as exc:
                relevance_error = str(exc)

        payload = _build_report_payload(
            report,
            cited_keys=bundle.cited_keys,
            claim_scope=bundle.claim_scope,
            relevance_error=relevance_error,
            relevance_scores=relevance_scores,
        )
        parsed_report = parse_stage23_verification_report(
            canonical_authority_json_text(payload)
        )
        summary = parsed_report["summary"]
        if summary["fatal"] is True:
            blockers = sorted(
                set(
                    summary["hallucinated_keys"]
                    + summary["suspicious_keys"]
                    + summary["skipped_keys"]
                    + summary["unscored_keys"]
                    + summary["low_relevance_keys"]
                )
            )
            detail = relevance_error or ", ".join(blockers[:20]) or "unknown"
            raise Stage23VerificationError(
                f"Citation verification is incomplete or invalid: {detail}"
            )
        verified_bib = _derive_verified_bibliography(bounded_bib, parsed_report)
        outputs = {
            "paper_final_verified.md": bundle.paper.content,
            "references_verified.bib": verified_bib.encode("utf-8"),
            "verification_report.json": canonical_authority_json_text(
                parsed_report
            ).encode("utf-8"),
        }
        publish_stage23_verification(
            run_dir,
            stage_dir,
            bundle=bundle,
            outputs=outputs,
            precommit_check=lambda: verify_stage23_input_bundle_unchanged(
                run_dir, runtime_config, bundle
            ),
        )
        return Stage23VerificationOutcome(
            artifacts=(*_OUTPUT_NAMES, _MANIFEST_NAME),
            degraded=summary["degraded"] is True,
        )
    except Exception as exc:
        try:
            _reset_stage23_namespace(run_dir, stage_dir)
        except Exception as cleanup_exc:  # noqa: BLE001
            exc.add_note(f"Stage 23 cleanup also failed: {cleanup_exc}")
        raise


def publish_stage23_verification(
    run_dir: Path,
    stage_dir: Path,
    *,
    bundle: Stage23InputBundle,
    outputs: Mapping[str, bytes],
    precommit_check: Callable[[], None],
) -> dict[str, object]:
    """Publish Stage 23 outputs and write their strict commit manifest last."""

    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-23") as namespace:
        try:
            namespace.invalidate((_MANIFEST_NAME,))
            namespace.reset_flat_namespace()
            if set(outputs) != set(_OUTPUT_NAMES):
                raise Stage23VerificationError("Stage 23 output namespace mismatch")
            expected = _build_manifest(bundle, outputs)
            for name, content in sorted(outputs.items()):
                namespace.write_bytes_atomic(name, content)
            _verify_publication(namespace, expected, bundle)
            precommit_check()
            namespace.assert_canonical()
            namespace.write_text_atomic(
                _MANIFEST_NAME, canonical_authority_json_text(expected)
            )
            stored = namespace.read_bytes(_MANIFEST_NAME)
            parsed = parse_stage23_verification_manifest(stored.decode("utf-8"))
            if parsed != expected:
                raise Stage23VerificationError("stored Stage 23 manifest differs")
            _verify_publication(namespace, parsed, bundle)
            precommit_check()
            namespace.assert_canonical()
            if namespace.read_bytes(_MANIFEST_NAME) != stored:
                raise Stage23VerificationError(
                    "Stage 23 manifest changed after final fixpoint"
                )
            _verify_publication(namespace, parsed, bundle)
        except Exception as exc:
            try:
                namespace.reset_flat_namespace()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 23 namespace cleanup failed: {cleanup_exc}")
            raise
    return expected


def validate_stage23_verification_publication(
    run_dir: Path,
    bundle: Stage23InputBundle,
) -> dict[str, object]:
    """Independently replay Stage 23 from disk and captured Stage 22 authority."""

    stage_dir = run_dir / "stage-23"
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-23") as namespace:
        namespace.assert_canonical()
        try:
            manifest_bytes = namespace.read_bytes(_MANIFEST_NAME)
            manifest = parse_stage23_verification_manifest(
                manifest_bytes.decode("utf-8")
            )
        except UnicodeDecodeError as exc:
            raise Stage23VerificationError("Stage 23 manifest is not UTF-8") from exc
        _verify_publication(namespace, manifest, bundle)
        namespace.assert_canonical()
        if namespace.read_bytes(_MANIFEST_NAME) != manifest_bytes:
            raise Stage23VerificationError("Stage 23 manifest changed during replay")
        return manifest


def parse_stage23_verification_report(text: str) -> dict[str, object]:
    value = _parse_json(text, "Stage 23 verification report")
    if not isinstance(value, dict) or set(value) != {"summary", "results"}:
        raise Stage23VerificationError("Stage 23 report fields mismatch")
    summary = value["summary"]
    results = value["results"]
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_FIELDS:
        raise Stage23VerificationError("Stage 23 report summary fields mismatch")
    if not isinstance(results, list):
        raise Stage23VerificationError("Stage 23 report results are invalid")
    for field in ("total", "verified", "suspicious", "hallucinated", "skipped"):
        if type(summary[field]) is not int or summary[field] < 0:
            raise Stage23VerificationError(f"Stage 23 {field} count is invalid")
    for field in ("integrity_score", "relevance_threshold"):
        _require_decimal_ratio(summary[field], field)
    for field in (
        "verification_complete",
        "relevance_complete",
        "degraded",
        "fatal",
    ):
        if type(summary[field]) is not bool:
            raise Stage23VerificationError(f"Stage 23 {field} flag is invalid")
    if summary["claim_scope"] not in {
        "pipeline_validation",
        "exploratory",
        "research_release",
    }:
        raise Stage23VerificationError("Stage 23 claim scope is invalid")
    key_lists = (
        "cited_keys",
        "hallucinated_keys",
        "suspicious_keys",
        "skipped_keys",
        "unscored_keys",
        "low_relevance_keys",
    )
    for field in key_lists:
        _require_sorted_string_list(summary[field], field)
    if summary["relevance_error"] is not None and (
        not isinstance(summary["relevance_error"], str)
        or not summary["relevance_error"].strip()
    ):
        raise Stage23VerificationError("Stage 23 relevance error is invalid")
    parsed_results = [_parse_result(item) for item in results]
    result_keys = [item["cite_key"] for item in parsed_results]
    if result_keys != sorted(set(result_keys)) or result_keys != summary["cited_keys"]:
        raise Stage23VerificationError("Stage 23 result key closure mismatch")
    counts = {
        status: sum(item["status"] == status for item in parsed_results)
        for status in ("verified", "suspicious", "hallucinated", "skipped")
    }
    if summary["total"] != len(parsed_results) or any(
        summary[status] != count for status, count in counts.items()
    ):
        raise Stage23VerificationError("Stage 23 report counts mismatch")
    expected_integrity = _integrity_score(
        summary["verified"], summary["total"], summary["skipped"]
    )
    if summary["integrity_score"] != expected_integrity:
        raise Stage23VerificationError("Stage 23 integrity score mismatch")
    expected_status_keys = {
        "hallucinated_keys": "hallucinated",
        "suspicious_keys": "suspicious",
        "skipped_keys": "skipped",
    }
    for field, status in expected_status_keys.items():
        expected = sorted(
            item["cite_key"] for item in parsed_results if item["status"] == status
        )
        if summary[field] != expected:
            raise Stage23VerificationError(f"Stage 23 {field} mismatch")
    unscored = sorted(
        item["cite_key"] for item in parsed_results if "relevance_score" not in item
    )
    low = sorted(
        item["cite_key"]
        for item in parsed_results
        if item.get("relevance_score", Decimal("1")) < summary["relevance_threshold"]
    )
    if summary["unscored_keys"] != unscored or summary["low_relevance_keys"] != low:
        raise Stage23VerificationError("Stage 23 relevance key closure mismatch")
    if summary["verification_complete"] is not (
        counts["hallucinated"] == counts["suspicious"] == counts["skipped"] == 0
    ):
        raise Stage23VerificationError("Stage 23 verification completeness mismatch")
    if summary["relevance_complete"] is not (
        not unscored and summary["relevance_error"] is None
    ):
        raise Stage23VerificationError("Stage 23 relevance completeness mismatch")
    strict = summary["claim_scope"] != "pipeline_validation"
    fatal = bool(counts["hallucinated"]) or (
        strict
        and bool(
            counts["suspicious"]
            or counts["skipped"]
            or unscored
            or low
            or summary["relevance_error"]
        )
    )
    degraded = not fatal and bool(
        counts["suspicious"]
        or counts["skipped"]
        or unscored
        or low
        or summary["relevance_error"]
    )
    if summary["fatal"] is not fatal or summary["degraded"] is not degraded:
        raise Stage23VerificationError("Stage 23 outcome flags mismatch")
    value["results"] = parsed_results
    return value


def parse_stage23_verification_manifest(text: str) -> dict[str, object]:
    value = _parse_json(text, "Stage 23 verification manifest")
    fields = {
        "schema_version",
        "publication_policy_version",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "stage22_export_manifest_path",
        "stage22_export_manifest_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "source_bibliography_path",
        "source_bibliography_sha256",
        "source_latex_path",
        "source_latex_sha256",
        "claim_scope",
        "outputs",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise Stage23VerificationError("Stage 23 manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage23VerificationError("Stage 23 manifest schema is invalid")
    if (
        type(value["publication_policy_version"]) is not int
        or value["publication_policy_version"] != 1
    ):
        raise Stage23VerificationError("Stage 23 publication policy is invalid")
    for field in (
        "canonical_experiment_evidence_path",
        "stage22_export_manifest_path",
        "source_paper_path",
        "source_bibliography_path",
        "source_latex_path",
    ):
        _require_relative_path(value[field], field)
    for field in (
        "canonical_experiment_evidence_sha256",
        "stage22_export_manifest_sha256",
        "source_paper_sha256",
        "source_bibliography_sha256",
        "source_latex_sha256",
    ):
        _require_sha256(value[field], field)
    if value["claim_scope"] not in {
        "pipeline_validation",
        "exploratory",
        "research_release",
    }:
        raise Stage23VerificationError("Stage 23 manifest claim scope is invalid")
    outputs = value["outputs"]
    if not isinstance(outputs, list):
        raise Stage23VerificationError("Stage 23 manifest outputs are invalid")
    parsed: list[dict[str, str]] = []
    for item in outputs:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise Stage23VerificationError("Stage 23 output entry fields mismatch")
        path = _require_relative_path(item["path"], "Stage 23 output path")
        sha256 = _require_sha256(item["sha256"], "Stage 23 output sha256")
        parsed.append({"path": path, "sha256": sha256})
    if [item["path"] for item in parsed] != sorted(_OUTPUT_NAMES):
        raise Stage23VerificationError("Stage 23 output namespace mismatch")
    value["outputs"] = parsed
    return value


def _build_report_payload(
    report: VerificationReport,
    *,
    cited_keys: tuple[str, ...],
    claim_scope: str,
    relevance_error: str,
    relevance_scores: Mapping[str, Decimal],
) -> dict[str, object]:
    results = sorted(report.results, key=lambda item: item.cite_key)
    hallucinated = sorted(
        item.cite_key for item in results if item.status is VerifyStatus.HALLUCINATED
    )
    suspicious = sorted(
        item.cite_key for item in results if item.status is VerifyStatus.SUSPICIOUS
    )
    skipped = sorted(
        item.cite_key for item in results if item.status is VerifyStatus.SKIPPED
    )
    unscored = sorted(
        item.cite_key for item in results if item.cite_key not in relevance_scores
    )
    low = sorted(
        item.cite_key
        for item in results
        if relevance_scores.get(item.cite_key, Decimal("1")) < Decimal("0.5")
    )
    strict = claim_scope != "pipeline_validation"
    fatal = bool(hallucinated) or (
        strict and bool(suspicious or skipped or unscored or low or relevance_error)
    )
    degraded = not fatal and bool(
        suspicious or skipped or unscored or low or relevance_error
    )
    return {
        "summary": {
            "total": report.total,
            "verified": report.verified,
            "suspicious": report.suspicious,
            "hallucinated": report.hallucinated,
            "skipped": report.skipped,
            "integrity_score": _integrity_score(
                report.verified, report.total, report.skipped
            ),
            "claim_scope": claim_scope,
            "cited_keys": list(cited_keys),
            "verification_complete": not (hallucinated or suspicious or skipped),
            "relevance_complete": not unscored and not relevance_error,
            "relevance_threshold": Decimal("0.5"),
            "hallucinated_keys": hallucinated,
            "suspicious_keys": suspicious,
            "skipped_keys": skipped,
            "unscored_keys": unscored,
            "low_relevance_keys": low,
            "relevance_error": relevance_error or None,
            "degraded": degraded,
            "fatal": fatal,
        },
        "results": [
            _result_payload(item, relevance_scores.get(item.cite_key))
            for item in results
        ],
    }


def _result_payload(
    result: CitationResult,
    relevance_score: Decimal | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "cite_key": result.cite_key,
        "title": result.title,
        "status": result.status.value,
        "confidence": _rounded_decimal(result.confidence, places=3),
        "method": result.method,
        "details": result.details,
    }
    if relevance_score is not None:
        payload["relevance_score"] = _exact_decimal(relevance_score)
    if result.matched_paper is not None:
        payload["matched_paper"] = {
            "title": result.matched_paper.title,
            "authors": [author.name for author in result.matched_paper.authors],
            "year": result.matched_paper.year,
            "source": result.matched_paper.source,
        }
    return payload


def _parse_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Stage23VerificationError("Stage 23 result is not an object")
    allowed = _RESULT_REQUIRED_FIELDS | {"relevance_score", "matched_paper"}
    if not _RESULT_REQUIRED_FIELDS <= set(value) or not set(value) <= allowed:
        raise Stage23VerificationError("Stage 23 result fields mismatch")
    for field in ("cite_key", "title", "method"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise Stage23VerificationError(f"Stage 23 result {field} is invalid")
    if not isinstance(value["details"], str):
        raise Stage23VerificationError("Stage 23 result details are invalid")
    if value["status"] not in {
        "verified",
        "suspicious",
        "hallucinated",
        "skipped",
    }:
        raise Stage23VerificationError("Stage 23 result status is invalid")
    _require_decimal_ratio(value["confidence"], "confidence")
    if "relevance_score" in value:
        _require_decimal_ratio(value["relevance_score"], "relevance score")
    if "matched_paper" in value:
        matched = value["matched_paper"]
        if not isinstance(matched, dict) or set(matched) != {
            "title",
            "authors",
            "year",
            "source",
        }:
            raise Stage23VerificationError("Stage 23 matched paper fields mismatch")
        if not isinstance(matched["authors"], list) or any(
            not isinstance(author, str) for author in matched["authors"]
        ):
            raise Stage23VerificationError("Stage 23 matched authors are invalid")
        if type(matched["year"]) is not int:
            raise Stage23VerificationError("Stage 23 matched year is invalid")
        for field in ("title", "source"):
            if not isinstance(matched[field], str):
                raise Stage23VerificationError(
                    f"Stage 23 matched paper {field} is invalid"
                )
    return value


def _validate_external_report(
    report: VerificationReport,
    cited_keys: tuple[str, ...],
) -> None:
    if not isinstance(report, VerificationReport):
        raise Stage23VerificationError("citation verifier returned an invalid report")
    keys = [result.cite_key for result in report.results]
    if len(keys) != len(set(keys)) or set(keys) != set(cited_keys):
        raise Stage23VerificationError("Citation verification result closure mismatch")
    counts = {
        status: sum(result.status is status for result in report.results)
        for status in VerifyStatus
    }
    if (
        report.total != len(cited_keys)
        or len(report.results) != len(cited_keys)
        or report.verified != counts[VerifyStatus.VERIFIED]
        or report.suspicious != counts[VerifyStatus.SUSPICIOUS]
        or report.hallucinated != counts[VerifyStatus.HALLUCINATED]
        or report.skipped != counts[VerifyStatus.SKIPPED]
    ):
        raise Stage23VerificationError("Citation verification result closure mismatch")
    for result in report.results:
        if (
            isinstance(result.confidence, bool)
            or not isinstance(result.confidence, (int, float))
            or not math.isfinite(float(result.confidence))
            or not 0 <= float(result.confidence) <= 1
        ):
            raise Stage23VerificationError("citation confidence is invalid")


def _bounded_bibliography(bib_text: str, cited_keys: set[str]) -> str:
    entries = parse_bibtex_entries(bib_text)
    available = {str(entry.get("key") or "").strip() for entry in entries}
    if cited_keys - available:
        raise Stage23VerificationError("Bounded bibliography is missing cited keys")
    bounded = _remove_bibtex_entries(bib_text, available - cited_keys)
    bounded_entries = parse_bibtex_entries(bounded)
    bounded_keys = [str(entry.get("key") or "").strip() for entry in bounded_entries]
    if len(bounded_keys) != len(cited_keys) or set(bounded_keys) != cited_keys:
        raise Stage23VerificationError(
            "Bounded bibliography does not exactly match final cited keys"
        )
    return bounded


def _derive_verified_bibliography(
    bounded_bib: str,
    report: Mapping[str, object],
) -> str:
    remove = {
        item["cite_key"]
        for item in report["results"]  # type: ignore[index]
        if item["status"] != "verified"
    }
    verified = _remove_bibtex_entries(bounded_bib, remove)
    return verified if verified.strip() else "% No verified citations\n"


def _remove_bibtex_entries(bib_text: str, keys_to_remove: set[str]) -> str:
    kept: list[str] = []
    for match in re.finditer(r"@\w+\{([^,]+),", bib_text):
        if match.group(1).strip() in keys_to_remove:
            continue
        depth = 0
        for index in range(match.start(), len(bib_text)):
            if bib_text[index] == "{":
                depth += 1
            elif bib_text[index] == "}":
                depth -= 1
                if depth == 0:
                    kept.append(bib_text[match.start() : index + 1])
                    break
    return "\n\n".join(kept) + "\n" if kept else ""


def _build_manifest(
    bundle: Stage23InputBundle,
    outputs: Mapping[str, bytes],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "publication_policy_version": 1,
        "canonical_experiment_evidence_path": bundle.stage22_inputs.evidence.manifest_path,
        "canonical_experiment_evidence_sha256": bundle.stage22_inputs.evidence.manifest_sha256,
        "stage22_export_manifest_path": bundle.publication.manifest.path,
        "stage22_export_manifest_sha256": bundle.publication.manifest.sha256,
        "source_paper_path": bundle.paper.path,
        "source_paper_sha256": bundle.paper.sha256,
        "source_bibliography_path": bundle.bibliography.path,
        "source_bibliography_sha256": bundle.bibliography.sha256,
        "source_latex_path": bundle.latex.path,
        "source_latex_sha256": bundle.latex.sha256,
        "claim_scope": bundle.claim_scope,
        "outputs": [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(outputs.items())
        ],
    }
    return parse_stage23_verification_manifest(canonical_authority_json_text(payload))


def _verify_publication(
    namespace: BoundOutputNamespace,
    manifest: Mapping[str, object],
    bundle: Stage23InputBundle,
) -> None:
    expected_direct = {*_OUTPUT_NAMES, _MANIFEST_NAME}
    direct = set(namespace.direct_entries())
    if direct not in (set(_OUTPUT_NAMES), expected_direct):
        raise Stage23VerificationError("Stage 23 direct output namespace mismatch")
    contents: dict[str, bytes] = {}
    for entry in manifest["outputs"]:  # type: ignore[index]
        content = namespace.read_bytes(entry["path"])
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise Stage23VerificationError(
                f"Stage 23 output hash mismatch: {entry['path']}"
            )
        contents[entry["path"]] = content
    report = parse_stage23_verification_report(
        contents["verification_report.json"].decode("utf-8")
    )
    if contents["verification_report.json"] != canonical_authority_json_text(
        report
    ).encode("utf-8"):
        raise Stage23VerificationError("Stage 23 report is not canonical JSON")
    if report["summary"]["fatal"] is True:
        raise Stage23VerificationError("fatal Stage 23 report cannot be published")
    if report["summary"]["claim_scope"] != bundle.claim_scope:
        raise Stage23VerificationError("Stage 23 report claim scope mismatch")
    if report["summary"]["cited_keys"] != list(bundle.cited_keys):
        raise Stage23VerificationError("Stage 23 report cited keys mismatch")
    if contents["paper_final_verified.md"] != bundle.paper.content:
        raise Stage23VerificationError("Stage 23 verified paper differs from Stage 22")
    bounded = _bounded_bibliography(
        bundle.bibliography.text(), set(bundle.cited_keys)
    )
    expected_bib = _derive_verified_bibliography(bounded, report).encode("utf-8")
    if contents["references_verified.bib"] != expected_bib:
        raise Stage23VerificationError("Stage 23 verified bibliography mismatch")
    if _build_manifest(bundle, contents) != manifest:
        raise Stage23VerificationError("Stage 23 manifest reconstruction mismatch")


def _reset_stage23_namespace(run_dir: Path, stage_dir: Path) -> None:
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-23") as namespace:
        namespace.invalidate((_MANIFEST_NAME,))
        namespace.reset_flat_namespace()


def _parse_json(text: str, label: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise Stage23VerificationError(f"invalid {label} JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_decimal_ratio(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise Stage23VerificationError(f"Stage 23 {field} is invalid")
    decimal = Decimal(value)
    if not decimal.is_finite() or not Decimal("0") <= decimal <= Decimal("1"):
        raise Stage23VerificationError(f"Stage 23 {field} is out of range")
    return decimal


def _integrity_score(verified: int, total: int, skipped: int) -> Decimal:
    verifiable = total - skipped
    if verifiable <= 0:
        return Decimal("1")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return (Decimal(verified) / Decimal(verifiable)).quantize(Decimal("0.001"))


def _rounded_decimal(value: int | float, *, places: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-places))


def _exact_decimal(value: int | float | Decimal) -> Decimal:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite():
        raise Stage23VerificationError("Stage 23 numeric value is non-finite")
    return decimal


def _require_sorted_string_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise Stage23VerificationError(f"Stage 23 {field} is invalid")
    return value


def _require_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Stage23VerificationError(f"{field} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise Stage23VerificationError(f"{field} is unsafe")
    return value


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage23VerificationError(f"{field} is not a SHA-256")
    return value
