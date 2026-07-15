from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.literature.models import Author, Paper
from researchclaw.literature.verify import (
    CitationResult,
    VerificationReport,
    VerifyStatus,
    parse_bibtex_entries,
)
from researchclaw.llm.client import LLMClient, LLMResponse
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.stage23_input_bundle import load_stage23_input_bundle
from researchclaw.pipeline.stage23_verification import (
    load_stage23_verification_publication,
)
from researchclaw.pipeline.stage24_publication import (
    load_stage24_publication_snapshot,
)
from researchclaw.pipeline.stage25_publication import (
    load_stage25_publication,
)
from researchclaw.pipeline.stages import Stage, StageStatus
pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")


_LLM_STAGES = frozenset(
    {
        Stage.LITERATURE_SCREEN,
        Stage.KNOWLEDGE_EXTRACT,
        Stage.ITERATIVE_REFINE,
        Stage.RESEARCH_DECISION,
        Stage.PAPER_DRAFT,
        Stage.CITATION_VERIFY,
        Stage.TRUTH_AUDIT,
    }
)


class _NoLLM:
    config = SimpleNamespace(base_url="", api_key="")


class _ProductionChainLLM:
    """Deterministic external transport; canonical producers remain unpatched."""

    config = SimpleNamespace(base_url="test://production-chain", api_key="test-key")

    def chat(
        self,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> LLMResponse:
        user = "\n".join(message.get("content", "") for message in messages)
        system = str(kwargs.get("system", ""))

        if "STAGE 5 BATCH OUTPUT CONTRACT" in user:
            response = self._screening_response(user)
        elif user.startswith("Extract structured summaries only"):
            response = self._card_response(user)
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
        elif "SECTION OUTPUT CONTRACT" in user:
            response = self._paper_part(user)
        elif "You assess citation relevance" in system:
            keys = re.findall(r"^- \[([^]]+)]", user, flags=re.MULTILINE)
            response = json.dumps({key: 1 for key in keys}, sort_keys=True)
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
        claims = re.findall(
            r"- CLAIM [^\n]+ \(section: ([^)]+)\)\n"
            r"  Allowed wording ceiling: ([^\n]+)\n"
            r"  Required citation key: \[([^]]+)]",
            user,
        )
        by_section: dict[str, list[str]] = {"Introduction": [], "Related Work": []}
        supported_sentences: list[str] = []
        for section, wording, cite_key in claims:
            sentence = f"{wording.rstrip('.')} [{cite_key}]."
            by_section.setdefault(section, []).append(sentence)
            supported_sentences.append(sentence)

        if "Start DIRECTLY with '## Title'" in user:
            return "\n\n".join(
                (
                    "## Title\nCounterGuard: Runtime Detection from Hardware Events",
                    "## Abstract\n" + cls._prose("abstract", 28),
                    "## Introduction\n"
                    + " ".join(by_section["Introduction"])
                    + " "
                    + cls._prose("introduction", 90),
                    "## Related Work\n"
                    + " ".join(by_section["Related Work"])
                    + " "
                    + cls._prose("related work", 70),
                )
            )
        if "Now write the next sections" in user:
            return "\n\n".join(
                (
                    "## Method\n" + cls._prose("method", 100),
                    "## Experiments\n" + cls._prose("experiments", 85),
                )
            )
        return "\n\n".join(
            (
                "## Results\n" + " ".join(supported_sentences[:2]),
                "## Discussion\n" + " ".join(supported_sentences[2:]),
                "## Limitations\n" + cls._prose("limitations", 28),
                "## Conclusion\n" + cls._prose("conclusion", 18),
            )
        )

    @staticmethod
    def _prose(label: str, count: int) -> str:
        sentence = (
            f"The {label} narrative describes the bounded workflow with careful "
            "scope, deterministic evidence ownership, and explicit limitations."
        )
        return " ".join(sentence for _ in range(count))

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


def _config(run_dir: Path) -> RCConfig:
    raw = yaml.safe_load(Path("config.deepseek.sectional-dry-run.yaml").read_text())
    raw["project"]["name"] = "canonical-production-chain"
    raw["research"]["quality_threshold"] = 1.0
    raw["research"]["graceful_degradation"] = True
    raw["llm"]["base_url"] = "test://production-chain"
    raw["llm"]["api_key"] = "test-key"
    raw["llm"]["primary_model"] = "writer-model"
    raw["llm"]["critic_model"] = "resolution-critic-model"
    raw["llm"]["critic_source"] = "model"
    raw["experiment"]["time_budget_sec"] = 30
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


def _papers() -> list[Paper]:
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
            title=f"Hardware Counter Detection Study {word}",
            authors=(Author(f"Researcher {word}"),),
            year=2020 + index,
            abstract=abstract,
            venue="Security Conference",
            citation_count=20 - index,
            doi=f"10.5555/production-chain-{index}",
            url=f"https://example.test/paper-{index}",
            source="openalex",
        )
        for index, (word, abstract) in enumerate(
            zip(("Alpha", "Beta", "Gamma"), abstracts, strict=True), start=1
        )
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


def test_stage04_through_stage25_uses_real_canonical_production_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    config = _config(run_dir)
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
    llm = _ProductionChainLLM()

    def llm_factory(_config: RCConfig) -> object:
        return llm if current_stage[0] in _LLM_STAGES else _NoLLM()

    monkeypatch.setattr(LLMClient, "from_rc_config", staticmethod(llm_factory))
    monkeypatch.setattr(
        "researchclaw.literature.search.search_papers_multi_query",
        lambda *_args, **_kwargs: _papers(),
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
