"""Exact finite-sphere interaction from Edwards et al., AJP (2017)."""

import argparse
import json
from pathlib import Path

import numpy as np
from step2torque import Motor

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=float, default=0.3)
    parser.add_argument("--radius", type=float, default=400)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()
    reference = json.loads((ROOT / "reference.json").read_text())
    motor = Motor(
        ROOT / "model.step", axis="+z",
        center=reference["geometry"]["target_center_mm"], symmetry=1, contact_gap=0,
    )
    for name in ["source", "target"]:
        polarization = np.array(reference[f"{name}_polarization_T"])
        motor.components[name].make_magnet(
            direction=polarization, br=np.linalg.norm(polarization), mur=1,
            rotating=name == "target",
        )
    sol = motor.solve(
        current={}, mesh_size=args.size, air_radius=args.radius,
        threads=4, device=args.device,
    ).wait()
    exact = reference["torque_Nm"][2]
    probes = np.load(ROOT / "reference-fields.npz")
    field = sol.flux_density(probes["points_mm"])
    row = dict(
        reference_Nm=exact, actual_Nm=sol.torque,
        magnitude_difference_percent=100 * (abs(sol.torque / exact) - 1),
        field_relative_L2_percent=100 * np.linalg.norm(field - probes["B_T"]) / np.linalg.norm(probes["B_T"]),
        elapsed_s=sol.result["elapsed_s"], path=str(sol.path),
    )
    (sol.path / "comparison.json").write_text(json.dumps(row, indent=2) + "\n")
    print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
