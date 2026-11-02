"""Power-normalized phase currents and the original eight-seed angle search."""

import math

import numpy as np
from scipy.linalg import null_space


def mapping(phases, coils, power, balanced):
    resistance = np.array(
        [sum(c["resistance"] for c in coils if c["phase"] == p) for p in phases]
    )
    root = 1 / np.sqrt(resistance)
    basis = (
        null_space(root[None, :])
        if balanced and len(phases) > 1
        else np.eye(len(phases))
    )
    return root[:, None] * basis * math.sqrt(power), resistance


def next_direction(trials, dimension):
    """Return (direction, angle_deg) or None once the current search converges."""
    if dimension == 1:
        return ([1.0 if not trials else -1.0], None) if len(trials) < 2 else None
    if dimension > 2:
        if len(trials) < 2 * dimension:
            d = np.eye(dimension)[len(trials) // 2] * (
                1 if len(trials) % 2 == 0 else -1
            )
            return d.tolist(), None
        best = max(trials, key=lambda r: r["torque_Nm"])
        d, g = np.array(best["direction"]), np.array(best["gradient"])
        g -= g @ d * d
        if np.linalg.norm(g) <= 0.005 * abs(best["torque_Nm"]):
            return None
        step = 0.25 / (
            1
            + sum(r["torque_Nm"] <= best["torque_Nm"] for r in trials[2 * dimension :])
        )
        d = d + step * g / np.linalg.norm(g)
        return (d / np.linalg.norm(d)).tolist(), None
    # Preserve the established 45-degree seed spacing and bracket stopping law.
    if len(trials) < 8:
        beta = len(trials) * 45.0
    else:
        positions = sorted(trials, key=lambda r: r["beta_deg"])
        best = max(trials, key=lambda r: r["torque_Nm"])
        brackets = []
        for i, left in enumerate(positions):
            right = positions[(i + 1) % len(positions)]
            lo, hi = (
                left["beta_deg"],
                right["beta_deg"] + (360 if i == len(positions) - 1 else 0),
            )
            if left["slope"] >= 0 >= right["slope"]:
                threshold = 0.005 * max(abs(left["torque_Nm"]), abs(right["torque_Nm"]))
                done = (
                    min(abs(left["slope"]), abs(right["slope"])) <= threshold
                    or hi - lo < 0.5
                )
                brackets.append((done, left, right, lo, hi))
        pending = [b for b in brackets if not b[0]]
        if brackets and not pending:
            return None
        if pending:
            _, left, right, lo, hi = max(
                pending, key=lambda b: max(b[1]["torque_Nm"], b[2]["torque_Nm"])
            )
            beta = (
                float(
                    np.clip(
                        lo
                        - left["slope"] * (hi - lo) / (right["slope"] - left["slope"]),
                        lo + 0.05 * (hi - lo),
                        hi - 0.05 * (hi - lo),
                    )
                )
                % 360
            )
            if (
                min(abs((beta - r["beta_deg"] + 180) % 360 - 180) for r in trials)
                < 0.25
            ):
                beta = ((lo + hi) / 2) % 360
        else:
            step = math.copysign(15.0, best["slope"] or 1)
            ahead = [
                (r["beta_deg"] - best["beta_deg"] + 180) % 360 - 180
                for r in trials
                if r is not best
            ]
            ahead = [a for a in ahead if a * step > 0]
            beta = (
                best["beta_deg"] + (0.5 * min(ahead, key=abs) if ahead else step)
            ) % 360
    angle = math.radians(beta)
    return [math.cos(angle), math.sin(angle)], beta
