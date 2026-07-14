# conftest.py — shared pytest fixtures for researchclaw tests

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest


@pytest.fixture
def canonical_evidence_migration_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let legacy behavior tests exercise their target below the C0 gate."""
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: capabilities.CAPABILITY_SCHEMA_VERSION
            for name in capabilities.REQUIRED_CAPABILITIES
        },
    )


@pytest.fixture
def consumer_evidence_fixture(monkeypatch: pytest.MonkeyPatch):
    """Adapt legacy unit fixtures to the immutable consumer interface.

    Real accessor replay is covered separately by canonical evidence tests.
    """
    def _load(run_dir: Path):
        contract_path = run_dir / "stage-09/experiment_contract.yaml"
        contract_bytes = contract_path.read_bytes() if contract_path.is_file() else b""
        summary_path = run_dir / "experiment_summary_best.json"
        if not summary_path.is_file():
            summary_path = run_dir / "stage-14/experiment_summary.json"
        if summary_path.is_file():
            summary_bytes = summary_path.read_bytes()
            summary = json.loads(summary_bytes, parse_float=Decimal)
        else:
            summary = {}
            summary_bytes = json.dumps(summary).encode("utf-8")

        structured: dict = {}
        metric_observations: dict[str, list[float]] = {}
        result_candidates = [
            run_dir / "stage-12/evidence-v1/results.json",
            run_dir / "stage-12/runs/results.json",
        ] + sorted((run_dir / "stage-12/runs").glob("*.json"))
        for path in result_candidates:
            if not path.is_file():
                continue
            try:
                payload = json.loads(
                    path.read_text(encoding="utf-8"), parse_float=Decimal
                )
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            structured = payload.get("structured_results", payload)
            observations = payload.get("metric_observations")
            if isinstance(observations, dict):
                metric_observations = observations
            else:
                metrics = structured.get("metrics", {}) if isinstance(structured, dict) else {}
                if isinstance(metrics, dict):
                    metric_observations = {
                        str(key): [Decimal(str(value))]
                        for key, value in metrics.items()
                        if not isinstance(value, bool)
                        and isinstance(value, (int, float, Decimal))
                    }
            break
        manifest_hash = hashlib.sha256(b"consumer-test-evidence").hexdigest()
        contract_hash = hashlib.sha256(contract_bytes).hexdigest()
        decision_dir = run_dir / "stage-15"
        decision_dir.mkdir(parents=True, exist_ok=True)
        decision_path = decision_dir / "decision.md"
        if not decision_path.is_file():
            decision_path.write_text("PROCEED: test fixture decision.\n", encoding="utf-8")
        decision_text = decision_path.read_text(encoding="utf-8")
        decision_binding_path = decision_dir / "decision_structured.json"
        if not decision_binding_path.is_file():
            decision_binding_path.write_text(
                json.dumps(
                    {
                        "decision": "proceed",
                        "raw_text_excerpt": decision_text[:500],
                        "quality_warnings": [],
                        "generated": "2026-07-14T00:00:00+00:00",
                        "decision_path": "stage-15/decision.md",
                        "decision_sha256": hashlib.sha256(
                            decision_text.encode("utf-8")
                        ).hexdigest(),
                        "canonical_experiment_evidence_path": (
                            "canonical_experiment_evidence.json"
                        ),
                        "canonical_experiment_evidence_sha256": manifest_hash,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        outline_path = run_dir / "stage-16/outline.md"
        outline_binding_path = run_dir / "stage-16/outline_binding.json"
        if outline_path.is_file() and not outline_binding_path.is_file():
            outline_text = outline_path.read_text(encoding="utf-8")
            outline_binding_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outline_path": "stage-16/outline.md",
                        "outline_sha256": hashlib.sha256(
                            outline_text.encode("utf-8")
                        ).hexdigest(),
                        "decision_path": "stage-15/decision.md",
                        "decision_sha256": hashlib.sha256(
                            decision_text.encode("utf-8")
                        ).hexdigest(),
                        "canonical_experiment_evidence_path": (
                            "canonical_experiment_evidence.json"
                        ),
                        "canonical_experiment_evidence_sha256": manifest_hash,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return SimpleNamespace(
            manifest_path="canonical_experiment_evidence.json",
            manifest_sha256=manifest_hash,
            manifest=MappingProxyType({}),
            candidate_manifest_path="stage-14/evidence_candidates/cand-test/experiment_evidence_candidate.json",
            candidate_manifest_sha256="b" * 64,
            candidate=MappingProxyType({}),
            selected_result_manifest_path="stage-12/experiment_result_set.json",
            selected_result_manifest_sha256="c" * 64,
            selected_result=MappingProxyType({}),
            experiment_contract_path="stage-09/experiment_contract.yaml",
            experiment_contract_sha256=contract_hash,
            experiment_contract_bytes=contract_bytes,
            run_config_path="config.yaml",
            run_config_sha256="d" * 64,
            run_config_bytes=b"",
            summary_bytes=summary_bytes,
            summary=MappingProxyType(summary),
            analysis_bytes=b"Analysis.\n",
            analysis_text="Analysis.\n",
            metric_observations=MappingProxyType(metric_observations),
            structured_results=MappingProxyType(structured),
            artifacts=(),
        )

    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._analysis.load_canonical_experiment_evidence",
        _load,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._paper_writing.load_canonical_experiment_evidence",
        _load,
    )
    monkeypatch.setattr(
        "researchclaw.literature.experiment_fact_closure.load_canonical_experiment_evidence",
        _load,
    )
    return _load
