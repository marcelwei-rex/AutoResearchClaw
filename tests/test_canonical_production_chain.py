from __future__ import annotations

import asyncio
import json
import hashlib
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_check  # noqa: E402

from researchclaw.adapters import AdapterBundle
from researchclaw.collaboration.publisher import ArtifactPublisher
from researchclaw.collaboration.repository import ResearchRepository
from researchclaw.config import RCConfig
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.literature.models import Author, Paper
from researchclaw.literature.verify import (
    CitationResult,
    VerificationReport,
    VerifyStatus,
    parse_bibtex_entries,
)
from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
from researchclaw.evolution import EvolutionStore, extract_lessons
from researchclaw.memory.experiment_memory import ExperimentMemory
from researchclaw.pipeline.executor import execute_stage
from researchclaw.experiment_runtime.contract import load_contract
from researchclaw.experiment_runtime.scaffold import render_main_py
from researchclaw.pipeline import independent_release_reconstruction as reconstruction_module
from researchclaw.pipeline.independent_release_reconstruction import (
    IndependentReleaseReconstruction,
    reconstruct_expected_release_publications,
)
from researchclaw.pipeline.external_release_projection import (
    load_external_release_projection,
)
from researchclaw.pipeline.stage23_input_bundle import load_stage23_input_bundle
from researchclaw.pipeline.stage23_verification import (
    load_stage23_verification_publication,
)
from researchclaw.pipeline.stage24_publication import (
    load_stage24_publication_snapshot,
)
from researchclaw.pipeline.stage24_input_bundle import load_stage24_input_bundle
from researchclaw.pipeline.stage25_publication import (
    execute_stage25_deai,
    load_stage25_publication,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.report import generate_report
from researchclaw.mcp import server as mcp_server_module
from researchclaw.mcp.server import ResearchClawMCPServer
pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")


_LLM_STAGES = frozenset(
    {
        Stage.LITERATURE_SCREEN,
        Stage.KNOWLEDGE_EXTRACT,
        Stage.CODE_GENERATION,
        Stage.ITERATIVE_REFINE,
        Stage.RESEARCH_DECISION,
        Stage.PAPER_DRAFT,
        Stage.QUALITY_GATE,
        Stage.CITATION_VERIFY,
        Stage.TRUTH_AUDIT,
    }
)


class _NoLLM:
    config = SimpleNamespace(base_url="", api_key="")


class _ProductionChainLLM(LLMClient):
    """Deterministic external transport; canonical producers remain unpatched."""

    def __init__(self, run_dir: Path) -> None:
        super().__init__(
            LLMConfig(
                base_url="test://production-chain",
                api_key="test-key",
                primary_model="production-chain",
                fallback_models=[],
                max_retries=1,
            )
        )
        self.run_dir = run_dir

    def chat(
        self,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> LLMResponse:
        user = "\n".join(message.get("content", "") for message in messages)
        system = str(kwargs.get("system", ""))

        if "Create a SMART research goal in markdown" in user:
            response = (
                "# Research Goal\n\n## Topic\nTrojNet hardware Trojan localization.\n\n"
                "## Novel Angle\nDeterministic graph-localization evidence is replayed "
                "under a bounded synthetic validation scope.\n\n## Scope\nISCAS-85 "
                "circuits and fixed seeds.\n\n## SMART Goal\nRun the complete canonical "
                "evaluator and preserve exact provenance.\n\n## Constraints\nSynthetic "
                "pipeline validation only.\n\n## Success Criteria\nAll canonical stages "
                "replay without unsupported authority.\n"
            )
        elif "Decompose this research problem" in user:
            response = (
                "# Problem Decomposition\n\n## Source\nCanonical TrojNet validation.\n\n"
                "## Sub-questions\n1. Are raw scores deterministic?\n2. Are metrics "
                "independently reconstructed?\n3. Is provenance complete?\n4. Does "
                "release replay reject mixed generations?\n\n## Priority Ranking\n"
                "1. Raw authority\n2. Metric reconstruction\n3. Release binding\n\n"
                "## Risks\nSynthetic evidence does not establish scientific validity.\n"
            )
        elif "Evaluate this research topic" in user:
            response = json.dumps(
                {
                    "novelty": 8,
                    "specificity": 10,
                    "feasibility": 10,
                    "overall": 9,
                    "suggestion": "Keep the pipeline-validation scope explicit.",
                },
                sort_keys=True,
            )
        elif "Create a merged search strategy package" in user:
            response = json.dumps(
                {
                    "search_plan_yaml": (
                        "topic: TrojNet hardware Trojan localization on ISCAS-85 circuits\n"
                        "search_strategies:\n"
                        "  - name: hardware_trojan_localization\n"
                        "    queries:\n"
                        "      - TrojNet hardware Trojan localization ISCAS-85\n"
                        "      - graph neural network hardware Trojan detection\n"
                        "filters:\n  min_year: 2020\n"
                    ),
                    "sources": [],
                },
                sort_keys=True,
            )
        elif "STAGE 5 BATCH OUTPUT CONTRACT" in user:
            response = self._screening_response(user)
        elif user.startswith("Extract structured summaries only"):
            response = self._card_response(user)
        elif "Required keys: objectives,datasets,baselines,proposed_methods," in user:
            response = (
                "objectives:\n"
                "  - Detect bounded hardware-counter anomalies.\n"
                "datasets:\n"
                "  - synthetic_counter_traces\n"
                "baselines:\n"
                "  - threshold_counter_detector\n"
                "proposed_methods:\n"
                "  - bounded_counterguard\n"
                "ablations:\n"
                "  - no_temporal_context\n"
                "metrics:\n"
                "  - detection_f1\n"
                "risks:\n"
                "  - synthetic_scope_only\n"
                "compute_budget:\n"
                "  - bounded_cpu_validation\n"
            )
        elif user.startswith("Generate exactly one Python file named detector_plugin.py"):
            response = self._research_release_experiment()
        elif "You improve only the model-owned files" in system:
            response = self._non_improving_plugin()
        elif "independent Socratic critic" in system:
            response = json.dumps({"findings": []})
        elif "research program lead making go/no-go decisions" in system.casefold():
            response = (
                "# Research Decision\n\n## Decision\nPROCEED\n\n"
                "## Justification\nThe canonical baseline run produced the configured "
                "metric and preserves the evidence boundary.\n"
            )
        elif "fixed, independently replayed domain-evaluator evidence projection" in system:
            response = (
                "## Decision\nPROCEED\n\n"
                "## Justification\nAll fixed projection gates are satisfied.\n\n"
                "## Evidence\nThe immutable evaluator projection is complete.\n\n"
                "## Next Actions\nProceed with the canonical release path.\n"
            )
        elif "SECTION OUTPUT CONTRACT" in user:
            response = self._paper_part(user)
        elif "You assess citation relevance" in system:
            keys = re.findall(r"^- \[([^]]+)]", user, flags=re.MULTILINE)
            response = json.dumps({key: 1 for key in keys}, sort_keys=True)
        elif system == "You are a final quality gate evaluator.":
            response = json.dumps(
                {
                    "score_1_to_10": 10,
                    "verdict": "proceed",
                    "strengths": ["Canonical evidence closure is complete."],
                    "weaknesses": [],
                    "required_actions": [],
                },
                sort_keys=True,
            )
        elif "response_schema" in user:
            request = json.loads(user)
            response_payload = dict(request["response_schema"])
            if "verdict" in response_payload:
                response_payload["verdict"] = "supported"
                response_payload["reason"] = (
                    "The bound canonical source supports the assessed statement."
                )
            else:
                response_payload["resolution"] = "fixed"
                response_payload["note"] = "The final paper resolves the bound finding."
            response = json.dumps(response_payload, sort_keys=True)
        else:
            raise AssertionError(
                "unexpected LLM transport request in production-chain test: "
                + (system + "\n" + user)[:500]
            )
        return LLMResponse(content=response, model="production-chain-fixture")

    @staticmethod
    def _screening_response(user: str) -> str:
        batch_match = re.search(r"batch_id must be exactly (screen-batch-\d+)", user)
        ids_match = re.search(r"EXPECTED SOURCE IDENTITIES: (\[[^\n]+])", user)
        assert batch_match is not None and ids_match is not None
        source_ids = json.loads(ids_match.group(1))
        return json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_match.group(1),
                "decisions": [
                    {
                        "source_identity": source_id,
                        "decision": "keep",
                        "relevance_score": 1,
                        "quality_score": 1,
                        "reason": "Directly addresses the configured security topic.",
                    }
                    for source_id in source_ids
                ],
            },
            sort_keys=True,
        )

    @staticmethod
    def _card_response(user: str) -> str:
        batch_match = re.search(r"batch_id must be (card-batch-\d+)", user)
        sources_match = re.search(r"SOURCES:\n(\[.*])\Z", user, flags=re.DOTALL)
        assert batch_match is not None and sources_match is not None
        sources = json.loads(sources_match.group(1))
        return json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_match.group(1),
                "cards": [
                    {
                        "source_identity": source["source_identity"],
                        "summary_text": {
                            "problem": "Runtime detection of transient attacks.",
                            "method": "Hardware counter anomaly monitoring.",
                            "data": "Synthetic microarchitectural traces.",
                            "metrics": "Detection quality under controlled evaluation.",
                            "findings": "Counter behavior exposes attack activity.",
                            "limitations": "The retained abstract limits generalization.",
                        },
                        "evidence_excerpt_texts": [source["abstract"]],
                    }
                    for source in sources
                ],
            },
            sort_keys=True,
        )

    @classmethod
    def _paper_part(cls, user: str) -> str:
        requested_headings = re.findall(r"^- ## (.+)$", user, flags=re.MULTILINE)
        if requested_headings:
            allowed_keys = re.findall(r"Required citation key: \[([^]]+)]", user)
            parts: list[str] = []
            for heading in requested_headings:
                if heading == "Title":
                    parts.append("## Title\nCounterGuard: Runtime Detection from Hardware Events")
                    continue
                prose = ""
                if heading == "Related Work" and allowed_keys:
                    prose = " ".join(
                        f"Bounded related-work evidence [{key}]."
                        for key in allowed_keys
                    )
                parts.append(f"## {heading}\n{prose}")
            return "\n\n".join(parts)

        claims = re.findall(
            r"- CLAIM [^\n]+ \(section: ([^)]+)\)\n"
            r"  Allowed wording ceiling: ([^\n]+)\n"
            r"  Required citation key: \[([^]]+)]",
            user,
        )
        assert claims
        support_key = claims[0][2]
        by_section: dict[str, list[str]] = {"Introduction": [], "Related Work": []}
        for section, wording, cite_key in claims:
            sentence = f"{wording.rstrip('.')} [{cite_key}]."
            by_section.setdefault(section, []).append(sentence)

        if "Start DIRECTLY with '## Title'" in user:
            return "\n\n".join(
                (
                    "## Title\nCounterGuard: Runtime Detection from Hardware Events",
                    "## Abstract\n" + cls._prose("abstract", 28, support_key),
                    "## Introduction\n"
                    + " ".join(by_section["Introduction"])
                    + " "
                    + cls._prose("introduction", 90, support_key),
                    "## Related Work\n"
                    + " ".join(by_section["Related Work"])
                    + " "
                    + cls._prose("related work", 70, support_key),
                )
            )
        if "Now write the next sections" in user:
            return "\n\n".join(
                (
                    "## Method\n" + cls._prose("method", 100, support_key),
                    "## Experiments\n" + cls._prose("experiments", 85, support_key),
                )
            )
        return "\n\n".join(
            (
                "## Results\n" + cls._prose("results", 38, support_key),
                "## Discussion\n" + cls._prose("discussion", 28, support_key),
                "## Limitations\n" + cls._prose("limitations", 28, support_key),
                "## Conclusion\n" + cls._prose("conclusion", 18, support_key),
            )
        )

    @staticmethod
    def _prose(label: str, count: int, cite_key: str) -> str:
        phrase = (
            f"The {label} narrative describes the bounded workflow with careful "
            "scope, deterministic evidence ownership, and explicit limitations"
        )
        suffix = f" [{cite_key}]." if cite_key else "."
        return " ".join(phrase for _ in range(count)) + suffix

    @staticmethod
    def _non_improving_plugin() -> str:
        return '''```filename:detector_plugin.py
"""Deliberately conservative refinement candidate."""

import numpy as np


class DetectorPlugin:
    name = "constant_benign_refinement"

    def fit(self, X_train, y_train):
        _ = X_train, y_train
        return self

    def predict(self, X_test):
        return np.zeros(len(X_test), dtype=int)

    def describe(self):
        return {
            "method": "constant benign prediction",
            "assumptions": ["bounded production-chain fixture"],
        }
```'''

    def _research_release_experiment(self) -> str:
        contract = load_contract(self.run_dir / "stage-09/experiment_contract.yaml")
        return (
            "```filename:main.py\n"
            + render_main_py(contract)
            + "\n```\n"
            + '''```filename:detector_plugin.py
import numpy as np


class DetectorPlugin:
    name = "mean_threshold"

    def fit(self, X_train, y_train):
        del y_train
        self.threshold = float(np.mean(X_train[:, 0]))
        return self

    def predict(self, X_test):
        return (X_test[:, 0] >= self.threshold).astype(int)

    def describe(self):
        return {
            "method": "deterministic mean threshold",
            "assumptions": ["bounded canonical production-chain fixture"],
        }
```'''
        )


def _config(
    run_dir: Path,
    *,
    claim_scope: str,
    topic: str | None = None,
) -> RCConfig:
    raw = yaml.safe_load(Path("config.deepseek.sectional-dry-run.yaml").read_text())
    raw["project"]["name"] = "canonical-production-chain"
    if topic is not None:
        raw["research"]["topic"] = topic
    raw["research"]["quality_threshold"] = 1.0
    raw["research"]["graceful_degradation"] = True
    raw["llm"]["base_url"] = "test://production-chain"
    raw["llm"]["api_key"] = "test-key"
    raw["llm"]["primary_model"] = "writer-model"
    raw["llm"]["critic_model"] = "resolution-critic-model"
    raw["llm"]["critic_source"] = "model"
    raw["experiment"]["time_budget_sec"] = 30
    raw["experiment"]["code_agent"]["enabled"] = False
    raw["experiment"]["claim_scope"] = claim_scope
    raw["experiment"]["dataset_origin"] = (
        "public" if claim_scope == "research_release" else "synthetic"
    )
    raw["experiment"]["sandbox"]["python_path"] = sys.executable
    raw["paper_revision"]["sectional_enabled"] = False
    raw["paper_revision"]["critic_model"] = "support-critic-model"
    raw["knowledge_base"]["root"] = str(run_dir.parent / "kb")
    raw.setdefault("web_search", {})["enabled"] = False
    raw["notifications"]["on_stage_start"] = False
    raw["notifications"]["on_gate_required"] = False

    run_dir.mkdir(parents=True)
    snapshot = run_dir / "config.yaml"
    snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    write_active_config_binding(run_dir, snapshot)
    return config


def _papers(count: int = 3) -> list[Paper]:
    family_names = (
        "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
        "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "November",
        "Oscar",
    )
    assert count <= len(family_names)
    abstracts = (
        "Hardware performance counters reveal repeatable transient attack activity "
        "and support bounded runtime anomaly monitoring on controlled workloads.",
        "Microarchitectural event streams distinguish speculative side channel activity "
        "from benign execution under a deterministic detector evaluation.",
        "Change point monitoring of processor counters provides retained evidence for "
        "lightweight security detection while acknowledging deployment limits.",
    )
    return [
        Paper(
            paper_id=f"paper-{index}",
            title=f"Hardware Counter Detection Study {index}",
            authors=(Author(f"Researcher {family_names[index - 1]}"),),
            year=2020 + index,
            abstract=abstract,
            venue="Security Conference",
            citation_count=20 - index,
            doi=f"10.5555/production-chain-{index}",
            url=f"https://example.test/paper-{index}",
            source="openalex",
        )
        for index in range(1, count + 1)
        for abstract in (abstracts[(index - 1) % len(abstracts)],)
    ]


def _verified_report(cited_keys: tuple[str, ...]) -> VerificationReport:
    results = [
        CitationResult(
            cite_key=key,
            title=f"Verified source for {key}",
            status=VerifyStatus.VERIFIED,
            confidence=1.0,
            method="deterministic_external_fixture",
            details="External citation boundary returned a verified record.",
        )
        for key in cited_keys
    ]
    return VerificationReport(
        total=len(results),
        verified=len(results),
        suspicious=0,
        hallucinated=0,
        skipped=0,
        results=results,
    )


def _expected_release_authority_paths(
    run_dir: Path, reconstructed: IndependentReleaseReconstruction
) -> set[str]:
    expected = {
        entry.artifact.path for entry in reconstructed.stage24_inputs.entries
    }
    expected.update(artifact.path for artifact in reconstructed.stage24.outputs)
    expected.update(
        artifact.path for artifact in reconstructed.stage24.assessment_files
    )
    expected.update(
        {
            reconstructed.stage24.manifest.path,
            reconstructed.stage25.audit.path,
            reconstructed.stage25.manifest.path,
            reconstructed.evidence.manifest["selected_summary"]["canonical_path"],
            reconstructed.evidence.manifest["selected_analysis"]["canonical_path"],
            "stage-09/experiment_contract.sha256",
            "stage-09/domain_selector_policy.json",
            "stage-09/domain_profile.json",
            "stage-09/metric_authority_index.json",
            "stage-09/metric_authority.json",
            "stage-10/selected_candidate_manifest.json",
            "stage-12/experiment_result_set.json",
        }
    )
    expected.update(
        path.relative_to(run_dir).as_posix()
        for path in (run_dir / "stage-10/selected_candidate").iterdir()
    )
    baseline = json.loads(
        (run_dir / "stage-12/experiment_result_set.json").read_text(encoding="utf-8")
    )
    expected.add(baseline["invocation_journal"]["path"])
    expected.update(ref["path"] for ref in baseline["evidence_files"])
    if reconstructed.evidence.selected_result_manifest_path.startswith("stage-13/"):
        expected.add(reconstructed.evidence.selected_result_manifest_path)
        refinement = json.loads(
            (run_dir / reconstructed.evidence.selected_result_manifest_path).read_text(
                encoding="utf-8"
            )
        )
        expected.add(refinement["refinement_log"]["path"])
        for iteration in refinement["iterations"]:
            for ref in (
                *iteration["project_files"],
                iteration["validation_report"],
                iteration["initial_execution"],
            ):
                expected.add(ref["path"])
        expected.add("stage-13/experiment_final.py")
        expected.update(
            path.relative_to(run_dir).as_posix()
            for path in (run_dir / "stage-13/experiment_final").iterdir()
        )
    for stage_root in run_dir.iterdir():
        if re.fullmatch(r"stage-14(?:_v[1-9][0-9]*)?", stage_root.name) is None:
            continue
        candidates = stage_root / "evidence_candidates"
        if not candidates.exists():
            continue
        expected.update(
            path.relative_to(run_dir).as_posix()
            for candidate in candidates.iterdir()
            for path in candidate.iterdir()
        )
    return expected


@pytest.mark.parametrize("claim_scope", ("pipeline_validation", "research_release"))
def test_stage04_through_stage25_uses_real_canonical_production_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim_scope: str,
) -> None:
    run_dir = tmp_path / "run"
    config = _config(run_dir, claim_scope=claim_scope)
    stage3 = run_dir / "stage-03"
    stage3.mkdir()
    (stage3 / "search_plan.yaml").write_text(
        "queries:\n  - hardware counter transient attack detection\n",
        encoding="utf-8",
    )
    (stage3 / "queries.json").write_text(
        json.dumps(
            {
                "queries": ["hardware counter transient attack detection"],
                "year_min": 2020,
            }
        ),
        encoding="utf-8",
    )

    current_stage: list[Stage] = [Stage.LITERATURE_COLLECT]
    llm = _ProductionChainLLM(run_dir)
    generic_llm_stages = _LLM_STAGES | {Stage.EXPERIMENT_DESIGN}

    def llm_factory(_config: RCConfig) -> object:
        return llm if current_stage[0] in generic_llm_stages else _NoLLM()

    monkeypatch.setattr(LLMClient, "from_rc_config", staticmethod(llm_factory))
    monkeypatch.setattr(
        "researchclaw.literature.search.search_papers_multi_query",
        lambda *_args, **_kwargs: _papers(
            15 if claim_scope == "research_release" else 3
        ),
    )
    monkeypatch.setattr("researchclaw.data.load_seminal_papers", lambda *_args: [])
    monkeypatch.setattr(
        "researchclaw.literature.novelty.check_novelty",
        lambda **_kwargs: {
            "novelty_score": 1.0,
            "assessment": "bounded test input",
            "recommendation": "continue",
        },
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda bib, **_kwargs: _verified_report(
            tuple(str(entry["key"]) for entry in parse_bibtex_entries(bib))
        ),
    )
    if claim_scope == "research_release":
        def compile_export(
            tex_bytes: bytes,
            _bibliography: bytes,
            _style_files: dict[str, bytes],
            *,
            generated: str,
        ) -> tuple[dict[str, bytes], bytes, dict[str, object]]:
            return (
                    {"paper.pdf": b"%PDF-1.4\n% canonical test fixture\n%%EOF\n"},
                tex_bytes,
                {
                    "schema_version": 1,
                    "success": True,
                    "attempts": 1,
                    "errors": [],
                    "status": "success",
                    "tooling_available": True,
                    "generated": generated,
                },
            )

        monkeypatch.setattr(
            "researchclaw.pipeline.stage22_export._compile_export",
            compile_export,
        )

    results = []
    for stage in Stage:
        if stage < Stage.LITERATURE_COLLECT:
            continue
        current_stage[0] = stage
        result = execute_stage(
            stage,
            run_dir=run_dir,
            run_id="canonical-production-chain",
            config=config,
            adapters=AdapterBundle(),
            auto_approve_gates=True,
        )
        results.append(result)
        assert result.status is StageStatus.DONE, (
            f"{stage.name} failed: {result.error}; artifacts={result.artifacts}"
        )

    assert [result.stage for result in results] == list(Stage)[3:]
    stage23_inputs = load_stage23_input_bundle(run_dir, config)
    stage23 = load_stage23_verification_publication(run_dir, stage23_inputs)
    stage24 = load_stage24_publication_snapshot(run_dir, config)
    stage25 = load_stage25_publication(run_dir, config)
    if claim_scope == "research_release":
        candidates = sorted((run_dir / "stage-14/evidence_candidates").iterdir())
        assert len(candidates) == 1
        duplicate = (
            run_dir
            / "stage-14_v1/evidence_candidates"
            / candidates[0].name
        )
        duplicate.parent.mkdir(parents=True)
        shutil.copytree(candidates[0], duplicate)
    original_capture = reconstruction_module._capture_expected_release_publications
    capture_count = 0
    blocked_writes: list[str] = []
    stage25_manifest_before = (run_dir / "stage-25/stage25_deai_manifest.json").read_bytes()

    def capture_with_late_writer(path: Path):
        nonlocal capture_count
        captured = original_capture(path)
        capture_count += 1
        if capture_count == 2:
            with pytest.raises(RuntimeError, match="release_graph.*locked"):
                execute_stage25_deai(
                    run_dir,
                    run_dir / "stage-25",
                    runtime_config=config,
                    llm=None,
                )
            blocked_writes.append("stage25")
        return captured

    monkeypatch.setattr(
        reconstruction_module,
        "_capture_expected_release_publications",
        capture_with_late_writer,
    )
    reconstructed = reconstruct_expected_release_publications(run_dir)
    assert blocked_writes == ["stage25"]
    assert (run_dir / "stage-25/stage25_deai_manifest.json").read_bytes() == (
        stage25_manifest_before
    )

    stage20 = stage23_inputs.stage22_inputs.stage21_inputs
    fabrication = json.loads(stage20.fabrication_flags.content)
    truth_audit = json.loads(stage24.require_output("truth_audit.json").content)
    assert stage20.quality_gate_outcome == "passed"
    assert fabrication["has_real_data"] is True
    assert fabrication["experiment_failed"] is False
    assert truth_audit["stage24_success"] is True
    assert truth_audit["unsupported_count"] == 0
    assert stage23.manifest.path == "stage-23/stage23_verification_manifest.json"
    assert stage24.manifest.path == "stage-24/stage24_truth_manifest.json"
    assert stage25.manifest.path == "stage-25/stage25_deai_manifest.json"
    assert reconstructed.evidence.manifest_path == "canonical_experiment_evidence.json"
    assert reconstructed.critique.state == "model_final"
    assert reconstructed.stage23.manifest.path == (
        "stage-23/stage23_verification_manifest.json"
    )
    assert reconstructed.stage24 == stage24
    assert reconstructed.stage25 == stage25
    authority_paths = {
        artifact.path for artifact in reconstructed.authority_artifacts
    }
    assert authority_paths == _expected_release_authority_paths(
        run_dir, reconstructed
    )
    external_projection = load_external_release_projection(run_dir)
    assert external_projection.canonical_manifest_sha256 == (
        reconstructed.evidence.manifest_sha256
    )
    assert external_projection.selected_execution_sha256 == (
        reconstructed.evidence.selected_execution_artifact.sha256
    )
    assert external_projection.metric_observations == (
        reconstructed.evidence.metric_observations
    )
    assert external_projection.paper_text == (
        reconstructed.stage24_inputs.paper.content.decode("utf-8")
    )
    assert external_projection.verification_report["summary"]["total"] >= 1
    assert external_projection.latex_text == next(
        artifact.content.decode("utf-8")
        for artifact in reconstructed.authority_artifacts
        if artifact.path == "stage-22/paper.tex"
    )
    lessons = extract_lessons([], run_dir=run_dir)
    lesson_store = EvolutionStore(run_dir / "evolution")
    lesson_store.append_many(lessons)
    assert lesson_store.load_all() == lessons
    memory = ExperimentMemory()
    memory.record_release(run_dir, task_type="ignored", run_id="ignored")
    recalled = memory.recall_best_configs("ignored", run_dir=run_dir)
    assert external_projection.selected_execution_sha256 in recalled
    report = generate_report(run_dir)
    assert reconstructed.evidence.manifest_sha256 in report
    repository = ResearchRepository(tmp_path / "shared-repository")
    assert ArtifactPublisher(repository).publish_from_run_dir(
        "canonical-run", run_dir
    ) >= 1
    published = repository.get_run_artifacts("canonical-run")
    assert published["experiment_results"]["canonical_manifest_sha256"] == (
        reconstructed.evidence.manifest_sha256
    )
    monkeypatch.setattr(mcp_server_module, "_validated_run_dir", lambda _run_id: run_dir)
    mcp = ResearchClawMCPServer()
    mcp_results = asyncio.run(
        mcp._handle_get_results({"run_id": "canonical-run"})
    )
    mcp_paper = asyncio.run(
        mcp._handle_get_paper({"run_id": "canonical-run", "format": "latex"})
    )
    assert mcp_results["canonical_manifest"]["sha256"] == (
        reconstructed.evidence.manifest_sha256
    )
    assert mcp_paper["content"] == external_projection.latex_text
    monkeypatch.setattr(
        reconstruction_module,
        "_capture_expected_release_publications",
        original_capture,
    )
    checker = release_check.ReleaseChecker(
        run_dir, quality_threshold=1.0, allow_suspicious=False
    )
    assert checker._load_independent_release_reconstruction() is True
    assert not checker.findings
    captured_truth = checker.read_json("stage-24/truth_audit.json", required=True)
    assert captured_truth is not None
    checker.check_canonical_source()
    findings_before_shadow = tuple(checker.findings)
    root_source = run_dir / "canonical_source.json"
    root_source.write_text('{"shadow": true}\n', encoding="utf-8")
    try:
        checker.check_canonical_source()
        assert tuple(checker.findings) == findings_before_shadow
    finally:
        root_source.unlink()

    tamper_paths = (
        "stage-15/critique.json",
        "stage-23/verification_report.json",
        "stage-24/truth_audit.json",
        "stage-25/deai_audit.json",
        "config.yaml",
        "canonical_experiment_evidence.json",
    )
    for relative in tamper_paths:
        path = run_dir / relative
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        try:
            tampered = release_check.ReleaseChecker(
                run_dir, quality_threshold=1.0, allow_suspicious=False
            )
            assert tampered._load_independent_release_reconstruction() is False
            assert {
                finding.code
                for finding in tampered.findings
                if finding.severity == release_check.SEVERITY_ERROR
            } == {"independent_release_reconstruction_failed"}
        finally:
            path.write_bytes(original)

    truth_path = run_dir / "stage-24/truth_audit.json"
    original_truth = truth_path.read_bytes()
    truth_path.write_text("{}\n", encoding="utf-8")
    try:
        assert checker.read_json("stage-24/truth_audit.json", required=True) == (
            captured_truth
        )
    finally:
        truth_path.write_bytes(original_truth)
    mixed_manifest = json.loads(stage24.manifest.content)
    mixed_manifest["stage23_publication"]["sha256"] = "0" * 64
    mixed_bytes = json.dumps(mixed_manifest, sort_keys=True).encode("utf-8")
    mixed_stage24 = replace(
        stage24,
        manifest=replace(
            stage24.manifest,
            content=mixed_bytes,
            sha256=hashlib.sha256(mixed_bytes).hexdigest(),
        ),
    )
    with pytest.raises(
        reconstruction_module.IndependentReleaseReconstructionError,
        match="different stage23_publication generation",
    ):
        reconstruction_module._require_release_generation_bindings(
            evidence=reconstructed.evidence,
            stage24_inputs=load_stage24_input_bundle(run_dir, config),
            stage23=stage23,
            stage24=mixed_stage24,
            stage25=stage25,
        )
    stage25_manifest = json.loads(stage25.manifest.content)
    assert stage25_manifest["stage24_manifest_path"] == stage24.manifest.path
    assert stage25_manifest["stage24_manifest_sha256"] == stage24.manifest.sha256
    for directory in (
        "citation-assessments",
        "generic-support-assessments",
        "resolution-assessments",
    ):
        assert (run_dir / "stage-24" / directory).is_dir()
    for stage_number in (15, 22, 23, 24, 25):
        stage_dir = run_dir / f"stage-{stage_number:02d}"
        assert not (stage_dir / "decision.json").exists()
        assert not (stage_dir / "stage_health.json").exists()

    if claim_scope == "research_release":
        (run_dir / "pipeline_summary.json").write_text(
            json.dumps(
                {
                    "final_stage": 25,
                    "final_status": "done",
                    "stages_failed": 0,
                    "degraded": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "run_manifest.json").write_text(
            json.dumps({"expected_final_stage": 25}) + "\n",
            encoding="utf-8",
        )
        deliverables = run_dir / "deliverables"
        deliverables.mkdir()
        (deliverables / "manifest.json").write_text(
            json.dumps({"release_ready": True}) + "\n",
            encoding="utf-8",
        )

    release_checker = release_check.ReleaseChecker(
        run_dir, quality_threshold=1.0, allow_suspicious=False
    )
    expected_exit = (
        release_check.EXIT_PASS
        if claim_scope == "research_release"
        else release_check.EXIT_FAIL
    )
    assert release_checker.run() == expected_exit
    release_codes = {
        finding.code
        for finding in release_checker.findings
        if finding.severity == release_check.SEVERITY_ERROR
    }
    assert "independent_release_reconstruction_failed" not in release_codes
    assert "sandbox_backend_mismatch" not in release_codes
    assert "unsafe_environment_policy" not in release_codes
    assert "claims_digest_invariance_broken" not in release_codes
    assert "citation_support_unmapped" not in release_codes
    if claim_scope == "research_release":
        assert not release_codes
    else:
        assert "non_release_claim_scope" in release_codes
