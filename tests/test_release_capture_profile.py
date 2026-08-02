"""Focused tests for the v1.3.1 release-profile tables and pure primitives."""

from __future__ import annotations

import pytest

from researchclaw.pipeline import release_capture_profile as profile
from researchclaw.pipeline.release_capture_profile import ReleaseProfileError


# ---------------------------------------------------------------------------
# P1 path canon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "stage-09/experiment_contract.yaml",
        "stage-14/evidence_candidates/c0/experiment_evidence_candidate.json",
        "deliverables/code/main.py",
        "run_manifest.json",
    ],
)
def test_canonical_release_path_accepts_profile_paths(path: str) -> None:
    assert profile.canonical_release_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/stage-09/x.json",
        "stage-09/",
        "stage-09//x.json",
        "stage-09/./x.json",
        "stage-09/../x.json",
        "stage-09\\x.json",
        "stage%2D09/x.json",
        "stage-09/x.json ",
        123,
        None,
    ],
)
def test_canonical_release_path_rejects(path) -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_release_path(path)


def test_canonical_release_path_rejects_non_nfc() -> None:
    # U+00E9 (NFC) vs e + U+0301 (NFD)
    nfd = "stage-09/expe\u0301rimental.json"
    with pytest.raises(ReleaseProfileError):
        profile.canonical_release_path(nfd)


# ---------------------------------------------------------------------------
# P5 ordering
# ---------------------------------------------------------------------------


def test_canonical_order_utf8_bytewise() -> None:
    paths = ["stage-06/cards/card-010.json", "stage-06/cards/card-002.json"]
    assert profile.canonical_order(paths) == (
        "stage-06/cards/card-002.json",
        "stage-06/cards/card-010.json",
    )


def test_canonical_order_rejects_duplicates() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(["a/b.json", "a/b.json"])


# ---------------------------------------------------------------------------
# P2 fixed vocabulary integrity
# ---------------------------------------------------------------------------


def _all_fixed_paths() -> tuple[str, ...]:
    paths: list[str] = []
    for group in (
        profile.STAGE04_FILES,
        profile.STAGE05_FILES,
        profile.STAGE06_FILES,
        profile.STAGE09_AUTHORITY_FILES,
        profile.STAGE12_FILES,
        profile.STAGE15_FILES,
        profile.STAGE16_FILES,
        profile.STAGE17_FILES,
        profile.STAGE18_FILES,
        profile.STAGE19_FILES,
        profile.STAGE20_FILES,
        profile.STAGE21_FILES,
        profile.STAGE22_DIRECT_FILES,
        profile.STAGE22_CODE_SUPPORT_FILES,
        profile.STAGE23_FILES,
        profile.STAGE24_DIRECT_FILES,
        profile.STAGE25_FILES,
        profile.ROOT_RELEASE_CONTROL_FILES,
        profile.ROOT_RELEASE_CONTROL_CONDITIONAL_FILES,
        profile.ROOT_CANONICAL_AUTHORITY_FILES,
        profile.ROOT_ACTIVE_CONFIG_AUTHORITY_FILES,
        profile.ALLOWED_NON_AUTHORITY_FILES,
        profile.NO_RESUME_FORBIDDEN_FILES,
    ):
        paths.extend(group)
    paths.extend(
        [
            profile.STAGE10_MANIFEST,
            profile.STAGE10_CAPTURE_MANIFEST,
            profile.STAGE13_REFINEMENT_MANIFEST,
            profile.STAGE22_MANIFEST,
            profile.STAGE22_OPTIONAL_PDF,
            profile.STAGE24_MANIFEST,
        ]
    )
    return tuple(paths)


def test_fixed_vocabularies_are_canonical_and_unique() -> None:
    paths = _all_fixed_paths()
    for path in paths:
        assert profile.canonical_release_path(path) == path
    assert len(paths) == len(set(paths))


def test_stage_counts_match_frozen_namespaces() -> None:
    assert len(profile.STAGE16_FILES) == 5
    assert len(profile.STAGE17_FILES) == 8
    assert len(profile.STAGE19_FILES) == 6
    assert len(profile.STAGE20_FILES) == 3
    assert len(profile.STAGE21_FILES) == 2
    assert len(profile.STAGE23_FILES) == 4
    assert len(profile.STAGE25_FILES) == 2
    assert len(profile.STAGE22_DIRECT_FILES) == 8
    assert len(profile.STAGE24_DIRECT_FILES) == 6
    assert len(profile.STAGE24_ASSESSMENT_DIRECTORIES) == 3
    assert len(profile.STAGE14_CANDIDATE_FILES) == 5
    assert len(profile.STAGE09_AUTHORITY_FILES) == 9


def test_stage09_required_and_optional_namespaces_are_separate() -> None:
    producer_authority = (
        "stage-09/experiment_contract.yaml",
        "stage-09/experiment_contract.sha256",
        "stage-09/domain_selector_policy.json",
        "stage-09/domain_profile.json",
        "stage-09/metric_authority_index.json",
        "stage-09/metric_authority.json",
        "stage-09/domain_evaluator_package_manifest.json",
        "stage-09/domain_evaluator_execution_policy.json",
    )
    assert profile.STAGE09_AUTHORITY_FILES == producer_authority + (
        "stage-09/exp_plan.yaml",
    )
    assert profile.STAGE09_OPTIONAL_FILES == (
        "stage-09/prompt_domain_profile.json",
    )
    assert profile.STAGE09_OPTIONAL_FILES[0] in profile.ALLOWED_NON_AUTHORITY_FILES
    assert "stage-09/unknown.json" not in (
        profile.STAGE09_AUTHORITY_FILES + profile.STAGE09_OPTIONAL_FILES
    )


def test_stage12_exact_evidence_v2_vocabulary() -> None:
    assert profile.STAGE12_TOP_LEVEL_FILES == (
        "stage-12/experiment_result_set.json",
        "stage-12/execution_invocation_journal.jsonl",
    )
    assert profile.STAGE12_EVIDENCE_FILES == (
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
    assert profile.STAGE12_FILES == (
        profile.STAGE12_TOP_LEVEL_FILES + profile.STAGE12_EVIDENCE_FILES
    )
    assert all("evidence-v1" not in path for path in profile.STAGE12_FILES)


def test_root_classification_tables_match_v131() -> None:
    assert profile.ROOT_RELEASE_CONTROL_FILES == (
        "run_manifest.json",
        "pipeline_summary.json",
    )
    assert profile.ROOT_RELEASE_CONTROL_CONDITIONAL_FILES == (
        "degradation_signal.json",
    )
    assert profile.ROOT_CANONICAL_AUTHORITY_FILES == (
        "canonical_experiment_evidence.json",
        "analysis_best.md",
        "experiment_summary_best.json",
    )
    assert profile.ROOT_ACTIVE_CONFIG_AUTHORITY_FILES == (
        "config.yaml",
        "active_config_snapshot.json",
        "config_snapshot_history.jsonl",
    )
    expected_bookkeeping = tuple(
        f"stage-{stage}/{name}"
        for stage in ("01", "02", "03", "04", "05", "06", "07", "08", "10", "11", "12", "16", "18", "21")
        for name in ("decision.json", "stage_health.json")
    )
    assert profile.ALLOWED_NON_AUTHORITY_FILES == expected_bookkeeping + (
        "stage-04/search_meta.json",
        "stage-09/prompt_domain_profile.json",
        "hitl/idea_workshop.json",
        "cost_log.jsonl",
    )
    assert profile.NO_RESUME_FORBIDDEN_FILES == ("checkpoint.json",)
    assert profile.NO_RESUME_FORBIDDEN_PREFIXES == ("config.resumed-",)
    assert not profile.is_no_resume_forbidden_root_name("config.yaml")
    for exported in (
        profile.ROOT_RELEASE_CONTROL_FILES,
        profile.ROOT_RELEASE_CONTROL_CONDITIONAL_FILES,
        profile.ROOT_CANONICAL_AUTHORITY_FILES,
        profile.ROOT_ACTIVE_CONFIG_AUTHORITY_FILES,
        profile.ALLOWED_NON_AUTHORITY_FILES,
        profile.NO_RESUME_FORBIDDEN_FILES,
        profile.NO_RESUME_FORBIDDEN_PREFIXES,
    ):
        assert isinstance(exported, tuple)


@pytest.mark.parametrize(
    "name",
    [
        "checkpoint.json",
        "CHECKPOINT.JSON",
        "config.resumed-20260801.yaml",
        "CONFIG.RESUMED-20260801.YAML",
        "Config.Resumed-x.yaml",
    ],
)
def test_no_resume_forbidden_root_names_are_casefolded(name: str) -> None:
    assert profile.is_no_resume_forbidden_root_name(name)


def test_stage16_exact_five_in_producer_order() -> None:
    assert profile.STAGE16_FILES == (
        "stage-16/outline.md",
        "stage-16/outline_binding.json",
        "stage-16/citation_policy_effective.json",
        "stage-16/citation_plan.preliminary.json",
        "stage-16/citation_plan.json",
    )


# ---------------------------------------------------------------------------
# P4 branch discriminators
# ---------------------------------------------------------------------------


def test_stage15_model_final_branch() -> None:
    branch = profile.stage15_branch("model_final", external_prose_present=False)
    assert branch.files == profile.STAGE15_FILES


def test_stage15_external_with_prose_branch() -> None:
    branch = profile.stage15_branch("external_final", external_prose_present=True)
    assert profile.STAGE15_EXTERNAL_STRUCTURED in branch.files
    assert profile.STAGE15_EXTERNAL_PROSE in branch.files


def test_stage15_external_without_prose_branch() -> None:
    branch = profile.stage15_branch("external_final", external_prose_present=False)
    assert profile.STAGE15_EXTERNAL_STRUCTURED in branch.files
    assert profile.STAGE15_EXTERNAL_PROSE not in branch.files


def test_stage15_pending_and_unknown_fail() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.stage15_branch("external_pending", external_prose_present=False)
    with pytest.raises(ReleaseProfileError):
        profile.stage15_branch("whatever", external_prose_present=False)


def test_stage15_prose_without_external_final_fails() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.stage15_branch("model_final", external_prose_present=True)


# ---------------------------------------------------------------------------
# P3 flat-set membership
# ---------------------------------------------------------------------------


def test_flat_member_accepts_leaf() -> None:
    assert (
        profile.require_flat_member("stage-15/external-review/", "structured.json")
        == "stage-15/external-review/structured.json"
    )


@pytest.mark.parametrize("name", ["sub/x.py", "../x.py", "", "x\\y.py"])
def test_flat_member_rejects_nested_or_alias(name: str) -> None:
    with pytest.raises(ReleaseProfileError):
        profile.require_flat_member("stage-15/external-review/", name)


def test_v12_flat_project_and_global_depth_apis_are_retired() -> None:
    assert not hasattr(profile, "require_project_logical_name")
    assert not hasattr(profile, "MAX_CAPTURE_DEPTH")
    assert "v1.2" not in (profile.__doc__ or "")
    assert "evidence-v1" not in (profile.__doc__ or "")


# ---------------------------------------------------------------------------
# P7 exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "stage-22/charts/fig-1.png",
        "stage-22/Charts/fig-1.png",
        "deliverables/charts/fig-1.png",
        "deliverables/CHARTS/fig-1.png",
    ],
)
def test_charts_subtrees_forbidden(path: str) -> None:
    with pytest.raises(ReleaseProfileError):
        profile.require_profile_path_allowed(path)


def test_deep_canonical_path_has_no_global_depth_rejection() -> None:
    deep = "stage-22/code/vendor/trojnet/src/models/deep/module.py"
    assert profile.require_profile_path_allowed(deep) == deep


def test_entry_and_runtime_rejection_vocabularies() -> None:
    assert "execute_iterative_pipeline" in profile.ENTRY_MODE_REJECTIONS
    assert "resume" in profile.ENTRY_MODE_REJECTIONS
    assert "stage15_pivot_rollback" in profile.RUNTIME_MODE_REJECTIONS


# ---------------------------------------------------------------------------
# Section 4 deliverables table
# ---------------------------------------------------------------------------


def test_deliverables_manifest_is_claim_not_selector() -> None:
    assert profile.DELIVERABLES_MANIFEST == "deliverables/manifest.json"
    assert profile.DELIVERABLES_MANIFEST not in profile.DELIVERABLES_FIXED_COPIES
    assert "generated" not in profile.DELIVERABLES_MANIFEST_COMPARE_FIELDS
    assert "generated" in profile.DELIVERABLES_MANIFEST_FORMAT_ONLY_FIELDS
    assert "profile_id" in profile.DELIVERABLES_MANIFEST_COMPARE_FIELDS
    assert "packager_kind" in profile.DELIVERABLES_MANIFEST_COMPARE_FIELDS


def test_deliverables_fixed_copies_map_to_stage_authority() -> None:
    sources = set(profile.DELIVERABLES_FIXED_COPIES.values())
    assert "stage-23/paper_final_verified.md" in sources
    assert "stage-22/paper.tex" in sources
    for root in profile.DELIVERABLES_FIXED_COPIES:
        assert root.startswith("deliverables/")
        profile.canonical_release_path(root)


# ---------------------------------------------------------------------------
# No admission routing in the vocabulary module
# ---------------------------------------------------------------------------


def test_profile_exports_no_admission_routing() -> None:
    assert not hasattr(profile, "is_domain_v2_generation")
    assert not hasattr(profile, "require_profile_generation")


def test_profile_identity_constants() -> None:
    assert profile.PROFILE_ID == "release-profile-trojnet-v1"
    assert profile.PROFILE_SCHEMA_VERSION == 1
    assert profile.PACKAGER_KIND == "structured-copy-only-v1"


def test_colon_rejected_in_authority_paths() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_release_path("C:/stage-22/paper.tex")
    with pytest.raises(ReleaseProfileError):
        profile.canonical_release_path("stage-22/a:b.tex")


# ---------------------------------------------------------------------------
# P1 path-identity hardening (producer-parity with _safe_relative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "stage-22/bad\x07name.json",  # control character
        "stage-22/bad\x7fname.json",  # DEL
        "stage-22/bad\u200bname.json",  # zero-width format character
        "stage-22/CON.json",  # Windows device stem, uppercase
        "stage-22/com1/data.json",  # Windows device stem as directory
        "stage-22/lpt9.txt",  # Windows device stem
        "stage-22/name /data.json",  # component trailing space
        "stage-22/name./data.json",  # component trailing dot
        "stage-22/con.txt",  # device stem before first dot
    ],
)
def test_platform_alias_spellings_rejected(path: str) -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_release_path(path)


def test_canonical_order_rejects_casefold_alias() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(["stage-22/A.py", "stage-22/a.py"])


def test_canonical_order_rejects_prefix_conflict() -> None:
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(["stage-22/code", "stage-22/code/main.py"])


def test_canonical_order_rejects_casefold_prefix_conflict() -> None:
    # Mixed-case combinations collapse on case-insensitive filesystems:
    # a file "Code" and a directory "code/" share one namespace.
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(["stage-22/Code", "stage-22/code/main.py"])
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(["stage-22/code", "stage-22/Code/main.py"])


def test_canonical_order_rejects_prefix_with_interleaved_path() -> None:
    # "code-x.py" sorts (bytewise) between "code" and "code/main.py";
    # an adjacent-only prefix check would miss this conflict.
    with pytest.raises(ReleaseProfileError):
        profile.canonical_order(
            ["stage-22/code", "stage-22/code-x.py", "stage-22/code/main.py"]
        )


def test_canonical_order_accepts_legal_siblings() -> None:
    assert profile.canonical_order(["b/z.json", "a/x.json"]) == (
        "a/x.json",
        "b/z.json",
    )


# ---------------------------------------------------------------------------
# Profile table immutability (code-owned authority)
# ---------------------------------------------------------------------------


def test_deliverables_fixed_copies_rejects_mutation() -> None:
    with pytest.raises(TypeError):
        profile.DELIVERABLES_FIXED_COPIES["deliverables/evil.py"] = "stage-22/x"  # type: ignore[index]
    with pytest.raises(TypeError):
        del profile.DELIVERABLES_FIXED_COPIES["deliverables/paper.tex"]  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        profile.DELIVERABLES_FIXED_COPIES.pop("deliverables/paper.tex")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Producer parity: profile vocabularies must equal producer constants
# ---------------------------------------------------------------------------


def _prefixed(prefix: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}/{name}" for name in names)


def test_stage09_parity_with_producer_authority_outputs() -> None:
    from researchclaw.pipeline.contracts import CONTRACTS
    from researchclaw.pipeline.stage_impls._experiment_design import (
        _STAGE9_AUTHORITY_OUTPUTS,
    )
    from researchclaw.pipeline.stages import Stage

    contract_outputs = tuple(CONTRACTS[Stage.EXPERIMENT_DESIGN].output_files)
    assert contract_outputs == ("exp_plan.yaml",)
    assert profile.STAGE09_AUTHORITY_FILES == _prefixed(
        "stage-09", tuple(_STAGE9_AUTHORITY_OUTPUTS)
    ) + _prefixed("stage-09", contract_outputs)


def test_stage14_parity_with_producer_artifact_layout() -> None:
    from researchclaw.pipeline.stage14_domain_evaluator import _ARTIFACT_LAYOUT

    producer_names = tuple(name for _, name in _ARTIFACT_LAYOUT) + (
        "experiment_evidence_candidate.json",
    )
    assert profile.STAGE14_CANDIDATE_FILES == producer_names


def test_stage16_parity_with_stage_contract() -> None:
    from researchclaw.pipeline.contracts import CONTRACTS
    from researchclaw.pipeline.stages import Stage

    contract = CONTRACTS[Stage.PAPER_OUTLINE]
    assert profile.STAGE16_FILES == _prefixed(
        "stage-16", tuple(contract.output_files)
    )


def test_stage20_parity_with_structured_publication() -> None:
    from researchclaw.pipeline.stage20_structured_publication import (
        STRUCTURED_STAGE20_ARTIFACTS,
    )

    assert profile.STAGE20_FILES == _prefixed(
        "stage-20", tuple(STRUCTURED_STAGE20_ARTIFACTS)
    )


def test_stage22_parity_with_structured_publication() -> None:
    from researchclaw.pipeline.stage22_structured_publication import (
        STRUCTURED_STAGE22_FIXED_FILES,
        STRUCTURED_STAGE22_MANIFEST,
    )

    assert profile.STAGE22_DIRECT_FILES == _prefixed(
        "stage-22", tuple(STRUCTURED_STAGE22_FIXED_FILES)
    )
    assert profile.STAGE22_MANIFEST == f"stage-22/{STRUCTURED_STAGE22_MANIFEST}"


def test_stage23_parity_with_structured_publication() -> None:
    from researchclaw.pipeline.stage23_structured_publication import _FORMAL_NAMES

    assert profile.STAGE23_FILES == _prefixed("stage-23", tuple(_FORMAL_NAMES))


def test_stage24_parity_with_structured_authority() -> None:
    from researchclaw.pipeline.stage24_structured_authority import (
        STRUCTURED_STAGE24_COARSE_ARTIFACTS,
    )

    producer_direct = tuple(
        name for name in STRUCTURED_STAGE24_COARSE_ARTIFACTS if name.endswith(".json")
    )
    producer_dirs = tuple(
        name.rstrip("/")
        for name in STRUCTURED_STAGE24_COARSE_ARTIFACTS
        if name.endswith("/")
    )
    assert profile.STAGE24_DIRECT_FILES == _prefixed(
        "stage-24", producer_direct[:-1]
    )
    assert profile.STAGE24_MANIFEST == f"stage-24/{producer_direct[-1]}"
    assert profile.STAGE24_ASSESSMENT_DIRECTORIES == _prefixed(
        "stage-24", producer_dirs
    )


def test_stage25_parity_with_structured_publication() -> None:
    from researchclaw.pipeline.stage25_structured_publication import (
        _AUDIT_NAME,
        _MANIFEST_NAME,
    )

    assert profile.STAGE25_FILES == (
        f"stage-25/{_AUDIT_NAME}",
        f"stage-25/{_MANIFEST_NAME}",
    )
