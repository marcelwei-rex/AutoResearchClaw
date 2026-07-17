"""
trojnet/eval.py
---------------
Evaluation metrics for TrojNet:

  Primary metrics (threshold-free):
    - AUROC          : Area Under the ROC curve
    - AUPRC          : Area Under the Precision-Recall curve

  Detection metrics (threshold at Youden-optimal operating point):
    - Accuracy, Precision, Recall, F1, FPR

  Localisation metrics:
    - top_k_precision : fraction of true Trojan nodes in top-k scored nodes
    - trigger_coverage: fraction of trigger-chain nodes in top-k scored nodes

  Stability metrics:
    - stability_stats : over N independent runs, compute mean/std/min AUROC

  Statistical tests:
    - wilcoxon_auroc  : paired Wilcoxon signed-rank test comparing two methods
                        across multiple circuits
    - bootstrap_ci    : 95% CI for AUROC via stratified bootstrap

Usage:
    from trojnet.eval import evaluate_circuit, stability_run, wilcoxon_auroc

    metrics = evaluate_circuit(node_scores, true_labels)
    # metrics: dict with keys auroc, auprc, acc, precision, recall, f1, fpr

    stats = stability_run(score_fn, n_runs=10)
    # stats: dict with auroc_mean, auroc_std, auroc_min

    p_val = wilcoxon_auroc(aurocs_method_a, aurocs_method_b)
"""

from __future__ import annotations

import warnings
from typing import Callable, Optional

import numpy as np
import torch
from sklearn.metrics import (
    roc_auc_score,
    average_precision_score,
    precision_recall_curve,
    roc_curve,
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
)
from scipy.stats import wilcoxon


# ── Per-circuit evaluation ────────────────────────────────────────────────────

def evaluate_circuit(
    node_scores: torch.Tensor | np.ndarray,
    true_labels: torch.Tensor | np.ndarray,
    top_k:       Optional[int] = None,
    trigger_mask: Optional[torch.Tensor | np.ndarray] = None,
) -> dict:
    """Compute all evaluation metrics for one circuit.

    Args:
        node_scores:  (N,) anomaly scores in [0, 1]; higher = more anomalous.
        true_labels:  (N,) binary ground truth (1 = Trojan, 0 = clean).
        top_k:        if given, compute top-k precision; defaults to |Trojan nodes|.
        trigger_mask: (N,) bool — marks nodes on actual trigger chains for
                      trigger_coverage metric (optional).

    Returns:
        dict with keys:
            auroc, auprc,
            acc, precision, recall, f1, fpr,
            top_k_precision, top_k_k,
            trigger_coverage  (if trigger_mask provided)
    """
    scores = _to_numpy(node_scores).astype(float)
    labels = _to_numpy(true_labels).astype(int)

    n_pos = labels.sum()
    if n_pos == 0:
        warnings.warn("No positive (Trojan) labels — AUROC/AUPRC undefined.")
        return _null_metrics()

    # --- Threshold-free metrics ---
    auroc = roc_auc_score(labels, scores)
    auprc = average_precision_score(labels, scores)

    # --- Youden-optimal threshold ---
    fpr_arr, tpr_arr, thresholds = roc_curve(labels, scores)
    youden_idx  = np.argmax(tpr_arr - fpr_arr)
    best_thresh = thresholds[youden_idx]
    preds       = (scores >= best_thresh).astype(int)

    acc  = accuracy_score(labels, preds)
    prec = precision_score(labels, preds, zero_division=0)
    rec  = recall_score(labels, preds, zero_division=0)
    f1   = f1_score(labels, preds, zero_division=0)
    fpr  = _false_positive_rate(labels, preds)

    # --- Top-k precision ---
    k = top_k if top_k is not None else int(n_pos)
    topk_prec = _top_k_precision(scores, labels, k)
    topk_tie = _top_k_tie_count(scores, k)

    result = dict(
        auroc=float(auroc),
        auprc=float(auprc),
        acc=float(acc),
        precision=float(prec),
        recall=float(rec),
        f1=float(f1),
        fpr=float(fpr),
        top_k_precision=float(topk_prec),
        top_k_k=k,
        top_k_tie_count=int(topk_tie),
        threshold=float(best_thresh),
        n_trojan=int(n_pos),
        n_total=int(len(labels)),
        trojan_density=float(n_pos / len(labels)),
    )

    # --- Trigger coverage ---
    if trigger_mask is not None:
        tmask = _to_numpy(trigger_mask).astype(bool)
        cov   = _trigger_coverage(scores, tmask, k)
        result["trigger_coverage"] = float(cov)

    return result


def evaluate_all_circuits(
    scores_list:   list[torch.Tensor | np.ndarray],
    labels_list:   list[torch.Tensor | np.ndarray],
    circuit_names: Optional[list[str]] = None,
    **kwargs,
) -> dict:
    """Evaluate each circuit and return per-circuit + aggregate metrics.

    Aggregate AUROC convention
    --------------------------
    ``mean["auroc"]`` is the **macro-average** (unweighted mean of per-circuit
    AUROCs).  This treats every circuit equally regardless of size and matches
    the reporting convention used in this paper.

    ``pooled_auroc`` is also returned: the **micro-average** AUROC computed by
    concatenating all node scores and labels across circuits.  Micro-AUROC is
    dominated by the largest circuit (AES 22K gates) and should NOT be used as
    the headline metric, but is useful as a sanity-check.

    Returns:
        dict with keys:
            per_circuit:  list of per-circuit metric dicts
            names:        circuit name list
            mean:         macro-average of each float metric  ← headline
            std:          std of each float metric across circuits
            pooled_auroc: micro-average AUROC (all nodes pooled)
    """
    names = circuit_names or [f"circuit_{i}" for i in range(len(scores_list))]
    per = [
        evaluate_circuit(s, l, **kwargs)
        for s, l in zip(scores_list, labels_list)
    ]

    metric_keys = [k for k in per[0] if isinstance(per[0][k], float)]
    # Macro-average: mean of per-circuit values (equal weight per circuit)
    mean_dict = {k: float(np.nanmean([p[k] for p in per])) for k in metric_keys}
    std_dict  = {k: float(np.nanstd ([p[k] for p in per])) for k in metric_keys}

    # Micro-average AUROC: pool all nodes across circuits
    all_scores = np.concatenate([_to_numpy(s).astype(float) for s in scores_list])
    all_labels = np.concatenate([_to_numpy(l).astype(int)   for l in labels_list])
    try:
        micro_auroc = float(roc_auc_score(all_labels, all_scores))
    except ValueError:
        micro_auroc = float("nan")

    return dict(
        per_circuit=per,
        names=names,
        mean=mean_dict,            # macro-average  ← use this in tables
        std=std_dict,
        pooled_auroc=micro_auroc,  # micro-average  ← sanity-check only
    )


def pooled_auroc(
    scores_list: list[torch.Tensor | np.ndarray],
    labels_list: list[torch.Tensor | np.ndarray],
) -> float:
    """Compute micro-average AUROC by pooling all nodes across circuits.

    Warning: dominated by the largest circuit.  Use macro-average (mean of
    per-circuit AUROCs) as the headline metric in tables.
    """
    all_scores = np.concatenate([_to_numpy(s).astype(float) for s in scores_list])
    all_labels = np.concatenate([_to_numpy(l).astype(int)   for l in labels_list])
    try:
        return float(roc_auc_score(all_labels, all_scores))
    except ValueError:
        return float("nan")


# ── Stability analysis ────────────────────────────────────────────────────────

def stability_run(
    score_fn:    Callable[[], tuple[torch.Tensor, torch.Tensor]],
    n_runs:      int = 10,
    seeds:       Optional[list[int]] = None,
) -> dict:
    """Run score_fn n_runs times; collect AUROC stability statistics.

    Args:
        score_fn: callable() → (node_scores, true_labels) for one circuit.
                  The function must accept an optional seed kwarg if seeds != None.
        n_runs:   number of independent runs.
        seeds:    list of seeds (len = n_runs); if None, uses range(n_runs).

    Returns:
        dict with auroc_mean, auroc_std, auroc_min, auroc_max,
                    auroc_runs (list of per-run values).
    """
    if seeds is None:
        seeds = list(range(n_runs))

    aurocs = []
    for seed in seeds:
        try:
            scores, labels = score_fn(seed=seed)
        except TypeError:
            scores, labels = score_fn()
        m = evaluate_circuit(scores, labels)
        aurocs.append(m["auroc"])

    return dict(
        auroc_mean=float(np.mean(aurocs)),
        auroc_std =float(np.std (aurocs)),
        auroc_min =float(np.min (aurocs)),
        auroc_max =float(np.max (aurocs)),
        auroc_runs=aurocs,
        n_runs=n_runs,
    )


# ── Statistical tests ─────────────────────────────────────────────────────────

def wilcoxon_auroc(
    aurocs_a: list[float] | np.ndarray,
    aurocs_b: list[float] | np.ndarray,
    alternative: str = "greater",
) -> dict:
    """Paired Wilcoxon signed-rank test: H₀: AUROC_a = AUROC_b.

    Args:
        aurocs_a: list of AUROC values for method A (one per circuit).
        aurocs_b: list of AUROC values for method B (one per circuit).
        alternative: "greater" tests H₁: A > B (two-sided by default allowed).

    Returns:
        dict with statistic, p_value, significant_at_05,
                    mean_diff (mean of a - b).
    """
    a = np.array(aurocs_a, dtype=float)
    b = np.array(aurocs_b, dtype=float)
    if len(a) < 2:
        return dict(statistic=np.nan, p_value=np.nan,
                    significant_at_05=False, mean_diff=float(np.mean(a - b)))

    stat, pval = wilcoxon(a, b, alternative=alternative)
    return dict(
        statistic=float(stat),
        p_value=float(pval),
        significant_at_05=bool(pval < 0.05),
        mean_diff=float(np.mean(a - b)),
    )


def bootstrap_ci(
    node_scores: torch.Tensor | np.ndarray,
    true_labels: torch.Tensor | np.ndarray,
    n_bootstrap: int = 2000,
    ci: float = 0.95,
    seed: int = 42,
) -> dict:
    """Stratified bootstrap confidence interval for AUROC.

    Stratified: positive and negative examples are sampled separately
    to maintain class balance across bootstrap samples.

    Returns:
        dict with auroc, ci_lower, ci_upper, ci_level.
    """
    rng    = np.random.default_rng(seed)
    scores = _to_numpy(node_scores).astype(float)
    labels = _to_numpy(true_labels).astype(int)

    pos_idx = np.where(labels == 1)[0]
    neg_idx = np.where(labels == 0)[0]

    if len(pos_idx) == 0 or len(neg_idx) == 0:
        return dict(auroc=np.nan, ci_lower=np.nan, ci_upper=np.nan, ci_level=ci)

    boot_aurocs = []
    for _ in range(n_bootstrap):
        boot_pos = rng.choice(pos_idx, size=len(pos_idx), replace=True)
        boot_neg = rng.choice(neg_idx, size=len(neg_idx), replace=True)
        idx      = np.concatenate([boot_pos, boot_neg])
        try:
            a = roc_auc_score(labels[idx], scores[idx])
        except ValueError:
            continue
        boot_aurocs.append(a)

    alpha    = (1.0 - ci) / 2.0
    ci_lower = float(np.percentile(boot_aurocs, alpha * 100))
    ci_upper = float(np.percentile(boot_aurocs, (1.0 - alpha) * 100))
    auroc    = float(roc_auc_score(labels, scores))

    return dict(auroc=auroc, ci_lower=ci_lower, ci_upper=ci_upper, ci_level=ci)


# ── Metric summary table ──────────────────────────────────────────────────────

def format_results_table(
    results_dict:   dict[str, dict],
    metric_order:   Optional[list[str]] = None,
) -> str:
    """Format a dict of {method_name: metrics_dict} as an ASCII table.

    Args:
        results_dict: {method_name: evaluate_circuit(...) dict}
        metric_order: metrics to show in order; defaults to
                      [auroc, auprc, acc, precision, recall, f1, fpr]

    Returns:
        Multi-line string suitable for logging or file output.
    """
    if metric_order is None:
        metric_order = ["auroc", "auprc", "acc", "precision", "recall", "f1", "fpr"]

    col_w   = 14
    headers = ["Method"] + [m.upper() for m in metric_order]
    sep     = "+" + "+".join("-" * (col_w + 2) for _ in headers) + "+"
    header_row = "|" + "|".join(f" {h:<{col_w}} " for h in headers) + "|"

    lines = [sep, header_row, sep]
    for method, metrics in results_dict.items():
        vals = [method] + [
            f"{metrics.get(m, float('nan')):.4f}" if m != "acc"
            else f"{metrics.get(m, float('nan')):.4f}"
            for m in metric_order
        ]
        row = "|" + "|".join(f" {v:<{col_w}} " for v in vals) + "|"
        lines.append(row)
    lines.append(sep)
    return "\n".join(lines)


# ── Scalability reporting ─────────────────────────────────────────────────────

def write_scalability_csv(
    train_stats_list: list[list[dict]],
    out_path: "str | Path",
    condition_names: Optional[list[str]] = None,
) -> None:
    """Write scalability stats from one or more training runs to CSV.

    Args:
        train_stats_list: list of trainer.train_stats_ (one list per condition).
            Each inner list has one dict per circuit with keys:
            circuit_name, circuit_variant, n_nodes, n_edges, n_modules,
            train_time_sec.
        out_path: destination CSV file.
        condition_names: labels for each condition; defaults to
            ["condition_0", "condition_1", ...].
    """
    from pathlib import Path as _Path
    import csv as _csv

    if not train_stats_list:
        return

    conditions = condition_names or [f"condition_{i}" for i in range(len(train_stats_list))]
    # Flatten: one row per (condition, circuit)
    rows = []
    for cond, stats in zip(conditions, train_stats_list):
        for s in stats:
            rows.append({
                "condition":      cond,
                "circuit_name":   s.get("circuit_name",    "?"),
                "circuit_variant":s.get("circuit_variant", "?"),
                "n_nodes":        s.get("n_nodes",    0),
                "n_edges":        s.get("n_edges",    0),
                "n_modules":      s.get("n_modules",  0),
                "train_time_sec": s.get("train_time_sec", 0.0),
            })

    if not rows:
        return

    fieldnames = list(rows[0].keys())
    _Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_numpy(x) -> np.ndarray:
    if isinstance(x, torch.Tensor):
        return x.detach().cpu().numpy()
    return np.array(x)


def _false_positive_rate(labels: np.ndarray, preds: np.ndarray) -> float:
    neg_mask = labels == 0
    fp = ((preds == 1) & neg_mask).sum()
    tn = ((preds == 0) & neg_mask).sum()
    return float(fp / (fp + tn)) if (fp + tn) > 0 else 0.0


def _top_k_precision(
    scores: np.ndarray,
    labels: np.ndarray,
    k: int,
) -> float:
    """Expected Trojan fraction in top-k under random tie breaking.

    A plain argsort can overstate precision when many nodes have exactly the
    same score. Nodes strictly above the kth score are always selected; nodes
    tied at the kth score are selected fractionally in expectation.
    """
    if k <= 0:
        return 0.0
    if k >= len(scores):
        return float(labels.sum() / k)

    cutoff = np.partition(scores, len(scores) - k)[len(scores) - k]
    above = scores > cutoff
    tied = scores == cutoff
    slots_from_tie = k - int(above.sum())

    hits = float(labels[above].sum())
    if slots_from_tie > 0 and tied.any():
        hits += float(slots_from_tie * labels[tied].mean())
    return float(hits / k)


def _top_k_tie_count(scores: np.ndarray, k: int) -> int:
    """Number of nodes tied at the top-k decision boundary."""
    if k <= 0 or len(scores) == 0:
        return 0
    if k >= len(scores):
        return len(scores)
    cutoff = np.partition(scores, len(scores) - k)[len(scores) - k]
    return int(np.sum(scores == cutoff))


def _trigger_coverage(
    scores:       np.ndarray,
    trigger_mask: np.ndarray,
    k: int,
) -> float:
    """Expected trigger-chain coverage under random top-k tie breaking."""
    n_trigger = trigger_mask.sum()
    if n_trigger == 0:
        return 0.0
    if k <= 0:
        return 0.0
    if k >= len(scores):
        return 1.0

    cutoff = np.partition(scores, len(scores) - k)[len(scores) - k]
    above = scores > cutoff
    tied = scores == cutoff
    slots_from_tie = k - int(above.sum())

    covered = float(trigger_mask[above].sum())
    if slots_from_tie > 0 and tied.any():
        covered += float(slots_from_tie * trigger_mask[tied].mean())
    return float(covered / n_trigger)


def _null_metrics() -> dict:
    return dict(
        auroc=float("nan"), auprc=float("nan"),
        acc=float("nan"), precision=float("nan"),
        recall=float("nan"), f1=float("nan"),
        fpr=float("nan"), top_k_precision=float("nan"),
        top_k_tie_count=0,
        top_k_k=0, threshold=float("nan"),
        n_trojan=0, n_total=0, trojan_density=0.0,
    )
