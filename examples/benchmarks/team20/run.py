"""TEAM 20 measured static force through the native off-axis torque operator."""

import argparse
import json
import os
from pathlib import Path
import shutil

import ngsolve as ng
import numpy as np
import psutil
from step2torque import Motor, _mesh, _source, _worker
from step2torque.solution import Solution

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", type=int, nargs="+", choices=[1000, 3000, 4500, 5000], default=[1000, 3000, 4500, 5000])
    parser.add_argument("--size", type=float, default=2)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()
    motor = Motor(ROOT / "model.step", axis="+y", center=(100, 0, 75), symmetry=1, contact_gap=0)
    for name in ["pole", "yoke"]:
        motor.components[name].set_material(ROOT / "steel.csv", rotating=name == "pole")
    motor.components["coil"].make_coil(phase=1, turns=381, direction="+z")
    measured = dict(np.loadtxt(ROOT / "measurements.csv", delimiter=",", skiprows=1))
    previous = None

    def start(solution):
        # Run in this process so the rounded-winding input adapter is retained.
        if previous is not None:
            for name in ["mesh.json", "mesh.vol.gz", "source0.sol", "torque.npz"]:
                shutil.copyfile(previous / name, solution.path / name)
        (solution.path / "worker.json").write_text(json.dumps(dict(pid=os.getpid(), started_at=psutil.Process().create_time())))
        try:
            _worker.run(solution.path)
        except Exception as error:
            (solution.path / "status.json").write_text(json.dumps(dict(status="failed", error=str(error), pid=os.getpid())))
            raise

    original = _mesh.coil_geometry, _source.annular_source, Solution._start
    _mesh.coil_geometry, _source.annular_source, Solution._start = winding, source, start
    rows = []
    try:
        for at in args.at:
            sol = motor.solve(current=at / 381, mesh_size=args.size, air_radius=400, threads=4, device=args.device).wait()
            force = abs(sol.torque / 0.1)
            row = dict(
                ampere_turns=at, native_torque_Nm=sol.torque,
                force_magnitude_N=force, measured_force_magnitude_N=measured[at],
                difference_percent=100 * (force / measured[at] - 1),
                elapsed_s=sol.result["elapsed_s"], cached_mesh_and_source=previous is not None,
                path=str(sol.path),
            )
            rows.append(row)
            previous = sol.path
            (sol.path / "comparison.json").write_text(json.dumps(rows, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    finally:
        _mesh.coil_geometry, _source.annular_source, Solution._start = original


def winding(shape, direction, name):
    # Published rounded square pack: 39–75 mm across, 96.6 mm high.
    return dict(
        center=(np.array(tuple(shape.center)) * 0.001).tolist(), axis=list(direction),
        height=0.0966, volume=shape.mass * 1e-9, geometry="rounded_rectangle",
        inner_x=0.0145, inner_y=0.0145, inner_radius=0.005, width=0.018,
        turn_length=8 * 0.0145 + 2 * np.pi * 0.014,
    )


def source(mesh, coil):
    """Prescribed unit current in nested rounded turns; 64-point quadrature."""
    points = (np.asarray(mesh.ngmesh.Coordinates()) - coil["center"]) @ _source.frame(coil["axis"]).T
    nodes = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2)
    a, b = points[nodes[:, 0]], points[nodes[:, 1]]
    values = np.zeros(len(nodes))
    q, w = np.polynomial.legendre.leggauss(64)
    for z, wz in zip(q * coil["height"] / 2, w):
        z0, z1 = a[:, 2] - z, b[:, 2] - z
        crossing = np.flatnonzero(((z0 < 0) & (z1 >= 0)) | ((z1 < 0) & (z0 >= 0)))
        t = -z0[crossing] / (z1[crossing] - z0[crossing])
        xy = a[crossing, :2] + t[:, None] * (b[crossing, :2] - a[crossing, :2])
        distance = np.linalg.norm(np.maximum(abs(xy) - [coil["inner_x"], coil["inner_y"]], 0), axis=1)
        sign = np.sign(z1[crossing] - z0[crossing])
        for radius, wr in zip(coil["inner_radius"] + (q + 1) / 2 * coil["width"], w):
            inside = distance <= radius
            values[crossing[inside]] += wz * wr / 4 * sign[inside]
    field = ng.GridFunction(ng.HCurl(mesh, order=0), name="winding_source")
    field.vec.FV().NumPy()[:] = _source.edge_factor() * values
    return field


if __name__ == "__main__":
    main()
