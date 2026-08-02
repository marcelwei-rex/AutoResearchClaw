"""Code-owned vocabulary and pure primitives for ``release-profile-trojnet-v1``.

Resolved Baseline v1.3.1 freezes immutable authority tables, path identity,
ordering, branch discriminators, exclusions, and deliverables derivation data.
This module performs no filesystem access, manifest parsing, replay, snapshot,
admission routing, or private-seam work.  Capability remains exactly ``1110``;
generic-v1 behavior is unchanged.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping

PROFILE_ID = "release-profile-trojnet-v1"
PROFILE_SCHEMA_VERSION = 1


class ReleaseProfileError(ValueError):
    """Raised when a run shape leaves the supported profile."""


# ---------------------------------------------------------------------------
# P1 path canon and P5 ordering primitives
# ---------------------------------------------------------------------------


_WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def _has_unsafe_characters(value: str) -> bool:
    return any(
        ord(character) < 32
        or ord(character) == 127
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )


def canonical_release_path(path: str) -> str:
    """Validate and return one NFC run-relative POSIX authority path.

    Rejects non-strings, empty/non-NFC spellings, backslash, percent,
    colon (``C:/x`` drive or scheme spellings), control/format
    characters, absolute prefixes, trailing slashes, empty/dot/dot-dot
    components, components ending in space or dot, and Windows
    device-name stems — mirroring the producer side
    (``stage22_structured_publication._safe_relative``) so the profile is
    never looser than production.  Single path authority: the legacy
    reconstructor's ``validate_release_authority_path`` converges here
    when the reconstruction module is rewritten.
    """

    if (
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or path != unicodedata.normalize("NFC", path)
        or "\\" in path
        or "%" in path
        or ":" in path
        or _has_unsafe_characters(path)
        or path.startswith("/")
        or path.endswith("/")
    ):
        raise ReleaseProfileError(f"noncanonical release authority path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseProfileError(f"noncanonical release authority path: {path!r}")
    if PurePosixPath(path).as_posix() != path:
        raise ReleaseProfileError(f"noncanonical release authority path: {path!r}")
    for part in parts:
        stem = part.split(".", 1)[0].casefold()
        if part.endswith((" ", ".")) or stem in _WINDOWS_DEVICE_STEMS:
            raise ReleaseProfileError(
                f"noncanonical release authority path alias: {path!r}"
            )
    return path


def canonical_order(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """P5 shared ordering: NFC UTF-8 bytewise order over validated paths.

    Beyond exact duplicates, rejects casefold aliases (``A.py`` vs
    ``a.py``) and file/directory prefix conflicts (``code`` vs
    ``code/main.py``): one capture set may never contain two spellings
    that one platform identity could collapse.  The prefix check runs on
    the casefolded spellings, so mixed-case combinations such as
    ``Code`` vs ``code/main.py`` are rejected as well; the returned
    order still uses the original NFC UTF-8 bytewise sequence.
    """

    validated = tuple(canonical_release_path(path) for path in paths)
    if len(set(validated)) != len(validated):
        raise ReleaseProfileError("duplicate path in canonical ordering input")
    folded = [item.casefold() for item in validated]
    if len(set(folded)) != len(folded):
        raise ReleaseProfileError("casefold alias in canonical ordering input")
    # All-pairs comparison on casefolded spellings: adjacency on a sorted
    # sequence is insufficient because an unrelated path (``code-x.py``)
    # may sort between a prefix (``code``) and its extension
    # (``code/main.py``).  Capture sets are small, so O(n^2) is fine.
    for index, earlier in enumerate(folded):
        prefix = earlier + "/"
        for other, later in enumerate(folded[index + 1 :], start=index + 1):
            if later.startswith(prefix) or earlier.startswith(later + "/"):
                raise ReleaseProfileError(
                    "casefold path prefix conflict in canonical ordering "
                    f"input: {validated[index]!r} vs {validated[other]!r}"
                )
    return tuple(sorted(validated, key=lambda item: item.encode("utf-8")))


# ---------------------------------------------------------------------------
# P2 fixed vocabularies (exact sets; missing or extra entries fail closed)
# ---------------------------------------------------------------------------

STAGE04_FILES = (
    "stage-04/candidates.jsonl",
    "stage-04/cite_key_registry.json",
    "stage-04/references.bib",
)

STAGE05_FILES = (
    "stage-05/shortlist.jsonl",
    "stage-05/screening_report.json",
)

STAGE06_FILES = (
    "stage-06/citation_allowlist.json",
    "stage-06/cards_manifest.json",
)

STAGE09_AUTHORITY_FILES = (
    "stage-09/experiment_contract.yaml",
    "stage-09/experiment_contract.sha256",
    "stage-09/domain_selector_policy.json",
    "stage-09/domain_profile.json",
    "stage-09/metric_authority_index.json",
    "stage-09/metric_authority.json",
    "stage-09/domain_evaluator_package_manifest.json",
    "stage-09/domain_evaluator_execution_policy.json",
    "stage-09/exp_plan.yaml",
)
STAGE09_OPTIONAL_FILES = ("stage-09/prompt_domain_profile.json",)

STAGE10_MANIFEST = "stage-10/selected_candidate_manifest.json"
STAGE10_CAPTURE_MANIFEST = "stage-10/evaluator-capture-v1/capture-manifest.json"
STAGE10_PROJECT_PREFIX = "stage-10/evaluator-capture-v1/project/"

STAGE12_TOP_LEVEL_FILES = (
    "stage-12/experiment_result_set.json",
    "stage-12/execution_invocation_journal.jsonl",
)
STAGE12_EVIDENCE_FILES = (
    "stage-12/evidence-v2/invocation-1/execution_meta.json",
    "stage-12/evidence-v2/invocation-1/score_evidence.jsonl",
    "stage-12/evidence-v2/invocation-2/execution_meta.json",
    "stage-12/evidence-v2/invocation-2/score_evidence.jsonl",
    "stage-12/evidence-v2/observations.json",
    "stage-12/evidence-v2/results.json",
    "stage-12/evidence-v2/run-1.json",
    "stage-12/evidence-v2/run-2.json",
    "stage-12/evidence-v2/verification-1.json",
    "stage-12/evidence-v2/verification-2.json",
)
STAGE12_FILES = STAGE12_TOP_LEVEL_FILES + STAGE12_EVIDENCE_FILES

STAGE13_REFINEMENT_MANIFEST = "stage-13/refinement_result_set.json"

STAGE14_CANDIDATE_FILES = (
    "analysis.md",
    "figure_plan.json",
    "results_table.tex",
    "experiment_summary.json",
    "experiment_evidence_candidate.json",
)
STAGE14_CANDIDATES_PREFIX = "stage-14/evidence_candidates/"

STAGE15_FILES = (
    "stage-15/decision.md",
    "stage-15/decision_structured.json",
    "stage-15/critique.json",
    "stage-15/stage15_critique_manifest.json",
)
STAGE15_EXTERNAL_STRUCTURED = "stage-15/external-review/structured.json"
STAGE15_EXTERNAL_PROSE = "stage-15/external-review/review.md"

STAGE16_FILES = (
    "stage-16/outline.md",
    "stage-16/outline_binding.json",
    "stage-16/citation_policy_effective.json",
    "stage-16/citation_plan.preliminary.json",
    "stage-16/citation_plan.json",
)

STAGE17_FILES = (
    "stage-17/scientific_evidence_facts.json",
    "stage-17/scientific_claim_registry.json",
    "stage-17/scientific_claim_selection.json",
    "stage-17/paper_draft.md",
    "stage-17/paper_structure_report.json",
    "stage-17/experiment_fact_closure_report.json",
    "stage-17/citation_closure_report.json",
    "stage-17/scientific_claim_authority_manifest.json",
)

STAGE18_FILES = (
    "stage-18/reviews.md",
    "stage-18/review_structure_report.json",
)

STAGE19_FILES = (
    "stage-19/scientific_claim_selection.json",
    "stage-19/scientific_claim_paper_revised.md",
    "stage-19/scientific_claim_paper_structure_report.json",
    "stage-19/scientific_claim_experiment_fact_closure_report.json",
    "stage-19/scientific_claim_citation_closure_report.json",
    "stage-19/scientific_claim_authority_manifest.json",
)

STAGE20_FILES = (
    "stage-20/quality_report.json",
    "stage-20/fabrication_flags.json",
    "stage-20/quality_gate_manifest.json",
)
ROOT_DEGRADATION_SIGNAL = "degradation_signal.json"

STAGE21_FILES = (
    "stage-21/archive.md",
    "stage-21/bundle_index.json",
)

STAGE22_DIRECT_FILES = (
    "stage-22/paper_final.md",
    "stage-22/paper_final_latex.md",
    "stage-22/references.bib",
    "stage-22/paper.tex",
    "stage-22/compile_status.json",
    "stage-22/paper_verification.json",
    "stage-22/sanitization_report.json",
    "stage-22/canonical_source.json",
)
STAGE22_OPTIONAL_PDF = "stage-22/paper.pdf"
STAGE22_CODE_SUPPORT_FILES = (
    "stage-22/code/README.md",
    "stage-22/code/requirements.txt",
)
STAGE22_CODE_PREFIX = "stage-22/code/"
STAGE22_MANIFEST = "stage-22/stage22_export_manifest.json"

STAGE23_FILES = (
    "stage-23/verification_report.json",
    "stage-23/references_verified.bib",
    "stage-23/paper_final_verified.md",
    "stage-23/stage23_verification_manifest.json",
)

STAGE24_DIRECT_FILES = (
    "stage-24/obligation_inventory.json",
    "stage-24/claims.json",
    "stage-24/citations.json",
    "stage-24/citation_support.json",
    "stage-24/critique_resolution.json",
    "stage-24/truth_audit.json",
)
STAGE24_ASSESSMENT_DIRECTORIES = (
    "stage-24/citation-assessments",
    "stage-24/generic-support-assessments",
    "stage-24/resolution-assessments",
)
STAGE24_MANIFEST = "stage-24/stage24_truth_manifest.json"

STAGE25_FILES = (
    "stage-25/deai_audit.json",
    "stage-25/stage25_deai_manifest.json",
)

ROOT_RELEASE_CONTROL_FILES = (
    "run_manifest.json",
    "pipeline_summary.json",
)
ROOT_RELEASE_CONTROL_CONDITIONAL_FILES = (ROOT_DEGRADATION_SIGNAL,)
ROOT_CANONICAL_AUTHORITY_FILES = (
    "canonical_experiment_evidence.json",
    "analysis_best.md",
    "experiment_summary_best.json",
)
ROOT_ACTIVE_CONFIG_AUTHORITY_FILES = (
    "config.yaml",
    "active_config_snapshot.json",
    "config_snapshot_history.jsonl",
)
OPTIONAL_COST_LOG = "cost_log.jsonl"
_BOOKKEEPING_STAGES = (
    "01", "02", "03", "04", "05", "06", "07", "08",
    "10", "11", "12", "16", "18", "21",
)
ALLOWED_NON_AUTHORITY_FILES = tuple(
    f"stage-{stage}/{name}"
    for stage in _BOOKKEEPING_STAGES
    for name in ("decision.json", "stage_health.json")
) + (
    "stage-04/search_meta.json",
    *STAGE09_OPTIONAL_FILES,
    "hitl/idea_workshop.json",
    OPTIONAL_COST_LOG,
)
NO_RESUME_FORBIDDEN_FILES = ("checkpoint.json",)
NO_RESUME_FORBIDDEN_PREFIXES = ("config.resumed-",)


def is_no_resume_forbidden_root_name(name: str) -> bool:
    """Return whether one root leaf is forbidden by the no-resume profile."""

    if not isinstance(name, str) or "/" in name:
        return False
    identity = name.casefold()
    forbidden_files = tuple(item.casefold() for item in NO_RESUME_FORBIDDEN_FILES)
    forbidden_prefixes = tuple(
        prefix.casefold() for prefix in NO_RESUME_FORBIDDEN_PREFIXES
    )
    return identity in forbidden_files or any(
        identity.startswith(prefix) for prefix in forbidden_prefixes
    )


# ---------------------------------------------------------------------------
# P4 branch discriminators
# ---------------------------------------------------------------------------

STAGE15_FINAL_STATES = ("model_final", "none_final", "external_final")


@dataclass(frozen=True)
class Stage15Branch:
    """One legal Stage 15 capture branch (P4 row)."""

    state: str
    external_structured_present: bool
    external_prose_present: bool

    @property
    def files(self) -> tuple[str, ...]:
        files = list(STAGE15_FILES[:2])
        if self.state == "external_final":
            files.append(STAGE15_EXTERNAL_STRUCTURED)
            if self.external_prose_present:
                files.append(STAGE15_EXTERNAL_PROSE)
        files.extend(STAGE15_FILES[2:])
        return tuple(files)


STAGE15_BRANCHES = (
    Stage15Branch("model_final", False, False),
    Stage15Branch("none_final", False, False),
    Stage15Branch("external_final", True, False),
    Stage15Branch("external_final", True, True),
)


def stage15_branch(state: str, *, external_prose_present: bool) -> Stage15Branch:
    """Resolve the exact P4 branch or fail closed on unknown states."""

    if state not in STAGE15_FINAL_STATES:
        raise ReleaseProfileError(f"Stage 15 final state leaves profile: {state!r}")
    if state != "external_final" and external_prose_present:
        raise ReleaseProfileError("Stage 15 prose present without external_final")
    expected_prose = external_prose_present if state == "external_final" else False
    for branch in STAGE15_BRANCHES:
        if branch.state == state and branch.external_prose_present is expected_prose:
            return branch
    raise ReleaseProfileError(
        f"Stage 15 branch leaves profile: {state!r} prose={external_prose_present}"
    )


STAGE20_OUTCOMES = ("passed", "degraded")


# ---------------------------------------------------------------------------
# P3 declared flat-set invariants
# ---------------------------------------------------------------------------


def require_flat_member(prefix: str, name: str, *, suffix: str = "") -> str:
    """Bind one flat-set member; any nesting or alias fails closed."""

    stripped = prefix.rstrip("/")
    if stripped:
        canonical_release_path(stripped)
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or name != unicodedata.normalize("NFC", name)
    ):
        raise ReleaseProfileError(f"non-flat profile member name: {name!r}")
    if suffix and not name.endswith(suffix):
        raise ReleaseProfileError(f"profile member suffix mismatch: {name!r}")
    return canonical_release_path(f"{stripped}/{name}" if stripped else name)


# ---------------------------------------------------------------------------
# Section 4 deliverables (copy-only packager) derivation table
# ---------------------------------------------------------------------------

DELIVERABLES_ROOT = "deliverables"
DELIVERABLES_MANIFEST = "deliverables/manifest.json"

# root -> (replay rule identifier, profile source of truth)
# Immutable: the profile table is a code-owned authority and must reject
# runtime mutation attempts (MappingProxyType raises TypeError).
DELIVERABLES_FIXED_COPIES: Mapping[str, str] = MappingProxyType(
    {
        "deliverables/paper_final.md": "stage-23/paper_final_verified.md",
        "deliverables/paper.tex": "stage-22/paper.tex",
        "deliverables/references.bib": "stage-23/references_verified.bib",
        "deliverables/verification_report.json": "stage-23/verification_report.json",
        "deliverables/sanitization_report.json": "stage-22/sanitization_report.json",
    }
)
DELIVERABLES_CODE_ROOT = "deliverables/code"
DELIVERABLES_CONDITIONAL_PDF = "deliverables/paper.pdf"

# Compared value-by-value after exact rebuild; ``generated`` is format-only.
DELIVERABLES_MANIFEST_COMPARE_FIELDS = (
    "run_id",
    "target_conference",
    "files",
    "release_ready",
    "not_release_ready",
    "release_blockers",
    "release_authority",
    "notes",
    "profile_id",
    "packager_kind",
)
DELIVERABLES_MANIFEST_FORMAT_ONLY_FIELDS = ("generated",)
PACKAGER_KIND = "structured-copy-only-v1"


# ---------------------------------------------------------------------------
# P7 profile exclusions (fail closed; generic-v1 behavior unchanged)
# ---------------------------------------------------------------------------

FORBIDDEN_SUBTREES = (
    "stage-22/charts",
    "deliverables/charts",
)
# Entry-known rejection modes (rejected before the first stage write).
ENTRY_MODE_REJECTIONS = (
    "execute_iterative_pipeline",
    "from_stage",
    "resume",
    "explicit_rerun",
)
# Runtime-known rejection: Stage 15 pivot must be refused before
# ``_version_rollback_stages_bound()`` and the recursive pipeline re-entry.
RUNTIME_MODE_REJECTIONS = ("stage15_pivot_rollback",)


def require_profile_path_allowed(path: str) -> str:
    """Apply the code-owned forbidden-subtree rules to one canonical path."""

    canonical = canonical_release_path(path)
    canonical_identity = canonical.casefold()
    for forbidden in FORBIDDEN_SUBTREES:
        forbidden_identity = forbidden.casefold()
        if canonical_identity == forbidden_identity or canonical_identity.startswith(
            forbidden_identity + "/"
        ):
            raise ReleaseProfileError(f"profile-forbidden subtree: {canonical}")
    return canonical
