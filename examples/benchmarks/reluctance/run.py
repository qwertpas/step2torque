"""Nonlinear 3D reluctance torque against an ellipsoid demagnetizing solution.

A prolate 1J22 rotor in large Helmholtz coils has nearly uniform applied B.
The independent algebraic reference includes the same full nonlinear B-H law;
it uses no mesh, field solution, or virtual-work evaluator.
"""

import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import brentq

from step2torque import Motor
from step2torque.materials import MU0, material

ROOT = Path(__file__).resolve().parent


def reference(current, angle):
    q, weights = np.polynomial.legendre.leggauss(32)
    radius = (0.050 + 0.001 * q)[:, None]
    field = 0.0
    for center in [-0.025, 0.025]:
        z = (center + 0.001 * q)[None, :]
        field += (
            MU0
            * 10000
            * current
            / 2
            * np.sum(
                weights[:, None]
                * weights[None, :]
                / 4
                * radius**2
                / (radius**2 + z**2) ** 1.5
            )
        )
    e = math.sqrt(1 - (1.5 / 3) ** 2)
    axial = (1 - e * e) / (2 * e**3) * (math.log((1 + e) / (1 - e)) - 2 * e)
    theta = math.radians(angle)
    # OCC Ellipsoid's first radius is along its local z axis.
    u = np.array([math.sin(theta), 0, math.cos(theta)])
    demag = (1 - axial) / 2 * np.eye(3) + (axial - (1 - axial) / 2) * np.outer(u, u)
    curve = material("1J22")
    external = np.array([0.0, 0.0, field / MU0])

    def state(magnitude):
        b = np.interp(magnitude, curve["h"], curve["b"]) + MU0 * max(
            magnitude - curve["h"][-1], 0
        )
        chi = b / (MU0 * magnitude) - 1
        internal = np.linalg.solve(np.eye(3) + chi * demag, external)
        return internal, chi * internal

    magnitude = brentq(
        lambda h: np.linalg.norm(state(h)[0]) - h, 1e-8, np.linalg.norm(external) + 1
    )
    h, magnetization = state(magnitude)
    volume = 4 * math.pi / 3 * 0.003 * 0.0015**2
    torque = volume * np.cross(magnetization, MU0 * external)[1]
    return dict(
        torque_Nm=float(torque),
        applied_B_T=field,
        internal_H_A_per_m=float(np.linalg.norm(h)),
    )


def main():
    motor = Motor(ROOT / "motor.step", axis="+y")
    motor.components["rotor"].set_material("1J22", rotating=True)
    for k in range(2):
        motor.components[f"coil{k}"].make_coil(
            turns=10000, direction="+z", resistance=1
        )
    rows = []
    for current in [0.5, 1.5, 4.0]:
        exact = reference(current, 45)
        sol = motor.solve(
            current=current, angle=45, mesh_size=0.6, air_radius=408, threads=8
        ).wait()
        row = dict(
            current_A=current,
            **exact,
            computed_Nm=sol.torque,
            relative_error=sol.torque / exact["torque_Nm"] - 1,
            elements=sol.result["elements"],
            elapsed_s=sol.result["elapsed_s"],
            path=str(sol.path),
        )
        rows.append(row)
        print(json.dumps(row), flush=True)
    (sol.path / "comparison.json").write_text(json.dumps(rows, indent=2))
    assert max(abs(r["relative_error"]) for r in rows) < 0.01


if __name__ == "__main__":
    main()
