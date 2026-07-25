# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportAttributeAccessIssue=false, reportUnknownLambdaType=false
from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.usefixtures(
    "canonical_evidence_migration_complete",
    "consumer_evidence_fixture",
)
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import PaperRevisionConfig, RCConfig
from researchclaw.experiment_runtime.contract import (
    derive_contract,
    dump_contract,
    load_contract,
    sha256_file,
)
from researchclaw.hitl.intervention import HumanAction, HumanInput
from researchclaw.literature.citation_plan import (
    CitationAnchor,
    CitationPlanContractError,
)
from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
from researchclaw.pipeline import executor as rc_executor
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.stage_impls import _release_audit as release_audit
from researchclaw.pipeline.stage_impls import _paper_writing, _review_publish
from researchclaw.pipeline.stages import Stage, StageStatus


@pytest.fixture(autouse=True)
def _stub_effective_citation_policy_for_legacy_unit_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = {
        "effective_min_unique_sources": 1,
        "effective_target_unique_sources": 15,
    }
    monkeypatch.setattr(
        _paper_writing, "load_effective_citation_policy", lambda *_args: policy
    )
    monkeypatch.setattr(
        _paper_writing, "build_effective_citation_policy", lambda *_args: policy
    )
    monkeypatch.setattr(
        _review_publish, "load_effective_citation_policy", lambda *_args: policy
    )
    monkeypatch.setattr(
        _review_publish, "validate_paper_citation_minimum", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        _paper_writing, "load_final_citation_plan", lambda *_args: {"claims": []}
    )
    monkeypatch.setattr(
        _paper_writing,
        "build_citation_writer_instruction",
        lambda *_args: "\nFINAL CITATION PLAN: legacy unit-test fixture\n",
    )
    monkeypatch.setattr(
        _paper_writing,
        "build_experiment_fact_closure_report",
        lambda *_args, **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        _paper_writing,
        "build_citation_closure_report",
        lambda *_args, **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        _paper_writing,
        "validate_experiment_fact_closure_report",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        _paper_writing,
        "validate_citation_closure_report",
        lambda *_args, **_kwargs: {},
    )


class FakeLLMClient:
    def __init__(self, response_text: str = "mock response"):
        self.response_text: str = response_text
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: object):
        _ = kwargs
        self.calls.append(messages)
        from researchclaw.llm.client import LLMResponse

        return LLMResponse(content=self.response_text, model="fake-model")


class FakeLLMClientWithConfig(FakeLLMClient):
    def __init__(self, response_text: str = "mock response"):
        super().__init__(response_text=response_text)
        self.config: SimpleNamespace = SimpleNamespace(
            base_url="http://fake", api_key="fake-key"
        )


class TestPaperRevisionRecovery:
    @staticmethod
    def _prompt_manager() -> object:
        class PromptManagerStub:
            def block(self, _name: str, **_kwargs: object) -> str:
                return ""

            def for_stage(self, _name: str, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    system="revision system",
                    user="revision user",
                    json_mode=False,
                    max_tokens=12000,
                )

        return PromptManagerStub()

    def test_retry_transport_failure_preserves_full_draft(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        stage_dir = run_dir / "stage-19"
        stage_dir.mkdir(parents=True)
        draft = " ".join(f"draft-{idx}" for idx in range(600))
        short_revision = " ".join(f"revision-{idx}" for idx in range(200))
        _write_prior_artifact(run_dir, 17, "paper_draft.md", draft)
        _write_prior_artifact(run_dir, 18, "reviews.md", "Fix the limitations.")
        calls = 0
        retry_values: list[object] = []

        def fake_chat(*_args: object, **kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            retry_values.append(kwargs.get("retries"))
            if calls == 1:
                return SimpleNamespace(content=short_revision)
            try:
                raise IncompleteRead(b"")
            except IncompleteRead as cause:
                raise RuntimeError("IncompleteRead(0 bytes read)") from cause

        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._review_publish._chat_with_prompt",
            fake_chat,
        )

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=cast(Any, FakeLLMClient()),
            prompts=cast(Any, self._prompt_manager()),
        )

        assert result.status == StageStatus.DONE
        assert calls == 2
        assert retry_values == [2, 2]
        assert result.artifacts == (
            "paper_revised.md",
            "revision_evidence_binding.json",
            "revision_notes_internal.md",
            "revision_retry_failure.json",
        )
        binding = json.loads(
            (stage_dir / "revision_evidence_binding.json").read_text(
                encoding="utf-8"
            )
        )
        assert binding["canonical_experiment_evidence_path"] == (
            "canonical_experiment_evidence.json"
        )
        assert binding["revised_paper_path"] == "stage-19/paper_revised.md"
        assert (stage_dir / "paper_revised.md").read_text(encoding="utf-8") == draft
        notes = (stage_dir / "revision_notes_internal.md").read_text(encoding="utf-8")
        assert notes.startswith("revision-0 revision-1")
        diagnostic = json.loads(
            (stage_dir / "revision_retry_failure.json").read_text(encoding="utf-8")
        )
        assert diagnostic["fallback"] == "full_original_draft"
        assert diagnostic["error_type"] == "RuntimeError"
        assert diagnostic["cause_type"] == "IncompleteRead"
        assert diagnostic["first_revision_word_count"] == 200

    def test_initial_transport_failure_removes_stale_revision(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        stage_dir = run_dir / "stage-19"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 17, "paper_draft.md", "draft content")
        _write_prior_artifact(run_dir, 18, "reviews.md", "review content")
        stale_revision = stage_dir / "paper_revised.md"
        stale_revision.write_text("stale prior revision", encoding="utf-8")

        def fail_chat(*_args: object, **_kwargs: object) -> SimpleNamespace:
            raise RuntimeError("IncompleteRead(0 bytes read)")

        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._review_publish._chat_with_prompt",
            fail_chat,
        )

        with pytest.raises(RuntimeError, match="IncompleteRead"):
            rc_executor._execute_paper_revision(
                stage_dir,
                run_dir,
                rc_config,
                adapters,
                llm=cast(Any, FakeLLMClient()),
                prompts=cast(Any, self._prompt_manager()),
            )

        assert not stale_revision.exists()

    def test_sectional_flag_without_configured_models_fails_without_legacy_fallback(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        run_dir = tmp_path / "run"
        stage_dir = run_dir / "stage-19"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(
            run_dir,
            17,
            "paper_draft.md",
            "## Title\n\nExample\n\n## Method\n\nBody.\n",
        )
        _write_prior_artifact(
            run_dir,
            18,
            "reviews.md",
            "## Reviewer A\n\n### Actionable Revisions\n1. Clarify.\n",
        )
        stale_revision = stage_dir / "paper_revised.md"
        stale_revision.write_text("stale legacy output", encoding="utf-8")
        config = replace(
            rc_config,
            paper_revision=PaperRevisionConfig(sectional_enabled=True),
        )
        llm = FakeLLMClient("legacy revision must not run")

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,
        )

        assert result.status == StageStatus.FAILED
        assert "writer_model and critic_model are required" in (result.error or "")
        assert not stale_revision.exists()
        assert llm.calls == []

    def test_sectional_flag_routes_to_deterministic_execution_shell(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from tests.test_sectional_execution import (
            _FakeProvider,
            _config,
            _prepare_run,
        )

        run_dir, stage_dir = _prepare_run(tmp_path)
        config = replace(
            rc_config,
            paper_revision=_config(),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
            ),
        )
        llm = FakeLLMClient("legacy revision must not run")

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,
            sectional_provider=_FakeProvider(),
        )

        assert result.status == StageStatus.DONE
        assert result.decision == "sectional"
        assert "paper_revised.md" in result.artifacts
        assert llm.calls == []

    def test_sectional_input_mutation_cleans_published_outputs(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A late Stage 17/18 mutation cannot leave a reusable Stage 19 bundle."""
        from researchclaw.pipeline.sectional_execution import (
            SectionalExecutionResult,
            _OWNED_DIRS,
            _OWNED_FILES,
        )
        from researchclaw.pipeline.stage19_input_bundle import Stage19InputBundleError
        from tests.test_sectional_execution import _config, _prepare_run

        run_dir, stage_dir = _prepare_run(tmp_path)
        config = replace(rc_config, paper_revision=_config())

        def write_success_bundle(*, stage_dir: Path, **_kwargs: object) -> SectionalExecutionResult:
            for name in _OWNED_FILES:
                (stage_dir / name).write_text("published", encoding="utf-8")
            for name in _OWNED_DIRS:
                directory = stage_dir / name
                directory.mkdir()
                (directory / "published.json").write_text("{}", encoding="utf-8")
            return SectionalExecutionResult(
                completed=True,
                paper_text="published",
                error=None,
                artifacts=tuple(_OWNED_FILES),
            )

        monkeypatch.setattr(
            "researchclaw.pipeline.sectional_execution.execute_sectional_revision",
            write_success_bundle,
        )
        monkeypatch.setattr(
            _review_publish,
            "verify_stage19_input_bundle_unchanged",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                Stage19InputBundleError("captured draft changed")
            ),
        )

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(),
            sectional_provider=object(),
        )

        assert result.status == StageStatus.FAILED
        assert result.artifacts == ()
        assert "inputs changed" in (result.error or "")
        assert all(not (stage_dir / name).exists() for name in _OWNED_FILES)
        assert all(not (stage_dir / name).exists() for name in _OWNED_DIRS)

    def test_sectional_flag_builds_isolated_llm_provider(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from tests.test_sectional_execution import _config, _prepare_run

        run_dir, stage_dir = _prepare_run(tmp_path)
        config = replace(
            rc_config,
            llm=replace(rc_config.llm, primary_model="writer-model"),
            paper_revision=_config(),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
            ),
        )

        class SectionalLLM:
            def __init__(self) -> None:
                self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

            def chat(self, messages, **kwargs):
                payload = json.loads(messages[0]["content"])
                self.calls.append((payload, kwargs))
                if "sections" in payload:
                    method = next(
                        section
                        for section in payload["sections"]
                        if section["title"] == "Method"
                    )
                    content = {
                        "schema_version": 1,
                        "planner_version": 1,
                        "source_paper_sha256": payload["source_paper_sha256"],
                        "source_reviews_sha256": payload["source_reviews_sha256"],
                        "section_model_version": 1,
                        "assignments": [
                            {
                                "comment_id": comment["comment_id"],
                                "target_section_ids": [method["section_id"]],
                                "disposition": "assigned",
                                "reason": None,
                            }
                            for comment in payload["comments"]
                        ],
                    }
                elif "deterministic_validator_codes" in payload:
                    content = {
                        "schema_version": 1,
                        "comment_id": payload["comment"]["comment_id"],
                        "section_id": payload["section"]["section_id"],
                        "attempt_id": payload["attempt_id"],
                        "verdict": "resolved",
                        "reason": "The requested change is present in the revision.",
                    }
                else:
                    content = {
                        "schema_version": 1,
                        "section_id": payload["section"]["section_id"],
                        "revised_body": (
                            "\nThe recorded detector score was 0.475 across three "
                            "seeds \\cite{smith2024}. This sentence clarifies the "
                            "reporting basis.\n\n"
                        ),
                        "resolutions": [
                            {
                                "comment_id": comment["comment_id"],
                                "writer_status": "addressed",
                                "reason": "The requested wording was added.",
                            }
                            for comment in payload["comments"]
                        ],
                    }
                from researchclaw.llm.client import LLMResponse

                return LLMResponse(
                    content=json.dumps(content),
                    model=str(kwargs["model"]),
                )

        llm = SectionalLLM()
        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,  # type: ignore[arg-type]
        )

        assert result.status == StageStatus.DONE
        assert result.decision == "sectional"
        assert llm.calls[0][1]["model"] == "writer-model"
        assert any(call[1]["model"] == "critic-model" for call in llm.calls)
        assert all(call[1]["json_mode"] is True for call in llm.calls)

    def test_sectional_llm_provider_identity_error_fails_at_stage_boundary(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from tests.test_sectional_execution import _config, _prepare_run

        run_dir, stage_dir = _prepare_run(tmp_path)
        config = replace(
            rc_config,
            llm=replace(rc_config.llm, primary_model="critic-model"),
            paper_revision=_config(),
        )

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(),
        )

        assert result.status == StageStatus.FAILED
        assert result.artifacts == ()
        assert "must differ" in (result.error or "")


    @pytest.mark.parametrize(
        ("claim_scope", "invalid_contract"),
        (
            ("research_release", False),
            ("exploratory", False),
            ("pipeline_validation", True),
        ),
    )
    def test_non_validation_or_invalid_contract_does_not_hide_retry_failure(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
        claim_scope: str,
        invalid_contract: bool,
    ) -> None:
        strict_config = replace(
            rc_config,
            experiment=replace(
                rc_config.experiment,
                claim_scope=claim_scope,
                dataset_origin="public",
            ),
        )
        run_dir = tmp_path / "run"
        stage_dir = run_dir / "stage-19"
        stage_dir.mkdir(parents=True)
        draft = " ".join(f"draft-{idx}" for idx in range(600))
        short_revision = " ".join(f"revision-{idx}" for idx in range(200))
        _write_prior_artifact(run_dir, 17, "paper_draft.md", draft)
        _write_prior_artifact(run_dir, 18, "reviews.md", "Fix the limitations.")
        if invalid_contract:
            _write_prior_artifact(
                run_dir,
                9,
                "experiment_contract.yaml",
                "claim_scope: [unterminated",
            )
        calls = 0

        def fake_chat(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(content=short_revision)
            raise RuntimeError("IncompleteRead(0 bytes read)")

        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._review_publish._chat_with_prompt",
            fake_chat,
        )

        result = rc_executor._execute_paper_revision(
            stage_dir,
            run_dir,
            strict_config,
            adapters,
            llm=cast(Any, FakeLLMClient()),
            prompts=cast(Any, self._prompt_manager()),
        )

        if invalid_contract:
            # The canonical snapshot contract is now the sole authority. A
            # malformed snapshot stops before any Stage 19 writer call.
            assert calls == 0
            assert result.status == StageStatus.FAILED
            assert not (stage_dir / "paper_revised.md").exists()
            return
        assert calls == 2
        assert result.status == StageStatus.DONE
        assert (stage_dir / "paper_revised.md").exists()
        assert (stage_dir / "revision_retry_failure.json").exists()


@pytest.fixture()
def rc_config(tmp_path: Path) -> RCConfig:
    data = {
        "project": {"name": "rc-test", "mode": "docs-first"},
        "research": {
            "topic": "test-driven science",
            "domains": ["ml", "systems"],
            "daily_paper_count": 2,
            "quality_threshold": 8.2,
        },
        "runtime": {"timezone": "UTC"},
        "notifications": {
            "channel": "local",
            "on_stage_start": True,
            "on_stage_fail": False,
            "on_gate_required": True,
        },
        "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
        "openclaw_bridge": {"use_memory": True, "use_message": True},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "RC_TEST_KEY",
            "api_key": "inline-test-key",
            "primary_model": "fake-model",
            "fallback_models": [],
        },
        "security": {"hitl_required_stages": [5, 9, 20]},
        "experiment": {"mode": "sandbox"},
    }
    return RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)


@pytest.fixture()
def adapters() -> AdapterBundle:
    return AdapterBundle()


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    return path


def _write_prior_artifact(
    run_dir: Path, stage_num: int, filename: str, content: str
) -> None:
    stage_dir = run_dir / f"stage-{stage_num:02d}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / filename).write_text(content, encoding="utf-8")


def _write_experiment_contract(run_dir: Path, cfg: RCConfig) -> Path:
    stage_dir = run_dir / "stage-09"
    stage_dir.mkdir(parents=True, exist_ok=True)
    contract_path = stage_dir / "experiment_contract.yaml"
    contract = derive_contract(
        cfg, {"datasets": ["synthetic traces"]}, stage_dir=stage_dir
    )
    # These legacy execution tests intentionally exercise model-owned main.py
    # below the simulated C5 gate; pipeline_validation now requires scaffold bytes.
    dump_contract(replace(contract, claim_scope="exploratory"), contract_path)
    return contract_path


def _write_sealed_candidate(run_dir: Path, cfg: RCConfig, main_code: str) -> Path:
    from researchclaw.literature.citation_policy import write_active_config_binding
    from researchclaw.pipeline.stage_impls._code_generation import (
        _seal_selected_candidate,
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "config.yaml"
    if not snapshot.exists():
        raw = json.loads(json.dumps(cfg.to_dict()))
        snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        write_active_config_binding(run_dir, snapshot)
    contract_path = _write_experiment_contract(run_dir, cfg)
    experiment = run_dir / "stage-10" / "experiment"
    experiment.mkdir(parents=True, exist_ok=True)
    main = experiment / "main.py"
    main.write_text(main_code, encoding="utf-8")
    _seal_selected_candidate(run_dir / "stage-10", experiment, contract_path, cfg)
    return run_dir / "stage-10" / "selected_candidate"


def test_executor_map_has_25_entries() -> None:
    executor_map = getattr(rc_executor, "EXECUTOR_MAP", rc_executor._STAGE_EXECUTORS)
    # v2: 23 pipeline stages + TRUTH_AUDIT (24) + DEAI_AUDIT (25).
    assert len(executor_map) == 25


def test_every_stage_member_has_matching_executor() -> None:
    executor_map = getattr(rc_executor, "EXECUTOR_MAP", rc_executor._STAGE_EXECUTORS)
    assert set(executor_map.keys()) == set(Stage)


def test_stage_result_dataclass_fields() -> None:
    result = rc_executor.StageResult(
        stage=Stage.TOPIC_INIT, status=StageStatus.DONE, artifacts=("goal.md",)
    )
    assert result.stage == Stage.TOPIC_INIT
    assert result.status == StageStatus.DONE
    assert result.artifacts == ("goal.md",)
    assert result.error is None
    assert result.decision == "proceed"
    assert result.evidence_refs == ()


def test_utcnow_iso_returns_valid_iso_timestamp() -> None:
    ts = rc_executor._utcnow_iso()
    assert ts.endswith("+00:00")
    assert "T" in ts


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("before\n```yaml\na: 1\n```\nafter", "a: 1"),
        ("```yml\nkey: value\n```", "key: value"),
        ("```\nplain: true\n```", "plain: true"),
        ("  x: y  ", "x: y"),
    ],
)
def test_extract_yaml_block_variants(text: str, expected: str) -> None:
    assert rc_executor._extract_yaml_block(text) == expected


@pytest.mark.parametrize(
    ("payload", "default", "expected"),
    [
        ('{"ok": true}', {"fallback": True}, {"ok": True}),
        ("[1, 2, 3]", {"fallback": True}, [1, 2, 3]),
        ("not-json", {"fallback": True}, {"fallback": True}),
    ],
)
def test_safe_json_loads_valid_and_invalid(payload: str, default, expected) -> None:
    assert rc_executor._safe_json_loads(payload, default) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ("../secret", "__secret"),
        ("name with spaces!.md", "name_with_spaces_.md"),
        ("", "unnamed"),
    ],
)
def test_safe_filename_sanitization(raw: str, expected: str) -> None:
    assert rc_executor._safe_filename(raw) == expected


def test_safe_filename_truncates_to_100_chars() -> None:
    raw = "x" * 120
    cleaned = rc_executor._safe_filename(raw)
    assert len(cleaned) == 100
    assert cleaned == "x" * 100


def test_build_context_preamble_basic_fields(
    rc_config: RCConfig, run_dir: Path
) -> None:
    text = rc_executor._build_context_preamble(rc_config, run_dir)
    assert "## Research Context" in text
    assert "test-driven science" in text
    assert "ml, systems" in text


def test_build_context_preamble_includes_selected_prior_artifacts(
    rc_config: RCConfig, run_dir: Path
) -> None:
    _write_prior_artifact(run_dir, 1, "goal.md", "goal content")
    _write_prior_artifact(run_dir, 8, "hypotheses.md", "hyp content")
    _write_prior_artifact(run_dir, 7, "synthesis.md", "synth content")
    text = rc_executor._build_context_preamble(
        rc_config,
        run_dir,
        include_goal=True,
        include_hypotheses=True,
        include_synthesis=True,
    )
    assert "### Goal" in text
    assert "goal content" in text
    assert "### Hypotheses" in text
    assert "hyp content" in text
    assert "### Synthesis" in text
    assert "synth content" in text


def test_read_prior_artifact_finds_newest_file(run_dir: Path) -> None:
    _write_prior_artifact(run_dir, 1, "goal.md", "old")
    _write_prior_artifact(run_dir, 3, "goal.md", "new")
    found = rc_executor._read_prior_artifact(run_dir, "goal.md")
    assert found == "new"


def test_read_prior_artifact_finds_directory_path(run_dir: Path) -> None:
    cards_dir = run_dir / "stage-06" / "cards"
    cards_dir.mkdir(parents=True)
    (cards_dir / "card-1.json").write_text("{}", encoding="utf-8")
    found = rc_executor._read_prior_artifact(run_dir, "cards/")
    assert found == str(cards_dir)


def test_read_prior_artifact_returns_none_when_not_found(run_dir: Path) -> None:
    assert rc_executor._read_prior_artifact(run_dir, "missing.md") is None


def test_read_best_analysis_prefers_best_file(run_dir: Path) -> None:
    """BUG-225: _read_best_analysis prefers analysis_best.md at run root."""
    from researchclaw.pipeline._helpers import _read_best_analysis

    # Create degenerate analysis in stage-14 and best at run root
    s14 = run_dir / "stage-14"
    s14.mkdir(parents=True)
    (s14 / "analysis.md").write_text("Degenerate analysis", encoding="utf-8")
    (run_dir / "analysis_best.md").write_text("Best analysis", encoding="utf-8")

    result = _read_best_analysis(run_dir)
    assert result == "Best analysis"


def test_read_best_analysis_falls_back_to_prior_artifact(run_dir: Path) -> None:
    """BUG-225: Falls back to _read_prior_artifact when no analysis_best.md."""
    from researchclaw.pipeline._helpers import _read_best_analysis

    s14 = run_dir / "stage-14"
    s14.mkdir(parents=True)
    (s14 / "analysis.md").write_text("Only analysis", encoding="utf-8")

    result = _read_best_analysis(run_dir)
    assert result == "Only analysis"


def test_read_best_analysis_returns_empty_when_none(run_dir: Path) -> None:
    """BUG-225: Returns empty string when no analysis exists at all."""
    from researchclaw.pipeline._helpers import _read_best_analysis

    result = _read_best_analysis(run_dir)
    assert result == ""


def test_write_stage_meta_writes_expected_json(run_dir: Path) -> None:
    stage_dir = run_dir / "stage-01"
    stage_dir.mkdir()
    result = rc_executor.StageResult(
        stage=Stage.TOPIC_INIT,
        status=StageStatus.DONE,
        artifacts=("goal.md",),
        decision="proceed",
        evidence_refs=("stage-01/goal.md",),
    )
    rc_executor._write_stage_meta(stage_dir, Stage.TOPIC_INIT, "run-abc", result)
    payload = cast(
        dict[str, Any],
        json.loads((stage_dir / "decision.json").read_text(encoding="utf-8")),
    )
    assert payload["stage_id"] == "01-topic_init"
    assert payload["run_id"] == "run-abc"
    assert payload["status"] == "done"
    assert payload["decision"] == "proceed"
    assert payload["output_artifacts"] == ["goal.md"]
    assert payload["evidence_refs"] == ["stage-01/goal.md"]
    assert payload["next_stage"] == 2
    assert re.match(r"\d{4}-\d{2}-\d{2}T", payload["ts"])


def test_write_stage_meta_keeps_paused_stage_as_next_stage(run_dir: Path) -> None:
    stage_dir = run_dir / "stage-02"
    stage_dir.mkdir()
    result = rc_executor.StageResult(
        stage=Stage.PROBLEM_DECOMPOSE,
        status=StageStatus.PAUSED,
        artifacts=("refinement_log.json",),
        decision="resume",
        error="ACP prompt timed out after 1800s",
        evidence_refs=("stage-02/refinement_log.json",),
    )
    rc_executor._write_stage_meta(
        stage_dir, Stage.PROBLEM_DECOMPOSE, "run-paused", result
    )
    payload = cast(
        dict[str, Any],
        json.loads((stage_dir / "decision.json").read_text(encoding="utf-8")),
    )
    assert payload["status"] == "paused"
    assert payload["decision"] == "resume"
    assert payload["next_stage"] == int(Stage.PROBLEM_DECOMPOSE)


def test_execute_stage_creates_stage_dir_writes_artifacts_and_meta(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    fake_llm = FakeLLMClientWithConfig("# Goal\n\nMocked goal body")
    monkeypatch.setattr(
        "researchclaw.pipeline.executor.LLMClient.from_rc_config",
        lambda _config: fake_llm,
    )

    result = rc_executor.execute_stage(
        Stage.TOPIC_INIT,
        run_dir=run_dir,
        run_id="run-1",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )

    assert result.status == StageStatus.DONE
    assert "goal.md" in result.artifacts
    assert "hardware_profile.json" in result.artifacts
    assert (run_dir / "stage-01").is_dir()
    assert (
        (run_dir / "stage-01" / "goal.md")
        .read_text(encoding="utf-8")
        .startswith("# Goal")
    )
    assert (run_dir / "stage-01" / "hardware_profile.json").exists()
    assert len(fake_llm.calls) == 1

    decision = cast(
        dict[str, Any],
        json.loads(
            (run_dir / "stage-01" / "decision.json").read_text(encoding="utf-8")
        ),
    )
    assert decision["run_id"] == "run-1"
    assert decision["status"] == "done"
    assert decision["output_artifacts"] == ["goal.md", "hardware_profile.json"]


def test_execute_stage_contract_validation_missing_output_file_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def bad_executor(
        _stage_dir: Path,
        _run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
    ):
        _ = llm
        return rc_executor.StageResult(
            stage=Stage.TOPIC_INIT, status=StageStatus.DONE, artifacts=("goal.md",)
        )

    monkeypatch.setitem(rc_executor._STAGE_EXECUTORS, Stage.TOPIC_INIT, bad_executor)
    result = rc_executor.execute_stage(
        Stage.TOPIC_INIT,
        run_dir=run_dir,
        run_id="run-2",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.FAILED
    assert "Missing or empty output: goal.md" in (result.error or "")


def test_execute_stage_contract_validation_missing_output_directory_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    _write_prior_artifact(run_dir, 5, "shortlist.jsonl", '{"title": "x"}')
    _write_prior_artifact(run_dir, 5, "screening_report.json", "{}")

    def bad_executor(
        _stage_dir: Path,
        _run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
    ):
        _ = llm
        return rc_executor.StageResult(
            stage=Stage.KNOWLEDGE_EXTRACT,
            status=StageStatus.DONE,
            artifacts=("cards/",),
        )

    monkeypatch.setitem(
        rc_executor._STAGE_EXECUTORS, Stage.KNOWLEDGE_EXTRACT, bad_executor
    )
    result = rc_executor.execute_stage(
        Stage.KNOWLEDGE_EXTRACT,
        run_dir=run_dir,
        run_id="run-3",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.FAILED
    assert "Missing output directory: cards/" in (result.error or "")


def test_execute_stage_missing_required_input_returns_failed(
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    result = rc_executor.execute_stage(
        Stage.PROBLEM_DECOMPOSE,
        run_dir=run_dir,
        run_id="run-4",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.FAILED
    assert "Missing input: goal.md" in (result.error or "")


def test_stage6_resume_rejects_stage5_partial_without_canonical_shortlist(
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    stage5 = run_dir / "stage-05"
    stage5.mkdir(parents=True)
    (stage5 / "screening_partial.jsonl").write_text(
        '{"source_identity":"doi:partial"}\n', encoding="utf-8"
    )
    (stage5 / "screening_report.json").write_text("{}\n", encoding="utf-8")

    result = rc_executor.execute_stage(
        Stage.KNOWLEDGE_EXTRACT,
        run_dir=run_dir,
        run_id="run-stage6-after-partial",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )

    assert result.status == StageStatus.FAILED
    assert "Missing input: shortlist.jsonl" in (result.error or "")


def test_execute_stage_gate_behavior_auto_approve_true_keeps_done(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    _write_prior_artifact(run_dir, 4, "candidates.jsonl", '{"title": "paper"}')
    _write_prior_artifact(run_dir, 4, "references.bib", "@article{paper,}\n")
    _write_prior_artifact(run_dir, 4, "cite_key_registry.json", "{}\n")

    def good_executor(
        stage_dir: Path,
        _run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
        **_kwargs: object,
    ):
        _ = llm
        (stage_dir / "shortlist.jsonl").write_text(
            '{"title": "paper"}\n', encoding="utf-8"
        )
        (stage_dir / "screening_report.json").write_text(
            "{}\n", encoding="utf-8"
        )
        return rc_executor.StageResult(
            stage=Stage.LITERATURE_SCREEN,
            status=StageStatus.DONE,
            artifacts=("shortlist.jsonl", "screening_report.json"),
        )

    monkeypatch.setitem(
        rc_executor._STAGE_EXECUTORS, Stage.LITERATURE_SCREEN, good_executor
    )
    result = rc_executor.execute_stage(
        Stage.LITERATURE_SCREEN,
        run_dir=run_dir,
        run_id="run-5",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.DONE
    memory_entries = getattr(adapters.memory, "entries", [])
    assert any(
        ns == "gates" and "auto-approved" in content for ns, content in memory_entries
    )


def test_execute_stage_gate_behavior_auto_approve_false_blocks(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    _write_prior_artifact(run_dir, 4, "candidates.jsonl", '{"title": "paper"}')
    _write_prior_artifact(run_dir, 4, "references.bib", "@article{paper,}\n")
    _write_prior_artifact(run_dir, 4, "cite_key_registry.json", "{}\n")

    def good_executor(
        stage_dir: Path,
        _run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
        **_kwargs: object,
    ):
        _ = llm
        (stage_dir / "shortlist.jsonl").write_text(
            '{"title": "paper"}\n', encoding="utf-8"
        )
        (stage_dir / "screening_report.json").write_text(
            "{}\n", encoding="utf-8"
        )
        return rc_executor.StageResult(
            stage=Stage.LITERATURE_SCREEN,
            status=StageStatus.DONE,
            artifacts=("shortlist.jsonl", "screening_report.json"),
        )

    monkeypatch.setitem(
        rc_executor._STAGE_EXECUTORS, Stage.LITERATURE_SCREEN, good_executor
    )
    result = rc_executor.execute_stage(
        Stage.LITERATURE_SCREEN,
        run_dir=run_dir,
        run_id="run-6",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=False,
    )
    assert result.status == StageStatus.BLOCKED_APPROVAL
    assert result.decision == "block"
    message_calls = getattr(adapters.message, "calls", [])
    assert message_calls
    assert "Approval required" in message_calls[-1][2]


def test_execute_stage_llm_client_creation_error_falls_back_without_crash(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def boom(_config: RCConfig):
        raise RuntimeError("llm init failed")

    monkeypatch.setattr("researchclaw.pipeline.executor.LLMClient.from_rc_config", boom)
    result = rc_executor.execute_stage(
        Stage.TOPIC_INIT,
        run_dir=run_dir,
        run_id="run-7",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.DONE
    assert (run_dir / "stage-01" / "goal.md").exists()


def test_execute_stage_executor_exception_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def raising_executor(
        _stage_dir: Path,
        _run_dir: Path,
        _config: RCConfig,
        _adapters: AdapterBundle,
        *,
        llm: object = None,
        **_kwargs: object,
    ):
        _ = llm
        raise RuntimeError("stage exploded")

    monkeypatch.setitem(
        rc_executor._STAGE_EXECUTORS, Stage.TOPIC_INIT, raising_executor
    )
    result = rc_executor.execute_stage(
        Stage.TOPIC_INIT,
        run_dir=run_dir,
        run_id="run-8",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert result.status == StageStatus.FAILED
    assert result.decision == "retry"
    assert "stage exploded" in (result.error or "")


@pytest.mark.parametrize(
    "stage",
    [
        Stage.TOPIC_INIT,
        Stage.PROBLEM_DECOMPOSE,
        Stage.SEARCH_STRATEGY,
        Stage.LITERATURE_COLLECT,
        Stage.LITERATURE_SCREEN,
        Stage.KNOWLEDGE_EXTRACT,
        Stage.SYNTHESIS,
        Stage.HYPOTHESIS_GEN,
        Stage.EXPERIMENT_DESIGN,
        Stage.CODE_GENERATION,
    ],
)
def test_stage_executor_mapping_values_are_callable(stage: Stage) -> None:
    assert callable(rc_executor._STAGE_EXECUTORS[stage])


def test_stage18_prompt_receives_complete_cfs_contract(
    tmp_path: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage17 = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    stage17.mkdir()
    (stage17 / "citation_closure_report.json").write_text("{}", encoding="utf-8")
    draft = "## Title\n\nBounded study.\n"
    draft_sha = __import__("hashlib").sha256(draft.encode()).hexdigest()
    evidence = SimpleNamespace(manifest_path="canonical.json", manifest_sha256="a" * 64)
    closure = {
        "paper_sha256": draft_sha,
        "canonical_experiment_evidence_path": "canonical.json",
        "canonical_experiment_evidence_sha256": "a" * 64,
        "structure_report_sha256": "b" * 64,
        "experiment_fact_closure_report_sha256": "c" * 64,
    }
    monkeypatch.setattr(_review_publish, "load_canonical_experiment_evidence", lambda _: evidence)
    monkeypatch.setattr(_review_publish, "_read_bound_stage17_draft", lambda _: (draft, draft_sha))
    monkeypatch.setattr(
        _review_publish,
        "load_effective_citation_policy",
        lambda *_a: {"effective_min_unique_sources": 0, "effective_target_unique_sources": 0},
    )
    monkeypatch.setattr(_review_publish, "validate_experiment_fact_closure_report", lambda *_a, **_k: closure)
    monkeypatch.setattr(_review_publish, "validate_citation_closure_report", lambda *_a, **_k: closure)
    monkeypatch.setattr(_review_publish, "_collect_experiment_evidence", lambda _e: "legacy evidence")
    monkeypatch.setattr(_review_publish, "build_canonical_fact_sheet", lambda _e: {"active": True})
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda _cfs, *, include_projection: "COMPLETE_CFS_WITH_PROJECTION",
    )

    class _Prompts:
        def for_stage(self, _name: str, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                system="review system",
                user=str(kwargs["experiment_evidence"]),
                json_mode=False,
                max_tokens=4096,
            )

    captured: list[str] = []
    reviews = """## Reviewer A

### Strengths
Bounded.

### Weaknesses
None.

### Actionable Revisions
1. Clarify wording.

## Reviewer B

### Strengths
Bounded.

### Weaknesses
None.

### Actionable Revisions
1. Clarify wording.

## Reviewer C

### Strengths
Bounded.

### Weaknesses
None.

### Actionable Revisions
1. Clarify wording.
"""

    def chat(_llm: object, _system: str, user: str, **_kwargs: object) -> SimpleNamespace:
        captured.append(user)
        return SimpleNamespace(content=reviews)

    monkeypatch.setattr(_review_publish, "_chat_with_prompt", chat)
    result = _review_publish._execute_peer_review(
        stage_dir,
        run_dir,
        rc_config,
        adapters,
        llm=object(),  # type: ignore[arg-type]
        prompts=_Prompts(),  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.DONE
    assert "COMPLETE_CFS_WITH_PROJECTION" in captured[0]
    assert "out_of_scope" in captured[0]
    assert "not alone justify reject" in captured[0]


class TestStageHealth:
    def test_stage_health_json_written(self, tmp_path: Path) -> None:
        from researchclaw.pipeline.executor import execute_stage
        from researchclaw.pipeline.stages import Stage

        config = RCConfig.load(
            Path(__file__).parent.parent / "config.researchclaw.example.yaml",
            check_paths=False,
        )
        result = execute_stage(
            Stage.TOPIC_INIT,
            run_dir=tmp_path,
            run_id="test-health",
            config=config,
            adapters=AdapterBundle(),
            auto_approve_gates=True,
        )
        health_path = tmp_path / "stage-01" / "stage_health.json"
        assert result is not None
        assert health_path.exists()

    def test_stage_health_has_required_fields(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from researchclaw.pipeline.executor import execute_stage
        from researchclaw.pipeline.stages import Stage

        config = RCConfig.load(
            Path(__file__).parent.parent / "config.researchclaw.example.yaml",
            check_paths=False,
        )

        with patch("researchclaw.pipeline.executor.LLMClient") as mock_llm_cls:
            mock_client = MagicMock()
            mock_client.chat.return_value = MagicMock(
                content='{"topic": "test", "research_questions": ["q1"]}'
            )
            mock_llm_cls.from_rc_config.return_value = mock_client

            execute_stage(
                Stage.TOPIC_INIT,
                run_dir=tmp_path,
                run_id="test-health-fields",
                config=config,
                adapters=AdapterBundle(),
                auto_approve_gates=True,
            )

        health_path = tmp_path / "stage-01" / "stage_health.json"
        if health_path.exists():
            data = json.loads(health_path.read_text(encoding="utf-8"))
            assert "stage_id" in data
            assert "run_id" in data
            assert "duration_sec" in data
            assert "status" in data
            assert "timestamp" in data
            assert data["duration_sec"] >= 0


    def test_stage_health_duration_positive(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from researchclaw.pipeline.executor import execute_stage
        from researchclaw.pipeline.stages import Stage

        config = RCConfig.load(
            Path(__file__).parent.parent / "config.researchclaw.example.yaml",
            check_paths=False,
        )

        with patch("researchclaw.pipeline.executor.LLMClient") as mock_llm_cls:
            mock_client = MagicMock()
            mock_client.chat.return_value = MagicMock(
                content='{"topic": "test", "sub_problems": []}'
            )
            mock_llm_cls.from_rc_config.return_value = mock_client

            execute_stage(
                Stage.TOPIC_INIT,
                run_dir=tmp_path,
                run_id="test-duration",
                config=config,
                adapters=AdapterBundle(),
                auto_approve_gates=True,
            )

        health_path = tmp_path / "stage-01" / "stage_health.json"
        if health_path.exists():
            data = json.loads(health_path.read_text(encoding="utf-8"))
            assert data["duration_sec"] >= 0

# Contracts import for Stage 13/22 preservation features.
from researchclaw.pipeline.contracts import CONTRACTS


class TestExportPublishCodePackage:
    def test_export_packages_experiment_final(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir, 19, "paper_revised.md", "# Test Paper\n\nSome content..."
        )
        _write_prior_artifact(
            run_dir,
            13,
            "experiment_final.py",
            'import numpy\nprint("val_loss: 0.1")\n',
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status is StageStatus.FAILED
        assert not (stage_dir / "code").exists()
        assert not (stage_dir / "stage22_export_manifest.json").exists()

    def test_export_falls_back_to_experiment_py(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir, 19, "paper_revised.md", "# Test Paper\n\nSome content..."
        )
        _write_prior_artifact(
            run_dir,
            10,
            "experiment.py",
            'import numpy\nprint("val_loss: 0.1")\n',
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status is StageStatus.FAILED
        assert not (stage_dir / "code").exists()

    def test_export_no_experiment_skips_code_dir(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir, 19, "paper_revised.md", "# Test Paper\n\nSome content..."
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=None,
        )

        assert not (stage_dir / "code").exists()
        assert "code/" not in result.artifacts
        assert result.status is StageStatus.FAILED

    def test_export_detects_multiple_dependencies(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir, 19, "paper_revised.md", "# Test Paper\n\nSome content..."
        )
        _write_prior_artifact(
            run_dir,
            13,
            "experiment_final.py",
            (
                "import numpy\n"
                "import torch\n"
                "from sklearn.metrics import accuracy_score\n"
                "print(accuracy_score([1], [1]))\n"
            ),
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status is StageStatus.FAILED
        assert not (stage_dir / "code").exists()

    def test_export_code_readme_contains_title(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir, 19, "paper_revised.md", "# My Great Paper\n\nSome content..."
        )
        _write_prior_artifact(
            run_dir,
            13,
            "experiment_final.py",
            'print("val_loss: 0.1")\n',
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status is StageStatus.FAILED
        assert not (stage_dir / "code").exists()

    def test_export_writes_canonical_source_metadata(
        self,
        tmp_path: Path,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(
            run_dir,
            19,
            "paper_revised.md",
            "# Canonical Paper\n\nThe measured loss is 0.1234.",
        )
        stage_dir = tmp_path / "run" / "stage-22"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_export_publish(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status is StageStatus.FAILED
        assert not (stage_dir / "canonical_source.json").exists()
        assert not (stage_dir / "stage22_export_manifest.json").exists()


def test_contracts_stage13_includes_experiment_final() -> None:
    contract = CONTRACTS[Stage.ITERATIVE_REFINE]
    assert contract.input_files == ()
    assert "refinement_result_set.json" in contract.output_files
    assert "experiment_final/" in contract.output_files
    assert contract.max_retries == 0


def test_contracts_stage22_includes_code_dir() -> None:
    contract = CONTRACTS[Stage.EXPORT_PUBLISH]
    assert contract.input_files == ()
    assert "code/" in contract.output_files
    assert "stage22_export_manifest.json" in contract.output_files
    assert contract.max_retries == 0


# ── P1-1: Topic keyword extraction tests ──


class TestExtractTopicKeywords:
    def test_basic_extraction(self) -> None:
        keywords = rc_executor._extract_topic_keywords(
            "Agent-based Reinforcement Learning for Automated Scientific Discovery"
        )
        assert "agent-based" in keywords
        assert "reinforcement" in keywords
        assert "learning" in keywords
        assert "automated" in keywords
        assert "scientific" in keywords
        assert "discovery" in keywords
        # Stop words excluded
        # Stop words excluded
        assert "for" not in keywords

    def test_includes_domain_keywords(self) -> None:
        keywords = rc_executor._extract_topic_keywords(
            "Neural network pruning", domains=("ml", "optimization")
        )
        assert "neural" in keywords
        assert "network" in keywords
        assert "pruning" in keywords
        assert "ml" in keywords
        assert "optimization" in keywords

    def test_deduplication(self) -> None:
        keywords = rc_executor._extract_topic_keywords(
            "Learning to learn meta-learning", domains=("learning",)
        )
        assert keywords.count("learning") == 1

    def test_empty_topic(self) -> None:
        keywords = rc_executor._extract_topic_keywords("")
        assert keywords == []


# ── P1-2: Topic constraint block test ──


class TestTopicConstraintBlock:
    def test_contains_topic(self) -> None:
        block = rc_executor._topic_constraint_block("Transformer attention for time series")
        assert "Transformer attention for time series" in block

    def test_contains_prohibition(self) -> None:
        block = rc_executor._topic_constraint_block("anything")
        assert "PROHIBITED" in block
        assert "environment" in block.lower()
        assert "infrastructure" in block.lower()

    def test_hard_constraint_markers(self) -> None:
        block = rc_executor._topic_constraint_block("test")
        assert "HARD TOPIC CONSTRAINT" in block
        assert "END CONSTRAINT" in block


# ── Multi-perspective debate tests ──


class TestParseDecision:
    def test_no_keyword_returns_none(self) -> None:
        # Without a PROCEED/PIVOT/REFINE keyword the parser must NOT default
        # to "proceed" — that previously caused inconclusive model output
        # to silently advance the pipeline. See researchclaw_rung_b_mapping.md.
        assert rc_executor._parse_decision("Some random text") is None

    def test_proceed_explicit(self) -> None:
        text = "## Decision\nPROCEED\n## Justification\nGood results."
        assert rc_executor._parse_decision(text) == "proceed"

    def test_pivot_detected(self) -> None:
        text = "## Decision\nPIVOT\n## Justification\nHypotheses flawed."
        assert rc_executor._parse_decision(text) == "pivot"

    def test_refine_detected(self) -> None:
        text = "## Decision\nREFINE\n## Justification\nNeed more tuning."
        assert rc_executor._parse_decision(text) == "refine"

    def test_pivot_case_insensitive(self) -> None:
        text = "## Decision\npivot\n## Justification\nBad approach."
        assert rc_executor._parse_decision(text) == "pivot"

    def test_pivot_takes_priority_over_proceed(self) -> None:
        text = "## Decision\nPIVOT\nWe should not PROCEED."
        assert rc_executor._parse_decision(text) == "pivot"

    def test_decision_in_body_not_heading(self) -> None:
        text = "The results suggest we should PIVOT to a new approach."
        assert rc_executor._parse_decision(text) == "pivot"


class TestResearchDecisionStructured:
    def test_execute_stage_fails_when_decision_model_is_unavailable(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.setattr(rc_executor, "_create_configured_llm", lambda _config: None)
        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis.load_canonical_experiment_evidence",
            lambda _run_dir: SimpleNamespace(),
        )

        result = rc_executor.execute_stage(
            Stage.RESEARCH_DECISION,
            run_dir=run_dir,
            run_id="no-decision-model",
            config=rc_config,
            adapters=adapters,
        )

        assert result.status is StageStatus.FAILED
        assert result.error == "decision_model_unavailable"
        stage15 = run_dir / "stage-15"
        assert not (stage15 / "decision.md").exists()
        assert not (stage15 / "decision_structured.json").exists()
        assert not (stage15 / "critique.json").exists()

    def test_execute_stage_fails_when_decision_model_construction_raises(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        def _raise(_cls: type[object], _config: RCConfig) -> None:
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(rc_executor.LLMClient, "from_rc_config", _raise)
        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis.load_canonical_experiment_evidence",
            lambda _run_dir: SimpleNamespace(),
        )
        result = rc_executor.execute_stage(
            Stage.RESEARCH_DECISION,
            run_dir=run_dir,
            run_id="decision-model-error",
            config=rc_config,
            adapters=adapters,
        )

        assert result.status is StageStatus.FAILED
        assert result.error == "decision_model_unavailable"
        stage15 = run_dir / "stage-15"
        assert not (stage15 / "decision.md").exists()
        assert not (stage15 / "decision_structured.json").exists()

    def test_fixed_domain_decision_uses_projection_prompt_and_binding(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        stage15 = run_dir / "stage-15"
        stage15.mkdir(parents=True)
        evidence = SimpleNamespace(
            manifest={"schema_version": 2, "generation_kind": "domain_evaluator"},
            manifest_path="canonical_experiment_evidence.json",
            manifest_sha256="a" * 64,
            analysis_text="narrow analysis",
            metric_observations={"auprc": (Decimal("0.1"), Decimal("0.2"))},
        )
        projection = SimpleNamespace(
            schema_version=1,
            policy_version="domain_evaluator_decision_v1",
            sha256="b" * 64,
            prompt_text='{"deterministic_gates":{"evidence_completeness":true}}\n',
        )
        captured: dict[str, str] = {}
        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis.load_canonical_experiment_evidence",
            lambda _run_dir: evidence,
        )
        monkeypatch.setattr(
            "researchclaw.pipeline.stage15_critique.load_canonical_experiment_evidence",
            lambda _run_dir: evidence,
        )
        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis.build_stage15_decision_projection",
            lambda _evidence: projection,
        )

        def _chat(_llm: object, system: str, user: str, **_kwargs: object) -> SimpleNamespace:
            captured["system"] = system
            captured["user"] = user
            return SimpleNamespace(
                content=(
                    "## Decision\nPROCEED\n## Justification\nComplete.\n"
                    "## Evidence\nBound.\n## Next Actions\nDraft.\n"
                )
            )

        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis._chat_with_prompt", _chat
        )
        result = rc_executor._execute_research_decision(
            stage15,
            run_dir,
            rc_config,
            adapters,
            llm=SimpleNamespace(),
        )

        assert result.status is StageStatus.DONE
        assert projection.prompt_text in captured["user"]
        assert "subjective analysis-quality score" in captured["user"]
        payload = json.loads((stage15 / "decision_structured.json").read_text())
        assert payload["decision_projection_sha256"] == projection.sha256
        assert payload["decision_policy_version"] == projection.policy_version

    def test_decision_produces_structured_json(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-15"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 14, "analysis.md", "# Analysis\nResults ok.")
        fake_llm = FakeLLMClient("## Decision\nPROCEED\n## Justification\nGood.")
        result = rc_executor._execute_research_decision(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )
        assert result.decision == "proceed"
        assert "decision_structured.json" in result.artifacts
        import json
        data = json.loads((stage_dir / "decision_structured.json").read_text())
        assert data["decision"] == "proceed"

    def test_pivot_decision_from_llm(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-15"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 14, "analysis.md", "# Analysis\nBad results.")
        fake_llm = FakeLLMClient("## Decision\nPIVOT\n## Justification\nFlawed.")
        result = rc_executor._execute_research_decision(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )
        assert result.decision == "pivot"

    def test_no_llm_fails_without_publishing_decision(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-15"
        stage_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "researchclaw.pipeline.stage_impls._analysis.load_canonical_experiment_evidence",
            lambda _run_dir: SimpleNamespace(),
        )
        result = rc_executor._execute_research_decision(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )
        assert result.status is StageStatus.FAILED
        assert result.decision == "retry"
        assert result.error == "decision_model_unavailable"
        assert result.artifacts == ()
        assert not (stage_dir / "decision.md").exists()
        assert not (stage_dir / "decision_structured.json").exists()

    def test_ambiguous_llm_response_pauses(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        # Inconclusive LLM prose with no PROCEED/PIVOT/REFINE keyword must
        # pause the pipeline rather than silently advancing as "proceed".
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-15"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 14, "analysis.md", "# Analysis\nMixed results.")
        fake_llm = FakeLLMClient(
            "After reviewing the results, the experimental evidence is "
            "inconclusive and additional data collection is needed."
        )
        result = rc_executor._execute_research_decision(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )
        assert result.status == StageStatus.PAUSED
        assert result.decision == "undecided"
        import json
        data = json.loads((stage_dir / "decision_structured.json").read_text())
        assert data["decision"] is None
        assert data["decision_parse_failed"] is True

    def test_agent_requirements_decision_uses_parseable_proceed_shape(self) -> None:
        from researchclaw.pipeline.stage_impls._analysis import (
            _format_agent_decision_md,
        )

        text = _format_agent_decision_md(
            {"verdict": "accept", "per_requirement": []},
            "proceed",
            retry_count=0,
            rerun_triggered=False,
        )
        assert rc_executor._parse_decision(text) == "proceed"


def _governed_stage9_config(config: RCConfig) -> RCConfig:
    return replace(
        config,
        research=replace(
            config.research,
            topic="Hardware-performance-counter detection of Spectre attacks",
        ),
        experiment=replace(
            config.experiment,
            metric_key="detection_f1",
            metric_direction="maximize",
        ),
    )


def _prepare_stage9_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stage_dir = run_dir / "stage-09"
    stage_dir.mkdir()
    _write_prior_artifact(
        run_dir, 8, "hypotheses.md", "# Hypotheses\n\nSecurity detection.\n"
    )
    return run_dir, stage_dir


def _valid_stage9_plan() -> dict[str, object]:
    return {
        "objectives": ["Evaluate detection"],
        "datasets": ["synthetic traces"],
        "baselines": ["threshold detector"],
        "proposed_methods": ["change point detector"],
        "ablations": ["without normalization"],
        "metrics": ["detection_f1"],
        "risks": ["distribution shift"],
    }


class _Stage9PostHITLSession:
    def __init__(
        self,
        action: HumanAction,
        *,
        edited: bool = False,
        guidance: str = "",
        on_wait: object | None = None,
        pause_before: bool = False,
        pause_after: bool = True,
    ) -> None:
        self.action = action
        self.edited = edited
        self.guidance = guidance
        self.on_wait = on_wait
        self.pause_before = pause_before
        self.pause_after = pause_after
        self.config = SimpleNamespace(cost_budget_usd=0.0)

    def should_pause_before(self, _stage: int) -> bool:
        return self.pause_before

    def should_pause_after(self, _stage: int) -> bool:
        return self.pause_after

    def pause(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_human(self) -> HumanInput:
        if callable(self.on_wait):
            self.on_wait()
        return HumanInput(
            action=self.action,
            guidance=self.guidance,
            edited_files={"exp_plan.yaml": "changed"} if self.edited else {},
        )

    def get_policy(self, _stage: int) -> SimpleNamespace:
        return SimpleNamespace(require_approval=False, min_quality_score=0.0)


class TestExperimentDesignGuard:
    # The schema-deficit guard added in _execute_experiment_design uses
    # _normalize_plan_field so that valid non-list field shapes (str, dict,
    # list[str], list[dict]) — which the rest of the file already supports
    # via _normalize_plan_field at the trim/conditions logic — do NOT
    # falsely trigger a PAUSED outcome.

    def test_normalize_string_returns_non_empty(self) -> None:
        from researchclaw.pipeline.stage_impls._experiment_design import _normalize_plan_field
        assert _normalize_plan_field("standard ResNet baseline") == [
            "standard ResNet baseline"
        ]

    def test_normalize_dict_returns_non_empty(self) -> None:
        from researchclaw.pipeline.stage_impls._experiment_design import _normalize_plan_field
        result = _normalize_plan_field({"groupnorm": "GroupNorm replacement"})
        assert len(result) == 1
        assert result[0]["name"] == "groupnorm"

    def test_normalize_none_returns_empty(self) -> None:
        from researchclaw.pipeline.stage_impls._experiment_design import _normalize_plan_field
        assert _normalize_plan_field(None) == []

    def test_normalize_empty_string_returns_empty(self) -> None:
        from researchclaw.pipeline.stage_impls._experiment_design import _normalize_plan_field
        assert _normalize_plan_field("") == []

    def test_empty_dict_response_pauses(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        # When the LLM returns "{}" the plan parses to an empty dict, every
        # fallback cascade is skipped (plan is never None), and the new
        # schema-deficit guard must pause rather than ship an exp_plan.yaml
        # with nothing but topic.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-09"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(
            run_dir, 8, "hypotheses.md",
            "# Hypotheses\n\nGeneral statements with no extractable method names.\n",
        )
        fake_llm = FakeLLMClient("{}")
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            _governed_stage9_config(rc_config),
            adapters,
            llm=fake_llm,
        )
        assert result.status == StageStatus.PAUSED
        assert result.decision == "schema_deficient"
        assert (stage_dir / "plan_meta.json").exists()
        assert not (stage_dir / "exp_plan.yaml").exists()
        import json
        meta = json.loads((stage_dir / "plan_meta.json").read_text())
        assert meta["outcome"] == "model_response_schema_deficient"
        assert set(meta["missing_required_keys"]) == {
            "baselines", "proposed_methods", "ablations",
        }

    def test_exact_experiment_plan_wrapper_is_unwrapped(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        from dataclasses import replace
        from researchclaw.experiment_runtime.contract import find_stage09_contract

        rc_config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="Hardware-performance-counter detection of Spectre attacks",
            ),
            experiment=replace(
                rc_config.experiment,
                metric_key="detection_f1",
                metric_direction="maximize",
            ),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-09"
        stage_dir.mkdir()
        (stage_dir / "exp_plan.yaml").write_text("stale: true\n", encoding="utf-8")
        (stage_dir / "experiment_contract.yaml").write_text(
            "stale pre-pivot contract\n", encoding="utf-8"
        )
        (stage_dir / "experiment_contract.sha256").write_text(
            "0" * 64 + "\n", encoding="utf-8"
        )
        _write_prior_artifact(
            run_dir, 8, "hypotheses.md", "# Hypotheses\n\nSecurity detection.\n"
        )
        wrapped = {
            "experiment_plan": {
                "objectives": ["Evaluate detection"],
                "datasets": ["synthetic traces"],
                "baselines": ["threshold detector"],
                "proposed_methods": ["change point detector"],
                "ablations": ["without normalization"],
                "metrics": ["detection_f1"],
                "risks": ["distribution shift"],
                "compute_budget": ["300 seconds"],
            }
        }
        fake_llm = FakeLLMClient(json.dumps(wrapped))

        result = rc_executor._execute_experiment_design(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )

        assert result.status == StageStatus.DONE
        plan = yaml.safe_load((stage_dir / "exp_plan.yaml").read_text())
        assert plan["baselines"] == ["threshold detector"]
        assert "experiment_plan" not in plan
        assert "stale pre-pivot contract" not in (
            stage_dir / "experiment_contract.yaml"
        ).read_text(encoding="utf-8")
        assert find_stage09_contract(run_dir) == (
            stage_dir / "experiment_contract.yaml"
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"experiment_plan": "not a mapping"},
            {"experiment_plan": {"objectives": ["only"]}},
            {
                "experiment_plan": {
                    "baselines": ["baseline"],
                    "proposed_methods": ["method"],
                    "ablations": ["ablation"],
                },
                "extra": "sibling prevents unwrapping",
            },
        ],
    )
    def test_invalid_or_ambiguous_experiment_plan_wrapper_pauses(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        payload: dict,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-09"
        stage_dir.mkdir()
        _write_prior_artifact(
            run_dir, 8, "hypotheses.md", "# Hypotheses\n\nNo method names.\n"
        )

        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            _governed_stage9_config(rc_config),
            adapters,
            llm=FakeLLMClient(json.dumps(payload)),
        )

        assert result.status == StageStatus.PAUSED
        assert result.decision == "schema_deficient"
        assert not (stage_dir / "experiment_contract.yaml").exists()

    def test_parent_replacement_between_snapshots_and_contract_fails_closed(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = _governed_stage9_config(rc_config)
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        detached = run_dir / "stage-09-detached"
        original = module.dump_contract

        def replace_before_contract(contract, path, *, namespace=None):
            stage_dir.rename(detached)
            stage_dir.mkdir()
            return original(contract, path, namespace=namespace)

        monkeypatch.setattr(module, "dump_contract", replace_before_contract)
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert list(stage_dir.iterdir()) == []
        assert list(detached.iterdir()) == []

    def test_domain_evaluator_stage9_publishes_v3_after_six_snapshot_replay(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
                dataset_origin="synthetic",
                metric_key="auprc",
                metric_direction="maximize",
                mode="sandbox",
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        llm = FakeLLMClient("{}")
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,
        )

        assert result.status == StageStatus.DONE, result.error
        assert llm.calls == []
        plan = yaml.safe_load((stage_dir / "exp_plan.yaml").read_text())
        assert plan["mode"] == "fixed_domain_evaluator"
        assert plan["baselines"] == [
            "raw_cc1",
            "scoap_isolation_forest",
        ]
        assert plan["proposed_methods"] == ["trojnet_community_graphsage"]
        assert plan["ablations"] == []
        assert plan["seeds"] == [0, 1, 2]
        assert plan["circuit_families"] == [
            "c1355",
            "c1908",
            "c3540",
            "c432",
            "c6288",
            "c880",
        ]
        assert plan["metrics"] == [
            "accuracy",
            "auprc",
            "auroc",
            "f1",
            "fpr",
            "precision",
            "recall",
            "top_k_precision",
        ]
        contract = load_contract(stage_dir / "experiment_contract.yaml")
        assert contract.schema_version == 3
        assert contract.evaluator_authority["kind"] == "domain_evaluator"
        assert {
            "domain_evaluator_package_manifest.json",
            "domain_evaluator_execution_policy.json",
        }.issubset(result.artifacts)
        assert (stage_dir / "experiment_contract.sha256").is_file()

    def test_domain_evaluator_stage9_executor_never_constructs_llm(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
                dataset_origin="synthetic",
                metric_key="auprc",
                metric_direction="maximize",
                mode="sandbox",
            ),
        )
        run_dir, _stage_dir = _prepare_stage9_run(tmp_path)

        def unexpected_llm(_config):
            raise AssertionError("fixed Stage 9 attempted to construct an LLM")

        monkeypatch.setattr(rc_executor, "_create_configured_llm", unexpected_llm)
        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-domain-zero-llm",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.DONE, result.error
        plan = yaml.safe_load(
            (run_dir / "stage-09/exp_plan.yaml").read_text(encoding="utf-8")
        )
        assert plan["mode"] == "fixed_domain_evaluator"

    def test_generic_stage9_executor_resolves_deferred_llm_on_first_chat(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir, _stage_dir = _prepare_stage9_run(tmp_path)
        llm = FakeLLMClient(json.dumps(_valid_stage9_plan()))
        monkeypatch.setattr(
            rc_executor, "_create_configured_llm", lambda _config: llm
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-generic-deferred-llm",
            config=_governed_stage9_config(rc_config),
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.DONE, result.error
        assert len(llm.calls) >= 1
        contract = load_contract(run_dir / "stage-09/experiment_contract.yaml")
        assert contract.schema_version == 2

    def test_domain_evaluator_stage9_rejects_late_plan_mutation(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
                dataset_origin="synthetic",
                metric_key="auprc",
                metric_direction="maximize",
                mode="sandbox",
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        original = module.dump_contract

        def mutate_plan_after_contract(contract, path, *, namespace=None):
            digest = original(contract, path, namespace=namespace)
            assert namespace is not None
            plan = yaml.safe_load(namespace.read_bytes("exp_plan.yaml"))
            plan["proposed_methods"] = ["shadow_method"]
            namespace.write_text_atomic(
                "exp_plan.yaml",
                yaml.safe_dump(plan, sort_keys=False),
            )
            return digest

        monkeypatch.setattr(module, "dump_contract", mutate_plan_after_contract)
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient("{}"),
        )

        assert result.status == StageStatus.FAILED
        assert "fixed domain evaluator experiment plan mismatch" in result.error
        assert list(stage_dir.iterdir()) == []

    def test_domain_evaluator_selector_failure_is_zero_llm_and_fail_closed(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.experiment_runtime.metric_authority import (
            MetricAuthorityError,
        )
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        llm = FakeLLMClient(json.dumps(_valid_stage9_plan()))

        def reject_selector(*_args, **_kwargs):
            raise MetricAuthorityError("injected trusted selector failure")

        monkeypatch.setattr(module, "select_metric_authority", reject_selector)
        result = rc_executor._execute_experiment_design(
            stage_dir, run_dir, config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert "injected trusted selector failure" in result.error
        assert llm.calls == []
        assert list(stage_dir.iterdir()) == []

    def test_domain_evaluator_execute_stage_selector_failure_has_zero_side_effects(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.experiment_runtime import metric_authority
        from researchclaw.experiment_runtime.metric_authority import (
            MetricAuthorityError,
        )
        from researchclaw.agents import benchmark_agent

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        (stage_dir / "experiment_contract.yaml").write_text(
            "stale contract\n", encoding="utf-8"
        )

        def reject_selector(*_args, **_kwargs):
            raise MetricAuthorityError("injected execute-stage selector failure")

        def unexpected_llm(_config):
            raise AssertionError("selector failure constructed an LLM")

        class UnexpectedBenchmark:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("selector failure constructed BenchmarkAgent")

        monkeypatch.setattr(metric_authority, "select_metric_authority", reject_selector)
        monkeypatch.setattr(rc_executor, "_create_configured_llm", unexpected_llm)
        monkeypatch.setattr(
            benchmark_agent, "BenchmarkOrchestrator", UnexpectedBenchmark
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-selector-failure-zero-side-effects",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert "injected execute-stage selector failure" in result.error
        assert not (stage_dir / "experiment_contract.yaml").exists()

    @pytest.mark.parametrize(
        ("selector_generations", "expected_llm_calls"),
        [
            (("v1", "v2"), 1),
            (("v2", "v1"), 0),
            (("v2", "v1", "v2"), 0),
        ],
    )
    def test_domain_evaluator_selector_generation_change_is_fail_closed(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
        selector_generations: tuple[str, ...],
        expected_llm_calls: int,
    ) -> None:
        from researchclaw.experiment_runtime import metric_authority
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        v2_config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
                dataset_origin="synthetic",
                metric_key="auprc",
                metric_direction="maximize",
                mode="sandbox",
            ),
        )
        v1_config = replace(
            _governed_stage9_config(rc_config),
            experiment=replace(
                _governed_stage9_config(rc_config).experiment,
                mode="sandbox",
            ),
        )
        config = v1_config if selector_generations[0] == "v1" else v2_config
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        real_selector = metric_authority.select_metric_authority
        selections = {
            "v2": real_selector(
                v2_config.research.topic, v2_config.experiment.mode
            ),
            "v1": real_selector(
                v1_config.research.topic, v1_config.experiment.mode
            ),
        }
        pending = list(selector_generations)
        calls: list[str] = []

        def changing_selector(*_args, **_kwargs):
            generation = pending.pop(0)
            calls.append(generation)
            return selections[generation]

        llm = FakeLLMClient(json.dumps(_valid_stage9_plan()))
        factory_calls = 0

        def configured_llm(_config):
            nonlocal factory_calls
            factory_calls += 1
            return llm

        monkeypatch.setattr(
            metric_authority, "select_metric_authority", changing_selector
        )
        monkeypatch.setattr(module, "select_metric_authority", changing_selector)
        monkeypatch.setattr(rc_executor, "_create_configured_llm", configured_llm)

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-selector-generation-change",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert result.artifacts == ()
        assert calls == list(selector_generations[:2])
        assert factory_calls == expected_llm_calls
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_classification_mismatch_always_withdraws_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.experiment_runtime import metric_authority
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(
                rc_config.experiment,
                claim_scope="pipeline_validation",
                dataset_origin="synthetic",
                metric_key="auprc",
                metric_direction="maximize",
                mode="sandbox",
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        real_selector = metric_authority.select_metric_authority
        v2_selection = real_selector(config.research.topic, config.experiment.mode)
        v1_selection = real_selector(
            "Hardware-performance-counter detection of Spectre attacks",
            config.experiment.mode,
        )
        selections = iter((v1_selection, v2_selection))
        changing_selector = lambda *_args, **_kwargs: next(selections)
        monkeypatch.setattr(
            metric_authority, "select_metric_authority", changing_selector
        )
        monkeypatch.setattr(module, "select_metric_authority", changing_selector)
        original_executor = rc_executor._STAGE_EXECUTORS[Stage.EXPERIMENT_DESIGN]

        def divergent_executor(*args, **kwargs):
            kwargs["authority_selection"] = v2_selection
            return original_executor(*args, **kwargs)

        monkeypatch.setitem(
            rc_executor._STAGE_EXECUTORS,
            Stage.EXPERIMENT_DESIGN,
            divergent_executor,
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-classification-mismatch",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert "Stage 9 authority classification failed" in result.error
        assert result.artifacts == ()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_skips_baseline_navigator_symlink(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        external = tmp_path / "external-hitl"
        external.mkdir()
        (external / "sentinel").write_text("unchanged", encoding="utf-8")
        (run_dir / "hitl").symlink_to(external, target_is_directory=True)

        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient("{}"),
        )

        assert result.status == StageStatus.DONE, result.error
        assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
        assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"

    @pytest.mark.parametrize("action", [HumanAction.SKIP, HumanAction.ABORT])
    def test_domain_evaluator_pre_hitl_withdraws_stale_authority_before_return(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        action: HumanAction,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        first = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-seed-stale-authority",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )
        assert first.status == StageStatus.DONE, first.error
        assert (stage_dir / "experiment_contract.yaml").is_file()

        session = _Stage9PostHITLSession(
            action,
            pause_before=True,
            pause_after=False,
        )
        second = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-pre-hitl-stop",
            config=config,
            adapters=replace(adapters, hitl=session),
            auto_approve_gates=True,
        )

        assert second.status == StageStatus.FAILED
        assert second.artifacts == ()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    @pytest.mark.parametrize(
        ("action", "edited", "guidance"),
        [
            (HumanAction.EDIT, True, ""),
            (HumanAction.REJECT, False, ""),
            (HumanAction.ABORT, False, ""),
            (HumanAction.APPROVE, False, "change the fixed plan"),
        ],
    )
    def test_domain_evaluator_post_hitl_rejects_mutation_and_withdraws_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        action: HumanAction,
        edited: bool,
        guidance: str,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)

        def mutate_plan() -> None:
            if edited:
                (stage_dir / "exp_plan.yaml").write_text(
                    "mode: shadow\n", encoding="utf-8"
                )

        session = _Stage9PostHITLSession(
            action,
            edited=edited,
            guidance=guidance,
            on_wait=mutate_plan,
        )
        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-post-hitl-guard",
            config=config,
            adapters=replace(adapters, hitl=session),
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert result.artifacts == ()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_post_hitl_approve_fresh_replay_rejects_mutation(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)

        def mutate_without_declaring_edit() -> None:
            (stage_dir / "exp_plan.yaml").write_text(
                "mode: shadow\n", encoding="utf-8"
            )

        session = _Stage9PostHITLSession(
            HumanAction.APPROVE,
            on_wait=mutate_without_declaring_edit,
        )
        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-post-hitl-fixpoint",
            config=config,
            adapters=replace(adapters, hitl=session),
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert "terminal replay failed" in result.error
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_post_hitl_stage_replacement_cleans_detached_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        detached = run_dir / "stage-09-detached"
        external = tmp_path / "external-stage9"
        external.mkdir()
        (external / "sentinel").write_text("unchanged", encoding="utf-8")

        def replace_stage_during_wait() -> None:
            stage_dir.rename(detached)
            stage_dir.symlink_to(external, target_is_directory=True)

        session = _Stage9PostHITLSession(
            HumanAction.EDIT,
            edited=True,
            on_wait=replace_stage_during_wait,
        )
        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-post-hitl-replacement",
            config=config,
            adapters=replace(adapters, hitl=session),
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
        assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"
        assert not any(
            (detached / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )
        stage_dir.unlink()
        detached.rename(stage_dir)
        assert not (stage_dir / "experiment_contract.yaml").exists()

    def test_domain_evaluator_prm_rejection_withdraws_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module
        from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
            metaclaw_bridge=replace(
                rc_config.metaclaw_bridge,
                enabled=True,
                prm=replace(
                    rc_config.metaclaw_bridge.prm,
                    enabled=True,
                    gate_stages=(9,),
                ),
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        rejecting_gate = SimpleNamespace(
            model="rejecting-test-gate",
            votes=1,
            evaluate_stage=lambda *_args: -1.0,
        )
        monkeypatch.setattr(
            ResearchPRMGate,
            "from_bridge_config",
            lambda *_args, **_kwargs: rejecting_gate,
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-prm-reject",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert "PRM quality gate" in result.error
        assert result.artifacts == ()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_prm_rejection_survives_report_collision(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
            metaclaw_bridge=replace(
                rc_config.metaclaw_bridge,
                enabled=True,
                prm=replace(
                    rc_config.metaclaw_bridge.prm,
                    enabled=True,
                    gate_stages=(9,),
                ),
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        (stage_dir / "prm_score.json").mkdir()
        monkeypatch.setattr(
            ResearchPRMGate,
            "from_bridge_config",
            lambda *_args, **_kwargs: SimpleNamespace(
                model="rejecting-collision-gate",
                votes=1,
                evaluate_stage=lambda *_args: -1.0,
            ),
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-prm-report-collision",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert "PRM quality gate" in result.error
        assert result.artifacts == ()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_domain_evaluator_prm_rejection_cleans_before_post_hitl_wait(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
            metaclaw_bridge=replace(
                rc_config.metaclaw_bridge,
                enabled=True,
                prm=replace(
                    rc_config.metaclaw_bridge.prm,
                    enabled=True,
                    gate_stages=(9,),
                ),
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        monkeypatch.setattr(
            ResearchPRMGate,
            "from_bridge_config",
            lambda *_args, **_kwargs: SimpleNamespace(
                model="reject-before-wait",
                votes=1,
                evaluate_stage=lambda *_args: -1.0,
            ),
        )

        def assert_clean_during_wait() -> None:
            assert not any(
                (stage_dir / name).exists()
                for name in module._STAGE9_AUTHORITY_OUTPUTS
            )

        session = _Stage9PostHITLSession(
            HumanAction.APPROVE,
            on_wait=assert_clean_during_wait,
        )
        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-prm-clean-before-hitl",
            config=config,
            adapters=replace(adapters, hitl=session),
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert result.artifacts == ()

    def test_domain_evaluator_prm_replacement_uses_held_namespace(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = replace(
            rc_config,
            research=replace(
                rc_config.research,
                topic="TrojNet hardware Trojan localization on ISCAS-85 circuits",
            ),
            experiment=replace(rc_config.experiment, mode="sandbox"),
            metaclaw_bridge=replace(
                rc_config.metaclaw_bridge,
                enabled=True,
                prm=replace(
                    rc_config.metaclaw_bridge.prm,
                    enabled=True,
                    gate_stages=(9,),
                ),
            ),
        )
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        detached = run_dir / "stage-09-detached"
        external = tmp_path / "external-prm"
        external.mkdir()
        (external / "sentinel").write_text("unchanged", encoding="utf-8")

        def reject_after_replacement(*_args) -> float:
            stage_dir.rename(detached)
            stage_dir.symlink_to(external, target_is_directory=True)
            return -1.0

        rejecting_gate = SimpleNamespace(
            model="replacement-test-gate",
            votes=1,
            evaluate_stage=reject_after_replacement,
        )
        monkeypatch.setattr(
            ResearchPRMGate,
            "from_bridge_config",
            lambda *_args, **_kwargs: rejecting_gate,
        )

        result = rc_executor.execute_stage(
            Stage.EXPERIMENT_DESIGN,
            run_dir=run_dir,
            run_id="stage9-fixed-prm-replacement",
            config=config,
            adapters=adapters,
            auto_approve_gates=True,
        )

        assert result.status == StageStatus.FAILED
        assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
        assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"
        assert not any(
            (detached / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )
        stage_dir.unlink()
        detached.rename(stage_dir)
        assert not (stage_dir / "experiment_contract.yaml").exists()

    def test_diagnostic_collision_cannot_preserve_stale_stage9_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        nested = stage_dir / "benchmark_agent" / "nested"
        nested.mkdir(parents=True)
        for name in module._STAGE9_AUTHORITY_OUTPUTS:
            (stage_dir / name).write_text("stale authority\n", encoding="utf-8")

        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            _governed_stage9_config(rc_config),
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert nested.is_dir()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )

    def test_parent_replacement_before_sidecar_never_writes_external_target(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = _governed_stage9_config(rc_config)
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        detached = run_dir / "stage-09-detached"
        external = tmp_path / "external"
        external.mkdir()
        (external / "sentinel").write_text("unchanged", encoding="utf-8")
        original = BoundOutputNamespace.write_text_atomic

        def replace_before_sidecar(namespace, name, text):
            if name == "experiment_contract.sha256":
                stage_dir.rename(detached)
                stage_dir.symlink_to(external, target_is_directory=True)
            return original(namespace, name, text)

        monkeypatch.setattr(
            BoundOutputNamespace, "write_text_atomic", replace_before_sidecar
        )
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
        assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"
        assert list(detached.iterdir()) == []

    def test_parent_replacement_after_cleanup_cannot_split_stage9_generation(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = _governed_stage9_config(rc_config)
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)
        detached = run_dir / "stage-09-detached"
        original = module._cleanup_stage9_outputs
        calls = 0

        def replace_after_cleanup(namespace, *, preserve_diagnostics=()):
            nonlocal calls
            original(namespace, preserve_diagnostics=preserve_diagnostics)
            calls += 1
            if calls == 1:
                stage_dir.rename(detached)
                stage_dir.mkdir()

        monkeypatch.setattr(module, "_cleanup_stage9_outputs", replace_after_cleanup)
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert list(stage_dir.iterdir()) == []
        assert list(detached.iterdir()) == []

    def test_final_stage9_replay_failure_removes_complete_commit_point(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.experiment_runtime.metric_authority import (
            MetricAuthorityError,
        )
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = _governed_stage9_config(rc_config)
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)

        def reject_replay(**_kwargs):
            raise MetricAuthorityError("injected final replay failure")

        monkeypatch.setattr(
            module, "_replay_metric_authority_selection", reject_replay
        )
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert list(stage_dir.iterdir()) == []

    def test_final_failure_collision_still_invalidates_stage9_authority(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.experiment_runtime.metric_authority import (
            MetricAuthorityError,
        )
        from researchclaw.pipeline.stage_impls import _experiment_design as module

        config = _governed_stage9_config(rc_config)
        run_dir, stage_dir = _prepare_stage9_run(tmp_path)

        def reject_with_collision(_namespace, _run_dir, _config):
            (stage_dir / "benchmark_agent" / "nested").mkdir(parents=True)
            raise MetricAuthorityError("injected final replay failure")

        monkeypatch.setattr(
            module, "_validate_stage9_publication", reject_with_collision
        )
        result = rc_executor._execute_experiment_design(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=FakeLLMClient(json.dumps(_valid_stage9_plan())),
        )

        assert result.status == StageStatus.FAILED
        assert (stage_dir / "benchmark_agent" / "nested").is_dir()
        assert not any(
            (stage_dir / name).exists()
            for name in module._STAGE9_AUTHORITY_OUTPUTS
        )


class TestResourcePlanningFallback:
    def test_wrong_schema_falls_back_to_template(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        # A parseable wrong-schema dict (no `tasks` key) must trigger the
        # template fallback rather than silently being accepted as the
        # schedule. The output must still satisfy the contract (DONE +
        # `tasks` populated) and record the source in `_meta`.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-11"
        stage_dir.mkdir(parents=True)
        fake_llm = FakeLLMClient('{"unrelated_key": "value"}')
        result = rc_executor._execute_resource_planning(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )
        assert result.status == StageStatus.DONE
        import json
        schedule = json.loads((stage_dir / "schedule.json").read_text())
        assert isinstance(schedule.get("tasks"), list)
        assert len(schedule["tasks"]) >= 2
        assert schedule["_meta"]["source"] == "template"


class TestMultiPerspectiveGenerate:
    def test_generates_all_perspectives(self, tmp_path: Path) -> None:
        roles = {
            "role_a": {"system": "You are A.", "user": "Do A for {topic}."},
            "role_b": {"system": "You are B.", "user": "Do B for {topic}."},
        }
        fake_llm = FakeLLMClient("perspective output")
        perspectives_dir = tmp_path / "perspectives"
        result = rc_executor._multi_perspective_generate(
            fake_llm, roles, {"topic": "test"}, perspectives_dir
        )
        assert set(result.keys()) == {"role_a", "role_b"}
        assert (perspectives_dir / "role_a.md").exists()
        assert (perspectives_dir / "role_b.md").exists()
        assert len(fake_llm.calls) == 2

    def test_saves_perspective_content(self, tmp_path: Path) -> None:
        roles = {"critic": {"system": "Be critical.", "user": "Criticize {topic}."}}
        fake_llm = FakeLLMClient("critical analysis here")
        perspectives_dir = tmp_path / "perspectives"
        rc_executor._multi_perspective_generate(
            fake_llm, roles, {"topic": "ml"}, perspectives_dir
        )
        content = (perspectives_dir / "critic.md").read_text()
        assert content == "critical analysis here"

    def test_renders_variables_in_prompts(self, tmp_path: Path) -> None:
        roles = {"r1": {"system": "Sys for {topic}.", "user": "User for {topic}."}}
        fake_llm = FakeLLMClient("ok")
        rc_executor._multi_perspective_generate(
            fake_llm, roles, {"topic": "RL"}, tmp_path / "p"
        )
        call = fake_llm.calls[0]
        assert "RL" in call[0]["content"]


class TestSynthesizePerspectives:
    def test_combines_perspectives(self) -> None:
        fake_llm = FakeLLMClient("synthesized result")
        pm = rc_executor.PromptManager()
        perspectives = {"innovator": "idea A", "contrarian": "idea B"}
        result = rc_executor._synthesize_perspectives(
            fake_llm, perspectives, "hypothesis_synthesize", pm
        )
        assert result == "synthesized result"
        # Check the user prompt contained both perspectives
        call_content = fake_llm.calls[0][0]["content"]
        assert "innovator" in call_content
        assert "contrarian" in call_content


class TestHypothesisGenDebate:
    def test_hypothesis_gen_with_llm_creates_perspectives(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-08"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 7, "synthesis.md", "# Synthesis\nGap found.")
        fake_llm = FakeLLMClient("## H1\nTest hypothesis")
        result = rc_executor._execute_hypothesis_gen(
            stage_dir, run_dir, rc_config, adapters, llm=fake_llm
        )
        assert result.status == StageStatus.DONE
        assert "hypotheses.md" in result.artifacts
        perspectives_dir = stage_dir / "perspectives"
        assert perspectives_dir.exists()
        # Should have 3 perspective files (innovator, pragmatist, contrarian)
        perspective_files = list(perspectives_dir.glob("*.md"))
        assert len(perspective_files) == 3

    def test_hypothesis_gen_without_llm_no_perspectives(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-08"
        stage_dir.mkdir(parents=True)
        _write_prior_artifact(run_dir, 7, "synthesis.md", "# Synthesis\nGap found.")
        result = rc_executor._execute_hypothesis_gen(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )
        assert result.status == StageStatus.DONE
        assert "hypotheses.md" in result.artifacts
        # No perspectives directory when no LLM
        assert not (stage_dir / "perspectives").exists()


class TestResultAnalysisDebate:
    def test_result_analysis_rejects_legacy_run_inputs(
        self,
        tmp_path: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

        monkeypatch.setattr(
            capabilities,
            "CANONICAL_EVIDENCE_CAPABILITIES",
            {name: 1 for name in capabilities.REQUIRED_CAPABILITIES},
        )
        run_dir = tmp_path / "run"
        legacy_runs = run_dir / "stage-12/runs"
        legacy_runs.mkdir(parents=True)
        (legacy_runs / "run-1.json").write_text(
            json.dumps({"status": "completed", "metrics": {"detection_f1": 0.99}}),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-14"
        stage_dir.mkdir()

        result = rc_executor._execute_result_analysis(
            stage_dir, run_dir, rc_config, adapters, llm=None
        )

        assert result.status == StageStatus.FAILED
        assert "Stage 12 result set is missing" in (result.error or "")
        assert not (run_dir / "canonical_experiment_evidence.json").exists()


    def test_release_audit_json_repair_handles_markdown_fence(self) -> None:
        data = release_audit._loads_json_repaired(
            '```json\n{"claims": [{"text": "x", "type": "result"}]}\n```',
            {},
        )
        assert data["claims"][0]["text"] == "x"

    def test_release_audit_json_repair_handles_comments_and_trailing_commas(self) -> None:
        data = release_audit._loads_json_repaired(
            '{\n  // model note\n  "claims": [{"text": "x", "type": "result",}],\n}',
            {},
        )
        assert data["claims"][0]["type"] == "result"

    def test_release_audit_json_repair_extracts_embedded_json(self) -> None:
        data = release_audit._loads_json_repaired(
            'Here is the audit:\n{"claims": [{"text": "x", "type": "result"}]}\nThanks.',
            {},
        )
        assert data["claims"][0]["text"] == "x"

    def test_release_audit_json_repair_preserves_valid_string_content(self) -> None:
        data = release_audit._loads_json_repaired(
            '{"claims": [{"text": "literal ,} in a string", "type": "result"}]}',
            {},
        )
        assert data["claims"][0]["text"] == "literal ,} in a string"

    def test_truth_audit_preflight_blocks_legacy_claim_fallback(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage23 = run_dir / "stage-23"
        stage23.mkdir(parents=True)
        paper_text = "# Paper\n\n## Results\n\nOur method reaches 0.1234 F1 on the synthetic benchmark."
        (stage23 / "paper_final_verified.md").write_text(paper_text, encoding="utf-8")
        stage_dir = run_dir / "stage-24"
        stage_dir.mkdir(parents=True)
        llm = FakeLLMClient('{"claims": []}')
        monkeypatch.setattr(
            "researchclaw.pipeline.stage24_publication.load_stage24_input_bundle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("invalid canonical graph")
            ),
        )

        result = rc_executor._execute_truth_audit(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert "invalid canonical graph" in (result.error or "")
        assert llm.calls == []
        assert tuple(stage_dir.iterdir()) == ()

    def test_truth_audit_preflight_blocks_legacy_empty_claim_path(
        self, tmp_path: Path, rc_config: RCConfig, adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage23 = run_dir / "stage-23"
        stage23.mkdir(parents=True)
        paper_text = "# Paper\n\n## Introduction\n\nThis document contains only generic background prose."
        (stage23 / "paper_final_verified.md").write_text(paper_text, encoding="utf-8")
        stage_dir = run_dir / "stage-24"
        stage_dir.mkdir(parents=True)
        llm = FakeLLMClient("not json")
        monkeypatch.setattr(
            "researchclaw.pipeline.stage24_publication.load_stage24_input_bundle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("invalid canonical graph")
            ),
        )

        result = rc_executor._execute_truth_audit(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert "invalid canonical graph" in (result.error or "")
        assert llm.calls == []
        assert tuple(stage_dir.iterdir()) == ()


class TestParseMetricsFromStdout:
    """Tests for _parse_metrics_from_stdout() helper."""

    def test_parses_simple_name_value(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = "loss: 0.0042\naccuracy: 0.95"
        metrics = _parse_metrics_from_stdout(stdout)
        assert metrics["loss"] == pytest.approx(0.0042)
        assert metrics["accuracy"] == pytest.approx(0.95)

    def test_parses_compound_names(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = "UCB (Stochastic) cumulative_regret: 361.9233\nEXP3 (Adversarial) total_rewards: 13368.4811"
        metrics = _parse_metrics_from_stdout(stdout)
        assert "UCB (Stochastic) cumulative_regret" in metrics
        assert metrics["UCB (Stochastic) cumulative_regret"] == pytest.approx(361.9233)

    def test_ignores_non_numeric_lines(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = "Running experiment...\nloss: 0.5\nDone."
        metrics = _parse_metrics_from_stdout(stdout)
        assert len(metrics) == 1
        assert metrics["loss"] == pytest.approx(0.5)

    def test_empty_stdout_returns_empty_dict(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        assert _parse_metrics_from_stdout("") == {}

    def test_handles_negative_values(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = "UCB (Adversarial) cumulative_regret: -3877.5323"
        metrics = _parse_metrics_from_stdout(stdout)
        assert metrics["UCB (Adversarial) cumulative_regret"] == pytest.approx(-3877.5323)

    def test_filters_log_lines(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = (
            "Running experiments for support set size: 1\n"
            "Loading model weights: 42\n"
            "Training epoch: 5\n"
            "loss: 0.123\n"
            "accuracy: 0.95\n"
        )
        metrics = _parse_metrics_from_stdout(stdout)
        assert "loss" in metrics
        assert "accuracy" in metrics
        assert len(metrics) == 2  # log lines should be excluded

    def test_filters_long_name_lines(self) -> None:
        from researchclaw.pipeline.executor import _parse_metrics_from_stdout

        stdout = "this is a very long status message that should not be a metric: 42\n"
        metrics = _parse_metrics_from_stdout(stdout)
        assert len(metrics) == 0


class TestDetectRuntimeIssues:
    """Tests for _detect_runtime_issues() helper."""

    def _make_sandbox_result(
        self,
        metrics: dict | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        from types import SimpleNamespace

        return SimpleNamespace(
            metrics=metrics or {},
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            elapsed_sec=1.0,
            timed_out=False,
        )

    def test_no_issues_returns_empty_string(self) -> None:
        r = self._make_sandbox_result(metrics={"loss": 0.5}, stdout="loss: 0.5")
        assert rc_executor._detect_runtime_issues(r) == ""

    def test_detects_nan_in_metrics(self) -> None:
        r = self._make_sandbox_result(metrics={"loss": float("nan")})
        result = rc_executor._detect_runtime_issues(r)
        assert "NaN" in result
        assert "loss" in result

    def test_detects_inf_in_metrics(self) -> None:
        r = self._make_sandbox_result(metrics={"loss": float("inf")})
        result = rc_executor._detect_runtime_issues(r)
        assert "Inf" in result

    def test_detects_nan_in_stdout(self) -> None:
        r = self._make_sandbox_result(stdout="accuracy: nan\nloss: 0.5")
        result = rc_executor._detect_runtime_issues(r)
        assert "NaN" in result or "nan" in result

    def test_detects_runtime_warning_in_stderr(self) -> None:
        stderr = (
            "optimizers.py:76: RuntimeWarning: invalid value encountered in divide\n"
            "  directions = np.vstack((directions[1:], new_direction / norm))\n"
        )
        r = self._make_sandbox_result(stderr=stderr)
        result = rc_executor._detect_runtime_issues(r)
        assert "RuntimeWarning" in result
        assert "invalid value" in result

    def test_detects_division_error_in_stderr(self) -> None:
        stderr = "ZeroDivisionError: division by zero\n"
        r = self._make_sandbox_result(stderr=stderr)
        result = rc_executor._detect_runtime_issues(r)
        assert "Error" in result

    def test_ignores_benign_stderr(self) -> None:
        # Non-warning stderr should be ignored
        r = self._make_sandbox_result(stderr="Loading module...\nDone.\n")
        assert rc_executor._detect_runtime_issues(r) == ""

    def test_combined_nan_and_stderr(self) -> None:
        r = self._make_sandbox_result(
            metrics={"accuracy": float("nan")},
            stderr="RuntimeWarning: invalid value\n",
        )
        result = rc_executor._detect_runtime_issues(r)
        assert "NaN" in result
        assert "RuntimeWarning" in result

    def test_detects_dummy_metric_identical_values(self) -> None:
        stdout = (
            "UCB (Stochastic) convergence_rate: 1.0000\n"
            "UCB (Adversarial) convergence_rate: 1.0000\n"
            "Thompson (Stochastic) convergence_rate: 1.0000\n"
            "Thompson (Adversarial) convergence_rate: 1.0000\n"
        )
        r = self._make_sandbox_result(stdout=stdout)
        result = rc_executor._detect_runtime_issues(r)
        assert "DUMMY" in result
        assert "convergence_rate" in result

    def test_no_dummy_metric_when_values_differ(self) -> None:
        stdout = (
            "UCB (Stochastic) regret: 78.5\n"
            "Thompson (Stochastic) regret: 121.0\n"
            "EpsilonGreedy (Stochastic) regret: 42.1\n"
        )
        r = self._make_sandbox_result(stdout=stdout)
        result = rc_executor._detect_runtime_issues(r)
        assert "DUMMY" not in result


class TestRemoveBibtexEntries:
    """Tests for _remove_bibtex_entries() helper."""

    def test_removes_specified_keys(self) -> None:
        bib = (
            '@article{smith2024,\n  title={Good Paper},\n  author={Smith},\n}\n\n'
            '@article{venus2024,\n  title={Venus Exploration},\n  author={NASA},\n}\n'
        )
        result = rc_executor._remove_bibtex_entries(bib, {"venus2024"})
        assert "smith2024" in result
        assert "venus2024" not in result

    def test_keeps_all_when_no_match(self) -> None:
        bib = '@article{smith2024,\n  title={Paper},\n}\n'
        result = rc_executor._remove_bibtex_entries(bib, {"other_key"})
        assert "smith2024" in result

    def test_empty_bib(self) -> None:
        assert rc_executor._remove_bibtex_entries("", {"key"}) == ""


class TestCollectRawExperimentMetrics:
    """Tests for _collect_raw_experiment_metrics() helper."""

    def test_returns_empty_when_no_runs(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        block, has_parsed = rc_executor._collect_raw_experiment_metrics(run_dir)
        assert block == ""
        assert not has_parsed

    def test_ignores_unbound_stdout_metrics(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True)
        payload = {
            "metrics": {},
            "stdout": "UCB regret: 361.92\nThompson regret: 576.24\n",
        }
        (runs_dir / "run-1.json").write_text(json.dumps(payload))
        result, has_parsed = rc_executor._collect_raw_experiment_metrics(run_dir)
        assert result == ""
        assert not has_parsed

    def test_extracts_from_metrics_dict(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True)
        payload = {"metrics": {"loss": 0.042, "accuracy": 0.95}, "stdout": ""}
        (runs_dir / "run-1.json").write_text(json.dumps(payload))
        result, has_parsed = rc_executor._collect_raw_experiment_metrics(run_dir)
        assert "loss" in result
        assert "0.042" in result
        assert has_parsed

    def test_deduplicates_metrics(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True)
        payload = {
            "metrics": {"loss": 0.5},
            "stdout": "loss: 0.5\nloss: 0.5\n",
        }
        (runs_dir / "run-1.json").write_text(json.dumps(payload))
        result, _ = rc_executor._collect_raw_experiment_metrics(run_dir)
        assert result.count("metric_observations.loss[0]: 0.5") == 1


class TestCollectExperimentEvidence:
    """Tests for the snapshot-only peer-review evidence renderer."""

    def test_renders_only_selected_snapshot(self) -> None:
        evidence = SimpleNamespace(
            manifest_path="canonical_experiment_evidence.json",
            manifest_sha256="a" * 64,
            selected_result={"result_set_type": "stage12_baseline"},
            metric_observations={"detection_f1": (Decimal("0.5"),)},
            structured_results={"metrics": {"detection_f1": Decimal("0.5")}},
            summary={"metrics_summary": {"detection_f1": {"mean": Decimal("0.5")}}},
            analysis_text="Canonical analysis only.\n",
        )

        result = rc_executor._collect_experiment_evidence(evidence)

        assert "Canonical Evidence Identity" in result
        assert "detection_f1" in result
        assert "Canonical analysis only." in result
        assert "stage12_baseline" in result

    def test_does_not_read_legacy_artifact_tree(self, tmp_path: Path) -> None:
        legacy = tmp_path / "stage-12" / "runs"
        legacy.mkdir(parents=True)
        (legacy / "poison.json").write_text(
            '{"metrics":{"poison_metric":999}}', encoding="utf-8"
        )
        evidence = SimpleNamespace(
            manifest_path="canonical_experiment_evidence.json",
            manifest_sha256="b" * 64,
            selected_result={"result_set_type": "stage13_refinement"},
            metric_observations={"safe_metric": (Decimal("0.5"),)},
            structured_results={"metrics": {"safe_metric": Decimal("0.5")}},
            summary={},
            analysis_text="",
        )

        result = rc_executor._collect_experiment_evidence(evidence)

        assert "safe_metric" in result
        assert "poison_metric" not in result


class TestWritePaperSections:
    """Tests for _write_paper_sections() multi-call writing."""

    def test_produces_three_part_draft(self) -> None:
        call_count = {"n": 0}
        parts = [
            "## Test Title\n\n## Abstract\nTest abstract.\n\n## Introduction\nTest intro.\n\n## Related Work\nTest related.",
            "## Method\nTest method.\n\n## Experiments\nTest experiments.",
            "## Results\nTest results.\n\n## Discussion\nTest discussion.\n\n## Limitations\nTest limits.\n\n## Conclusion\nTest conclusion.",
        ]

        class MultiCallLLM:
            def __init__(self):
                self.calls: list = []

            def chat(self, messages, **kwargs):
                self.calls.append(messages)
                from researchclaw.llm.client import LLMResponse
                idx = len(self.calls) - 1
                return LLMResponse(content=parts[min(idx, 2)], model="fake")

        llm = MultiCallLLM()
        from researchclaw.prompts import PromptManager
        pm = PromptManager()

        draft = rc_executor._write_paper_sections(
            llm=llm,
            pm=pm,
            preamble="Test preamble",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="Test outline",
        )

        assert llm.calls is not None
        assert len(llm.calls) == 3
        assert "## Abstract" in draft
        assert "## Method" in draft
        assert "## Results" in draft
        assert "## Conclusion" in draft

    def test_metric_instruction_reaches_all_three_writing_calls(self) -> None:
        class TrackingLLM:
            def __init__(self):
                self.user_prompts: list[str] = []
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages, **kwargs):
                for m in messages:
                    if m.get("role") == "user":
                        self.user_prompts.append(m["content"])
                from researchclaw.llm.client import LLMResponse
                return LLMResponse(content=next(self.responses), model="fake")

        llm = TrackingLLM()
        from researchclaw.prompts import PromptManager
        pm = PromptManager()

        rc_executor._write_paper_sections(
            llm=llm,
            pm=pm,
            preamble="Preamble",
            topic_constraint="",
            exp_metrics_instruction="GROUNDED METRIC VALUE WHITELIST: detection_f1 = 0.4753",
            citation_instruction="",
            outline="Outline",
        )

        assert len(llm.user_prompts) == 3
        assert all("detection_f1 = 0.4753" in p for p in llm.user_prompts)

    def test_each_call_receives_prior_context(self) -> None:
        class ContextTrackingLLM:
            def __init__(self):
                self.user_prompts: list[str] = []
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages, **kwargs):
                for m in messages:
                    if m.get("role") == "user":
                        self.user_prompts.append(m["content"])
                from researchclaw.llm.client import LLMResponse
                return LLMResponse(content=next(self.responses), model="fake")

        llm = ContextTrackingLLM()
        from researchclaw.prompts import PromptManager
        pm = PromptManager()

        rc_executor._write_paper_sections(
            llm=llm,
            pm=pm,
            preamble="Preamble",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="Outline",
        )

        assert len(llm.user_prompts) == 3
        # Call 2 and 3 should contain "sections written so far"
        assert "sections written so far" in llm.user_prompts[1]
        assert "completing a paper" in llm.user_prompts[2]


class TestLoadHardwareProfile:
    """Tests for _load_hardware_profile()."""

    @pytest.fixture()
    def run_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "run"
        d.mkdir()
        return d

    def test_loads_valid_profile(self, run_dir: Path) -> None:
        stage = run_dir / "stage-01"
        stage.mkdir()
        profile = {"has_gpu": True, "gpu_type": "mps", "tier": "limited"}
        (stage / "hardware_profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )
        result = rc_executor._load_hardware_profile(run_dir)
        assert result is not None
        assert result["gpu_type"] == "mps"

    def test_returns_none_when_missing(self, run_dir: Path) -> None:
        assert rc_executor._load_hardware_profile(run_dir) is None

    def test_returns_none_on_invalid_json(self, run_dir: Path) -> None:
        stage = run_dir / "stage-01"
        stage.mkdir()
        (stage / "hardware_profile.json").write_text("not json", encoding="utf-8")
        assert rc_executor._load_hardware_profile(run_dir) is None


class TestExpandSearchQueries:
    """Tests for _expand_search_queries()."""

    def test_adds_broader_queries(self) -> None:
        queries = ["gradient descent optimization algorithms"]
        topic = "Comparing gradient descent optimization algorithms on benchmark functions"
        result = rc_executor._expand_search_queries(queries, topic)
        assert len(result) > len(queries)

    def test_deduplicates(self) -> None:
        queries = ["gradient descent survey"]
        topic = "gradient descent optimization"
        result = rc_executor._expand_search_queries(queries, topic)
        lowered = [q.lower().strip() for q in result]
        assert len(lowered) == len(set(lowered))

    def test_preserves_original_queries(self) -> None:
        queries = ["query A", "query B"]
        topic = "some research topic about machine learning methods"
        result = rc_executor._expand_search_queries(queries, topic)
        assert result[0] == "query A"
        assert result[1] == "query B"

    def test_adds_survey_benchmark_variants(self) -> None:
        queries = ["deep learning"]
        topic = "deep learning for image classification with limited data"
        result = rc_executor._expand_search_queries(queries, topic)
        has_survey = any("survey" in q.lower() for q in result)
        has_benchmark = any("benchmark" in q.lower() for q in result)
        assert has_survey
        assert has_benchmark


# ── R4-1: Experiment Budget Guard Tests ──────────────────────────────


class TestComputeBudgetBlock:
    """Test compute_budget prompt block injection (R4-1a)."""

    def test_compute_budget_block_exists_in_prompt_manager(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        block = pm.block("compute_budget")
        assert "time_budget_sec" in block or "Compute Budget" in block

    def test_compute_budget_injected_into_code_generation(
        self, tmp_path: Path, run_dir: Path, adapters: AdapterBundle
    ) -> None:
        import sys

        data = {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {
                "topic": (
                    "Hardware-performance-counter runtime detection of Spectre "
                    "and transient-execution attacks"
                ),
                "domains": ["security"],
                "daily_paper_count": 2,
                "quality_threshold": 8.2,
            },
            "runtime": {"timezone": "UTC"},
            "notifications": {
                "channel": "local",
                "on_stage_start": True,
                "on_stage_fail": False,
                "on_gate_required": True,
            },
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {"use_memory": True, "use_message": True},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "security": {"hitl_required_stages": [5, 9, 20]},
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 60,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "sandbox": {
                    "python_path": sys.executable,
                    "gpu_required": False,
                    "max_memory_mb": 1024,
                },
            },
        }
        cfg = RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)

        # Write exp_plan prior artifact
        _write_prior_artifact(run_dir, 10, "exp_plan.yaml", "objectives: test")
        _write_experiment_contract(run_dir, cfg)

        # Capture what the LLM receives
        llm = FakeLLMClient(
            "```filename:main.py\nimport numpy as np\nprint('detection_f1: 0.1')\n```"
        )
        stage_dir = run_dir / "stage-11"
        stage_dir.mkdir(parents=True, exist_ok=True)

        rc_executor._execute_code_generation(
            stage_dir, run_dir, cfg, adapters, llm=llm
        )

        # The LLM should have received compute budget info in some call
        # (may be first call in legacy mode, or second call with CodeAgent)
        assert len(llm.calls) >= 1
        all_user_msgs = " ".join(
            call[-1]["content"] for call in llm.calls if call
        )
        assert "60" in all_user_msgs or "Compute Budget" in all_user_msgs


class TestPartialTimeoutStatus:
    """Test partial status for timed-out experiments with data (R4-1c)."""

    def test_timed_out_with_metrics_sets_partial_status(
        self, tmp_path: Path, run_dir: Path, adapters: AdapterBundle
    ) -> None:
        import sys

        data = {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {
                "topic": (
                    "Hardware-performance-counter runtime detection of Spectre "
                    "and transient-execution attacks"
                ),
                "domains": ["security"],
                "daily_paper_count": 2,
                "quality_threshold": 8.2,
            },
            "runtime": {"timezone": "UTC"},
            "notifications": {
                "channel": "local",
                "on_stage_start": True,
                "on_stage_fail": False,
                "on_gate_required": True,
            },
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {"use_memory": True, "use_message": True},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "security": {"hitl_required_stages": [5, 9, 20]},
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 2,
                "allow_legacy_experiment_path": True,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "sandbox": {
                    "python_path": sys.executable,
                    "gpu_required": False,
                    "max_memory_mb": 1024,
                },
            },
        }
        cfg = RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)

        # Write sealed candidate code that prints some metrics then sleeps
        _write_sealed_candidate(
            run_dir,
            cfg,
            "import time, sys\n"
            "print('detection_f1: 0.5', flush=True)\n"
            "sys.stdout.flush()\n"
            "time.sleep(10)\n",
        )

        stage_dir = run_dir / "stage-12"
        stage_dir.mkdir(parents=True, exist_ok=True)

        result = rc_executor._execute_experiment_run(
            stage_dir, run_dir, cfg, adapters
        )

        assert result.status == StageStatus.FAILED
        assert not (stage_dir / "experiment_result_set.json").exists()
        journal = [
            json.loads(line)
            for line in (stage_dir / "execution_invocation_journal.jsonl")
            .read_text(encoding="utf-8")
            .split("\n")
            if line
        ]
        assert journal[-1]["status"] == "failed"


class TestDataIntegrityBlock:
    """Test paper draft blocked when no metrics exist (R4-2a)."""

    def test_paper_draft_blocked_with_no_metrics(
        self, tmp_path: Path, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Write prior artifacts with NO metrics
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        # No experiment_summary.json, no run files with metrics
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-1.json").write_text(
            json.dumps({"run_id": "run-1", "status": "failed", "metrics": {}, "timed_out": True}),
            encoding="utf-8",
        )

        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)

        # Ensure domain detection returns an empirical domain so the block triggers
        from researchclaw.pipeline.stage_impls import _paper_writing
        monkeypatch.setattr(
            _paper_writing, "_detect_domain",
            lambda topic, domains=(): ("ml", "machine learning", "NeurIPS, ICML, ICLR"),
        )

        llm = FakeLLMClient("should not be called")
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        # PAUSED with explicit decision + meta artifact (cleanup of the
        # previous FAILED + "unknown error" framing).
        assert result.status == StageStatus.PAUSED
        assert result.decision == "blocked_no_metrics"
        assert "no real metrics" in (result.error or "")
        draft = (stage_dir / "paper_draft.md").read_text(encoding="utf-8")
        assert "Blocked" in draft or "BLOCKED" in draft or "no metrics" in draft.lower()
        meta = json.loads((stage_dir / "paper_meta.json").read_text(encoding="utf-8"))
        assert meta["outcome"] == "blocked_no_metrics"
        # LLM should NOT have been called
        assert len(llm.calls) == 0

    def test_unbound_simulated_files_do_not_enter_writer_evidence(
        self, tmp_path: Path, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle,
    ) -> None:
        # Legacy run files are diagnostic-only after canonical migration.
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (runs_dir / f"run-{i + 1}.json").write_text(
                json.dumps(
                    {
                        "run_id": f"run-{i + 1}",
                        "status": "simulated",
                        "key_metrics": {"primary_metric": 0.3 + i * 0.03},
                    }
                ),
                encoding="utf-8",
            )

        block, has_metrics = rc_executor._collect_raw_experiment_metrics(run_dir)
        assert block == ""
        assert has_metrics is False

    def test_paper_draft_proceeds_with_metrics(
        self, tmp_path: Path, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        # Write experiment data with real metrics
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-1.json").write_text(
            json.dumps({
                "run_id": "run-1",
                "status": "completed",
                "metrics": {"best_loss": 0.123},
                "stdout": "best_loss: 0.123\n",
            }),
            encoding="utf-8",
        )

        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)

        llm = FakeLLMClient("# Paper Title\n## Abstract\nSome abstract text.")
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        # Should proceed (LLM was called)
        assert len(llm.calls) >= 1
        # The prompt should contain anti-fabrication instructions
        all_prompts = " ".join(
            msg["content"] for call in llm.calls for msg in call
        )
        assert "Data Integrity" in all_prompts or "ONLY report numbers" in all_prompts

    def test_grounded_metric_whitelist_uses_selected_result_manifest(
        self, run_dir: Path
    ) -> None:
        from researchclaw.pipeline.stage_impls import _paper_writing

        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {
                        "detection_f1": 0.4753327669,
                        "fpr": 0.0291666667,
                    },
                    "per_seed": [
                        {"seed": 42, "metrics": {"detection_f1": 0.486}},
                    ],
                }
            ),
            encoding="utf-8",
        )

        block = _paper_writing._collect_grounded_metric_whitelist(run_dir)

        assert "GROUNDED METRIC VALUE WHITELIST" in block
        assert (
            "stage-12/experiment_result_set.json :: structured_results.metrics.detection_f1 = 0.4753327669"
            in block
        )
        assert (
            "stage-12/experiment_result_set.json :: structured_results.metrics.fpr = 0.0291666667"
            in block
        )
        assert "Do NOT introduce any other decimal metric values" in block

    def test_paper_draft_injects_scaffold_results_into_first_prompt(
        self, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )

        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)

        class SequentialPaperLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper Title\n\n## Abstract\n\nAbstract text.\n\n"
                        "## Introduction\n\nIntroduction.\n\n## Related Work\n\nPrior work.",
                        "## Method\n\nMethod text.\n\n## Experiments\n\nSetup text.",
                        "## Results\n\nResults text.\n\n## Discussion\n\nDiscussion.\n\n"
                        "## Limitations\n\nLimitations.\n\n## Conclusion\n\nConclusion text.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        llm = SequentialPaperLLM()
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.DONE
        assert len(llm.calls) >= 1
        first_prompt = " ".join(m["content"] for m in llm.calls[0])
        assert "GROUNDED METRIC VALUE WHITELIST" in first_prompt
        assert "detection_f1 = 0.4753" in first_prompt

    def test_paper_draft_rejects_duplicate_heading_paths(
        self, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        llm = FakeLLMClient("## Method\n\nRepeated method text.")

        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        generation = json.loads(
            (stage_dir / "section_generation_report.json").read_text(encoding="utf-8")
        )
        assert generation["parts"][0]["attempts"][-1]["valid"] is False
        assert "section_part_major_sequence_mismatch" in generation["parts"][0][
            "attempts"
        ][-1]["violations"]
        assert not (stage_dir / "paper_draft.md").exists()
        assert (stage_dir / "paper_draft_invalid.md").exists()

    def test_paper_draft_hitl_structure_failure_keeps_only_invalid_draft(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "hitl_guidance.md").write_text("Improve clarity.", encoding="utf-8")

        class HitlSequenceLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Abstract\n\nDuplicate.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        result = rc_executor._execute_paper_draft(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=HitlSequenceLLM(),
        )

        assert result.status == StageStatus.FAILED
        assert not (stage_dir / "paper_draft.md").exists()
        assert (stage_dir / "paper_draft_invalid.md").exists()
        report = json.loads((stage_dir / "paper_structure_report.json").read_text())
        assert "duplicate_heading_path" in {
            issue["code"] for issue in report["issues"]
        }

    def test_domain_stage17_rejects_unscoped_hitl_without_rewrite_call(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.pipeline.stage_impls import _paper_writing

        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps({"metrics": {"detection_f1": 0.4753327669}}),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "hitl_guidance.md").write_text(
            "Rewrite all result claims.", encoding="utf-8"
        )
        monkeypatch.setattr(
            _paper_writing, "build_canonical_fact_sheet", lambda _evidence: {"active": True}
        )
        monkeypatch.setattr(
            _paper_writing,
            "build_heading_grounding_contexts",
            lambda _cfs, headings: {heading: "CFS_BOUND" for heading in headings},
        )

        class DomainHitlLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nR.\n\n## Discussion\n\nD.",
                        "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        llm = DomainHitlLLM()
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert "does not accept unscoped HITL" in str(result.error)
        assert len(llm.calls) == 0
        assert not (stage_dir / "paper_draft.md").exists()

    def test_paper_draft_closure_failure_removes_canonical_artifacts(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.literature.experiment_fact_closure import (
            ExperimentFactClosureError,
        )
        from researchclaw.pipeline.stage_impls import _paper_writing

        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)

        class ValidSequenceLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        monkeypatch.setattr(
            _paper_writing,
            "build_experiment_fact_closure_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ExperimentFactClosureError("forced closure failure")
            ),
        )
        result = rc_executor._execute_paper_draft(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=ValidSequenceLLM(),
        )

        assert result.status == StageStatus.FAILED
        assert "closure failed" in (result.error or "")
        assert (stage_dir / "paper_draft_invalid.md").exists()
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            assert not (stage_dir / name).exists()

    def test_domain_v2_batch_preflight_failure_clears_success_authority(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.literature.citation_plan import CitationPlanContractError
        from researchclaw.pipeline.stage_impls import _paper_writing

        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            (stage_dir / name).write_text("stale authority", encoding="utf-8")

        def fail_preflight(*_args: object, **_kwargs: object) -> str:
            raise CitationPlanContractError(
                "single citation anchor exceeds prompt budget"
            )

        monkeypatch.setattr(_paper_writing, "_write_paper_sections", fail_preflight)
        llm = FakeLLMClient("must not be called")

        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert "citation batch preflight failed" in (result.error or "").lower()
        assert llm.calls == []
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            assert not (stage_dir / name).exists()

    def test_domain_v2_post_generation_authority_drift_clears_success_authority(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.literature.citation_plan import CitationPlanContractError
        from researchclaw.pipeline.stage_impls import _paper_writing

        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        captured = SimpleNamespace(
            replayed=SimpleNamespace(
                effective_policy={
                    "effective_min_unique_sources": 1,
                    "effective_target_unique_sources": 1,
                },
                plan={"claims": []},
                cards=(),
            )
        )
        draft = (
            "## Title\n\nPaper.\n\n"
            "## Abstract\n\nA.\n\n"
            "## Introduction\n\nI.\n\n"
            "## Related Work\n\nR.\n\n"
            "## Method\n\nM.\n\n"
            "## Experiments\n\nE.\n\n"
            "## Results\n\nR.\n\n"
            "## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n"
            "## Conclusion\n\nC."
        )

        monkeypatch.setattr(
            _paper_writing, "build_canonical_fact_sheet", lambda _evidence: {"active": True}
        )
        monkeypatch.setattr(
            _paper_writing,
            "_capture_replayed_citation_authority",
            lambda *_args, **_kwargs: captured,
        )
        monkeypatch.setattr(
            _paper_writing,
            "build_heading_citation_writer_instructions_from_authority",
            lambda *_args, heading_names, **_kwargs: {
                heading: "None. Do not add citation markers."
                for heading in heading_names
            },
        )
        monkeypatch.setattr(
            _paper_writing,
            "project_citation_anchors",
            lambda _plan: (),
        )

        def write_draft(*_args: object, **kwargs: object) -> str:
            target = cast(Path, kwargs["stage_dir"])
            (target / "section_generation_report.json").write_text(
                json.dumps({"schema_version": 1, "parts": []}),
                encoding="utf-8",
            )
            return draft

        monkeypatch.setattr(_paper_writing, "_write_paper_sections", write_draft)
        verify_calls = 0

        def reject_drift(*_args: object, **_kwargs: object) -> None:
            nonlocal verify_calls
            verify_calls += 1
            raise CitationPlanContractError("captured citation authority changed")

        monkeypatch.setattr(
            _paper_writing,
            "verify_captured_citation_authority_unchanged",
            reject_drift,
        )

        result = rc_executor._execute_paper_draft(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=FakeLLMClient("unused"),
        )

        assert result.status == StageStatus.FAILED
        assert "authority changed" in (result.error or "").lower()
        assert verify_calls == 1
        assert (stage_dir / "paper_draft_invalid.md").exists()
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            assert not (stage_dir / name).exists()

    def test_domain_v2_code_owned_anchor_failure_clears_success_authority(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "claim_scope": "pipeline_validation",
                    "dataset_origin": "synthetic",
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            (stage_dir / name).write_text("stale authority", encoding="utf-8")

        anchor = CitationAnchor(
            claim_id="planned-claim-001",
            heading="Related Work",
            claim_text="Exact bounded claim.",
            cite_key="smith2024deep",
        )

        class NoCitationCallLLM(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://primary.invalid/v1",
                        api_key="test-key",
                        primary_model="primary-model",
                    )
                )
                self.calls: list[str] = []

            def chat(
                self,
                messages: list[dict[str, str]],
                **kwargs: object,
            ) -> LLMResponse:
                del messages, kwargs
                self.calls.append("citation")
                raise AssertionError("code-owned citation anchor called the provider")

        llm = NoCitationCallLLM()

        def write_invalid_scaffold(
            *_args: object,
            **kwargs: object,
        ) -> str:
            return _paper_writing._write_batched_domain_v2_paper_sections(
                llm=cast(LLMClient, kwargs["llm"]),
                system="system",
                groups=(("Related Work",),),
                grounding_contexts={"Related Work": "CFS_BOUND"},
                citation_anchors=(anchor,),
                model_name="primary-model",
                stage_dir=cast(Path, kwargs["stage_dir"]),
            )

        monkeypatch.setattr(
            _paper_writing,
            "_write_paper_sections",
            write_invalid_scaffold,
        )
        monkeypatch.setattr(
            _paper_writing,
            "validate_citation_free_anchor_fragment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CitationPlanContractError("forced code-owned anchor failure")
            ),
        )

        result = rc_executor._execute_paper_draft(
            stage_dir,
            run_dir,
            rc_config,
            adapters,
            llm=llm,
        )

        assert result.status == StageStatus.FAILED
        assert "forced code-owned anchor failure" in (result.error or "")
        assert llm.calls == []
        assert not (stage_dir / "paper_draft_invalid.md").exists()
        for name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            assert not (stage_dir / name).exists()

    def test_paper_draft_fact_repair_closes_before_publish(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        invalid_report = {
            "paper_sha256": "a" * 64,
            "experiment_contract_path": "stage-09/experiment_contract.yaml",
            "experiment_contract_sha256": "b" * 64,
            "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
            "canonical_experiment_evidence_sha256": "d" * 64,
            "dataset_origin": "synthetic",
            "grounded_numeric_values": [Decimal("0.4753327669")],
            "manuscript_numeric_values": [Decimal("0.4753327669"), Decimal("0.47")],
            "unknown_numeric_values": [Decimal("0.47")],
            "dataset_claim_violations": ["SPEC CPU2006"],
            "valid": False,
        }
        valid_report = {**invalid_report, "paper_sha256": "c" * 64}
        valid_report.update(
            manuscript_numeric_values=[Decimal("0.4753327669")],
            unknown_numeric_values=[],
            dataset_claim_violations=[],
            valid=True,
        )
        reports = iter((invalid_report, valid_report))
        monkeypatch.setattr(
            _paper_writing,
            "build_experiment_fact_closure_report",
            lambda *_args, **_kwargs: next(reports),
        )

        repaired = (
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
            "## Introduction\n\nI.\n\n## Related Work\n\nR.\n\n"
            "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
            "## Results\n\n\n\n## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n## Conclusion\n\nC."
        )

        class FactSequenceLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nF1 was 0.4753327669; an unsupported summary said 0.47.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        llm = FactSequenceLLM()
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.DONE
        assert len(llm.calls) == 3
        assert (stage_dir / "paper_draft.md").read_text(encoding="utf-8") == repaired
        assert not (stage_dir / "experiment_fact_closure_invalid.json").exists()
        assert (stage_dir / "experiment_fact_closure_initial.json").exists()
        assert (stage_dir / "experiment_fact_closure_after.json").exists()
        repair_log = json.loads(
            (stage_dir / "experiment_fact_repair_log.json").read_text()
        )
        assert repair_log["strategy"] == "deterministic_block_removal"
        assert repair_log["operations"][0]["block_type"] == "sentence"

    def test_fact_repair_cannot_add_grounded_numeric_authority(self) -> None:
        before = {
            "manuscript_numeric_values": [Decimal("0.64")],
            "unknown_numeric_values": [Decimal("0.64")],
        }
        after = {
            "manuscript_numeric_values": [Decimal("0.639877")],
            "unknown_numeric_values": [],
        }
        assert _paper_writing._fact_repair_added_numeric_authority(
            before, after
        )

    def test_paper_draft_fact_repair_fails_once_with_diagnostic(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        invalid_report = {
            "paper_sha256": "a" * 64,
            "experiment_contract_path": "stage-09/experiment_contract.yaml",
            "experiment_contract_sha256": "b" * 64,
            "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
            "canonical_experiment_evidence_sha256": "d" * 64,
            "dataset_origin": "synthetic",
            "grounded_numeric_values": [Decimal("0.4753327669")],
            "manuscript_numeric_values": [Decimal("0.47")],
            "unknown_numeric_values": [Decimal("0.47")],
            "dataset_claim_violations": [],
            "valid": False,
        }
        monkeypatch.setattr(
            _paper_writing,
            "build_experiment_fact_closure_report",
            lambda *_args, **_kwargs: invalid_report,
        )

        class FactFailureLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nF1 was 0.47.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        llm = FactFailureLLM()
        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        assert result.status == StageStatus.FAILED
        assert len(llm.calls) == 3
        assert not (stage_dir / "paper_draft.md").exists()
        diagnostic = json.loads(
            (stage_dir / "experiment_fact_closure_invalid.json").read_text()
        )
        assert "valid" not in diagnostic
        assert diagnostic["unknown_numeric_values"] == [0.47]

    def test_paper_draft_fact_repair_reruns_structure_gate(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        invalid_report = {
            "paper_sha256": "a" * 64,
            "experiment_contract_path": "stage-09/experiment_contract.yaml",
            "experiment_contract_sha256": "b" * 64,
            "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
            "canonical_experiment_evidence_sha256": "d" * 64,
            "dataset_origin": "synthetic",
            "grounded_numeric_values": [Decimal("0.4753327669")],
            "manuscript_numeric_values": [Decimal("0.47")],
            "unknown_numeric_values": [Decimal("0.47")],
            "dataset_claim_violations": [],
            "valid": False,
        }
        calls = 0

        def _build_once(*_args: object, **_kwargs: object) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return invalid_report

        monkeypatch.setattr(
            _paper_writing, "build_experiment_fact_closure_report", _build_once
        )

        class BrokenStructureLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nF1 was 0.47.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=BrokenStructureLLM()
        )

        assert result.status == StageStatus.FAILED
        assert calls == 2
        assert not (stage_dir / "paper_draft.md").exists()
        structure = json.loads(
            (stage_dir / "paper_structure_report.json").read_text()
        )
        assert structure["valid"] is True
        assert (stage_dir / "experiment_fact_closure_invalid.json").exists()

    def test_paper_draft_fact_repair_reruns_citation_gate(
        self,
        run_dir: Path,
        rc_config: RCConfig,
        adapters: AdapterBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from researchclaw.literature.citation_plan import CitationPlanContractError

        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n## Abstract\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "results.json").write_text(
            json.dumps(
                {
                    "evaluator_owner": "scaffold",
                    "metrics": {"detection_f1": 0.4753327669},
                }
            ),
            encoding="utf-8",
        )
        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)
        invalid_report = {
            "paper_sha256": "a" * 64,
            "experiment_contract_path": "stage-09/experiment_contract.yaml",
            "experiment_contract_sha256": "b" * 64,
            "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
            "canonical_experiment_evidence_sha256": "d" * 64,
            "dataset_origin": "synthetic",
            "grounded_numeric_values": [Decimal("0.4753327669")],
            "manuscript_numeric_values": [Decimal("0.4753327669"), Decimal("0.47")],
            "unknown_numeric_values": [Decimal("0.47")],
            "dataset_claim_violations": [],
            "valid": False,
        }
        valid_report = {**invalid_report, "paper_sha256": "c" * 64}
        valid_report.update(
            manuscript_numeric_values=[Decimal("0.4753327669")],
            unknown_numeric_values=[],
            valid=True,
        )
        reports = iter((invalid_report, valid_report))
        monkeypatch.setattr(
            _paper_writing,
            "build_experiment_fact_closure_report",
            lambda *_args, **_kwargs: next(reports),
        )
        citation_checked = False

        def _build_citation(*_args: object, **kwargs: object) -> dict[str, bool]:
            nonlocal citation_checked
            citation_checked = True
            assert "[unplanned2024]" in str(kwargs["paper_text"])
            return {"valid": False}

        monkeypatch.setattr(
            _paper_writing, "build_citation_closure_report", _build_citation
        )
        monkeypatch.setattr(
            _paper_writing,
            "validate_citation_closure_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CitationPlanContractError("citation drift")
            ),
        )

        class CitationDriftLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.responses = iter(
                    (
                        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
                        "## Method\n\nM.\n\n## Experiments\n\nE.",
                        "## Results\n\nF1 was 0.4753327669 and 0.47. A separate claim remains [unplanned2024].\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
                    )
                )

            def chat(self, messages: list[dict[str, str]], **kwargs: object):
                self.response_text = next(self.responses)
                return super().chat(messages, **kwargs)

        result = rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=CitationDriftLLM()
        )

        assert result.status == StageStatus.FAILED
        assert citation_checked is True
        assert "citation drift" in (result.error or "")
        assert not (stage_dir / "paper_draft.md").exists()
        assert not (stage_dir / "citation_closure_report.json").exists()


# ── R4-3: Conference-Grade Title Guidelines Tests ────────────────────


class TestTitleGuidelines:
    """Test title_guidelines and abstract_structure blocks (R4-3)."""

    def test_title_guidelines_block_exists(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        block = pm.block("title_guidelines")
        assert "novelty" in block.lower() or "TITLE RULES" in block
        assert "14 words" in block or "15 words" in block or "concrete" in block.lower()

    def test_abstract_structure_block_exists(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        block = pm.block("abstract_structure")
        assert "5-sentence" in block or "problem" in block.lower()

    def test_title_guidelines_injected_into_paper_draft(
        self, tmp_path: Path, run_dir: Path, rc_config: RCConfig, adapters: AdapterBundle
    ) -> None:
        _write_prior_artifact(run_dir, 16, "outline.md", "# Outline\n")
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-1.json").write_text(
            json.dumps({"run_id": "run-1", "status": "completed",
                        "metrics": {"best_loss": 0.1}, "stdout": "best_loss: 0.1\n"}),
            encoding="utf-8",
        )

        stage_dir = run_dir / "stage-17"
        stage_dir.mkdir(parents=True, exist_ok=True)

        llm = FakeLLMClient("# Paper Title\n## Abstract\nText.")
        rc_executor._execute_paper_draft(
            stage_dir, run_dir, rc_config, adapters, llm=llm
        )

        all_prompts = " ".join(
            msg["content"] for call in llm.calls for msg in call
        )
        assert "Title" in all_prompts or "TITLE" in all_prompts


# ── R4-4: Conference-Grade Writing Quality Tests ─────────────────────


class TestConferenceWritingQuality:
    """Test enhanced writing prompts and writing_guide.py (R4-4)."""

    def test_writing_guide_format_all(self) -> None:
        from researchclaw.writing_guide import format_writing_tips

        result = format_writing_tips()
        assert "Conference Writing Best Practices" in result
        assert "Title" in result
        assert "Common Rejections" in result

    def test_writing_guide_format_subset(self) -> None:
        from researchclaw.writing_guide import format_writing_tips

        result = format_writing_tips(["title", "abstract"])
        assert "Title" in result
        assert "Abstract" in result
        assert "Common Rejections" not in result

    def test_paper_draft_system_includes_principles(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        sp = pm.for_stage(
            "paper_draft",
            preamble="test",
            topic_constraint="test",
            exp_metrics_instruction="test",
            citation_instruction="test",
            outline="test",
        )
        # System prompt should mention key principles
        assert "NOVELTY" in sp.system or "novelty" in sp.system.lower()
        assert "fabricate" in sp.system.lower() or "real experimental" in sp.system.lower()


# ── R5-1 & R5-2: Bug Fixes Tests ────────────────────────────────────


class TestNaNDivergenceDetection:
    """Test NaN/Inf filtering and divergence detection (R5-3)."""

    def test_parse_metrics_filters_nan(self) -> None:
        from researchclaw.experiment.sandbox import parse_metrics

        stdout = "best_loss: 0.5\nbad_metric: nan\ngood_metric: 1.23\n"
        metrics = parse_metrics(stdout)
        assert "best_loss" in metrics
        assert "good_metric" in metrics
        assert "bad_metric" not in metrics  # NaN should be filtered

    def test_parse_metrics_filters_inf(self) -> None:
        from researchclaw.experiment.sandbox import parse_metrics

        stdout = "metric_a: inf\nmetric_b: -inf\nmetric_c: 0.42\n"
        metrics = parse_metrics(stdout)
        assert "metric_c" in metrics
        assert "metric_a" not in metrics
        assert "metric_b" not in metrics

    def test_detect_nan_divergence_finds_nan(self) -> None:
        from researchclaw.experiment.sandbox import detect_nan_divergence

        result = detect_nan_divergence("loss: nan\nstep 5 done", "")
        assert result is not None
        assert "NaN" in result or "nan" in result.lower()

    def test_detect_nan_divergence_finds_diverging_loss(self) -> None:
        from researchclaw.experiment.sandbox import detect_nan_divergence

        result = detect_nan_divergence("best_loss: 999.5\n", "")
        assert result is not None
        assert "loss" in result.lower() or "999" in result

    def test_detect_nan_divergence_returns_none_for_clean(self) -> None:
        from researchclaw.experiment.sandbox import detect_nan_divergence

        result = detect_nan_divergence("best_loss: 0.123\naccuracy: 0.95\n", "")
        assert result is None

    def test_runtime_issues_detects_diverging_loss(self) -> None:
        from types import SimpleNamespace

        fake_result = SimpleNamespace(
            metrics={"best_loss": 500.0},
            stdout="best_loss: 500.0\n",
            stderr="",
        )
        issues = rc_executor._detect_runtime_issues(fake_result)
        assert "DIVERGING" in issues or "diverging" in issues.lower()

    def test_compute_budget_includes_nan_guard(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        block = pm.block("compute_budget")
        assert "NaN" in block or "nan" in block.lower() or "divergence" in block.lower()


# ── R5-4: Experiment Harness Template Tests ──────────────────────────


class TestExperimentHarness:
    """Test the immutable experiment harness (R5-4)."""

    def test_harness_should_stop(self) -> None:
        from researchclaw.experiment.harness_template import ExperimentHarness

        h = ExperimentHarness(time_budget=1)
        assert not h.should_stop()  # Just created, not at 80% yet
        import time
        time.sleep(0.9)
        assert h.should_stop()  # Should be past 80% of 1s

    def test_harness_report_metric(self, capsys: pytest.CaptureFixture[str]) -> None:
        from researchclaw.experiment.harness_template import ExperimentHarness

        h = ExperimentHarness(time_budget=60)
        h.report_metric("best_loss", 0.123)
        captured = capsys.readouterr()
        assert "best_loss: 0.123" in captured.out
        assert h._metrics["best_loss"] == 0.123

    def test_harness_rejects_nan(self, capsys: pytest.CaptureFixture[str]) -> None:
        from researchclaw.experiment.harness_template import ExperimentHarness

        h = ExperimentHarness(time_budget=60)
        h.report_metric("bad", float("nan"))
        captured = capsys.readouterr()
        assert "bad" not in h._metrics
        assert "non-finite" in captured.err.lower() or "WARNING" in captured.err

    def test_harness_rejects_inf(self, capsys: pytest.CaptureFixture[str]) -> None:
        from researchclaw.experiment.harness_template import ExperimentHarness

        h = ExperimentHarness(time_budget=60)
        h.report_metric("bad", float("inf"))
        assert "bad" not in h._metrics

    def test_harness_finalize(self, tmp_path: Path) -> None:
        import os
        from researchclaw.experiment.harness_template import ExperimentHarness

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            h = ExperimentHarness(time_budget=60)
            h.report_metric("accuracy", 0.95)
            h.report_metric("loss", 0.05)
            h.log_result({"condition": "A", "value": 1.0})
            h.finalize()

            results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
            assert results["metrics"]["accuracy"] == 0.95
            assert results["metrics"]["loss"] == 0.05
            assert len(results["results"]) == 1
        finally:
            os.chdir(old_cwd)

    def test_harness_progress(self) -> None:
        from researchclaw.experiment.harness_template import ExperimentHarness

        h = ExperimentHarness(time_budget=1000)
        assert h.progress < 0.01  # Just started
        assert 0.0 <= h.progress <= 1.0

    def test_harness_injected_into_sandbox(self, tmp_path: Path) -> None:
        import sys
        from researchclaw.config import SandboxConfig
        from researchclaw.experiment.sandbox import ExperimentSandbox

        config = SandboxConfig(python_path=sys.executable)
        sandbox = ExperimentSandbox(config, tmp_path / "sandbox")

        # Create a project dir
        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("print('test: 1.0')\n", encoding="utf-8")

        sandbox.run_project(project, timeout_sec=5)

        # Check that harness was injected (BUG-DA8-06: dir is now _project_{N})
        project_dirs = list((tmp_path / "sandbox").glob("_project_*"))
        assert project_dirs, "No _project_N directory found"
        harness_path = project_dirs[0] / "experiment_harness.py"
        assert harness_path.exists()
        content = harness_path.read_text(encoding="utf-8")
        assert "ExperimentHarness" in content

    def test_harness_not_overwritten_by_project(self, tmp_path: Path) -> None:
        import sys
        from researchclaw.config import SandboxConfig
        from researchclaw.experiment.sandbox import ExperimentSandbox

        config = SandboxConfig(python_path=sys.executable)
        sandbox = ExperimentSandbox(config, tmp_path / "sandbox")

        # Create a project with a fake experiment_harness.py
        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("print('test: 1.0')\n", encoding="utf-8")
        (project / "experiment_harness.py").write_text("# FAKE HARNESS", encoding="utf-8")

        sandbox.run_project(project, timeout_sec=5)

        # The real harness should be there, not the fake one (BUG-DA8-06)
        project_dirs = list((tmp_path / "sandbox").glob("_project_*"))
        assert project_dirs
        harness_path = project_dirs[0] / "experiment_harness.py"
        content = harness_path.read_text(encoding="utf-8")
        assert "ExperimentHarness" in content
        assert "FAKE HARNESS" not in content

    def test_prompt_mentions_harness(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        block = pm.block("compute_budget")
        assert "experiment_harness" in block or "ExperimentHarness" in block


# ── R5-5: Stdout Truncation Tests ────────────────────────────────────


class TestStdoutFailureDetection:
    """R6-2: Detect stdout failure signals even when exit code is 0."""

    def test_fail_signal_in_stdout_marks_failed(self, tmp_path: Path) -> None:
        """Exit code 0 + 'FAIL:' in stdout + no metrics → status='failed'."""
        from researchclaw.pipeline.executor import _execute_experiment_run

        # Create necessary structure
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "stage-11").mkdir()
        (run_dir / "stage-11" / "schedule.json").write_text("{}", encoding="utf-8")

        stage_dir = run_dir / "stage-12"
        stage_dir.mkdir()

        data = {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {"topic": "Hardware-performance-counter runtime detection of Spectre and transient-execution attacks", "domains": ["security"],
                         "daily_paper_count": 2, "quality_threshold": 8.2},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "local", "on_stage_start": True,
                              "on_stage_fail": False, "on_gate_required": True},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {"use_memory": True, "use_message": True},
            "llm": {"provider": "openai-compatible", "base_url": "http://localhost:1234/v1",
                    "api_key_env": "RC_TEST_KEY", "api_key": "inline-test-key",
                    "primary_model": "fake-model", "fallback_models": []},
            "security": {"hitl_required_stages": [5, 9, 20]},
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 30,
                "allow_legacy_experiment_path": True,
                "max_iterations": 1,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "sandbox": {
                    "python_path": sys.executable,
                    "gpu_required": False,
                    "max_memory_mb": 512,
                    "allowed_imports": ["json"],
                },
            },
        }
        cfg = RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)
        adapters = AdapterBundle()
        _write_sealed_candidate(
            run_dir,
            cfg,
            "print('FAIL: NaN/divergence detected')\n",
        )

        result = _execute_experiment_run(
            stage_dir, run_dir, cfg, adapters
        )

        assert result.status == StageStatus.FAILED
        assert not (stage_dir / "experiment_result_set.json").exists()
        journal = [
            json.loads(line)
            for line in (stage_dir / "execution_invocation_journal.jsonl")
            .read_text(encoding="utf-8")
            .split("\n")
            if line
        ]
        assert journal[-1]["status"] == "failed"

    def test_stdout_metric_without_evaluator_result_is_not_authority(self, tmp_path: Path) -> None:
        """Stdout metrics cannot replace the domain-owned evaluator result."""
        from researchclaw.pipeline.executor import _execute_experiment_run

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "stage-11").mkdir()
        (run_dir / "stage-11" / "schedule.json").write_text("{}", encoding="utf-8")

        stage_dir = run_dir / "stage-12"
        stage_dir.mkdir()

        data = {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {"topic": "Hardware-performance-counter runtime detection of Spectre and transient-execution attacks", "domains": ["security"],
                         "daily_paper_count": 2, "quality_threshold": 8.2},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "local", "on_stage_start": True,
                              "on_stage_fail": False, "on_gate_required": True},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {"use_memory": True, "use_message": True},
            "llm": {"provider": "openai-compatible", "base_url": "http://localhost:1234/v1",
                    "api_key_env": "RC_TEST_KEY", "api_key": "inline-test-key",
                    "primary_model": "fake-model", "fallback_models": []},
            "security": {"hitl_required_stages": [5, 9, 20]},
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 30,
                "allow_legacy_experiment_path": True,
                "max_iterations": 1,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "sandbox": {
                    "python_path": sys.executable,
                    "gpu_required": False,
                    "max_memory_mb": 512,
                    "allowed_imports": ["json"],
                },
            },
        }
        cfg = RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)
        adapters = AdapterBundle()
        _write_sealed_candidate(
            run_dir,
            cfg,
            "print('detection_f1: 0.95')\n",
        )

        result = _execute_experiment_run(
            stage_dir, run_dir, cfg, adapters
        )

        assert result.status == StageStatus.FAILED
        assert not (stage_dir / "experiment_result_set.json").exists()
        journal = [
            json.loads(line)
            for line in (stage_dir / "execution_invocation_journal.jsonl")
            .read_text(encoding="utf-8")
            .split("\n")
            if line
        ]
        assert journal[-1]["status"] == "failed"


class TestConsecutiveEmptyMetrics:
    """R6-4: Pipeline should detect consecutive empty-metrics REFINE cycles."""

    def test_detects_consecutive_empty(self, tmp_path: Path) -> None:
        """Two cycles with empty metrics should return True."""
        from researchclaw.pipeline.runner import _consecutive_empty_metrics

        run_dir = tmp_path / "run"
        # Current cycle (stage-14)
        s14 = run_dir / "stage-14"
        s14.mkdir(parents=True)
        (s14 / "experiment_summary.json").write_text(json.dumps({
            "metrics_summary": {},
            "best_run": {"metrics": {}},
        }))
        # Previous cycle (stage-14_v1)
        s14v1 = run_dir / "stage-14_v1"
        s14v1.mkdir(parents=True)
        (s14v1 / "experiment_summary.json").write_text(json.dumps({
            "metrics_summary": {},
            "best_run": {"metrics": {}},
        }))

        assert _consecutive_empty_metrics(run_dir, pivot_count=1) is True

    def test_not_empty_when_metrics_exist(self, tmp_path: Path) -> None:
        """If any cycle has real metrics, return False."""
        from researchclaw.pipeline.runner import _consecutive_empty_metrics

        run_dir = tmp_path / "run"
        s14 = run_dir / "stage-14"
        s14.mkdir(parents=True)
        (s14 / "experiment_summary.json").write_text(json.dumps({
            "metrics_summary": {},
            "best_run": {"metrics": {"loss": 0.5}},
        }))
        s14v1 = run_dir / "stage-14_v1"
        s14v1.mkdir(parents=True)
        (s14v1 / "experiment_summary.json").write_text(json.dumps({
            "metrics_summary": {},
            "best_run": {"metrics": {}},
        }))

        assert _consecutive_empty_metrics(run_dir, pivot_count=1) is False

    def test_false_when_no_previous_cycle(self, tmp_path: Path) -> None:
        """First cycle (no v1) should return False."""
        from researchclaw.pipeline.runner import _consecutive_empty_metrics

        run_dir = tmp_path / "run"
        s14 = run_dir / "stage-14"
        s14.mkdir(parents=True)
        (s14 / "experiment_summary.json").write_text(json.dumps({
            "metrics_summary": {},
            "best_run": {"metrics": {}},
        }))

        # No stage-14_v1 exists
        assert _consecutive_empty_metrics(run_dir, pivot_count=1) is False


# ===================================================================
# R7 Tests — Experiment-Paper Quality Alignment
# ===================================================================


class TestMultiConditionEnforcement:
    """R7-1: Code generation prompt must enforce multi-condition experiments."""

    def test_code_generation_prompt_has_multi_condition_block(self) -> None:
        """The code_generation prompt should contain multi-condition instructions."""
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "code_generation",
            topic="test topic",
            metric="primary_metric",
            pkg_hint="",
            exp_plan="conditions:\n  - echo_chamber\n  - bridge_building\n  - random",
        )
        assert "MULTI-CONDITION REQUIREMENT" in sp.user
        assert "condition=" in sp.user
        assert "SUMMARY" in sp.user

    def test_multi_condition_labels_required(self) -> None:
        """Prompt must mention per-condition labeled output format."""
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "code_generation",
            topic="test",
            metric="loss",
            pkg_hint="",
            exp_plan="treatments: [A, B, C]",
        )
        assert "condition=<name>" in sp.user


class TestEvidenceBoundedWriting:
    """R7-2: Paper draft prompt must enforce evidence-bounded claims."""

    def test_paper_draft_has_evidence_bounding_rules(self) -> None:
        """System prompt should contain evidence-bounding rules."""
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "paper_draft",
            preamble="test preamble",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="# Outline",
        )
        assert "EVIDENCE-BOUNDING RULES" in sp.system
        assert "title" in sp.system.lower()
        assert "causal claim" in sp.system.lower() or "causal claims" in sp.system.lower()

    def test_hedging_language_guidance(self) -> None:
        """Should suggest hedged alternatives like 'Toward...' for partial data."""
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "paper_draft",
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
        )
        assert "Toward" in sp.system or "Investigating" in sp.system


class TestBreadthFirstPrompt:
    """R8-1: Code generation prompt should require breadth-first condition ordering."""

    def test_breadth_first_in_code_generation(self) -> None:
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "code_generation",
            topic="test",
            metric="primary_metric",
            pkg_hint="",
            exp_plan="conditions: [A, B, C]",
        )
        assert "BREADTH-FIRST" in sp.user
        assert "ONE representative" in sp.user


class TestCodeGenTopicNeutral:
    """R9-1: Code generation prompt should be topic-neutral, not optimization-biased."""

    def test_no_gradient_descent_bias(self) -> None:
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "code_generation",
            topic="multi-agent simulation",
            metric="primary_metric",
            pkg_hint="",
            exp_plan="conditions: [L1, L2, L3, L4]",
        )
        # Should NOT contain optimization-specific examples as recommended approaches
        assert "Adam" not in sp.user
        assert "SGD" not in sp.user
        assert "Rosenbrock" not in sp.user
        # "gradient descent" may appear as anti-pattern warning but not as example
        assert "e.g., gradient descent" not in sp.user

    def test_topic_relevant_guidance(self) -> None:
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.for_stage(
            "code_generation",
            topic="multi-agent simulation",
            metric="primary_metric",
            pkg_hint="",
            exp_plan="conditions: [L1, L2, L3, L4]",
        )
        # Should contain generic guidance that works for any topic
        assert "simulation" in sp.user.lower() or "appropriate" in sp.user.lower()
        assert "ACTUAL experiment" in sp.user or "relevant to the TOPIC" in sp.user


class TestRefineTopicAlignment:
    """R9-2: Refine prompt should include topic-code alignment check."""

    def test_topic_alignment_in_refine_prompt(self) -> None:
        from researchclaw.prompts import PromptManager
        pm = PromptManager()
        sp = pm.sub_prompt(
            "iterative_improve",
            metric_key="primary_metric",
            metric_direction="maximize",
            files_context="# main.py\nprint('hello')",
            run_summaries="{}",
            condition_coverage_hint="",
            topic="multi-agent diversity scaling",
            exp_plan_anchor="",
        )
        assert "EXPERIMENT PLAN ANCHOR" in sp.user
        assert "multi-agent diversity scaling" in sp.user
        assert "NEVER rename" in sp.user


# =====================================================================
# _validate_draft_quality tests
# =====================================================================


def _make_prose(word_count: int) -> str:  # noqa: E302
    """Generate flowing prose text of approximately *word_count* words."""
    sentence = (
        "This is a flowing academic prose sentence "
        "that demonstrates our research findings. "
    )
    words_per = len(sentence.split())
    return sentence * (word_count // words_per + 1)


def _make_bullets(word_count: int) -> str:
    """Generate bullet-point text of approximately *word_count* words."""
    line = "- This is a bullet point about a research finding\n"
    words_per = len(line.split())
    return line * (word_count // words_per + 1)


def _make_comparative_prose(word_count: int) -> str:
    """Generate related-work style prose with comparative language."""
    sentence = (
        "Unlike prior work that focuses on simple baselines, "
        "our approach differs by incorporating novel techniques. "
        "In contrast to existing methods, we address key limitations. "
        "However, while previous approaches rely on heuristics, "
        "our method provides theoretical guarantees. "
    )
    words_per = len(sentence.split())
    return sentence * (word_count // words_per + 1)


def _make_results_prose(word_count: int) -> str:
    """Generate results prose with statistical measures."""
    sentence = (
        "Our method achieves 85.3 ± 1.2 accuracy averaged over 5 seeds. "
        "The baseline comparison yields a p-value of 0.003, confirming "
        "statistical significance with 95% confidence interval. "
    )
    words_per = len(sentence.split())
    return sentence * (word_count // words_per + 1)


def _build_draft(**section_overrides: str) -> str:
    """Build a paper draft with default prose sections."""
    defaults = {
        "Abstract": _make_prose(200),
        "Introduction": _make_prose(900),
        "Related Work": _make_comparative_prose(700),
        "Method": _make_prose(1200),
        "Experiments": _make_prose(1000),
        "Results": _make_results_prose(700),
        "Discussion": _make_prose(500),
        "Limitations": _make_prose(250),
        "Conclusion": _make_prose(250),
    }
    defaults.update(section_overrides)
    parts = ["# My Research Title\n"]
    for heading, body in defaults.items():
        parts.append(f"# {heading}\n{body}\n")
    return "\n".join(parts)


class TestValidateDraftQuality:
    """Tests for _validate_draft_quality()."""

    def test_short_section_triggers_warning(self) -> None:
        """Short Method section triggers expand warning."""
        draft = _build_draft(Method=_make_prose(200))
        result = rc_executor._validate_draft_quality(draft)
        assert any("Method" in w for w in result["overall_warnings"])
        assert any("EXPAND" in d or "Expand" in d
                    for d in result["revision_directives"])

    def test_bullet_density_triggers_warning(self) -> None:
        """Bullet-heavy Method section triggers rewrite warning."""
        draft = _build_draft(Method=_make_bullets(1200))
        result = rc_executor._validate_draft_quality(draft)
        assert any(
            "bullet" in w.lower() or "density" in w.lower()
            for w in result["overall_warnings"]
        )
        assert any("REWRITE" in d for d in result["revision_directives"])

    def test_clean_draft_no_warnings(self) -> None:
        """Balanced prose draft produces zero warnings."""
        draft = _build_draft()
        result = rc_executor._validate_draft_quality(draft)
        assert len(result["overall_warnings"]) == 0
        assert len(result["revision_directives"]) == 0

    def test_balance_warning(self) -> None:
        """Large imbalance between sections triggers balance warning."""
        draft = _build_draft(
            Introduction=_make_prose(1500),
            Results=_make_prose(100),
        )
        result = rc_executor._validate_draft_quality(draft)
        bal = [w for w in result["overall_warnings"]
               if "imbalance" in w.lower()]
        assert len(bal) >= 1, (
            f"Expected balance warning, got: {result['overall_warnings']}"
        )

    def test_writes_json_to_stage_dir(self, tmp_path: Path) -> None:
        """Quality report is written as draft_quality.json."""
        draft = _build_draft(Method=_make_prose(200))
        rc_executor._validate_draft_quality(draft, stage_dir=tmp_path)
        assert (tmp_path / "draft_quality.json").exists()
        data = json.loads(
            (tmp_path / "draft_quality.json").read_text(encoding="utf-8")
        )
        assert "section_analysis" in data
        assert "overall_warnings" in data
        assert "revision_directives" in data


class TestExperimentValidatorPrecision:
    def test_deep_validation_detects_undefined_helper_calls(self) -> None:
        from researchclaw.experiment.validator import deep_validate_files

        issues = deep_validate_files(
            {
                "main.py": (
                    "def main():\n"
                    "    create_empty_csv('tmp.csv', ['a'])\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n"
                )
            }
        )

        assert any(
            "Call to undefined function 'create_empty_csv()'" in issue
            for issue in issues
        )

    def test_deep_validation_allows_inherited_single_core_method_subclass(
        self,
    ) -> None:
        from researchclaw.experiment.validator import deep_validate_files

        issues = deep_validate_files(
            {
                "main.py": (
                    "class BaseVerifier:\n"
                    "    def __init__(self, scale=1.0):\n"
                    "        self.scale = float(scale)\n\n"
                    "class ChildVerifier(BaseVerifier):\n"
                    "    def predict(self, value):\n"
                    "        total = value * self.scale\n"
                    "        shifted = total + 1.0\n"
                    "        centered = shifted - 0.5\n"
                    "        bounded = max(centered, 0.0)\n"
                    "        return {'score': bounded}\n"
                )
            }
        )

        assert not any(
            "Class 'ChildVerifier' has only 1 non-dunder method" in issue
            for issue in issues
        )

    def test_deep_validation_detects_duplicate_algorithm_classes_across_files(
        self,
    ) -> None:
        from researchclaw.experiment.validator import deep_validate_files

        issues = deep_validate_files(
            {
                "main.py": (
                    "class DuplicateVerifier:\n"
                    "    def __init__(self, bias=0.0):\n"
                    "        self.bias = float(bias)\n\n"
                    "    def predict(self, value):\n"
                    "        shifted = value + self.bias\n"
                    "        bounded = max(shifted, 0.0)\n"
                    "        return {'score': bounded}\n"
                ),
                "models.py": (
                    "class DuplicateVerifier:\n"
                    "    def __init__(self, bias=0.0):\n"
                    "        self.bias = float(bias)\n\n"
                    "    def predict(self, value):\n"
                    "        shifted = value + self.bias\n"
                    "        bounded = max(shifted, 0.0)\n"
                    "        return {'score': bounded}\n"
                ),
            }
        )

        assert any(
            "Class 'DuplicateVerifier' is defined in multiple files" in issue
            for issue in issues
        )
        assert not any(
            "Classes 'DuplicateVerifier' and 'DuplicateVerifier' have identical"
            in issue
            for issue in issues
        )
