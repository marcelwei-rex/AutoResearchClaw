"""Canonical Stage 22 export implementation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from researchclaw.config import RCConfig
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage22_input_bundle import (
    Stage22InputBundle,
    verify_stage22_input_bundle_unchanged,
)
from researchclaw.pipeline.stage22_publication import (
    _reset_stage22_publication_namespace,
    publish_stage22_outputs,
)
from researchclaw.pipeline.stage22_semantics import (
    Stage22SemanticError,
    build_stage22_deterministic_outputs,
)
from researchclaw.templates.compiler import compile_latex


class Stage22ExportError(ValueError):
    """Raised when canonical Stage 22 export cannot be published."""


def export_canonical_stage22(
    run_dir: Path,
    stage_dir: Path,
    *,
    bundle: Stage22InputBundle,
    runtime_config: RCConfig,
    generated: str,
) -> tuple[str, ...]:
    """Own the complete lifecycle of one canonical Stage 22 export attempt."""

    _reset_stage22_namespace(run_dir, stage_dir)
    try:
        return _export_canonical_stage22_after_invalidation(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=runtime_config,
            generated=generated,
        )
    except Exception as exc:
        try:
            _reset_stage22_namespace(run_dir, stage_dir)
        except Exception as cleanup_exc:  # noqa: BLE001
            exc.add_note(f"Stage 22 producer cleanup failed: {cleanup_exc}")
        raise


def _export_canonical_stage22_after_invalidation(
    run_dir: Path,
    stage_dir: Path,
    *,
    bundle: Stage22InputBundle,
    runtime_config: RCConfig,
    generated: str,
) -> tuple[str, ...]:
    """Render immutable Stage 19 paper and code after authority invalidation."""

    try:
        deterministic = build_stage22_deterministic_outputs(
            bundle, generated=generated
        )
    except Stage22SemanticError as exc:
        raise Stage22ExportError(str(exc)) from exc
    direct_files = dict(deterministic.direct_files)
    bibliography = direct_files["references.bib"]
    style_files = {
        name: direct_files[name] for name, _sha256 in deterministic.template_files
    }
    compile_outputs, tex_bytes, compile_status = _compile_export(
        direct_files["paper.tex"],
        bibliography,
        style_files,
        generated=generated,
    )
    if tex_bytes != direct_files["paper.tex"]:
        raise Stage22ExportError("compiler changed deterministic paper.tex")
    direct_files["compile_status.json"] = canonical_authority_json_text(
        compile_status
    ).encode("utf-8")
    direct_files.update(compile_outputs)
    publish_stage22_outputs(
        run_dir,
        stage_dir,
        bundle=bundle,
        direct_files=direct_files,
        code_files=deterministic.code_files,
        generated=generated,
        precommit_check=lambda: verify_stage22_input_bundle_unchanged(
            run_dir, runtime_config, bundle
        ),
    )
    artifacts = [
        *sorted(direct_files),
        "code/",
        "stage22_export_manifest.json",
    ]
    return tuple(artifacts)


def _reset_stage22_namespace(run_dir: Path, stage_dir: Path) -> None:
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-22") as namespace:
        _reset_stage22_publication_namespace(namespace)


def _compile_export(
    tex_bytes: bytes,
    bibliography: bytes,
    style_files: dict[str, bytes],
    *,
    generated: str,
) -> tuple[dict[str, bytes], bytes, dict[str, object]]:
    outputs: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="researchclaw-stage22-") as temporary:
        root = Path(temporary)
        tex_path = root / "paper.tex"
        tex_path.write_bytes(tex_bytes)
        (root / "references.bib").write_bytes(bibliography)
        for name, content in style_files.items():
            (root / name).write_bytes(content)
        result = compile_latex(tex_path, max_attempts=2)
        errors = [str(error) for error in (getattr(result, "errors", ()) or ())][:5]
        tooling_available = not any("pdflatex not installed" in error.lower() for error in errors)
        status = {
            "schema_version": 1,
            "success": bool(result.success),
            "attempts": int(getattr(result, "attempts", 0) or 0),
            "errors": errors,
            "status": "success" if result.success else (
                "toolchain_missing" if not tooling_available else "latex_error"
            ),
            "tooling_available": tooling_available,
            "generated": generated,
        }
        if result.success:
            pdf_path = root / "paper.pdf"
            if not pdf_path.is_file() or pdf_path.is_symlink():
                raise Stage22ExportError("compiler reported success without paper.pdf")
            outputs["paper.pdf"] = pdf_path.read_bytes()
        return outputs, tex_path.read_bytes(), status
