"""Deterministic Stage 22 output construction and semantic replay."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from researchclaw.config import RCConfig
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.paper_verifier import verify_paper
from researchclaw.pipeline.stage20_publication import thaw_canonical_summary
from researchclaw.pipeline.stage22_input_bundle import Stage22InputBundle
from researchclaw.pipeline.verified_registry import VerifiedRegistry
from researchclaw.templates import ML_CHECKLIST_TEMPLATES, get_template, markdown_to_latex
from researchclaw.templates.compiler import remove_missing_figures


class Stage22SemanticError(ValueError):
    """Raised when Stage 22 outputs do not replay from canonical inputs."""


@dataclass(frozen=True)
class Stage22DeterministicOutputs:
    direct_files: Mapping[str, bytes]
    code_files: Mapping[str, bytes]
    template_name: str
    template_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Stage22SemanticInputs:
    """Authority bytes required by both generic and structured Stage 22."""

    evidence: Any
    canonical_config: RCConfig
    paper_path: str
    paper_sha256: str
    paper_content: bytes
    bibliography_content: bytes
    quality_outcome: str


def build_stage22_deterministic_outputs(
    bundle: Stage22InputBundle,
    *,
    generated: str,
) -> Stage22DeterministicOutputs:
    """Rebuild every non-compiler Stage 22 output from captured authority."""

    return build_stage22_outputs_from_inputs(
        Stage22SemanticInputs(
            evidence=bundle.evidence,
            canonical_config=bundle.canonical_config,
            paper_path=bundle.stage20_inputs.revised_paper.path,
            paper_sha256=bundle.stage20_inputs.revised_paper.sha256,
            paper_content=bundle.stage20_inputs.revised_paper.content,
            bibliography_content=bundle.stage19_inputs.bibliography.content,
            quality_outcome=bundle.stage21_inputs.quality_gate_outcome,
        ),
        generated=generated,
    )


def build_stage22_outputs_from_inputs(
    inputs: Stage22SemanticInputs,
    *,
    generated: str,
) -> Stage22DeterministicOutputs:
    """Pure deterministic projection from already replayed authority bytes."""

    try:
        paper = inputs.paper_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage22SemanticError("canonical Stage 19 paper is not UTF-8") from exc
    if not paper.strip():
        raise Stage22SemanticError("canonical Stage 19 paper is empty")
    if inputs.quality_outcome == "degraded":
        paper = _insert_degradation_notice(paper)
    paper = _remove_unavailable_markdown_figures(paper)

    bibliography = inputs.bibliography_content
    try:
        bibliography_text = bibliography.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage22SemanticError("canonical bibliography is not UTF-8") from exc
    valid_keys = set(re.findall(r"@\w+\{([^,]+),", bibliography_text))
    latex_markdown = _convert_citations_to_latex(paper, valid_keys)

    template = get_template(inputs.canonical_config.export.target_conference)
    tex_source = latex_markdown
    if template.name in ML_CHECKLIST_TEMPLATES:
        from researchclaw.pipeline._helpers import _generate_neurips_checklist

        if "NeurIPS Paper Checklist" not in tex_source:
            tex_source = tex_source.rstrip() + "\n\n" + _generate_neurips_checklist(
                has_experiments=True,
                has_code=True,
            )
    tex_text = markdown_to_latex(
        tex_source,
        template,
        title=_extract_title(tex_source),
        authors=inputs.canonical_config.export.authors,
        bib_file=inputs.canonical_config.export.bib_file,
    )
    with tempfile.TemporaryDirectory(prefix="researchclaw-stage22-render-") as root:
        tex_text, _removed = remove_missing_figures(tex_text, Path(root))
    tex_bytes = tex_text.encode("utf-8")

    registry = VerifiedRegistry.from_experiment(
        thaw_canonical_summary(inputs.evidence),
        metric_direction=inputs.canonical_config.experiment.metric_direction,
    )
    verification = verify_paper(tex_text, registry)
    if verification.severity == "REJECT":
        raise Stage22SemanticError(
            "canonical Stage 22 LaTeX verification rejected the publication"
        )

    markdown_bytes = paper.encode("utf-8")
    style_files = {path.name: path.read_bytes() for path in template.get_style_files()}
    reserved = {
        "paper_final.md",
        "paper_final_latex.md",
        "references.bib",
        "paper.tex",
        "compile_status.json",
        "paper_verification.json",
        "sanitization_report.json",
        "canonical_source.json",
        "paper.pdf",
    }
    if reserved.intersection(style_files):
        raise Stage22SemanticError("template style file collides with Stage 22 output")
    direct_files: dict[str, bytes] = {
        "paper_final.md": markdown_bytes,
        "paper_final_latex.md": latex_markdown.encode("utf-8"),
        "references.bib": bibliography,
        "paper.tex": tex_bytes,
        "paper_verification.json": _verification_json(verification),
        "sanitization_report.json": canonical_authority_json_text(
            {
                "schema_version": 1,
                "policy": "no_numeric_rewrite",
                "source_paper_sha256": inputs.paper_sha256,
                "numbers_replaced": 0,
                "generated": generated,
            }
        ).encode("utf-8"),
        **style_files,
    }
    direct_files["canonical_source.json"] = canonical_authority_json_text(
        {
            "schema_version": 2,
            "source_paper_path": inputs.paper_path,
            "source_paper_sha256": inputs.paper_sha256,
            "markdown_path": "stage-22/paper_final.md",
            "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
            "latex_path": "stage-22/paper.tex",
            "latex_sha256": hashlib.sha256(tex_bytes).hexdigest(),
            "generated": generated,
        }
    ).encode("utf-8")
    template_files = tuple(
        (name, hashlib.sha256(content).hexdigest())
        for name, content in sorted(style_files.items())
    )
    return Stage22DeterministicOutputs(
        direct_files=direct_files,
        code_files=_build_code_package_from_inputs(inputs.evidence, paper),
        template_name=template.name,
        template_files=template_files,
    )


def validate_stage22_output_semantics(
    bundle: Stage22InputBundle,
    *,
    direct_files: Mapping[str, bytes],
    code_files: Mapping[str, bytes],
    generated: str,
) -> Stage22DeterministicOutputs:
    """Require outputs to equal an independent reconstruction from authority."""

    expected = build_stage22_deterministic_outputs(bundle, generated=generated)
    if code_files != expected.code_files:
        raise Stage22SemanticError("Stage 22 code package differs from reconstruction")
    status_bytes = direct_files.get("compile_status.json")
    if status_bytes is None:
        raise Stage22SemanticError("Stage 22 compile status is missing")
    status = _parse_compile_status(status_bytes, generated=generated)
    expected_names = set(expected.direct_files) | {"compile_status.json"}
    pdf = direct_files.get("paper.pdf")
    if status["success"]:
        expected_names.add("paper.pdf")
        if (
            pdf is None
            or len(pdf) <= 10
            or not pdf.startswith(b"%PDF-")
            or b"%%EOF" not in pdf[-1024:]
        ):
            raise Stage22SemanticError("successful Stage 22 compile has invalid PDF")
    elif pdf is not None:
        raise Stage22SemanticError("failed Stage 22 compile must not publish a PDF")
    if set(direct_files) != expected_names:
        raise Stage22SemanticError("Stage 22 mandatory output set mismatch")
    for name, content in expected.direct_files.items():
        if direct_files.get(name) != content:
            raise Stage22SemanticError(f"Stage 22 semantic output mismatch: {name}")
    return expected


def _parse_compile_status(content: bytes, *, generated: str) -> dict[str, object]:
    try:
        text = content.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage22SemanticError("invalid Stage 22 compile status") from exc
    fields = {
        "schema_version",
        "success",
        "attempts",
        "errors",
        "status",
        "tooling_available",
        "generated",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise Stage22SemanticError("Stage 22 compile status fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage22SemanticError("Stage 22 compile status schema is invalid")
    if type(value["success"]) is not bool or type(value["tooling_available"]) is not bool:
        raise Stage22SemanticError("Stage 22 compile booleans are invalid")
    if type(value["attempts"]) is not int or value["attempts"] < 0:
        raise Stage22SemanticError("Stage 22 compile attempts are invalid")
    if not isinstance(value["errors"], list) or any(
        not isinstance(item, str) or not item.strip() for item in value["errors"]
    ):
        raise Stage22SemanticError("Stage 22 compile errors are invalid")
    if value["generated"] != generated:
        raise Stage22SemanticError("Stage 22 compile generation mismatch")
    if value["success"]:
        if (
            value["status"] != "success"
            or not value["tooling_available"]
            or value["attempts"] < 1
            or value["errors"]
        ):
            raise Stage22SemanticError("successful Stage 22 compile state is inconsistent")
    else:
        expected_status = (
            "latex_error" if value["tooling_available"] else "toolchain_missing"
        )
        if value["status"] != expected_status or not value["errors"]:
            raise Stage22SemanticError("failed Stage 22 compile state is inconsistent")
    if canonical_authority_json_text(value).encode("utf-8") != content:
        raise Stage22SemanticError("Stage 22 compile status is not canonical JSON")
    return value


def _build_code_package(bundle: Stage22InputBundle, paper: str) -> dict[str, bytes]:
    return _build_code_package_from_inputs(bundle.evidence, paper)


def _build_code_package_from_inputs(evidence: Any, paper: str) -> dict[str, bytes]:
    project = {
        artifact.logical_name: artifact.content
        for artifact in evidence.project_artifacts
    }
    if not project or "main.py" not in project:
        raise Stage22SemanticError("canonical selected project is incomplete")
    packages = _detect_requirements(project)
    project_list = "\n".join(f"- `{name}`" for name in sorted(project))
    domain_capture = any(
        artifact.source_path.startswith("stage-10/evaluator-capture-v1/")
        for artifact in evidence.project_artifacts
    )
    run_command = (
        "`python main.py --vendor-root trojnet --data-root data "
        "--policy execution-policy-v1.json --output score_evidence.jsonl`"
        if domain_capture
        else "`python main.py`"
    )
    readme = (
        f"# Code Package for {_extract_title(paper)}\n\n"
        "## Project Files\n"
        f"{project_list}\n\n"
        "## How to Run\n"
        f"{run_command}\n\n"
        "## Dependencies\n"
        "Install dependencies with `pip install -r requirements.txt`.\n"
    )
    return {
        **project,
        "README.md": readme.encode("utf-8"),
        "requirements.txt": (
            "\n".join(packages) + ("\n" if packages else "")
        ).encode("utf-8"),
    }


def _detect_requirements(project: Mapping[str, bytes]) -> list[str]:
    known = {
        "numpy": "numpy",
        "torch": "torch",
        "tensorflow": "tensorflow",
        "sklearn": "scikit-learn",
        "scipy": "scipy",
        "pandas": "pandas",
        "matplotlib": "matplotlib",
        "seaborn": "seaborn",
        "transformers": "transformers",
        "datasets": "datasets",
        "jax": "jax",
    }
    detected: set[str] = set()
    for name, content in sorted(project.items()):
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(content.decode("utf-8"), filename=name)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise Stage22SemanticError(
                f"canonical selected project has invalid Python: {name}"
            ) from exc
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module_name in modules:
                root = module_name.split(".", 1)[0]
                if root in known:
                    detected.add(known[root])
    return sorted(detected)


def _verification_json(result: object) -> bytes:
    return canonical_authority_json_text(
        {
            "schema_version": 1,
            "passed": bool(result.passed),
            "severity": str(result.severity),
            "total_checked": int(result.total_numbers_checked),
            "total_verified": int(result.total_numbers_verified),
            "strict_violations": int(result.strict_violations),
            "lenient_violations": int(result.lenient_violations),
            "unverified_numbers": [
                {
                    "value": str(item.value),
                    "line": int(item.line_number),
                    "section": str(item.section),
                    "in_table": bool(item.in_table),
                }
                for item in result.unverified_numbers
            ],
            "fabricated_conditions": [
                {"name": str(item.name), "line": int(item.line_number)}
                for item in result.fabricated_conditions
            ],
            "config_warnings": [
                str(item) for item in getattr(result, "config_warnings", ())
            ],
            "summary": str(result.summary),
        }
    ).encode("utf-8")


def _insert_degradation_notice(paper: str) -> str:
    notice = (
        "\n\n> **Validation notice:** The configured quality gate completed in "
        "degraded mode. This artifact is not release-ready.\n"
    )
    marker = "\n## "
    abstract = paper.find("## Abstract")
    if abstract >= 0:
        next_section = paper.find(marker, abstract + len("## Abstract"))
        if next_section >= 0:
            return paper[:next_section] + notice + paper[next_section:]
    return notice.lstrip("\n") + "\n" + paper


def _remove_unavailable_markdown_figures(paper: str) -> str:
    return re.sub(r"(?m)^\s*!\[[^\]]*\]\(charts/[^)]+\)\s*\n?", "", paper)


def _convert_citations_to_latex(paper: str, valid_keys: set[str]) -> str:
    key = r"[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*"

    def replace(match: re.Match[str]) -> str:
        keys = [item.strip() for item in re.split(r"[,;]", match.group(1))]
        if keys and all(item in valid_keys for item in keys):
            return "\\cite{" + ", ".join(keys) + "}"
        return match.group(0)

    return re.sub(rf"\[({key}(?:\s*[,;]\s*{key})*)\]", replace, paper)


def _extract_title(paper: str) -> str:
    for line in paper.splitlines():
        if line.startswith("## "):
            return line[3:].strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled Paper"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stage22SemanticError(f"duplicate Stage 22 JSON key: {key}")
        result[key] = value
    return result
