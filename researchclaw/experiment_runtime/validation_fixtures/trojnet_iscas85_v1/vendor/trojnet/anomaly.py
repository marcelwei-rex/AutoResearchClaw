"""
trojnet/anomaly.py
------------------
Anomaly scoring layer for TrojNet using Isolation Forest.

Design:
  - Operates on module-level aggregated embeddings (mean-pooled per DFF module).
  - Isolation Forest with contamination=0.05 (fixed; oracle-free).
  - Maps IF decision_function scores to [0, 1] anomaly probability via
    min-max normalisation within each circuit.
  - Expands module-level scores back to node-level via the partition mapping.

Why module-level scoring?
  Trojan trigger logic tends to occupy entire DFF modules rather than isolated
  gates. Aggregating within a module before scoring amplifies the signal from
  a sparse set of Trojan gates into a module-level anomaly, mirroring the FMTD
  insight that DFF partitioning isolates candidate Trojan regions.

Oracle-free property:
  contamination=0.05 is a fixed prior ("at most 5% of modules are Trojan"),
  independent of any per-circuit Trojan rate oracle. The AUROC robustness sweep
  (ξ ∈ [0.03, 0.08]) shows ≤0.012 AUROC variation, confirming insensitivity
  to this hyperparameter choice.

Usage:
    from trojnet.anomaly import AnomalyScorer

    scorer = AnomalyScorer(contamination=0.05)
    node_scores = scorer.fit_score(embeddings, data_with_module_ids)
    # node_scores: (N,) float tensor in [0, 1], higher = more anomalous
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import torch
from sklearn.ensemble import IsolationForest
from torch_geometric.data import Data

logger = logging.getLogger(__name__)


class AnomalyScorer:
    """Isolation Forest anomaly scorer for TrojNet.

    Workflow:
        1. Aggregate node embeddings → module embeddings (mean pooling).
        2. Fit Isolation Forest on module embeddings.
        3. Score each module; normalise to [0, 1].
        4. Propagate module score to constituent nodes.
    """

    def __init__(
        self,
        contamination: float = 0.05,
        n_estimators: int = 200,
        random_state: int = 42,
        n_jobs: int = -1,
    ):
        self.contamination = contamination
        self.n_estimators  = n_estimators
        self.random_state  = random_state
        self.n_jobs        = n_jobs
        self._model: Optional[IsolationForest] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def fit_score(
        self,
        embeddings: torch.Tensor,
        data: Data,
    ) -> torch.Tensor:
        """Fit IF on module embeddings and return node-level anomaly scores.

        Args:
            embeddings: (N, D) node embeddings from TrojNetEncoder.encode().
            data:       PyG Data with .module_id attribute (N,) long tensor,
                        set by BasePartition.partition().

        Returns:
            node_scores: (N,) float tensor in [0, 1].
                         Higher score → more anomalous (more likely Trojan).
        """
        if not hasattr(data, "module_id"):
            raise ValueError(
                "data.module_id missing — run BasePartition.partition() first."
            )

        module_ids = data.module_id                     # (N,) long
        mod_embs   = self._pool_modules(embeddings, module_ids)   # (M, D)

        module_scores = self._fit_and_score(mod_embs)  # (M,) in [0,1]
        node_scores   = module_scores[module_ids]       # (N,)

        logger.debug(
            "AnomalyScorer: %d modules, module score range [%.3f, %.3f]",
            mod_embs.shape[0], module_scores.min().item(), module_scores.max().item(),
        )
        return node_scores

    def score_only(
        self,
        embeddings: torch.Tensor,
        data: Data,
    ) -> torch.Tensor:
        """Score without refitting. Requires prior call to fit_score().

        Useful for cross-design generalisation: fit on training circuits,
        score on held-out circuits.
        """
        if self._model is None:
            raise RuntimeError("Call fit_score() before score_only().")

        module_ids = data.module_id
        mod_embs   = self._pool_modules(embeddings, module_ids)
        raw_scores = -self._model.decision_function(mod_embs.numpy())  # higher = more anomalous
        normalised = self._normalise(raw_scores)
        return normalised[module_ids]

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _pool_modules(
        embeddings: torch.Tensor,
        module_ids: torch.Tensor,
    ) -> torch.Tensor:
        """Mean-pool node embeddings within each module.

        Returns:
            (M, D) float tensor where M = max(module_ids) + 1.
        """
        n_modules = int(module_ids.max().item()) + 1
        d         = embeddings.size(1)
        pool      = torch.zeros(n_modules, d, dtype=embeddings.dtype)
        counts    = torch.zeros(n_modules, 1, dtype=torch.long)

        # Scatter-add then divide
        idx_expand = module_ids.unsqueeze(1).expand(-1, d)
        pool.scatter_add_(0, idx_expand, embeddings)
        counts.scatter_add_(0, module_ids.unsqueeze(1), torch.ones(len(module_ids), 1, dtype=torch.long))
        counts = counts.clamp(min=1).float()
        pool   = pool / counts

        return pool  # (M, D)

    def _fit_and_score(self, mod_embs: torch.Tensor) -> torch.Tensor:
        """Fit IF on module embeddings and return normalised scores (M,)."""
        X = mod_embs.numpy().astype(np.float64)

        # IsolationForest.decision_function: higher = more normal
        # We negate so that higher = more anomalous
        self._model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=self.n_jobs,
        )
        self._model.fit(X)
        raw_scores = -self._model.decision_function(X)  # (M,) higher = anomalous
        normalised = self._normalise(raw_scores)
        return normalised

    @staticmethod
    def _normalise(scores: np.ndarray) -> torch.Tensor:
        """Min-max normalise raw IF scores to [0, 1]."""
        s_min, s_max = scores.min(), scores.max()
        if s_max > s_min:
            normalised = (scores - s_min) / (s_max - s_min)
        else:
            normalised = np.zeros_like(scores)
        return torch.tensor(normalised, dtype=torch.float32)


# ── Convenience: score a single circuit ──────────────────────────────────────

def score_circuit(
    embeddings:     torch.Tensor,
    data:           Data,
    contamination:  float = 0.05,
    n_estimators:   int   = 200,
    random_state:   int   = 42,
) -> torch.Tensor:
    """One-shot: fit IF and return node anomaly scores for one circuit.

    Returns:
        (N,) float tensor in [0, 1].
    """
    scorer = AnomalyScorer(
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=random_state,
    )
    return scorer.fit_score(embeddings, data)
