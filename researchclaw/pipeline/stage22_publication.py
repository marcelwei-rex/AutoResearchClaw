"""Deterministic commit-point publication for Stage 22 exports."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage22_input_bundle import Stage22InputBundle
from researchclaw.pipeline.stage22_semantics import (
    Stage22SemanticError,
    validate_stage22_output_semantics,
)


_CANONICAL_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")


class Stage22PublicationError(ValueError):
    """Raised when Stage 22 outputs do not form one replayable publication."""


def validate_stage22_export_publication(
    run_dir: Path,
    bundle: Stage22InputBundle,
) -> dict[str, object]:
    """Independently replay the committed Stage 22 publication from disk."""

    stage_dir = run_dir / "stage-22"
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-22") as namespace:
        namespace.assert_canonical()
        try:
            manifest_text = namespace.read_bytes(
                "stage22_export_manifest.json"
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stage22PublicationError(
                "Stage 22 export manifest is not UTF-8"
            ) from exc
        manifest = parse_stage22_export_manifest(manifest_text)
        _verify_published_files(
            namespace,
            manifest,
            bundle=bundle,
            generated=manifest["generated"],  # type: ignore[arg-type]
        )
        if set(namespace.direct_entries()) != _expected_direct_entries(manifest):
            raise Stage22PublicationError("Stage 22 direct output namespace mismatch")
        namespace.assert_canonical()
        if namespace.read_bytes("stage22_export_manifest.json") != manifest_text.encode(
            "utf-8"
        ):
            raise Stage22PublicationError("Stage 22 export manifest changed during replay")
        return manifest


def publish_stage22_outputs(
    run_dir: Path,
    stage_dir: Path,
    *,
    bundle: Stage22InputBundle,
    direct_files: Mapping[str, bytes],
    code_files: Mapping[str, bytes],
    generated: str,
    precommit_check: Callable[[], None],
) -> dict[str, object]:
    """Publish outputs first and the strict export manifest last."""

    if "stage22_export_manifest.json" in direct_files:
        raise Stage22PublicationError("export manifest must be published last")
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-22") as namespace:
        try:
            namespace.invalidate(("stage22_export_manifest.json",))
            expected = _build_manifest(
                bundle=bundle,
                direct_files=direct_files,
                code_files=code_files,
                generated=generated,
            )
            namespace.reset_flat_namespace()
            for name, content in sorted(direct_files.items()):
                _require_direct_name(name)
                namespace.write_bytes_atomic(name, content)
            namespace.publish_flat_directory("code", code_files)
            namespace.assert_canonical()
            _verify_published_files(
                namespace, expected, bundle=bundle, generated=generated
            )
            precommit_check()
            namespace.assert_canonical()
            namespace.write_text_atomic(
                "stage22_export_manifest.json",
                canonical_authority_json_text(expected),
            )
            stored = namespace.read_bytes("stage22_export_manifest.json")
            parsed = parse_stage22_export_manifest(stored.decode("utf-8"))
            if parsed != expected:
                raise Stage22PublicationError("stored Stage 22 export manifest differs")
            _verify_published_files(
                namespace, parsed, bundle=bundle, generated=generated
            )
            expected_direct = _expected_direct_entries(parsed)
            if set(namespace.direct_entries()) != expected_direct:
                raise Stage22PublicationError(
                    "Stage 22 direct output namespace mismatch"
                )
            precommit_check()
            namespace.assert_canonical()
            if namespace.read_bytes("stage22_export_manifest.json") != stored:
                raise Stage22PublicationError(
                    "Stage 22 export manifest changed after final fixpoint"
                )
            _verify_published_files(
                namespace, parsed, bundle=bundle, generated=generated
            )
            if set(namespace.direct_entries()) != expected_direct:
                raise Stage22PublicationError(
                    "Stage 22 output namespace changed after final fixpoint"
                )
        except Exception as exc:
            try:
                namespace.invalidate(("stage22_export_manifest.json",))
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 22 manifest cleanup failed: {cleanup_exc}")
            try:
                namespace.reset_flat_namespace()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 22 namespace cleanup failed: {cleanup_exc}")
            raise
    return expected


def _expected_direct_entries(manifest: Mapping[str, object]) -> set[str]:
    return {
        "code",
        "stage22_export_manifest.json",
        *(
            entry["path"]
            for entry in manifest["outputs"]  # type: ignore[index]
            if "/" not in entry["path"]
        ),
    }


def parse_stage22_export_manifest(text: str) -> dict[str, object]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise Stage22PublicationError("invalid Stage 22 export manifest JSON") from exc
    fields = {
        "schema_version",
        "publication_policy_version",
        "generated",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "selected_result_manifest_path",
        "selected_result_manifest_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage19_publication_mode",
        "stage19_publication_binding_path",
        "stage19_publication_binding_sha256",
        "quality_gate_manifest_path",
        "quality_gate_manifest_sha256",
        "quality_gate_outcome",
        "bibliography_source_path",
        "bibliography_source_sha256",
        "template_name",
        "template_files",
        "project_files",
        "outputs",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise Stage22PublicationError("Stage 22 export manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage22PublicationError("Stage 22 export schema is invalid")
    if (
        type(value["publication_policy_version"]) is not int
        or value["publication_policy_version"] != 1
    ):
        raise Stage22PublicationError("Stage 22 publication policy is invalid")
    for field in (
        "generated",
        "stage19_publication_mode",
        "quality_gate_outcome",
        "template_name",
    ):
        if not isinstance(value[field], str) or not value[field].strip():
            raise Stage22PublicationError(f"Stage 22 {field} is invalid")
    if value["stage19_publication_mode"] not in {"legacy", "sectional"}:
        raise Stage22PublicationError("Stage 22 publication mode is invalid")
    if value["quality_gate_outcome"] not in {"passed", "degraded"}:
        raise Stage22PublicationError("Stage 22 quality outcome is invalid")
    for field in (
        "canonical_experiment_evidence_path",
        "selected_result_manifest_path",
        "source_paper_path",
        "stage19_publication_binding_path",
        "quality_gate_manifest_path",
        "bibliography_source_path",
    ):
        _require_relative_path(value[field], field)
    for field in (
        "canonical_experiment_evidence_sha256",
        "selected_result_manifest_sha256",
        "source_paper_sha256",
        "stage19_publication_binding_sha256",
        "quality_gate_manifest_sha256",
        "bibliography_source_sha256",
    ):
        _require_sha256(value[field], field)
    _parse_file_entries(value["project_files"], project=True)
    _parse_file_entries(value["outputs"], project=False)
    _parse_template_entries(value["template_files"])
    output_hashes = {entry["path"]: entry["sha256"] for entry in value["outputs"]}
    for project in value["project_files"]:
        if output_hashes.get(project["export_path"]) != project["export_sha256"]:
            raise Stage22PublicationError(
                "project export is missing from Stage 22 outputs"
            )
    return value


def _build_manifest(
    *,
    bundle: Stage22InputBundle,
    direct_files: Mapping[str, bytes],
    code_files: Mapping[str, bytes],
    generated: str,
) -> dict[str, object]:
    try:
        deterministic = validate_stage22_output_semantics(
            bundle,
            direct_files=direct_files,
            code_files=code_files,
            generated=generated,
        )
    except Stage22SemanticError as exc:
        raise Stage22PublicationError(str(exc)) from exc
    for artifact in bundle.evidence.project_artifacts:
        if code_files.get(artifact.logical_name) != artifact.content:
            raise Stage22PublicationError(
                f"exported project differs from canonical snapshot: {artifact.logical_name}"
            )
    outputs = [
        {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(direct_files.items())
    ] + [
        {"path": f"code/{name}", "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(code_files.items())
    ]
    project_files = [
        {
            "logical_name": artifact.logical_name,
            "source_path": artifact.source_path,
            "source_sha256": artifact.sha256,
            "export_path": f"code/{artifact.logical_name}",
            "export_sha256": artifact.sha256,
        }
        for artifact in bundle.evidence.project_artifacts
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "publication_policy_version": 1,
        "generated": generated,
        "canonical_experiment_evidence_path": bundle.evidence.manifest_path,
        "canonical_experiment_evidence_sha256": bundle.evidence.manifest_sha256,
        "selected_result_manifest_path": bundle.evidence.selected_result_manifest_path,
        "selected_result_manifest_sha256": bundle.evidence.selected_result_manifest_sha256,
        "source_paper_path": bundle.stage20_inputs.revised_paper.path,
        "source_paper_sha256": bundle.stage20_inputs.revised_paper.sha256,
        "stage19_publication_mode": bundle.stage20_inputs.publication_mode,
        "stage19_publication_binding_path": bundle.stage20_inputs.publication_binding.path,
        "stage19_publication_binding_sha256": bundle.stage20_inputs.publication_binding.sha256,
        "quality_gate_manifest_path": bundle.stage21_inputs.quality_gate_manifest.path,
        "quality_gate_manifest_sha256": bundle.stage21_inputs.quality_gate_manifest.sha256,
        "quality_gate_outcome": bundle.stage21_inputs.quality_gate_outcome,
        "bibliography_source_path": bundle.stage19_inputs.bibliography.path,
        "bibliography_source_sha256": bundle.stage19_inputs.bibliography.sha256,
        "template_name": deterministic.template_name,
        "template_files": [
            {
                "logical_name": name,
                "source_sha256": sha256,
                "export_path": name,
                "export_sha256": sha256,
            }
            for name, sha256 in deterministic.template_files
        ],
        "project_files": project_files,
        "outputs": sorted(outputs, key=lambda item: item["path"]),
    }
    return parse_stage22_export_manifest(canonical_authority_json_text(payload))


def _verify_published_files(
    namespace: BoundOutputNamespace,
    manifest: Mapping[str, object],
    *,
    bundle: Stage22InputBundle,
    generated: str,
) -> None:
    code_files = namespace.read_flat_directory("code")
    expected_code = {
        entry["path"].removeprefix("code/"): entry["sha256"]
        for entry in manifest["outputs"]  # type: ignore[index]
        if entry["path"].startswith("code/")
    }
    if set(code_files) != set(expected_code):
        raise Stage22PublicationError("Stage 22 code output namespace mismatch")
    for name, expected_sha in expected_code.items():
        if hashlib.sha256(code_files[name]).hexdigest() != expected_sha:
            raise Stage22PublicationError(f"Stage 22 output hash mismatch: code/{name}")
    direct_files: dict[str, bytes] = {}
    for entry in manifest["outputs"]:  # type: ignore[index]
        path = entry["path"]
        if path.startswith("code/"):
            continue
        else:
            content = namespace.read_bytes(path)
            direct_files[path] = content
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise Stage22PublicationError(f"Stage 22 output hash mismatch: {path}")
    rebuilt = _build_manifest(
        bundle=bundle,
        direct_files=direct_files,
        code_files=code_files,
        generated=generated,
    )
    if rebuilt != manifest:
        raise Stage22PublicationError(
            "Stage 22 manifest differs from semantic reconstruction"
        )


def _parse_template_entries(value: object) -> None:
    if not isinstance(value, list):
        raise Stage22PublicationError("Stage 22 template file list is invalid")
    names: list[str] = []
    expected = {
        "logical_name",
        "source_sha256",
        "export_path",
        "export_sha256",
    }
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != expected:
            raise Stage22PublicationError("Stage 22 template entry fields mismatch")
        name = _require_direct_name(entry["logical_name"])
        path = _require_relative_path(entry["export_path"], "template export path")
        if path != name:
            raise Stage22PublicationError("template export path mismatch")
        _require_sha256(entry["source_sha256"], "template source sha256")
        _require_sha256(entry["export_sha256"], "template export sha256")
        if entry["source_sha256"] != entry["export_sha256"]:
            raise Stage22PublicationError("template export hash differs from source")
        names.append(name)
    if names != sorted(set(names)):
        raise Stage22PublicationError("Stage 22 template files are not unique and sorted")

def _parse_file_entries(value: object, *, project: bool) -> None:
    if not isinstance(value, list) or not value:
        raise Stage22PublicationError("Stage 22 file list must be nonempty")
    expected = (
        {"logical_name", "source_path", "source_sha256", "export_path", "export_sha256"}
        if project
        else {"path", "sha256"}
    )
    paths: list[str] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != expected:
            raise Stage22PublicationError("Stage 22 file entry fields mismatch")
        path = entry["export_path"] if project else entry["path"]
        _require_relative_path(path, "Stage 22 file path")
        paths.append(path)
        if project:
            _require_direct_name(entry["logical_name"])
            _require_relative_path(entry["source_path"], "project source path")
            _require_sha256(entry["source_sha256"], "project source sha256")
            _require_sha256(entry["export_sha256"], "project export sha256")
            if path != f"code/{entry['logical_name']}":
                raise Stage22PublicationError("project export path mismatch")
            if entry["source_sha256"] != entry["export_sha256"]:
                raise Stage22PublicationError("project export hash differs from source")
        else:
            _require_sha256(entry["sha256"], "output sha256")
    if paths != sorted(set(paths)):
        raise Stage22PublicationError("Stage 22 file paths are not unique and sorted")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stage22PublicationError(f"duplicate Stage 22 JSON key: {key}")
        result[key] = value
    return result


def _require_relative_path(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or _CANONICAL_PATH_RE.fullmatch(value) is None
    ):
        raise Stage22PublicationError(f"{field} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise Stage22PublicationError(f"{field} is unsafe")
    return value


def _require_direct_name(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise Stage22PublicationError("Stage 22 direct name is invalid")
    if "/" in value or "\\" in value:
        raise Stage22PublicationError("Stage 22 direct name is unsafe")
    return value


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage22PublicationError(f"{field} is not a SHA-256")
    return value
