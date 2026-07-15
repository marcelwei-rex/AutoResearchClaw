"""Stages 24-25: Release Audit (v2).

Stage 24 TRUTH_AUDIT — builds the machine-readable claim ledger
(claims.json), maps citation instances to claims (citations.json),
resolves Socratic critique findings (critique_resolution.json), and
freezes the paper hash + claims digest (truth_audit.json).

Stage 25 DEAI_AUDIT — recommend-only prose audit (deai_audit.json).
It NEVER modifies the paper. It verifies the paper hash is unchanged
since the truth audit; if prose edits were adopted in between, the stage
fails and the truth audit must be re-run first.

Ordering is a hard invariant: truth before prose. Do not reorder, and do
not turn the de-AI audit into an auto-rewriter.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.prompts import PromptManager
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline._helpers import StageResult, _safe_json_loads
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundleError,
)
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationError,
    execute_stage24_truth,
)
from researchclaw.pipeline.stage25_publication import (
    Stage25PublicationError,
    execute_stage25_deai,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 24: Truth Audit
# ---------------------------------------------------------------------------

def _execute_truth_audit(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """Publish one manifest-bound canonical Stage 24 truth generation."""

    del adapters, prompts
    try:
        snapshot = execute_stage24_truth(
            run_dir,
            stage_dir,
            runtime_config=config,
            llm=llm,
        )
    except (
        CanonicalEvidenceMigrationIncomplete,
        Stage24InputBundleError,
        Stage24PublicationError,
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.TRUTH_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Truth audit failed: {exc}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.TRUTH_AUDIT,
        status=StageStatus.DONE,
        artifacts=tuple(
            artifact.path.removeprefix("stage-24/")
            for artifact in (
                *snapshot.outputs,
                *snapshot.assessment_files,
                snapshot.manifest,
            )
        ),
        evidence_refs=(snapshot.manifest.path,),
    )


def _execute_disabled_legacy_truth_audit(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    del stage_dir, run_dir, config, adapters, llm, prompts
    raise RuntimeError("legacy Stage 24 producer is disabled")

# ---------------------------------------------------------------------------
# Stage 25: De-AI Audit (recommend-only)
# ---------------------------------------------------------------------------

def _execute_deai_audit(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    del adapters, prompts
    try:
        snapshot = execute_stage25_deai(
            run_dir,
            stage_dir,
            runtime_config=config,
            llm=llm,
        )
    except (
        CanonicalEvidenceMigrationIncomplete,
        Stage24InputBundleError,
        Stage24PublicationError,
        Stage25PublicationError,
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.DEAI_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"De-AI audit failed: {exc}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.DEAI_AUDIT,
        status=StageStatus.DONE,
        artifacts=(
            snapshot.audit.path.removeprefix("stage-25/"),
            snapshot.manifest.path.removeprefix("stage-25/"),
        ),
        evidence_refs=(snapshot.manifest.path,),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chat_json(
    llm: LLMClient, system: str, user: str, *, model: str | None = None
) -> Any:
    """One stateless JSON-mode call. The audit stages never share the
    writer's conversational context — every call starts fresh."""
    try:
        resp = llm.chat(
            [{"role": "user", "content": user}],
            system=system,
            json_mode=True,
            model=model,
            strip_thinking=True,
        )
        return _loads_json_repaired(resp.content, {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Release audit LLM call failed: %s", exc)
        return {}


def _loads_json_repaired(text: str, default: Any) -> Any:
    """Parse JSON from LLM output with bounded, deterministic repair.

    This is intentionally conservative: it only removes common transport/
    formatting noise (markdown fences, line comments, trailing commas, prose
    around a single JSON object/array). It never fabricates missing fields.
    """
    for candidate in _json_candidates(text):
        stripped = _strip_markdown_fence(candidate.strip())
        try:
            return json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            pass
        repaired = _strip_json_comments(stripped)
        repaired = _strip_trailing_commas(repaired)
        try:
            return json.loads(repaired)
        except (TypeError, json.JSONDecodeError):
            continue
    return _safe_json_loads(text, default)


def _json_candidates(text: str) -> list[str]:
    raw = text or ""
    candidates = [raw]
    fenced = _strip_markdown_fence(raw.strip())
    if fenced != raw:
        candidates.append(fenced)
    embedded = _extract_first_json_value(raw)
    if embedded:
        candidates.append(embedded)
    if embedded:
        unfenced = _strip_markdown_fence(embedded.strip())
        if unfenced != embedded:
            candidates.append(unfenced)
    # Preserve order while dropping exact duplicates.
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _strip_markdown_fence(text: str) -> str:
    m = re.fullmatch(r"\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?\s*```\s*", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _extract_first_json_value(text: str) -> str | None:
    starts = [i for i, ch in enumerate(text) if ch in "{["]
    for start in starts:
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        stack = [closer]
        in_string = False
        escape = False
        for idx in range(start + 1, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or ch != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    return text[start : idx + 1]
        # Try the next possible opening brace/bracket.
    return None
