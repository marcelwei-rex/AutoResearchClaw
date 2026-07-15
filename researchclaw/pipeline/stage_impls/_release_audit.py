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
from researchclaw.pipeline._helpers import StageResult, _safe_json_loads, _utcnow_iso
from researchclaw.pipeline import release_artifacts as ra
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

logger = logging.getLogger(__name__)


_DEAI_SYSTEM = """You are a prose auditor detecting AI-generated stylistic tics in a research paper. \
You are RECOMMEND-ONLY: you never rewrite the paper. Output STRICT JSON only: \
{"suggestions": [{"span": "<verbatim excerpt>", "issue": "<what reads as AI-generated>", \
"suggested_rewrite": "<optional shorter human alternative>", "risk": "style_only|touches_claim"}]}
Rules:
- Mark risk=touches_claim if the excerpt contains a number, comparison, or cited statement.
- Never suggest changing any numeric value, claim meaning, or citation.
- Maximum 40 suggestions."""


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

#: Deterministic stylistic-tic patterns (governance layer owns the taste;
#: this list only produces *recommendations*, never a gate).
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


def _execute_deai_audit(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    truth = ra.read_json(run_dir / "stage-24" / "truth_audit.json")
    if not isinstance(truth, dict) or not truth.get("paper_sha256"):
        return StageResult(
            stage=Stage.DEAI_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            error="De-AI audit requires a completed truth audit (stage-24/truth_audit.json).",
            decision="retry",
        )

    paper_path = ra.canonical_paper_path(run_dir)
    if paper_path is None:
        return StageResult(
            stage=Stage.DEAI_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            error="De-AI audit: no canonical paper artifact found.",
            decision="retry",
        )
    paper_text = paper_path.read_text(encoding="utf-8")
    current_hash = ra.paper_sha256(paper_text)
    frozen_hash = str(truth.get("paper_sha256"))

    if current_hash != frozen_hash:
        # The paper changed after the truth audit. Prose edits invalidate the
        # frozen claim ledger — re-run stage 24 (and stage 23 if citations
        # were touched) before auditing style. Fail closed.
        return StageResult(
            stage=Stage.DEAI_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            error=(
                "Paper hash changed since truth audit "
                f"({frozen_hash[:12]}… → {current_hash[:12]}…). "
                "Re-run TRUTH_AUDIT before the de-AI audit."
            ),
            decision="retry",
        )

    suggestions: list[dict[str, Any]] = []
    stripped = re.sub(r"```.*?```", "", paper_text, flags=re.DOTALL)
    for pattern, issue in _DEAI_PATTERNS:
        for m in re.finditer(pattern, stripped, flags=re.IGNORECASE):
            lo = max(0, m.start() - 80)
            hi = min(len(stripped), m.end() + 80)
            span = ra.normalize_paper_text(stripped[lo:hi])
            touches_claim = bool(re.search(r"\d|\\cite|\[[A-Za-z]+\d{4}", span))
            suggestions.append(
                {
                    "source": "heuristic",
                    "span": span[:300],
                    "issue": issue,
                    "suggested_rewrite": "",
                    "risk": "touches_claim" if touches_claim else "style_only",
                }
            )
            if len(suggestions) >= 60:
                break

    if llm is not None:
        raw = _chat_json(llm, _DEAI_SYSTEM, paper_text[:60000])
        if isinstance(raw, dict) and isinstance(raw.get("suggestions"), list):
            for s in raw["suggestions"][:40]:
                if not isinstance(s, dict):
                    continue
                risk = str(s.get("risk", "style_only"))
                if risk not in ("style_only", "touches_claim"):
                    risk = "touches_claim"  # unknown → conservative
                suggestions.append(
                    {
                        "source": "llm",
                        "span": str(s.get("span", ""))[:300],
                        "issue": str(s.get("issue", ""))[:300],
                        "suggested_rewrite": str(s.get("suggested_rewrite", ""))[:500],
                        "risk": risk,
                    }
                )

    ra.write_json_atomic(
        stage_dir / "deai_audit.json",
        {
            "schema_version": ra.SCHEMA_VERSION,
            "recommend_only": True,
            "applied": False,
            "paper_path": str(paper_path.relative_to(run_dir)),
            "paper_sha256": current_hash,
            "truth_audit_sha256": frozen_hash,
            "hash_invariant_ok": True,
            "suggestions": suggestions,
            "counts": {
                "total": len(suggestions),
                "touches_claim": sum(
                    1 for s in suggestions if s["risk"] == "touches_claim"
                ),
            },
            "rework_rule": (
                "If suggestions are adopted: edits touching citation instances or "
                "claim spans require re-running stages 23+24; style-only edits "
                "require re-running stage 24. Never edit automatically."
            ),
            "generated": _utcnow_iso(),
        },
    )

    return StageResult(
        stage=Stage.DEAI_AUDIT,
        status=StageStatus.DONE,
        artifacts=("deai_audit.json",),
        evidence_refs=("stage-25/deai_audit.json",),
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
