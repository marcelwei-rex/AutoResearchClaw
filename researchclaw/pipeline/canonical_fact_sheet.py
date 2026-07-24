"""Deterministic grounding facts derived from canonical experiment evidence."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any, Mapping, Sequence

from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    parse_contract_bytes,
    validate_contract_structure_dict,
)
from researchclaw.experiment_runtime.metric_authority import (
    MetricAuthorityError,
    parse_domain_execution_policy_bytes,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    _freeze_authority_value,
    _parse_json_value,
    _thaw_authority_value,
    canonical_authority_json_text,
    canonical_decimal,
)
from researchclaw.pipeline.stage14_domain_evaluator import (
    Stage14DomainEvaluatorError,
    project_domain_metric_observations,
)

MAX_PROJECTION_ROWS = 162
MAX_PROJECTION_UTF8_BYTES = 65536

_CFS_SCHEMA_VERSION = 1
_CITATION_USAGE_AUTHORITY_SCHEMA_VERSION = 1
_CITATION_USAGE_POLICY_VERSION = 1
_DECIMAL_REPLAY_TOLERANCE = Decimal("1e-49")
_VIEWS = frozenset({"introduction", "method", "results", "limitations"})
_BENCHMARK_TOKEN = re.compile(r"[a-z]{2,}[0-9]+[a-z0-9]*")
_VERSION_TOKEN = re.compile(r"v[0-9]+")
_PROMPT_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:/-]*")
_METHOD_USAGE_REGISTRY: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "raw_pca": ("method:principal_component_analysis", ("PCA", "principal component analysis")),
    "scoap_isolation_forest": (
        "method:isolation_forest",
        ("isolation forest",),
    ),
    "trojnet_community_graphsage": ("method:graphsage", ("GraphSAGE",)),
    "trojnet_iscas85_graphsage_localization": (
        "method:graphsage",
        ("GraphSAGE",),
    ),
}
_EVALUATION_PROTOCOL_FIELDS = (
    "primary_observation_set",
    "primary_aggregation",
    "execution_backend_policy",
    "raw_evidence_policy",
    "observation_policy",
    "aggregation_policy",
)
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_COUNT_TOKEN_PATTERN = "|".join((r"[0-9]+", *map(re.escape, _COUNT_WORDS)))
_REQUEST_COUNT = re.compile(
    rf"\b(?P<count>{_COUNT_TOKEN_PATTERN})"
    r"\s+(?P<kind>seeds?|conditions?|variants?)\b",
    re.IGNORECASE,
)
_NEW_EXPERIMENT_REQUEST = re.compile(
    r"\b(?:re-?run|rerun|conduct|perform)\b.{0,80}"
    r"\b(?:experiment|evaluation|ablation|benchmark|trial|results?)\b",
    re.IGNORECASE,
)
_NEW_RESULTS_REQUEST = re.compile(
    r"\b(?:new|additional)\s+(?:experiment|evaluation|ablation|results?)\b",
    re.IGNORECASE,
)
_STATISTICAL_REQUEST = re.compile(
    r"\b(?:p[- ]?values?|confidence intervals?|statistical significance|"
    r"significance tests?|hypothesis tests?)\b",
    re.IGNORECASE,
)
_METRIC_REQUEST = re.compile(
    r"\b(?:report|compute|provide|include|add)\s+(?:the\s+)?"
    r"(?P<metric>[A-Za-z][A-Za-z0-9_-]*)\s+(?:metric|score)\b",
    re.IGNORECASE,
)
_CONDITION_NAME_REQUEST = re.compile(
    r"\b(?:add|include|use|evaluate|test|compare\s+(?:against|with|to))\s+"
    r"(?:an?\s+|the\s+)?(?P<name>[A-Za-z][A-Za-z0-9_.-]*)\s+"
    r"(?:condition|comparator|baseline)\b",
    re.IGNORECASE,
)
_NEW_CONDITION_REQUEST = re.compile(
    r"\b(?:new|additional|stronger)\s+(?:condition|comparator|baseline)\b",
    re.IGNORECASE,
)
_VARIANT_NAME_REQUEST = re.compile(
    r"\b(?:add|include|use|evaluate|test)\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_.-]*)\s+variant\b",
    re.IGNORECASE,
)
_COUNT_CHANGE_INTENT = re.compile(
    r"\b(?:use|add|include|increase|decrease|expand|reduce|raise|change|"
    r"evaluate|test|run|re-?run|rerun)\b"
    r"(?P<body>[^.!?\n]{0,100})\b(?P<kind>seeds?|conditions?|variants?)\b"
    r"(?:\s+count\s+(?:to|of)\s+[A-Za-z0-9]+)?",
    re.IGNORECASE,
)
_SUBSTANTIVE_CLAUSE_SPLIT = re.compile(
    r"(?<!\d)[.;!?]+(?!\d)|\b(?:because|but|although|while|whereas|since)\b",
    re.IGNORECASE,
)
_REQUEST_TOKEN = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*|"
    r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?%?"
)
_EXCLUSIVE_REQUEST_WORDS = frozenset(
    {
        "a", "add", "additional", "against", "an", "are", "baseline",
        "benchmark", "change", "compute", "condition", "conditions",
        "confidence", "conduct", "count", "decrease", "evaluate",
        "evaluated", "evaluation", "experiment", "expand", "for",
        "hypothesis", "include", "included", "increase", "interval",
        "intervals", "is", "metric", "more", "new", "of", "only",
        "perform", "please", "provide", "p-value", "p-values", "random",
        "raise", "re-run", "reduce", "report", "rerun", "result",
        "results", "run", "score", "seed", "seeds", "significance",
        "statistical", "stronger", "test", "tested", "tests", "the",
        "to", "trial", "use", "used", "variant", "variants", "was",
        "were", "with",
    }
)


class CFSIntegrityError(ValueError):
    """Canonical evidence cannot be projected into one consistent fact sheet."""


class ProjectionBudgetError(ValueError):
    """The projection header itself cannot fit within the requested budget."""


def build_canonical_fact_sheet(
    evidence: CanonicalExperimentEvidence,
) -> Mapping[str, Any] | None:
    """Build an immutable CFS for exact domain-evaluator v2 evidence."""

    if not _is_domain_evaluator_v2(evidence):
        return None
    try:
        artifact = evidence.execution_policy_artifact
        if artifact is None or artifact.role != "execution_policy":
            raise CFSIntegrityError("canonical execution policy artifact is missing")
        if hashlib.sha256(artifact.content).hexdigest() != artifact.sha256:
            raise CFSIntegrityError("canonical execution policy artifact hash mismatch")

        contract = parse_contract_bytes(evidence.experiment_contract_bytes)
        validate_contract_structure_dict(contract)
        if (
            hashlib.sha256(evidence.experiment_contract_bytes).hexdigest()
            != evidence.experiment_contract_sha256
        ):
            raise CFSIntegrityError("canonical experiment contract hash mismatch")
        authority = contract.get("evaluator_authority")
        if not isinstance(authority, dict):
            raise CFSIntegrityError("canonical evaluator authority is missing")
        if (
            artifact.path != authority.get("execution_policy_snapshot_path")
            or artifact.sha256
            != authority.get("execution_policy_snapshot_sha256")
        ):
            raise CFSIntegrityError("canonical execution policy binding mismatch")

        policy = parse_domain_execution_policy_bytes(artifact.content)
        if (
            hashlib.sha256(evidence.selected_execution_artifact.content).hexdigest()
            != evidence.selected_execution_artifact.sha256
        ):
            raise CFSIntegrityError("canonical observation artifact hash mismatch")
        replayed_flat_metrics = project_domain_metric_observations(
            evidence.selected_execution_artifact.content
        )
        observations = _parse_json_object(
            evidence.selected_execution_artifact.content, "metric observations"
        )
        results = _plain_mapping(evidence.structured_results, "structured results")
        return _build_validated_fact_sheet(
            evidence,
            contract,
            policy,
            observations,
            results,
            replayed_flat_metrics,
        )
    except CFSIntegrityError:
        raise
    except (
        CanonicalExperimentEvidenceError,
        ContractValidationError,
        MetricAuthorityError,
        Stage14DomainEvaluatorError,
        UnicodeDecodeError,
    ) as exc:
        raise CFSIntegrityError(str(exc)) from exc


def canonical_fact_sheet_sha256(cfs: Mapping[str, Any]) -> str:
    """Hash the complete CFS using the repository canonical JSON encoding."""

    try:
        text = canonical_authority_json_text(_thaw_authority_value(cfs))
    except (TypeError, ValueError) as exc:
        raise CFSIntegrityError(f"canonical fact sheet is not serializable: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_citation_usage_authority(
    evidence: CanonicalExperimentEvidence,
) -> Mapping[str, Any] | None:
    """Project exact Method/Experiments citation eligibility from canonical evidence."""

    cfs = build_canonical_fact_sheet(evidence)
    if cfs is None:
        return None
    artifact = evidence.execution_policy_artifact
    if artifact is None:
        raise CFSIntegrityError("canonical execution policy artifact is missing")
    policy = parse_domain_execution_policy_bytes(artifact.content)

    tokens: dict[str, dict[str, Any]] = {}

    def add_token(
        *,
        usage_token: str,
        usage_kind: str,
        section: str,
        claim_type: str,
        source_kind: str,
        source_identity: str,
        evidence_terms: tuple[str, ...],
    ) -> None:
        if usage_token in tokens:
            return
        if not evidence_terms or any(not term for term in evidence_terms):
            raise CFSIntegrityError("citation usage evidence terms are invalid")
        tokens[usage_token] = {
            "usage_token": usage_token,
            "usage_kind": usage_kind,
            "section": section,
            "claim_type": claim_type,
            "source_kind": source_kind,
            "source_identity": source_identity,
            "evidence_terms": evidence_terms,
        }

    condition_ids = tuple(str(item["id"]) for item in cfs["conditions"])
    evaluator_id = str(cfs["bound_labels"]["evaluator_id"])
    for source_kind, source_identity in (
        *(("condition", identity) for identity in condition_ids),
        ("evaluator", evaluator_id),
    ):
        registered = _METHOD_USAGE_REGISTRY.get(source_identity)
        if registered is None:
            continue
        usage_token, terms = registered
        add_token(
            usage_token=usage_token,
            usage_kind="method",
            section="Method",
            claim_type="algorithm_definition",
            source_kind=source_kind,
            source_identity=source_identity,
            evidence_terms=terms,
        )

    dataset = str(cfs["bound_labels"]["dataset"])
    add_token(
        usage_token=f"dataset:{_normalize(dataset)}",
        usage_kind="dataset",
        section="Experiments",
        claim_type="dataset_origin",
        source_kind="dataset",
        source_identity=dataset,
        evidence_terms=(dataset,),
    )
    for benchmark in cfs["bound_labels"]["benchmark_tokens"]:
        benchmark_text = str(benchmark)
        terms = (
            ("ISCAS-85", "ISCAS85")
            if benchmark_text == "iscas85"
            else (benchmark_text,)
        )
        add_token(
            usage_token=f"benchmark:{benchmark_text}",
            usage_kind="benchmark",
            section="Experiments",
            claim_type="benchmark_definition",
            source_kind="benchmark",
            source_identity=benchmark_text,
            evidence_terms=terms,
        )

    for field in _EVALUATION_PROTOCOL_FIELDS:
        identity = policy.get(field)
        if not isinstance(identity, str) or not identity:
            raise CFSIntegrityError(
                f"canonical evaluation protocol is missing: {field}"
            )
        add_token(
            usage_token=f"protocol:{identity}",
            usage_kind="evaluation_protocol",
            section="Experiments",
            claim_type="evaluation_protocol",
            source_kind="execution_policy",
            source_identity=identity,
            evidence_terms=(identity,),
        )

    return _freeze_authority_value(
        {
            "schema_version": _CITATION_USAGE_AUTHORITY_SCHEMA_VERSION,
            "policy_version": _CITATION_USAGE_POLICY_VERSION,
            "canonical_fact_sheet_sha256": canonical_fact_sheet_sha256(cfs),
            "execution_policy_sha256": artifact.sha256,
            "tokens": [tokens[key] for key in sorted(tokens)],
        }
    )


def render_fact_sheet_text(cfs: Mapping[str, Any], *, view: str) -> str:
    """Render one section-scoped, data-only grounding view."""

    payload = fact_sheet_view_payload(cfs, view=view)
    body = canonical_authority_json_text(_thaw_authority_value(payload)).rstrip("\n")
    text = f"canonical_fact_sheet view={view}\n```json\n{body}\n```"
    if view == "results":
        projection = render_observation_projection(
            cfs["observation_rows"],
            primary_metric_key=cfs["primary_metric"]["key"],
            metric_keys=cfs["metric_keys"],
        )
        text += f"\n```text\n{projection}\n```"
    return text


def render_complete_fact_sheet_text(
    cfs: Mapping[str, Any], *, include_projection: bool
) -> str:
    """Render the complete CFS, optionally with the bounded observation projection."""

    payload = {key: value for key, value in cfs.items() if key != "observation_rows"}
    body = canonical_authority_json_text(_thaw_authority_value(payload)).rstrip("\n")
    text = f"canonical_fact_sheet view=complete\n```json\n{body}\n```"
    if include_projection:
        projection = render_observation_projection(
            cfs["observation_rows"],
            primary_metric_key=cfs["primary_metric"]["key"],
            metric_keys=cfs["metric_keys"],
        )
        text += f"\n```text\n{projection}\n```"
    return text


def classify_out_of_scope_request(
    text: str, cfs: Mapping[str, Any]
) -> tuple[str, ...]:
    """Classify explicit review requests that exceed one canonical CFS."""

    if not isinstance(text, str) or not text.strip():
        return ()
    allowed_counts = {
        "seed": len(cfs["seeds"]),
        "condition": len(cfs["conditions"]),
        "variant": len(cfs["variant_ids"]),
    }
    codes: set[str] = set()
    for match in _REQUEST_COUNT.finditer(text):
        kind = match.group("kind").casefold().removesuffix("s")
        count_text = match.group("count").casefold()
        count = int(count_text) if count_text.isdigit() else _COUNT_WORDS[count_text]
        prefix = text[max(0, match.start() - 12) : match.start()].casefold()
        if count != allowed_counts[kind]:
            codes.add(f"{kind}_count_out_of_scope")
        elif re.search(r"\bonly\s*$", prefix):
            codes.add(f"{kind}_contract_limit_criticism")
    for match in _COUNT_CHANGE_INTENT.finditer(text):
        kind = match.group("kind").casefold().removesuffix("s")
        if kind == "condition" and _CONDITION_NAME_REQUEST.search(match.group(0)):
            continue
        if kind == "variant" and _VARIANT_NAME_REQUEST.search(match.group(0)):
            continue
        count = _requested_count(match.group(0), kind)
        if count is None or count != allowed_counts[kind]:
            codes.add(f"{kind}_count_out_of_scope")
    if _NEW_EXPERIMENT_REQUEST.search(text) or _NEW_RESULTS_REQUEST.search(text):
        codes.add("new_experiment_request")
    if _STATISTICAL_REQUEST.search(text):
        codes.add("unsupported_statistical_request")
    metric_keys = {str(item).casefold() for item in cfs["metric_keys"]}
    for match in _METRIC_REQUEST.finditer(text):
        if match.group("metric").casefold() not in metric_keys:
            codes.add("unknown_metric_request")
    condition_ids = {str(item["id"]).casefold() for item in cfs["conditions"]}
    for match in _CONDITION_NAME_REQUEST.finditer(text):
        if match.group("name").casefold() not in condition_ids:
            codes.add("unknown_condition_request")
    if _NEW_CONDITION_REQUEST.search(text):
        codes.add("new_condition_request")
    variant_ids = {str(item).casefold() for item in cfs["variant_ids"]}
    for match in _VARIANT_NAME_REQUEST.finditer(text):
        if match.group("name").casefold() not in variant_ids:
            codes.add("unknown_variant_request")
    return tuple(sorted(codes))


def is_exclusively_out_of_scope_request(
    text: str, cfs: Mapping[str, Any]
) -> bool:
    """Return true only when every substantive token is a classified request."""

    if not isinstance(text, str) or not text.strip():
        return False
    clauses = [
        clause.strip()
        for clause in _SUBSTANTIVE_CLAUSE_SPLIT.split(text)
        if clause.strip()
    ]
    if not clauses:
        return False
    for clause in clauses:
        if not classify_out_of_scope_request(clause, cfs):
            return False
        allowed = set(_EXCLUSIVE_REQUEST_WORDS)
        allowed.update(str(item).casefold() for item in cfs["metric_keys"])
        allowed.update(str(item["id"]).casefold() for item in cfs["conditions"])
        allowed.update(str(item).casefold() for item in cfs["variant_ids"])
        allowed.update(
            match.group("metric").casefold()
            for match in _METRIC_REQUEST.finditer(clause)
        )
        allowed.update(
            match.group("name").casefold()
            for pattern in (_CONDITION_NAME_REQUEST, _VARIANT_NAME_REQUEST)
            for match in pattern.finditer(clause)
        )
        allowed_count_spans = {
            match.span("count") for match in _REQUEST_COUNT.finditer(clause)
        }
        for match in _COUNT_CHANGE_INTENT.finditer(clause):
            kind = match.group("kind").casefold().removesuffix("s")
            count = _requested_count_token(match.group(0), kind)
            if count is not None:
                allowed_count_spans.add(
                    (match.start() + count[1], match.start() + count[2])
                )
        for match in _REQUEST_TOKEN.finditer(clause):
            token = match.group(0).casefold()
            if token[0].isdigit() or token in _COUNT_WORDS:
                if match.span() not in allowed_count_spans:
                    return False
            elif token not in allowed:
                return False
    return True


def _requested_count(fragment: str, kind: str) -> int | None:
    token = _requested_count_token(fragment, kind)
    if token is None:
        return None
    return int(token[0]) if token[0].isdigit() else _COUNT_WORDS[token[0]]


def _requested_count_token(
    fragment: str, kind: str
) -> tuple[str, int, int] | None:
    kind_pattern = rf"{re.escape(kind)}s?"
    after_kind = re.search(
        rf"\b{kind_pattern}\s+count\s+(?:to|of)\s+"
        rf"(?P<count>{_COUNT_TOKEN_PATTERN})\b",
        fragment,
        re.IGNORECASE,
    )
    if after_kind is not None:
        token = after_kind.group("count").casefold()
        start, end = after_kind.span("count")
        return token, start, end
    before_kind = re.search(
        rf"\b(?P<count>{_COUNT_TOKEN_PATTERN})\b"
        rf"(?:\s+[A-Za-z-]+){{0,2}}\s+{kind_pattern}\b",
        fragment,
        re.IGNORECASE,
    )
    if before_kind is None:
        return None
    token = before_kind.group("count").casefold()
    start, end = before_kind.span("count")
    return token, start, end


def fact_sheet_view_payload(
    cfs: Mapping[str, Any], *, view: str
) -> Mapping[str, Any]:
    """Return the exact deterministic authority exposed to one heading view."""

    if view not in _VIEWS:
        raise ValueError(f"unsupported canonical fact sheet view: {view}")
    common = {
        "schema_version": cfs["schema_version"],
        "claim_scope": cfs["claim_scope"],
        "dataset_origin": cfs["dataset_origin"],
        "bound_labels": cfs["bound_labels"],
        "conditions": cfs["conditions"],
    }
    if view == "introduction":
        payload = {
            **common,
            "seeds": cfs["seeds"],
            "counts": cfs["counts"],
        }
    elif view == "method":
        payload = {
            **common,
            "seeds": cfs["seeds"],
            "circuit_families": cfs["circuit_families"],
            "variant_ids": cfs["variant_ids"],
            "variants_per_family": cfs["variants_per_family"],
            "counts": cfs["counts"],
            "scale": cfs["scale"],
            "runtime": cfs["runtime"],
        }
    elif view == "results":
        payload = {
            **common,
            "primary_metric": cfs["primary_metric"],
            "condition_aggregates": cfs["condition_aggregates"],
            "per_seed_aggregates": cfs["per_seed_aggregates"],
        }
    else:
        payload = {
            **common,
            "primary_metric": cfs["primary_metric"],
            "counts": cfs["counts"],
            "seeds": cfs["seeds"],
            "circuit_families": cfs["circuit_families"],
            "variant_ids": cfs["variant_ids"],
            "variants_per_family": cfs["variants_per_family"],
            "scale": cfs["scale"],
            "derived_facts": cfs["derived_facts"],
        }
    return _freeze_authority_value(payload)


def fact_sheet_numeric_authority(
    cfs: Mapping[str, Any], *, view: str
) -> tuple[int | Decimal, ...]:
    """Return only metric values explicitly authoritative for one heading view."""

    return tuple(
        record["value"]
        for record in fact_sheet_numeric_authority_records(cfs, view=view)
    )


def fact_sheet_numeric_authority_records(
    cfs: Mapping[str, Any], *, view: str
) -> tuple[Mapping[str, Any], ...]:
    """Return metric values with stable pointers into the derived CFS."""

    if view not in _VIEWS:
        raise ValueError(f"unsupported canonical fact sheet view: {view}")
    if view != "results":
        return ()
    records: list[Mapping[str, Any]] = []
    for condition_index, condition in enumerate(cfs["condition_aggregates"]):
        for key in cfs["metric_keys"]:
            summary = condition["metrics"][key]
            for name in ("mean", "std", "min", "max"):
                records.append(
                    _freeze_authority_value(
                        {
                            "metric": key,
                            "pointer": (
                                "/derived/canonical_fact_sheet/v1/"
                                f"condition_aggregates/{condition_index}/"
                                f"metrics/{key}/{name}"
                            ),
                            "value": _metric_value(summary[name]),
                        }
                    )
                )
    for row_index, row in enumerate(cfs["per_seed_aggregates"]):
        for key in cfs["metric_keys"]:
            records.append(
                _freeze_authority_value(
                    {
                        "metric": key,
                        "pointer": (
                            "/derived/canonical_fact_sheet/v1/"
                            f"per_seed_aggregates/{row_index}/metrics/{key}"
                        ),
                        "value": _metric_value(row["metrics"][key]),
                    }
                )
            )
    return tuple(records)


def render_observation_projection(
    rows: Sequence[Mapping[str, Any]],
    *,
    primary_metric_key: str,
    metric_keys: Sequence[str],
    max_rows: int = MAX_PROJECTION_ROWS,
    max_bytes: int = MAX_PROJECTION_UTF8_BYTES,
) -> str:
    """Render complete observation rows under deterministic row and byte budgets."""

    if type(max_rows) is not int or max_rows < 0:
        raise ProjectionBudgetError("projection row budget is invalid")
    if type(max_bytes) is not int or max_bytes < 0:
        raise ProjectionBudgetError("projection byte budget is invalid")
    order = _metric_order(primary_metric_key, metric_keys)
    rendered = _validate_and_render_rows(rows, order)
    total = len(rendered)

    full = _projection_text("full", rendered, total, max_rows, max_bytes)
    if total <= max_rows and len(full.encode("utf-8")) <= max_bytes:
        return full

    grouped: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
    for identity, line in rendered:
        condition, seed_text, _variant = identity.split("/", 2)
        grouped[(condition, int(seed_text.removeprefix("seed=")))].append((identity, line))
    for group in grouped.values():
        group.sort(key=lambda item: item[0])

    if grouped:
        quota = min(max_rows // len(grouped), min(len(group) for group in grouped.values()))
        while quota > 0:
            selected = [item for key in sorted(grouped) for item in grouped[key][:quota]]
            sampled = _projection_text(
                "sampled", selected, total, max_rows, max_bytes
            )
            if len(sampled.encode("utf-8")) <= max_bytes:
                return sampled
            quota -= 1

    aggregate = _projection_text("aggregate_only", [], total, max_rows, max_bytes)
    if len(aggregate.encode("utf-8")) > max_bytes:
        raise ProjectionBudgetError("projection header exceeds byte budget")
    return aggregate


def compose_grounding_context(cfs: Mapping[str, Any] | None, *, view: str) -> str:
    """Compose the section-scoped CFS and its optional observation projection."""

    if cfs is None:
        raise ValueError("canonical fact sheet is required")
    return render_fact_sheet_text(cfs, view=view)


def fact_sheet_view_for_heading(heading: str) -> str:
    """Map an active template heading to one deterministic grounding view."""

    normalized = heading.casefold()
    if any(token in normalized for token in ("result", "discussion")):
        return "results"
    if any(
        token in normalized
        for token in (
            "method",
            "experiment",
            "model",
            "theoretical",
            "phenomenology",
            "computational",
        )
    ):
        return "method"
    if any(token in normalized for token in ("limitation", "conclusion")):
        return "limitations"
    return "introduction"


def build_heading_grounding_contexts(
    cfs: Mapping[str, Any], headings: Sequence[str]
) -> Mapping[str, str]:
    """Render exact section-scoped contexts for one active heading template."""

    if not headings or any(not isinstance(heading, str) or not heading for heading in headings):
        raise ValueError("grounding headings must be non-empty strings")
    return {
        heading: compose_grounding_context(
            cfs, view=fact_sheet_view_for_heading(heading)
        )
        for heading in headings
    }


def _is_domain_evaluator_v2(evidence: Any) -> bool:
    manifest = evidence.manifest
    candidate = evidence.candidate
    selected = evidence.selected_result
    return bool(
        type(manifest.get("schema_version")) is int
        and manifest["schema_version"] == 2
        and manifest.get("generation_kind") == "domain_evaluator"
        and type(candidate.get("schema_version")) is int
        and candidate["schema_version"] == 2
        and type(selected.get("schema_version")) is int
        and selected["schema_version"] == 2
        and selected.get("result_set_type") == "stage13_refinement"
    )


def _build_validated_fact_sheet(
    evidence: CanonicalExperimentEvidence,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    observations: Mapping[str, Any],
    results: Mapping[str, Any],
    replayed_flat_metrics: Mapping[str, Any],
) -> Mapping[str, Any]:
    if type(contract.get("schema_version")) is not int or contract["schema_version"] != 3:
        raise CFSIntegrityError("experiment contract schema is invalid")
    if (
        type(policy.get("schema_version")) is not int
        or policy["schema_version"] != 1
        or type(policy.get("execution_policy_version")) is not int
        or policy["execution_policy_version"] != 1
    ):
        raise CFSIntegrityError("execution policy schema is invalid")
    if (
        type(observations.get("schema_version")) is not int
        or observations["schema_version"] != 2
        or type(observations.get("observation_policy_version")) is not int
        or observations["observation_policy_version"] != 1
    ):
        raise CFSIntegrityError("observation schema is invalid")
    if (
        type(results.get("schema_version")) is not int
        or results["schema_version"] != 2
        or type(results.get("results_policy_version")) is not int
        or results["results_policy_version"] != 2
    ):
        raise CFSIntegrityError("structured result schema is invalid")
    conditions = _string_tuple(policy.get("conditions"), "conditions")
    seeds = _int_tuple(policy.get("seeds"), "seeds")
    families = _string_tuple(policy.get("circuit_families"), "circuit families")
    variants_per_family = policy.get("variants_per_family")
    invocation_count = policy.get("invocation_count")
    metric_keys = _string_tuple(policy.get("metric_keys"), "metric keys")
    if tuple(observations.get("metric_keys", ())) != metric_keys:
        raise CFSIntegrityError("observation metric key order mismatch")
    primary_condition = _required_string(policy, "primary_condition")
    primary_key = _required_string(policy, "primary_metric_key")
    if (
        type(variants_per_family) is not int
        or variants_per_family <= 0
        or type(invocation_count) is not int
        or invocation_count <= 0
        or primary_condition not in conditions
        or primary_key not in metric_keys
    ):
        raise CFSIntegrityError("execution policy identity is invalid")

    variants = tuple(
        f"{family}_ht{variant}"
        for family in families
        for variant in range(1, variants_per_family + 1)
    )
    rows = observations.get("observations")
    if not isinstance(rows, list):
        raise CFSIntegrityError("metric observation rows are invalid")
    expected_identities = {
        (condition, seed, variant)
        for condition in conditions
        for seed in seeds
        for variant in variants
    }
    projected_rows, identities, n_totals = _canonical_observation_rows(
        rows, metric_keys, families
    )
    if identities != expected_identities:
        raise CFSIntegrityError("metric observation identity closure mismatch")

    _validate_flat_metric_projection(
        replayed_flat_metrics, projected_rows, metric_keys
    )
    _validate_flat_metric_projection(
        evidence.metric_observations, projected_rows, metric_keys
    )
    aggregates = _canonical_aggregates(observations.get("aggregate"), metric_keys)
    result_aggregates = _canonical_aggregates(results.get("structured_results"), metric_keys)
    if aggregates != result_aggregates:
        raise CFSIntegrityError("structured result aggregates mismatch observations")
    per_seed = _canonical_per_seed(observations.get("per_seed"), metric_keys)
    expected_per_seed = _recompute_per_seed(
        projected_rows, conditions, seeds, metric_keys, len(variants)
    )
    _assert_aggregate_replay(per_seed, expected_per_seed, "per-seed")
    expected_aggregates = _recompute_condition_aggregates(
        expected_per_seed, conditions, metric_keys, len(seeds)
    )
    _assert_aggregate_replay(aggregates, expected_aggregates, "condition")
    _validate_results_schema(
        results,
        evidence,
        result_aggregates,
        observations.get("primary_metric"),
    )

    primary = observations.get("primary_metric")
    if not isinstance(primary, dict):
        raise CFSIntegrityError("primary metric is invalid")
    expected_primary = {
        "key": primary_key,
        "condition": primary_condition,
        "aggregation": _required_string(policy, "primary_aggregation"),
        "observation_set": _required_string(policy, "primary_observation_set"),
    }
    replayed_aggregate_map = {
        item["condition"]: item for item in expected_aggregates
    }
    primary_value = replayed_aggregate_map[primary_condition]["metrics"][primary_key][
        "mean"
    ]
    canonical_primary = {**expected_primary, "value": primary_value}
    for stored in (
        primary,
        evidence.manifest.get("primary_metric"),
        evidence.candidate.get("primary_metric"),
        results.get("primary_metric"),
    ):
        replayed_primary = _canonical_primary_metric(stored)
        if any(
            replayed_primary[key] != expected_primary[key]
            for key in expected_primary
        ) or not _decimal_replay_equal(replayed_primary["value"], primary_value):
            raise CFSIntegrityError("stored primary metric projection mismatch")

    runtime = policy.get("runtime_projection")
    if not isinstance(runtime, dict):
        raise CFSIntegrityError("runtime projection is missing")
    packages = runtime.get("packages")
    if not isinstance(packages, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and _valid_prompt_identity(key)
        and _valid_prompt_identity(value)
        for key, value in packages.items()
    ):
        raise CFSIntegrityError("runtime package projection is invalid")
    if type(runtime.get("torch_deterministic_algorithms")) is not bool:
        raise CFSIntegrityError("runtime deterministic flag is invalid")
    if type(runtime.get("torch_num_threads")) is not int:
        raise CFSIntegrityError("runtime thread count is invalid")

    dataset = _required_string(contract, "dataset_name")
    claim_scope = _required_string(contract, "claim_scope")
    dataset_origin = _required_string(contract, "dataset_origin")
    evaluator_authority = contract.get("metric_authority")
    if not isinstance(evaluator_authority, dict):
        raise CFSIntegrityError("metric authority is missing")
    evaluator_id = _required_string(evaluator_authority, "evaluator_id")
    evaluator_schema = evidence.manifest.get("bindings", {}).get("evaluator_schema")
    if not _valid_prompt_identity(evaluator_schema):
        raise CFSIntegrityError("evaluator schema binding is missing")
    manifest_bindings = evidence.manifest.get("bindings")
    if not isinstance(manifest_bindings, Mapping):
        raise CFSIntegrityError("root bindings are invalid")
    for key in ("claim_scope", "dataset_origin", "dataset_name"):
        if manifest_bindings.get(key) != contract.get(key):
            raise CFSIntegrityError(f"contract/root binding mismatch: {key}")

    benchmark_tokens = _benchmark_tokens((dataset, evaluator_schema, evaluator_id))
    circuit_tokens = tuple(sorted({_normalize(item) for item in (*families, *variants)}))
    condition_entries = tuple(
        {"id": condition, "role": "primary" if condition == primary_condition else "comparator"}
        for condition in conditions
    )
    observation_identity_payload = [
        {"condition": condition, "seed": seed, "circuit_variant": variant}
        for condition, seed, variant in sorted(identities)
    ]
    identity_sha = hashlib.sha256(
        canonical_authority_json_text(observation_identity_payload).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": _CFS_SCHEMA_VERSION,
        "dataset_origin": dataset_origin,
        "claim_scope": claim_scope,
        "bound_labels": {
            "dataset": dataset,
            "evaluator_schema": evaluator_schema,
            "evaluator_id": evaluator_id,
            "benchmark_tokens": benchmark_tokens,
            "circuit_tokens": circuit_tokens,
        },
        "conditions": condition_entries,
        "seeds": seeds,
        "circuit_families": families,
        "variants_per_family": variants_per_family,
        "variant_ids": variants,
        "counts": {
            "observations": len(projected_rows),
            "observations_per_condition": len(seeds) * len(variants),
            "observations_per_condition_seed": len(variants),
            "invocations": invocation_count,
        },
        "metric_keys": metric_keys,
        "primary_metric": canonical_primary,
        "condition_aggregates": expected_aggregates,
        "per_seed_aggregates": expected_per_seed,
        "scale": {"n_total_min": min(n_totals), "n_total_max": max(n_totals)},
        "runtime": {
            "device": _required_string(runtime, "device"),
            "python": _required_string(runtime, "python_major_minor"),
            "packages": dict(sorted(packages.items())),
            "torch_deterministic_algorithms": runtime["torch_deterministic_algorithms"],
            "torch_num_threads": runtime["torch_num_threads"],
        },
        "derived_facts": {},
        "provenance": {"observation_identities_sha256": identity_sha},
        "observation_rows": projected_rows,
    }
    return _freeze_authority_value(payload)


def _canonical_observation_rows(
    rows: Sequence[Any], metric_keys: tuple[str, ...], families: tuple[str, ...]
) -> tuple[list[dict[str, Any]], set[tuple[str, int, str]], list[int]]:
    projected: list[dict[str, Any]] = []
    identities: set[tuple[str, int, str]] = set()
    n_totals: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CFSIntegrityError("metric observation row is invalid")
        condition = _required_string(row, "condition")
        variant = _required_string(row, "circuit_variant")
        family = _required_string(row, "circuit_family")
        seed = row.get("seed")
        n_total = row.get("n_total")
        if type(seed) is not int or type(n_total) is not int or n_total < 0:
            raise CFSIntegrityError("metric observation identity/count is invalid")
        if family not in families or not variant.startswith(family + "_"):
            raise CFSIntegrityError("metric observation family binding mismatch")
        identity = (condition, seed, variant)
        if identity in identities:
            raise CFSIntegrityError("duplicate metric observation identity")
        identities.add(identity)
        metrics = _canonical_metric_map(row.get("metrics"), metric_keys)
        projected.append(
            {"condition": condition, "seed": seed, "circuit_variant": variant, "metrics": metrics}
        )
        n_totals.append(n_total)
    return projected, identities, n_totals


def _validate_flat_metric_projection(
    flat: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], metric_keys: tuple[str, ...]
) -> None:
    if set(flat) != set(metric_keys):
        raise CFSIntegrityError("flat metric projection keys mismatch")
    for key in metric_keys:
        values = flat[key]
        if not isinstance(values, (list, tuple)) or len(values) != len(rows):
            raise CFSIntegrityError("flat metric projection length mismatch")
        if any(
            not _decimal_replay_equal(value, row["metrics"][key])
            for value, row in zip(values, rows, strict=True)
        ):
            raise CFSIntegrityError("flat metric projection value mismatch")


def _canonical_aggregates(value: Any, metric_keys: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise CFSIntegrityError("condition aggregates are invalid")
    result = []
    for item in value:
        if not isinstance(item, Mapping) or type(item.get("n_seeds")) is not int:
            raise CFSIntegrityError("condition aggregate entry is invalid")
        _exact_fields(item, {"condition", "metrics", "n_seeds"}, "condition aggregate")
        metrics = item.get("metrics")
        if not isinstance(metrics, Mapping) or set(metrics) != set(metric_keys):
            raise CFSIntegrityError("condition aggregate metrics mismatch")
        canonical_metrics = {}
        for key in metric_keys:
            summary = metrics[key]
            if not isinstance(summary, Mapping) or set(summary) != {"mean", "std", "min", "max"}:
                raise CFSIntegrityError("condition aggregate summary mismatch")
            canonical_metrics[key] = {
                name: _metric_value(summary[name])
                for name in ("mean", "std", "min", "max")
            }
        result.append(
            {
                "condition": _required_string(item, "condition"),
                "n_seeds": item["n_seeds"],
                "metrics": canonical_metrics,
            }
        )
    return tuple(result)


def _canonical_per_seed(value: Any, metric_keys: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise CFSIntegrityError("per-seed aggregates are invalid")
    result = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or type(item.get("seed")) is not int
            or type(item.get("n_variants")) is not int
        ):
            raise CFSIntegrityError("per-seed aggregate entry is invalid")
        _exact_fields(
            item,
            {"condition", "seed", "n_variants", "metrics"},
            "per-seed aggregate",
        )
        result.append({
            "condition": _required_string(item, "condition"),
            "seed": item["seed"],
            "n_variants": item["n_variants"],
            "metrics": _canonical_metric_map(item.get("metrics"), metric_keys),
        })
    return tuple(result)


def _canonical_metric_map(
    value: Any, metric_keys: tuple[str, ...]
) -> dict[str, int | Decimal]:
    if not isinstance(value, Mapping) or set(value) != set(metric_keys):
        raise CFSIntegrityError("metric layout mismatch")
    return {key: _metric_value(value[key]) for key in metric_keys}


def _canonical_primary_metric(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CFSIntegrityError("stored primary metric is invalid")
    expected = {"key", "condition", "aggregation", "observation_set", "value"}
    if set(value) != expected:
        raise CFSIntegrityError("stored primary metric schema mismatch")
    return {
        "key": _required_string(value, "key"),
        "condition": _required_string(value, "condition"),
        "aggregation": _required_string(value, "aggregation"),
        "observation_set": _required_string(value, "observation_set"),
        "value": _metric_value(value["value"]),
    }


def _metric_text(value: Any) -> str:
    return canonical_decimal(_metric_value(value))


def _metric_value(value: Any) -> int | Decimal:
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return value
    raise CFSIntegrityError("metric value must be a finite true int or Decimal")


def _recompute_per_seed(
    rows: Sequence[Mapping[str, Any]],
    conditions: tuple[str, ...],
    seeds: tuple[int, ...],
    metric_keys: tuple[str, ...],
    variants_per_group: int,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for condition in conditions:
        for seed in seeds:
            selected = [
                row
                for row in rows
                if row["condition"] == condition and row["seed"] == seed
            ]
            if len(selected) != variants_per_group:
                raise CFSIntegrityError("per-seed observation closure mismatch")
            result.append(
                {
                    "condition": condition,
                    "seed": seed,
                    "n_variants": variants_per_group,
                    "metrics": {
                        key: _decimal_mean(
                            [_as_decimal(row["metrics"][key]) for row in selected]
                        )
                        for key in metric_keys
                    },
                }
            )
    return tuple(result)


def _recompute_condition_aggregates(
    per_seed: Sequence[Mapping[str, Any]],
    conditions: tuple[str, ...],
    metric_keys: tuple[str, ...],
    seed_count: int,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for condition in conditions:
        selected = [item for item in per_seed if item["condition"] == condition]
        if len(selected) != seed_count:
            raise CFSIntegrityError("condition aggregate seed closure mismatch")
        metrics: dict[str, dict[str, Decimal]] = {}
        for key in metric_keys:
            values = [_as_decimal(item["metrics"][key]) for item in selected]
            mean = _decimal_mean(values)
            with localcontext() as context:
                context.prec = 50
                context.rounding = ROUND_HALF_EVEN
                variance = sum(
                    ((item - mean) ** 2 for item in values), Decimal(0)
                ) / Decimal(len(values) - 1)
                std = +variance.sqrt()
            metrics[key] = {
                "mean": mean,
                "std": std,
                "min": min(values),
                "max": max(values),
            }
        result.append(
            {"condition": condition, "n_seeds": seed_count, "metrics": metrics}
        )
    return tuple(result)


def _assert_aggregate_replay(
    stored: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    label: str,
) -> None:
    if len(stored) != len(expected):
        raise CFSIntegrityError(f"{label} aggregate cardinality mismatch")
    for actual, replayed in zip(stored, expected, strict=True):
        identity_fields = (
            ("condition", "seed", "n_variants")
            if label == "per-seed"
            else ("condition", "n_seeds")
        )
        if any(actual.get(field) != replayed.get(field) for field in identity_fields):
            raise CFSIntegrityError(f"{label} aggregate identity mismatch")
        if set(actual["metrics"]) != set(replayed["metrics"]):
            raise CFSIntegrityError(f"{label} aggregate metric layout mismatch")
        for key, expected_value in replayed["metrics"].items():
            actual_value = actual["metrics"][key]
            if isinstance(expected_value, Mapping):
                if set(actual_value) != set(expected_value):
                    raise CFSIntegrityError(f"{label} metric summary mismatch")
                for summary_key, summary_value in expected_value.items():
                    if not _decimal_replay_equal(
                        actual_value[summary_key], summary_value
                    ):
                        raise CFSIntegrityError(f"{label} aggregate value mismatch")
            elif not _decimal_replay_equal(actual_value, expected_value):
                raise CFSIntegrityError(f"{label} aggregate value mismatch")


def _decimal_mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise CFSIntegrityError("cannot average empty metric values")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return +(sum(values, Decimal(0)) / Decimal(len(values)))


def _as_decimal(value: Any) -> Decimal:
    canonical = _metric_value(value)
    return Decimal(canonical)


def _decimal_replay_equal(actual: Any, expected: Any) -> bool:
    left = _as_decimal(actual)
    right = _as_decimal(expected)
    if left == right:
        return True
    # All fixed-domain metrics are ratios. Stored Fraction projections and
    # Decimal-row replay are independently rounded at precision 50, so a fixed
    # absolute final-place bound covers their observed replay difference.
    return abs(left - right) <= _DECIMAL_REPLAY_TOLERANCE


def _validate_results_schema(
    results: Mapping[str, Any],
    evidence: CanonicalExperimentEvidence,
    aggregates: Sequence[Mapping[str, Any]],
    primary: Any,
) -> None:
    _exact_fields(
        results,
        {
            "schema_version",
            "results_policy_version",
            "runs",
            "observations",
            "structured_results",
            "primary_metric",
        },
        "structured results",
    )
    runs = results["runs"]
    if not isinstance(runs, list) or len(runs) != 2:
        raise CFSIntegrityError("structured result run references are invalid")
    for item in runs:
        _validate_file_ref(item, "structured result run")
    observation_ref = results["observations"]
    _validate_file_ref(observation_ref, "structured result observations")
    artifact = evidence.selected_execution_artifact
    if observation_ref != {
        "path": artifact.path,
        "sha256": artifact.sha256,
        "size": len(artifact.content),
    }:
        raise CFSIntegrityError("structured result observation binding mismatch")
    if tuple(results["structured_results"]) != tuple(aggregates):
        raise CFSIntegrityError("structured result aggregate projection mismatch")
    if _canonical_primary_metric(results["primary_metric"]) != (
        _canonical_primary_metric(primary)
    ):
        raise CFSIntegrityError("structured result primary metric mismatch")


def _validate_file_ref(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise CFSIntegrityError(f"{label} must be an object")
    _exact_fields(value, {"path", "sha256", "size"}, label)
    if (
        not _valid_prompt_identity(value["path"], max_length=256)
        or not isinstance(value["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        or type(value["size"]) is not int
        or value["size"] < 0
    ):
        raise CFSIntegrityError(f"{label} is invalid")


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CFSIntegrityError(f"{label} schema mismatch")


def _valid_prompt_identity(value: Any, *, max_length: int = 128) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= max_length
        and _PROMPT_IDENTITY.fullmatch(value)
    )


def _validate_and_render_rows(
    rows: Sequence[Mapping[str, Any]], order: tuple[str, ...]
) -> list[tuple[str, str]]:
    rendered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise CFSIntegrityError("projection row is invalid")
        condition = _required_string(row, "condition")
        variant = _required_string(row, "circuit_variant")
        seed = row.get("seed")
        if type(seed) is not int:
            raise CFSIntegrityError("projection seed is invalid")
        identity = f"{condition}/seed={seed}/{variant}"
        if identity in seen:
            raise CFSIntegrityError("duplicate projection identity")
        seen.add(identity)
        metrics = _canonical_metric_map(row.get("metrics"), order)
        values = " ".join(f"{key}={metrics[key]}" for key in order)
        rendered.append((identity, f"obs {identity}: {values}"))
    return rendered


def _projection_text(
    mode: str,
    rows: Sequence[tuple[str, str]],
    total: int,
    max_rows: int,
    max_bytes: int,
) -> str:
    header = (
        f"projection mode={mode} shown={len(rows)} total={total} "
        f"budget_rows={max_rows} budget_bytes={max_bytes}"
    )
    if not rows:
        return header
    return "\n".join((header, *(line for _identity, line in rows)))


def _metric_order(primary: str, keys: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(primary, str) or not primary:
        raise CFSIntegrityError("primary metric key is invalid")
    if not isinstance(keys, (list, tuple)) or any(
        not isinstance(key, str) or not key for key in keys
    ):
        raise CFSIntegrityError("metric key order is invalid")
    ordered = (primary,) + tuple(key for key in keys if key != primary)
    if primary not in keys or len(set(ordered)) != len(ordered):
        raise CFSIntegrityError("metric key order is inconsistent")
    return ordered


def _parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = _parse_json_value(content.decode("utf-8"), label)
    except (UnicodeDecodeError, CanonicalExperimentEvidenceError) as exc:
        raise CFSIntegrityError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CFSIntegrityError(f"{label} root is invalid")
    return value


def _plain_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    thawed = _thaw_authority_value(value)
    if not isinstance(thawed, dict):
        raise CFSIntegrityError(f"{label} root is invalid")
    return thawed


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not _valid_prompt_identity(item):
        raise CFSIntegrityError(f"{key} is invalid")
    return item


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or any(
        not _valid_prompt_identity(item) for item in value
    ):
        raise CFSIntegrityError(f"{label} are invalid")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise CFSIntegrityError(f"{label} contain duplicates")
    return result


def _int_tuple(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value or any(
        type(item) is not int for item in value
    ):
        raise CFSIntegrityError(f"{label} are invalid")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise CFSIntegrityError(f"{label} contain duplicates")
    return result


def _benchmark_tokens(values: Sequence[str]) -> tuple[str, ...]:
    tokens: set[str] = set()
    for value in values:
        for segment in re.split(r"[^A-Za-z0-9]+", value):
            segment = segment.casefold()
            if _BENCHMARK_TOKEN.fullmatch(segment) and not _VERSION_TOKEN.fullmatch(segment):
                tokens.add(segment)
    return tuple(sorted(tokens))


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
