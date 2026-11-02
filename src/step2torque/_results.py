"""Read a saved solution; no PDE solve is needed for field sampling."""

import json

import ngsolve as ng
import numpy as np

from ._mesh import rotation
from .materials import MU0


def flux_density(folder, points_mm):
    spec = json.loads((folder / "run.json").read_text())
    info = json.loads((folder / "mesh.json").read_text())
    result = json.loads((folder / "result.json").read_text())
    points = np.asarray(points_mm, dtype=float)
    single = points.ndim == 1
    points = np.atleast_2d(points)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("Supply a point or array of 3D points in mm")
    rot = rotation(spec["axis"])
    canonical = (points - spec["center"]) @ rot.T * 0.001
    mesh = ng.Mesh(str(folder / "mesh.vol.gz"))
    space = ng.H1(mesh, order=2, dirichlet="outer")
    if info["sectors"] > 1:
        space = ng.Periodic(space)
        # Fold sample points into the meshed periodic sector, then rotate B back.
        angle = np.arctan2(canonical[:, 1], canonical[:, 0])
        period = 2 * np.pi / info["sectors"]
        turns = np.floor((angle - info["sector_start_rad"]) / period)
        unfolded = turns * period
        x, y = canonical[:, 0].copy(), canonical[:, 1].copy()
        canonical[:, 0] = x * np.cos(unfolded) + y * np.sin(unfolded)
        canonical[:, 1] = -x * np.sin(unfolded) + y * np.cos(unfolded)
    else:
        unfolded = np.zeros(len(points))
    potential = ng.GridFunction(space)
    potential.Load(str(folder / "best.sol"))
    mapped = mesh(*canonical.T)
    if np.any(mapped["nr"] < 0):
        raise ValueError("A point is outside the solved air domain")
    h = -np.asarray(ng.grad(potential)(mapped)).reshape(-1, 3)
    currents = dict(zip(result["phases"], result["currents_A"]))
    for i, c in enumerate(info["coils"]):
        unit = ng.GridFunction(ng.HCurl(mesh, order=0))
        unit.Load(str(folder / f"source{i}.sol"))
        h += c["turns"] * currents[c["phase"]] * np.asarray(unit(mapped)).reshape(-1, 3)
    indices = np.asarray(
        mesh.MaterialCF({n: j for j, n in enumerate(mesh.GetMaterials())})(mapped), int
    ).reshape(-1)
    b = MU0 * h
    for j, name in enumerate(mesh.GetMaterials()):
        mask = indices == j
        material = info["materials"].get(name, dict(mur=1.0))
        if "h" in material:
            magnitude = np.sqrt(np.sum(h[mask] ** 2, axis=1) + 1e-8)
            value = np.interp(magnitude, material["h"], material["b"])
            value += MU0 * np.maximum(magnitude - material["h"][-1], 0)
            b[mask] = h[mask] * (value / magnitude)[:, None]
        else:
            b[mask] = MU0 * material.get("mur", 1) * h[mask] + material.get(
                "br", [0, 0, 0]
            )
    x, y = b[:, 0].copy(), b[:, 1].copy()
    b[:, 0] = x * np.cos(unfolded) - y * np.sin(unfolded)
    b[:, 1] = x * np.sin(unfolded) + y * np.cos(unfolded)
    b = b @ rot
    return b[0] if single else b
