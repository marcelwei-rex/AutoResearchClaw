"""
trojnet/model.py
----------------
GNN encoder variants (GCN, GraphSAGE, GAT) and the GRACE-style
InfoNCE contrastive objective used in TrojNet.

Architecture:
  - 2-layer GNN encoder (configurable variant)
  - Projection head: Linear → ReLU → Linear  (following SimCLR convention)
  - InfoNCE loss with in-batch negatives (O(B²) per mini-batch)

Augmentation (GRACE-style dual view):
  - View 1: random feature masking (drop features to 0 with prob p_feat)
  - View 2: random edge dropout (remove edges with prob p_edge)

Usage:
    from trojnet.model import TrojNetEncoder, InfoNCELoss, augment

    model = TrojNetEncoder(in_dim=19, hidden_dim=64, out_dim=32, gnn_type="GAT")
    loss_fn = InfoNCELoss(temperature=0.5)

    z1 = model(*augment(x, edge_index, p_feat=0.3, p_edge=0.4))
    z2 = model(*augment(x, edge_index, p_feat=0.3, p_edge=0.4))
    loss = loss_fn(z1, z2)
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch_geometric.nn import GCNConv, SAGEConv, GATConv


# ── Augmentation ─────────────────────────────────────────────────────────────

def augment(
    x: Tensor,
    edge_index: Tensor,
    p_feat: float = 0.3,
    p_edge: float = 0.4,
    training: bool = True,
) -> tuple[Tensor, Tensor]:
    """GRACE-style dual augmentation.

    Feature masking: each feature dimension independently zeroed with prob p_feat.
    Edge dropout:    each edge independently removed with prob p_edge.

    Returns (augmented_x, augmented_edge_index).
    """
    if not training:
        return x, edge_index

    # Feature masking
    feat_mask = torch.bernoulli(
        torch.ones_like(x) * (1 - p_feat)
    ).to(x.device)
    x_aug = x * feat_mask

    # Edge dropout
    if edge_index.size(1) > 0:
        keep_mask = torch.bernoulli(
            torch.ones(edge_index.size(1), device=edge_index.device) * (1 - p_edge)
        ).bool()
        edge_index_aug = edge_index[:, keep_mask]
    else:
        edge_index_aug = edge_index

    return x_aug, edge_index_aug


# ── GNN Encoder ──────────────────────────────────────────────────────────────

class GCNEncoder(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int):
        super().__init__()
        self.conv1 = GCNConv(in_dim, hidden_dim)
        self.conv2 = GCNConv(hidden_dim, out_dim)

    def forward(self, x: Tensor, edge_index: Tensor) -> Tensor:
        x = F.relu(self.conv1(x, edge_index))
        x = self.conv2(x, edge_index)
        return x


class SAGEEncoder(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int):
        super().__init__()
        self.conv1 = SAGEConv(in_dim, hidden_dim)
        self.conv2 = SAGEConv(hidden_dim, out_dim)

    def forward(self, x: Tensor, edge_index: Tensor) -> Tensor:
        x = F.relu(self.conv1(x, edge_index))
        x = self.conv2(x, edge_index)
        return x


class GATEncoder(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int,
                 heads: int = 4, dropout: float = 0.1):
        super().__init__()
        self.conv1 = GATConv(in_dim, hidden_dim // heads, heads=heads,
                             dropout=dropout, concat=True)
        self.conv2 = GATConv(hidden_dim, out_dim, heads=1,
                             dropout=dropout, concat=False)

    def forward(self, x: Tensor, edge_index: Tensor) -> Tensor:
        x = F.elu(self.conv1(x, edge_index))
        x = self.conv2(x, edge_index)
        return x


_ENCODER_MAP = {
    "GCN":       GCNEncoder,
    "GraphSAGE": SAGEEncoder,
    "GAT":       GATEncoder,
}


class TrojNetEncoder(nn.Module):
    """TrojNet encoder: GNN backbone + projection head.

    Args:
        in_dim:     input feature dimension (19 for TrojNet node features)
        hidden_dim: GNN hidden dimension
        out_dim:    GNN output dimension (node embedding size)
        proj_dim:   projection head output dimension (used in contrastive loss)
        gnn_type:   one of "GCN", "GraphSAGE", "GAT"
    """

    def __init__(
        self,
        in_dim:     int = 19,
        hidden_dim: int = 128,
        out_dim:    int = 64,
        proj_dim:   int = 32,
        gnn_type:   str = "GAT",
    ):
        super().__init__()
        if gnn_type not in _ENCODER_MAP:
            raise ValueError(f"gnn_type must be one of {list(_ENCODER_MAP)}; got {gnn_type!r}")

        self.gnn = _ENCODER_MAP[gnn_type](in_dim, hidden_dim, out_dim)

        # Projection head (SimCLR-style: 2-layer MLP)
        self.proj = nn.Sequential(
            nn.Linear(out_dim, out_dim),
            nn.ReLU(inplace=True),
            nn.Linear(out_dim, proj_dim),
        )

        self.gnn_type  = gnn_type
        self.in_dim    = in_dim
        self.out_dim   = out_dim
        self.proj_dim  = proj_dim

    def encode(self, x: Tensor, edge_index: Tensor) -> Tensor:
        """Return node embeddings (before projection head)."""
        return self.gnn(x, edge_index)

    def forward(self, x: Tensor, edge_index: Tensor) -> Tensor:
        """Return projected node embeddings (used in contrastive loss)."""
        h = self.encode(x, edge_index)
        return self.proj(h)


# ── InfoNCE Loss ─────────────────────────────────────────────────────────────

class InfoNCELoss(nn.Module):
    """GRACE-style node-level InfoNCE contrastive loss.

    For each node v, view 1 embedding z1[v] is the anchor.
    The positive pair is z2[v] (same node, different augmentation).
    All other nodes in the mini-batch are negatives from both views.

    Loss:
        ℓ(v,1) = -log [ exp(sim(z1[v], z2[v]) / τ) /
                        Σ_{u≠v} [exp(sim(z1[v], z1[u]) / τ) +
                                  exp(sim(z1[v], z2[u]) / τ)] ]

    Total loss = mean over both views.
    Complexity: O(B²) per mini-batch (B = number of nodes in batch).
    """

    def __init__(self, temperature: float = 0.5):
        super().__init__()
        self.tau = temperature

    def forward(self, z1: Tensor, z2: Tensor) -> Tensor:
        """
        Args:
            z1, z2: node embeddings from view 1 and view 2, shape (N, D).
        Returns:
            scalar loss.
        """
        z1 = F.normalize(z1, dim=1)
        z2 = F.normalize(z2, dim=1)

        loss1 = self._single_view_loss(z1, z2)
        loss2 = self._single_view_loss(z2, z1)
        return (loss1 + loss2) * 0.5

    def _single_view_loss(self, anchor: Tensor, positive: Tensor) -> Tensor:
        """Compute loss with anchor as query and positive as target."""
        n = anchor.size(0)
        device = anchor.device

        # Positive similarity: diag of (anchor @ positive.T)
        pos_sim = (anchor * positive).sum(dim=1, keepdim=True) / self.tau  # (N,1)

        # All-pair similarity with concatenated [positive, anchor]
        both = torch.cat([positive, anchor], dim=0)            # (2N, D)
        sim_all = anchor @ both.T / self.tau                   # (N, 2N)

        # Mask out self-similarities
        mask = torch.zeros(n, 2 * n, dtype=torch.bool, device=device)
        idx  = torch.arange(n, device=device)
        mask[idx, idx]     = True   # anchor[i] vs positive[i] (keep only as positive)
        mask[idx, idx + n] = True   # anchor[i] vs anchor[i]

        # Numerator: positive pair only
        # Denominator: all non-self pairs
        sim_all_masked = sim_all.masked_fill(mask, float("-inf"))

        # log-sum-exp denominator
        log_denom = torch.logsumexp(sim_all_masked, dim=1)   # (N,)
        loss = -(pos_sim.squeeze(1) - log_denom)
        return loss.mean()
