"""
trojnet/train.py
----------------
Training loop for the TrojNet contrastive GNN encoder.

Algorithm (per epoch):
  1. For each circuit, partition into modules via DFFPartition.
  2. For each module, generate two GRACE-style augmented views.
  3. Forward both views through TrojNetEncoder → projected embeddings z1, z2.
  4. Compute symmetric InfoNCE loss over all nodes in the module.
  5. Accumulate gradients, step optimiser.

After training, call `embed_circuit()` to extract node-level embeddings
(before the projection head — encoder output only, following SimCLR convention).

Usage:
    from trojnet.train import TrojNetTrainer

    trainer = TrojNetTrainer(
        gnn_type="GAT",
        hidden_dim=128,
        out_dim=64,
        proj_dim=32,
        lr=1e-3,
        epochs=200,
        temperature=0.5,
        p_feat=0.3,
        p_edge=0.4,
        device="cpu",
    )
    trainer.fit(data_list)           # list of PyG Data objects (one per circuit)
    embeddings = trainer.embed(data) # (N, out_dim) node embeddings
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import torch
import torch.optim as optim
from torch_geometric.data import Data

from .model import TrojNetEncoder, InfoNCELoss, augment
from .partition import DFFPartition

logger = logging.getLogger(__name__)


# ── Training config dataclass ─────────────────────────────────────────────────

@dataclass
class TrainConfig:
    gnn_type:    str   = "GAT"
    in_dim:      int   = 19
    hidden_dim:  int   = 128
    out_dim:     int   = 64
    proj_dim:    int   = 32
    lr:          float = 1e-3
    weight_decay: float = 1e-5
    epochs:      int   = 200
    temperature: float = 0.5
    p_feat:      float = 0.3
    p_edge:      float = 0.4
    min_module_size: int = 4    # skip modules smaller than this (too few negatives)
    device:      str   = "cpu"
    seed:        int   = 42
    log_every:   int   = 20     # log loss every N epochs

    # Scheduler: cosine annealing from lr to eta_min over 'epochs' steps
    use_scheduler: bool  = True
    eta_min:       float = 1e-5


# ── Trainer ───────────────────────────────────────────────────────────────────

class TrojNetTrainer:
    """Trains TrojNetEncoder using contrastive self-supervised learning.

    Trains on one or more circuits. Modules from all circuits are interleaved
    within each epoch (shuffle=True) so the encoder sees structural diversity.
    """

    def __init__(self, config: Optional[TrainConfig] = None, partitioner=None, **kwargs):
        if config is None:
            config = TrainConfig(**kwargs)
        self.cfg = config

        torch.manual_seed(config.seed)
        self.device = torch.device(config.device)

        self.model = TrojNetEncoder(
            in_dim=config.in_dim,
            hidden_dim=config.hidden_dim,
            out_dim=config.out_dim,
            proj_dim=config.proj_dim,
            gnn_type=config.gnn_type,
        ).to(self.device)

        self.loss_fn   = InfoNCELoss(temperature=config.temperature)
        self.optimizer = optim.Adam(
            self.model.parameters(),
            lr=config.lr,
            weight_decay=config.weight_decay,
        )
        self.scheduler = (
            optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer, T_max=config.epochs, eta_min=config.eta_min
            )
            if config.use_scheduler else None
        )

        self._partition = partitioner if partitioner is not None else DFFPartition()
        self.loss_history: list[float] = []
        # Scalability stats — populated by fit(); one entry per circuit
        self.train_stats_: list[dict] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def fit(self, data_list: list[Data]) -> "TrojNetTrainer":
        """Train on a list of PyG Data objects (one per circuit).

        Each Data object should have:
            x           (N, 19) node features
            edge_index  (2, E) edges
            dff_mask    (N,) bool  [optional; inferred as zero if absent]

        After training, ``self.train_stats_`` contains per-circuit scalability
        records::

            [{"circuit_name": "AES", "n_nodes": 22436, "n_edges": 31012,
              "n_modules": 38, "train_time_sec": 47.3}, ...]
        """
        cfg = self.cfg
        modules = self._collect_modules(data_list)
        logger.info(
            "Training %s on %d total modules from %d circuit(s), %d epochs",
            cfg.gnn_type, len(modules), len(data_list), cfg.epochs,
        )

        # Record per-circuit topology stats before training
        self.train_stats_ = []
        for data in data_list:
            _, data_with_ids = self._partition.partition(data)
            n_mods = int(data_with_ids.module_id.max().item()) + 1 if hasattr(data_with_ids, "module_id") else 0
            self.train_stats_.append({
                "circuit_name":  getattr(data, "circuit_name",  "?"),
                "circuit_variant": getattr(data, "circuit_variant", getattr(data, "circuit_name", "?")),
                "n_nodes":       int(data.num_nodes),
                "n_edges":       int(data.edge_index.size(1)),
                "n_modules":     n_mods,
                "train_time_sec": 0.0,  # filled in after training
            })

        t0 = time.time()
        for epoch in range(1, cfg.epochs + 1):
            epoch_loss = self._train_epoch(modules)
            self.loss_history.append(epoch_loss)

            if self.scheduler is not None:
                self.scheduler.step()

            if epoch % cfg.log_every == 0 or epoch == 1:
                elapsed = time.time() - t0
                logger.info(
                    "Epoch %d/%d  loss=%.4f  lr=%.2e  elapsed=%.1fs",
                    epoch, cfg.epochs, epoch_loss,
                    self.optimizer.param_groups[0]["lr"], elapsed,
                )

        total_time = time.time() - t0
        # Distribute training time proportionally to n_nodes
        total_nodes = sum(s["n_nodes"] for s in self.train_stats_) or 1
        for s in self.train_stats_:
            s["train_time_sec"] = round(total_time * s["n_nodes"] / total_nodes, 2)

        logger.info(
            "Training complete. Final loss: %.4f  total_time=%.1fs",
            self.loss_history[-1], total_time,
        )
        return self

    def embed(self, data: Data) -> torch.Tensor:
        """Return node embeddings (encoder output, before projection head).

        Args:
            data: PyG Data object with x and edge_index.

        Returns:
            Tensor of shape (N, out_dim), on CPU.
        """
        self.model.eval()
        x = data.x.to(self.device)
        ei = data.edge_index.to(self.device)
        with torch.no_grad():
            h = self.model.encode(x, ei)
        return h.cpu()

    def embed_all(self, data_list: list[Data]) -> list[torch.Tensor]:
        """Embed a list of circuits. Returns list of (N_i, out_dim) tensors."""
        return [self.embed(d) for d in data_list]

    def save(self, path: str) -> None:
        torch.save({
            "model_state": self.model.state_dict(),
            "config": self.cfg,
            "loss_history": self.loss_history,
        }, path)
        logger.info("Model saved to %s", path)

    @classmethod
    def load(cls, path: str, device: str = "cpu") -> "TrojNetTrainer":
        ckpt = torch.load(path, map_location=device)
        cfg  = ckpt["config"]
        cfg.device = device
        trainer = cls(config=cfg)
        trainer.model.load_state_dict(ckpt["model_state"])
        trainer.loss_history = ckpt.get("loss_history", [])
        return trainer

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _collect_modules(self, data_list: list[Data]) -> list[Data]:
        """Partition each circuit into DFF-boundary modules; filter small ones."""
        all_modules: list[Data] = []
        for data in data_list:
            modules, _ = self._partition.partition(data)
            for m in modules:
                if m.num_nodes >= self.cfg.min_module_size:
                    all_modules.append(m)
        logger.info(
            "Collected %d modules (min_size=%d)",
            len(all_modules), self.cfg.min_module_size,
        )
        return all_modules

    def _train_epoch(self, modules: list[Data]) -> float:
        """One training epoch over all modules in random order."""
        self.model.train()
        cfg   = self.cfg
        perm  = torch.randperm(len(modules))
        total_loss = 0.0

        for idx in perm:
            m  = modules[idx.item()]
            x  = m.x.to(self.device)
            ei = m.edge_index.to(self.device)

            # Two independent augmented views
            x1, ei1 = augment(x, ei, p_feat=cfg.p_feat, p_edge=cfg.p_edge, training=True)
            x2, ei2 = augment(x, ei, p_feat=cfg.p_feat, p_edge=cfg.p_edge, training=True)

            z1 = self.model(x1, ei1)
            z2 = self.model(x2, ei2)

            loss = self.loss_fn(z1, z2)

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            total_loss += loss.item()

        return total_loss / len(modules)


# ── Convenience function ──────────────────────────────────────────────────────

def train_and_embed(
    data_list:   list[Data],
    gnn_type:    str   = "GAT",
    hidden_dim:  int   = 128,
    out_dim:     int   = 64,
    proj_dim:    int   = 32,
    epochs:      int   = 200,
    lr:          float = 1e-3,
    temperature: float = 0.5,
    p_feat:      float = 0.3,
    p_edge:      float = 0.4,
    device:      str   = "cpu",
    seed:        int   = 42,
) -> tuple["TrojNetTrainer", list[torch.Tensor]]:
    """Train TrojNet and return (trainer, embeddings_per_circuit).

    Returns:
        trainer:     fitted TrojNetTrainer
        embeddings:  list of (N_i, out_dim) CPU tensors, one per circuit
    """
    cfg = TrainConfig(
        gnn_type=gnn_type,
        in_dim=data_list[0].x.size(1) if data_list else 19,
        hidden_dim=hidden_dim,
        out_dim=out_dim,
        proj_dim=proj_dim,
        epochs=epochs,
        lr=lr,
        temperature=temperature,
        p_feat=p_feat,
        p_edge=p_edge,
        device=device,
        seed=seed,
    )
    trainer = TrojNetTrainer(config=cfg)
    trainer.fit(data_list)
    embeddings = trainer.embed_all(data_list)
    return trainer, embeddings
