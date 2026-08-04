"""Stage 3.4 — the neural candidate (NHITS-style).

Included to test the claim, not because it is expected to win. The competition
evidence on M5-shaped retail data is that well-tuned gradient boosting beats deep
learning, and a thesis that omits a neural model cannot say that with authority.
If it loses here, that is a result; if it wins on some planning class, the
meta-learner in Stage 4 will find it and give it weight there.

Architecture follows NHITS: a stack of MLP blocks operating at different temporal
resolutions via average pooling, each block producing a backcast it subtracts from
the residual and a forecast it adds to the output. Multi-rate pooling is what lets
one small model capture both weekly and multi-week structure without an attention
mechanism. Direct 28-step output, consistent with the rest of Stage 3.

Deliberately modest: 3 stacks, 192 hidden units, CPU-trainable in a few minutes.
Scaling it up would not change the conclusion and would cost hours.
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn

from ..config import NHITS as NHITS_CFG
from ..config import SEED
from ..features import Panel


class Block(nn.Module):
    """One NHITS block: pool -> MLP -> (backcast, forecast) basis coefficients."""

    def __init__(self, input_size: int, horizon: int, hidden: int, pool: int):
        super().__init__()
        self.pool = pool
        self.horizon = horizon
        self.input_size = input_size
        pooled = int(np.ceil(input_size / pool))
        self.net = nn.Sequential(
            nn.Linear(pooled, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, input_size + horizon),
        )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        z = x.unsqueeze(1)
        z = nn.functional.avg_pool1d(z, self.pool, ceil_mode=True).squeeze(1)
        out = self.net(z)
        return out[:, : self.input_size], out[:, self.input_size :]


class NHITS(nn.Module):
    def __init__(self, input_size: int, horizon: int, hidden: int, pools: tuple[int, ...],
                 n_static: int = 0):
        super().__init__()
        self.blocks = nn.ModuleList(
            Block(input_size + n_static, horizon, hidden, p) for p in pools
        )
        self.n_static = n_static
        self.input_size = input_size

    def forward(self, x: torch.Tensor, static: torch.Tensor | None = None) -> torch.Tensor:
        z = torch.cat([x, static], dim=1) if static is not None and self.n_static else x
        residual = z
        forecast = 0.0
        for b in self.blocks:
            back, fore = b(residual)
            residual = residual - back
            forecast = forecast + fore
        return forecast


def _windows(
    P: Panel, max_origin: int, horizon: int, input_size: int, n_samples: int, rng
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample (context, target) windows strictly inside the training region."""
    y = np.nan_to_num(P.y[:, : max_origin + 1], nan=0.0).astype("float32")
    valid = ~np.isnan(P.y[:, : max_origin + 1])
    S, T = y.shape
    lo, hi = input_size, T - horizon
    if hi <= lo:
        raise ValueError("training region too short for the configured input size")

    si = rng.integers(0, S, size=n_samples)
    ti = rng.integers(lo, hi, size=n_samples)
    ctx = np.stack([y[s, t - input_size : t] for s, t in zip(si, ti)])
    tgt = np.stack([y[s, t : t + horizon] for s, t in zip(si, ti)])
    msk = np.stack([valid[s, t : t + horizon] for s, t in zip(si, ti)]).astype("float32")

    # Per-window scale normalisation. Without it the loss is dominated by the
    # few high-volume series and the model ignores everything sparse.
    scale = ctx.mean(axis=1, keepdims=True)
    scale = np.where(scale < 0.05, 0.05, scale)
    return (ctx / scale).astype("float32"), (tgt / scale).astype("float32"), msk, scale.astype("float32")


class NeuralCandidate:
    def __init__(self, horizon: int = 28, cfg: dict = NHITS_CFG):
        self.horizon = horizon
        self.cfg = cfg
        self.input_size = cfg["input_size"]
        self.model: NHITS | None = None

    def fit(self, P: Panel, max_origin: int, n_samples: int = 60_000, verbose: bool = True):
        torch.manual_seed(SEED)
        rng = np.random.default_rng(SEED)
        ctx, tgt, msk, _ = _windows(
            P, max_origin, self.horizon, self.input_size, n_samples, rng
        )
        X = torch.from_numpy(ctx)
        Y = torch.from_numpy(tgt)
        M = torch.from_numpy(msk)

        self.model = NHITS(self.input_size, self.horizon, self.cfg["hidden"],
                           tuple(self.cfg["pool_kernels"]))
        opt = torch.optim.Adam(self.model.parameters(), lr=self.cfg["lr"])
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=self.cfg["epochs"])
        bs = self.cfg["batch_size"]
        n = len(X)

        for ep in range(self.cfg["epochs"]):
            perm = torch.randperm(n)
            total, seen = 0.0, 0
            self.model.train()
            for i in range(0, n, bs):
                idx = perm[i : i + bs]
                opt.zero_grad()
                pred = self.model(X[idx])
                # Masked L1 on the normalised scale: robust to the spikes that
                # dominate lumpy series, and it ignores censored target days.
                w = M[idx]
                loss = (torch.abs(pred - Y[idx]) * w).sum() / w.sum().clamp(min=1.0)
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                opt.step()
                total += float(loss.detach()) * len(idx)
                seen += len(idx)
            sched.step()
            if verbose and (ep % 4 == 0 or ep == self.cfg["epochs"] - 1):
                print(f"      nhits epoch {ep + 1:>2}/{self.cfg['epochs']}  "
                      f"masked L1 {total / seen:.4f}", flush=True)
        return self

    @torch.no_grad()
    def predict(self, P: Panel, O: int, horizons: tuple[int, ...] | range) -> np.ndarray:
        assert self.model is not None, "call fit() first"
        self.model.eval()
        y = np.nan_to_num(P.y[:, : O + 1], nan=0.0).astype("float32")
        ctx = y[:, -self.input_size :]
        if ctx.shape[1] < self.input_size:
            ctx = np.pad(ctx, ((0, 0), (self.input_size - ctx.shape[1], 0)))
        scale = ctx.mean(axis=1, keepdims=True)
        scale = np.where(scale < 0.05, 0.05, scale)
        pred = self.model(torch.from_numpy(ctx / scale)).numpy() * scale
        hs = [h - 1 for h in horizons]
        return np.maximum(pred[:, hs], 0.0)
