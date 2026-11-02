"""Full STEP/API pipeline versus an exact 3D sphere-and-coil torque.

The coil's on-axis field is the Biot-Savart integral over its winding window.
A uniformly magnetized linear sphere has effective moment 3 Br V/(mu0*(mur+2)).
"""

import json
import math
from pathlib import Path

import numpy as np

from step2torque import Motor
from step2torque.materials import MU0


def ring(root):
    motor = Motor(root / "ring.step", axis="+y")
    for name in ("coil0", "coil1"):
        motor.components[name].make_coil(phase=1, turns=100, direction="+z")
    motor.components["magnet"].make_magnet(
        direction="radial", poles=2, offset=15, mur=1
    )
    sol = motor.solve(
        current=1, angle=30, mesh_size=0.6, air_radius=168, threads=8
    ).wait()
    q, w = np.polynomial.legendre.leggauss(32)
    radius = (0.020 + 0.001 * q)[:, None]
    z = (0.010 + 0.0005 * q)[None, :]
    b = (
        MU0
        * 100
        * np.sum(w[:, None] * w[None, :] * radius**2 / (radius**2 + z**2) ** 1.5)
        / 4
    )
    volume = motor.components["magnet"].shape.mass * 1e-9
    exact = -2 / math.pi * 1.2 * volume / MU0 * b * math.cos(math.pi / 4)
    row = dict(
        model="two-pole ring",
        path=str(sol.path),
        computed_Nm=sol.torque,
        reference_Nm=exact,
        relative_error=sol.torque / exact - 1,
    )
    print(json.dumps(row), flush=True)
    assert abs(row["relative_error"]) < 0.01
    return row


def main():
    root = Path(__file__).resolve().parent
    motor = Motor(root / "sphere.step", axis="+y")
    motor.components["coil"].make_coil(turns=50, direction="+z", resistance=0.1)
    motor.components["magnet"].make_magnet(direction="+x")
    b = MU0 * 50 / (2 * 0.002) * (math.asinh(0.010 / 0.002) - math.asinh(0.008 / 0.002))
    moment = 3 * 1.2 * (4 * math.pi / 3 * 0.002**3) / (MU0 * (1.05 + 2))
    rows = []
    for size, angle in ((1.0, 0), (0.6, 0), (0.25, 0), (0.35, 60), (0.35, 90)):
        sol = motor.solve(current=1.0, angle=angle, mesh_size=size, threads=8).wait()
        exact = -moment * b * math.cos(math.radians(angle))
        row = dict(
            mesh_mm=size,
            path=str(sol.path),
            angle_deg=angle,
            torque_Nm=sol.torque,
            exact_Nm=exact,
            relative_error=sol.torque / exact - 1
            if abs(exact) > moment * b * 1e-10
            else None,
            error_over_peak=abs(sol.torque - exact) / (moment * b),
            elements=sol.result["elements"],
            elapsed_s=sol.result["elapsed_s"],
            device=sol.result["device"],
        )
        print(json.dumps(row), flush=True)
        rows.append(row)
    rows.append(ring(root))
    (sol.path / "comparison.json").write_text(json.dumps(rows, indent=2))
    assert abs(rows[2]["relative_error"]) < 0.01
    assert abs(rows[3]["relative_error"]) < 0.02
    assert rows[4]["error_over_peak"] < 0.005


if __name__ == "__main__":
    main()
