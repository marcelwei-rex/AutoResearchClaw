"""Independent standard-library verifier for TrojNet score evidence."""

import hashlib
import json
import re
import sys
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from pathlib import Path


NODE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SCORE_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
GATES = {"AND", "NAND", "OR", "NOR", "XOR", "XNOR", "NOT", "BUF", "DFF"}
METRIC_KEYS = (
    "accuracy", "auprc", "auroc", "f1", "fpr", "precision", "recall",
    "top_k_precision",
)
SEEDS = (0, 1, 2)
CIRCUIT_FAMILIES = ("c1355", "c1908", "c3540", "c432", "c6288", "c880")
CONDITIONS = ("raw_cc1", "scoap_isolation_forest", "trojnet_community_graphsage")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_text_bytes(path: Path, label: str) -> str:
    return _decode_text_bytes(path.read_bytes(), label)


def _decode_text_bytes(data: bytes, label: str) -> str:
    if not data.endswith(b"\n") or b"\r" in data or b"\0" in data or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{label} has noncanonical bytes")
    return data.decode("utf-8")


def _bench_nodes(path: Path) -> list[str]:
    nodes: list[str] = []
    defined: set[str] = set()
    referenced: set[str] = set()
    outputs: set[str] = set()

    def add(name: str) -> None:
        if name not in nodes:
            nodes.append(name)

    for raw in _read_text_bytes(path, "bench").splitlines():
        semantic = raw.split("#", 1)[0].strip(" \t")
        if not semantic:
            continue
        if semantic.startswith("INPUT(") and semantic.endswith(")"):
            name = semantic[6:-1]
            if NODE_RE.fullmatch(name) is None or name in defined:
                raise ValueError("invalid or duplicate INPUT")
            add(name)
            defined.add(name)
            continue
        if semantic.startswith("OUTPUT(") and semantic.endswith(")"):
            name = semantic[7:-1]
            if NODE_RE.fullmatch(name) is None or name in outputs:
                raise ValueError("invalid or duplicate OUTPUT")
            add(name)
            outputs.add(name)
            continue
        match = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_]*) = ([A-Z]+)\(([A-Za-z_][A-Za-z0-9_]*(?:, [A-Za-z_][A-Za-z0-9_]*)*)\)",
            semantic,
        )
        if match is None or match.group(2) not in GATES:
            raise ValueError("invalid bench statement")
        target = match.group(1)
        inputs = match.group(3).split(", ")
        if target in defined or len(inputs) != len(set(inputs)):
            raise ValueError("duplicate bench definition or fan-in")
        add(target)
        defined.add(target)
        for name in inputs:
            add(name)
            referenced.add(name)
    if not nodes or not referenced.issubset(defined) or not outputs.issubset(defined):
        raise ValueError("bench references unresolved nodes")
    return nodes


def _labels(path: Path, nodes: list[str]) -> set[str]:
    values = _read_text_bytes(path, "labels").splitlines()
    if not values or any(NODE_RE.fullmatch(item) is None or item.strip() != item for item in values):
        raise ValueError("label grammar mismatch")
    labels = set(values)
    if len(labels) != len(values) or not labels < set(nodes):
        raise ValueError("label namespace mismatch")
    return labels


def _ratio(numerator: int, denominator: int) -> Fraction:
    return Fraction(numerator, denominator) if denominator else Fraction(0)


def _metrics(scores: list[Decimal], labels: list[int]) -> dict[str, Fraction]:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        raise ValueError("each observation needs positive and negative labels")
    positive_scores = [score for score, label in zip(scores, labels) if label]
    negative_scores = [score for score, label in zip(scores, labels) if not label]
    greater = sum(p > n for p in positive_scores for n in negative_scores)
    ties = sum(p == n for p in positive_scores for n in negative_scores)
    auroc = Fraction(2 * greater + ties, 2 * positives * negatives)

    groups: list[tuple[Decimal, int, int]] = []
    for score in sorted(set(scores), reverse=True):
        indexes = [index for index, value in enumerate(scores) if value == score]
        groups.append((score, sum(labels[index] for index in indexes), len(indexes)))
    tp = fp = 0
    previous_recall = Fraction(0)
    auprc = Fraction(0)
    candidates = [(Fraction(0), 0, 0)]
    for _, group_positive, group_total in groups:
        tp += group_positive
        fp += group_total - group_positive
        recall = Fraction(tp, positives)
        precision = Fraction(tp, tp + fp)
        auprc += (recall - previous_recall) * precision
        previous_recall = recall
        candidates.append((Fraction(tp, positives) - Fraction(fp, negatives), tp, fp))
    _, selected_tp, selected_fp = max(candidates, key=lambda item: item[0])
    fn = positives - selected_tp
    tn = negatives - selected_fp
    precision = _ratio(selected_tp, selected_tp + selected_fp)
    recall = Fraction(selected_tp, positives)
    f1 = _ratio(2 * selected_tp, 2 * selected_tp + selected_fp + fn)

    k = positives
    cutoff = sorted(scores, reverse=True)[k - 1]
    above = [index for index, score in enumerate(scores) if score > cutoff]
    tied = [index for index, score in enumerate(scores) if score == cutoff]
    remaining = k - len(above)
    top_positive = sum(labels[index] for index in above)
    top_positive += Fraction(remaining * sum(labels[index] for index in tied), len(tied))
    return {
        "accuracy": Fraction(selected_tp + tn, len(labels)),
        "auprc": auprc,
        "auroc": auroc,
        "f1": f1,
        "fpr": Fraction(selected_fp, negatives),
        "precision": precision,
        "recall": recall,
        "top_k_precision": top_positive / k,
    }


def _decimal_text(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        decimal = +(Decimal(value.numerator) / Decimal(value.denominator))
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _sqrt_text(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        decimal = +(
            Decimal(value.numerator) / Decimal(value.denominator)
        ).sqrt()
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_json(value: object) -> str:
    if isinstance(value, Fraction):
        return _decimal_text(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite Decimal is forbidden")
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in {"", "-0"} else text
    if value is None or type(value) in {bool, int, str}:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON keys must be strings")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=True) + ":" + _canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def _mean(values: list[Fraction]) -> Fraction:
    if not values:
        raise ValueError("cannot average an empty sequence")
    return sum(values, Fraction(0)) / len(values)


def _build_per_seed(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for condition in CONDITIONS:
        for seed in SEEDS:
            selected = [
                row for row in rows
                if row["condition"] == condition and row["seed"] == seed
            ]
            if len(selected) != 18:
                raise ValueError("per-seed observation closure mismatch")
            result.append({
                "condition": condition,
                "metrics": {
                    key: _mean([row["metrics"][key] for row in selected])
                    for key in METRIC_KEYS
                },
                "n_variants": 18,
                "seed": seed,
            })
    return result


def _build_aggregate(per_seed: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for condition in CONDITIONS:
        selected = [row for row in per_seed if row["condition"] == condition]
        if len(selected) != 3:
            raise ValueError("aggregate seed closure mismatch")
        metrics: dict[str, object] = {}
        for key in METRIC_KEYS:
            values = [row["metrics"][key] for row in selected]
            mean = _mean(values)
            variance = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
            metrics[key] = {
                "max": max(values),
                "mean": mean,
                "min": min(values),
                "std": Decimal(_sqrt_text(variance)),
            }
        result.append({"condition": condition, "metrics": metrics, "n_seeds": 3})
    return result


def verify(
    *,
    data_root: Path,
    scores_path: Path,
    output_path: Path,
    dataset_capture_sha256: str,
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", dataset_capture_sha256) is None:
        raise ValueError("dataset capture hash is invalid")
    score_bytes = scores_path.read_bytes()
    rows: list[dict[str, object]] = []
    expected_order = [
        (condition, seed, family, f"{family}_ht{variant}")
        for seed in SEEDS
        for family in CIRCUIT_FAMILIES
        for variant in (1, 2, 3)
        for condition in CONDITIONS
    ]
    for ordinal, line in enumerate(
        _decode_text_bytes(score_bytes, "score evidence").splitlines()
    ):
        row = json.loads(line, object_pairs_hook=_strict_object)
        if set(row) != {"schema_version", "condition", "seed", "circuit_family", "circuit_variant", "node_ids", "scores"}:
            raise ValueError("score row schema mismatch")
        if type(row["schema_version"]) is not int or row["schema_version"] != 1 or type(row["seed"]) is not int:
            raise ValueError("score row version or seed mismatch")
        if not isinstance(row["scores"], list) or not all(
            isinstance(item, str)
            and item != "-0"
            and SCORE_RE.fullmatch(item)
            for item in row["scores"]
        ):
            raise ValueError("score grammar mismatch")
        family = row["circuit_family"]
        variant = row["circuit_variant"]
        if (row["condition"], row["seed"], family, variant) != expected_order[ordinal]:
            raise ValueError("score row authority order mismatch")
        bench = data_root / family / f"{variant}.bench"
        labels_path = data_root / family / f"{variant}_trojan_nodes.txt"
        nodes = _bench_nodes(bench)
        if row["node_ids"] != nodes or len(row["scores"]) != len(nodes):
            raise ValueError("score node namespace mismatch")
        label_names = _labels(labels_path, nodes)
        metrics = _metrics(
            [Decimal(item) for item in row["scores"]],
            [int(node in label_names) for node in nodes],
        )
        rows.append({
            "circuit_family": family,
            "circuit_variant": variant,
            "condition": row["condition"],
            "metrics": metrics,
            "n_total": len(nodes),
            "n_trojan": len(label_names),
            "seed": row["seed"],
        })
    if len(rows) != 162:
        raise ValueError("verification requires exactly 162 observations")
    per_seed = _build_per_seed(rows)
    aggregate = _build_aggregate(per_seed)
    primary_values = [
        row["metrics"]["auprc"]
        for row in per_seed
        if row["condition"] == "trojnet_community_graphsage"
    ]
    payload = {
        "aggregate": aggregate,
        "dataset_capture_sha256": dataset_capture_sha256,
        "metric_keys": list(METRIC_KEYS),
        "observation_policy_version": 1,
        "observations": rows,
        "per_seed": per_seed,
        "primary_metric": {
            "aggregation": "mean_variants_then_mean_seeds_v1",
            "condition": "trojnet_community_graphsage",
            "key": "auprc",
            "observation_set": "exact_18_variants_per_seed",
            "value": _mean(primary_values),
        },
        "schema_version": 2,
        "score_evidence_sha256": hashlib.sha256(score_bytes).hexdigest(),
    }
    output_path.write_bytes((_canonical_json(payload) + "\n").encode("utf-8"))


def main() -> int:
    if len(sys.argv) != 9 or sys.argv[1::2] != [
        "--data-root", "--scores", "--output", "--dataset-capture-sha256"
    ]:
        raise ValueError("verifier argv mismatch")
    verify(
        data_root=Path(sys.argv[2]),
        scores_path=Path(sys.argv[4]),
        output_path=Path(sys.argv[6]),
        dataset_capture_sha256=sys.argv[8],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
