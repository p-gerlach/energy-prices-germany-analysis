"""Transparent robust statistics. No infinite scores, no hidden smoothing."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

K_MAD = 0.67449  # makes MAD consistent with sigma for normal data: z = 0.67449 * (x - median) / MAD
K_IQR = 1.349    # IQR of a normal distribution in sigmas


@dataclass
class RobustScore:
    z: float | None
    method: str          # "MAD" | "IQR-fallback" | "suppressed"
    median: float | None
    scale: float | None  # MAD or IQR used
    n: int
    reason: str = ""

    def as_dict(self):
        return {"z": self.z, "method": self.method, "baseline_median": self.median, "scale": self.scale,
                "n_baseline": self.n, "reason": self.reason}


def robust_z(x: float, baseline, min_n: int = 20) -> RobustScore:
    """Robust z of x against `baseline` (which must NOT contain x itself).

    * fewer than min_n finite baseline values -> suppressed (insufficient history)
    * MAD == 0 -> documented IQR fallback: z = 1.349 * (x - median) / IQR (scaled to sigma units, same as MAD z)
    * MAD == 0 and IQR == 0 -> suppressed (insufficient variability); never an infinite score
    """
    b = np.asarray([v for v in baseline if v is not None and np.isfinite(v)], dtype=float)
    n = int(b.size)
    if x is None or not np.isfinite(x):
        return RobustScore(None, "suppressed", None, None, n, "current value missing")
    if n < min_n:
        return RobustScore(None, "suppressed", float(np.median(b)) if n else None, None, n,
                           f"insufficient history ({n} < {min_n})")
    med = float(np.median(b))
    mad = float(np.median(np.abs(b - med)))
    if mad > 0:
        return RobustScore(K_MAD * (x - med) / mad, "MAD", med, mad, n)
    q75, q25 = np.percentile(b, [75, 25])
    iqr = float(q75 - q25)
    if iqr > 0:
        # sigma estimate = IQR / 1.349, so z = 1.349 * (x - median) / IQR
        return RobustScore(K_IQR * (x - med) / iqr, "IQR-fallback", med, iqr, n, "MAD=0; IQR fallback used")
    return RobustScore(None, "suppressed", med, 0.0, n, "insufficient variability (MAD=0 and IQR=0)")


def empirical_quantile_position(x: float, baseline) -> float | None:
    """Share of baseline values <= x (0..1). Used for sparse counts instead of Gaussian-style scores."""
    b = np.asarray([v for v in baseline if v is not None and np.isfinite(v)], dtype=float)
    if b.size == 0 or x is None or not np.isfinite(x):
        return None
    return float((b <= x).mean())
