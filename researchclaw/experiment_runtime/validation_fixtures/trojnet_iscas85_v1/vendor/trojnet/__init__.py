"""TrojNet: Unsupervised GNN Embeddings for Hardware Trojan Detection."""

from .model import TrojNetEncoder, InfoNCELoss, augment
from .train import TrojNetTrainer, TrainConfig, train_and_embed
from .anomaly import AnomalyScorer, score_circuit
from .eval import (
    evaluate_circuit, evaluate_all_circuits, wilcoxon_auroc,
    write_scalability_csv, pooled_auroc, format_results_table,
)
from .data import load_circuit, load_all_circuits

__all__ = [
    # model
    "TrojNetEncoder", "InfoNCELoss", "augment",
    # training
    "TrojNetTrainer", "TrainConfig", "train_and_embed",
    # anomaly scoring
    "AnomalyScorer", "score_circuit",
    # evaluation
    "evaluate_circuit", "evaluate_all_circuits", "wilcoxon_auroc",
    "write_scalability_csv", "pooled_auroc", "format_results_table",
    # data loading
    "load_circuit", "load_all_circuits",
]
