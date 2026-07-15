"""Immutable canonical Stage 22 publication consumed by Stage 23."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from researchclaw.config import RCConfig
from researchclaw.literature.verify import parse_bibtex_entries
from researchclaw.pipeline.sectional_validation import extract_citation_keys
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage22_input_bundle import (
    Stage22InputBundle,
    load_stage22_input_bundle,
)
from researchclaw.pipeline.stage22_publication import (
    Stage22PublicationSnapshot,
    load_stage22_export_publication,
)


class Stage23InputBundleError(ValueError):
    """Raised when Stage 23 cannot replay one canonical Stage 22 generation."""


@dataclass(frozen=True)
class Stage23InputBundle:
    stage22_inputs: Stage22InputBundle
    publication: Stage22PublicationSnapshot
    paper: BoundArtifact
    bibliography: BoundArtifact
    latex: BoundArtifact
    cited_keys: tuple[str, ...]
    claim_scope: str


def load_stage23_input_bundle(
    run_dir: Path,
    runtime_config: RCConfig,
) -> Stage23InputBundle:
    """Replay Stage 22 and capture every Stage 23 input from its publication."""

    try:
        stage22_inputs = load_stage22_input_bundle(run_dir, runtime_config)
        publication = load_stage22_export_publication(run_dir, stage22_inputs)
        paper = publication.require_output("paper_final.md")
        bibliography = publication.require_output("references.bib")
        latex = publication.require_output("paper.tex")
        markdown_keys = set(extract_citation_keys(paper.text()))
        latex_keys = _extract_latex_citation_keys(latex.text())
        if latex_keys != markdown_keys:
            raise Stage23InputBundleError(
                "Stage 22 Markdown and LaTeX citation key sets differ"
            )
        entries = parse_bibtex_entries(bibliography.text())
        bib_keys = [str(entry.get("key") or "").strip() for entry in entries]
        if any(not key for key in bib_keys) or len(bib_keys) != len(set(bib_keys)):
            raise Stage23InputBundleError(
                "Stage 22 bibliography keys are empty or duplicated"
            )
        missing = sorted(markdown_keys - set(bib_keys))
        if missing:
            raise Stage23InputBundleError(
                f"Stage 22 bibliography is missing cited keys: {missing}"
            )
    except Stage23InputBundleError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage23InputBundleError(f"Stage 23 input replay failed: {exc}") from exc
    return Stage23InputBundle(
        stage22_inputs=stage22_inputs,
        publication=publication,
        paper=paper,
        bibliography=bibliography,
        latex=latex,
        cited_keys=tuple(sorted(markdown_keys)),
        claim_scope=stage22_inputs.claim_scope,
    )


def verify_stage23_input_bundle_unchanged(
    run_dir: Path,
    runtime_config: RCConfig,
    bundle: Stage23InputBundle,
) -> None:
    """Fresh-load the complete Stage 23 authority graph and require a fixpoint."""

    current = load_stage23_input_bundle(run_dir, runtime_config)
    if current != bundle:
        raise Stage23InputBundleError("Stage 23 input bundle changed after capture")


def _extract_latex_citation_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"\\cite[pt]?\{([^}]+)\}", text):
        keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    return keys
