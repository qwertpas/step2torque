"""Frozen-angle 3D torque for COMSOL's axial-field magnetic gear (22301)."""

import argparse
import json
import math
import csv
import tempfile
from pathlib import Path
import time

import netgen.occ as occ
import ngsolve as ng
import numpy as np

from step2torque import _field, _motion, _torque
from step2torque._worker import device

ROOT = Path(__file__).resolve().parent
MU0 = 4e-7 * math.pi


def parts(hs=0, ls=0):
    """OCC exact circular annular sectors, in the supplied mm coordinates."""
    from step2torque._mesh import wedge

    def sector(start, width, z, height):
        solid = wedge(50, math.radians(start), math.radians(start + width), z, height)
        return solid - occ.Cylinder(occ.Pnt(0, 0, z - 1), occ.Z, 30, height + 2)

    bodies = []
    for rotor, count, angle, z, thick in (("hs", 4, hs, 5, 5), ("ls", 10, ls, 26, 5)):
        for k in range(count):
            bodies.append(
                (
                    f"{rotor}_magnet{k}",
                    sector(angle + 360 * k / count, 360 / count, z, thick),
                    rotor,
                    (-1) ** (k + 1),
                )
            )
        ring = occ.Cylinder(
            occ.Pnt(0, 0, 0 if rotor == "hs" else 31),
            occ.Z,
            50,
            5 if rotor == "hs" else 8,
        )
        hole = occ.Cylinder(
            occ.Pnt(0, 0, -1 if rotor == "hs" else 30),
            occ.Z,
            30,
            7 if rotor == "hs" else 10,
        )
        bodies.append((f"{rotor}_iron", ring - hole, rotor, 0))
    for k in range(7):
        bodies.append((f"pole{k}", sector(360 * k / 7, 360 / 14, 13, 10), "fixed", 0))
    for name, solid, _, _ in bodies:
        solid.name = name
    expected = math.pi * (50**2 - 30**2) * (5 + 5 + 5 + 8 + 10 / 2)
    assert abs(sum(s.mass for _, s, _, _ in bodies) / expected - 1) < 1e-10
    return bodies


def materials(subdivide=1):
    curve = np.loadtxt(ROOT / "bh.csv", delimiter=",", skiprows=1)
    # Retain COMSOL's linear extrapolation rather than the package's default
    # vacuum slope beyond the last datum. The solved H range is checked below.
    h = np.r_[curve[:, 0], 1e7]
    b = np.r_[
        curve[:, 1],
        curve[-1, 1]
        + (1e7 - curve[-1, 0]) * np.diff(curve[-2:, 1])[0] / np.diff(curve[-2:, 0])[0],
    ]
    fine_h = np.unique(
        np.concatenate(
            [np.linspace(a, z, subdivide + 1) for a, z in zip(h[:-1], h[1:])]
        )
    )
    fine_b = np.interp(fine_h, h, b)
    laws = dict(
        hs_iron=dict(mur=500), ls_iron=dict(h=fine_h.tolist(), b=fine_b.tolist())
    )
    for rotor, count in (("hs", 4), ("ls", 10)):
        laws.update(
            {
                f"{rotor}_magnet{k}": dict(mur=1.05, br=[0, 0, 1.42 * (-1) ** (k + 1)])
                for k in range(count)
            }
        )
    laws.update({f"pole{k}": dict(mur=1000) for k in range(7)})
    return laws


def make_mesh(folder, hs, ls, size, radius, padding, gap):
    path = folder / "mesh.vol.gz"
    bodies = parts(hs, ls)
    boundary = occ.Cylinder(occ.Pnt(0, 0, -padding), occ.Z, radius, 39 + 2 * padding)
    boundary.faces.name = "outer"
    solids = []
    for name, solid, _, _ in bodies:
        solid.mat(name)
        solid.maxh = size * 2
        solid.faces.maxh = size
        for face in solid.faces:
            z = face.center.z
            if min(abs(z - k) for k in (10, 13, 23, 26)) < 1e-6:
                face.maxh = gap
        solids.append(solid)
    fused = occ.Fuse(solids)
    assert abs(fused.mass / sum(s.mass for s in solids) - 1) < 1e-10
    air = boundary - fused
    air.mat("air")
    air.maxh = max(8, radius / 8)
    shape = occ.Glue([air, *solids])
    print(
        json.dumps(
            dict(
                stage="meshing",
                size_mm=size,
                gap_mm=gap,
                radius_mm=radius,
                hs=hs,
                ls=ls,
            )
        ),
        flush=True,
    )
    native = occ.OCCGeometry(shape).GenerateMesh(
        maxh=max(8, radius / 8), grading=0.35, curvaturesafety=8, minedgelen=1e-6
    )
    native.Scale(0.001)
    native.Save(str(path))
    mesh = ng.Mesh(str(path))
    volumes = ng.Integrate(1, mesh, region_wise=True)
    (folder / "volumes.json").write_text(
        json.dumps(dict(zip(mesh.GetMaterials(), list(volumes))), indent=2)
    )
    return mesh


def space(mesh):
    fes = ng.H1(mesh, order=2)
    fes.FreeDofs()[0] = False  # One gauge; n.B=0 is the natural boundary.
    return fes


def torque(mesh, fes, phi, rotor):
    moving = [m for m in mesh.GetMaterials() if m.startswith(rotor + "_")]
    velocity, spin = _motion.velocity(mesh, moving, [])
    evaluate, info = _torque.make_torque(
        mesh, fes, [], 1, None, None, velocity=velocity, spin=spin
    )
    return evaluate(phi), info


def solve(folder, row, threads, cpu):
    started = time.monotonic()
    folder.mkdir(parents=True, exist_ok=True)
    config = dict(
        hs=float(row["hs_deg"]),
        ls=float(row["ls_deg"]),
        size=1.5,
        gap=0.75,
        radius=75,
        padding=20,
        boundary="insulation",
        threads=threads,
    )
    (folder / "input.json").write_text(json.dumps(config, indent=2))
    mesh = make_mesh(folder, config["hs"], config["ls"], 1.5, 75, 20, 0.75)
    fes = space(mesh)
    method = device("cpu" if cpu else "auto", fes.ndof)
    print("field", mesh.ne, fes.ndof, method, flush=True)
    result = _field.solve(
        mesh,
        ng.CF((0, 0, 0)),
        materials(),
        space=fes,
        method=method,
        tolerance=1e-9,
    )
    if method == "cuda":
        result["tangent_inverse"].cleanup()
    phi = result["potential"]
    phi.Save(str(folder / "potential.sol"))
    output = dict(
        case=row["case"], input=config, method=method, diagnostics=result["diagnostics"]
    )
    for rotor in ("hs", "ls"):
        # Only this rotor moves in this derivative; the equilibrium includes both.
        value, info = torque(mesh, fes, phi, rotor)
        output[rotor + "_Nm"] = value
        output[rotor + "_reference_Nm"] = float(row[rotor + "_Nm"])
        output[rotor + "_error_percent"] = 100 * (value / float(row[rotor + "_Nm"]) - 1)
        output[rotor + "_torque"] = info
    cells = mesh.ngmesh.Elements3D().NumPy()
    selected = np.flatnonzero(
        np.array(mesh.GetMaterials())[cells["index"] - 1] == "ls_iron"
    )
    coords = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
    centers = coords[cells["nodes"][selected] - 1].mean(1)
    mapped = np.repeat(mesh(*centers[:1].T), len(selected))
    mapped["nr"] = selected
    maximum = 0
    for point in ng.IntegrationRule(ng.ET.TET, 6):
        for j, key in enumerate(("x", "y", "z")):
            mapped[key] = point.point[j]
        maximum = max(maximum, float(np.linalg.norm(result["H"](mapped), axis=1).max()))
    assert maximum < 1e7  # Within the explicitly preserved linear extrapolation.
    output["ls_max_H_A_per_m"] = maximum
    output["elapsed_s"] = time.monotonic() - started
    (folder / "result.json").write_text(json.dumps(output, indent=2))
    final = output["diagnostics"]["newton"][-1]
    assert final["decrement_sqrt_j"] < 1e-9 and final["hessian_floor"] == 0
    assert max(abs(output[r + "_error_percent"]) for r in ("hs", "ls")) < 1
    print(
        json.dumps(
            {
                k: output[k]
                for k in (
                    "case",
                    "hs_Nm",
                    "ls_Nm",
                    "hs_error_percent",
                    "ls_error_percent",
                    "elapsed_s",
                )
            }
        ),
        flush=True,
    )
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument(
        "--initial", action="store_true", help="solve only the supplied initial pose"
    )
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    ng.SetNumThreads(args.threads)
    folder = args.output or Path(tempfile.mkdtemp(prefix="step2torque-gear-"))
    print("output", folder, flush=True)
    with (ROOT / "reference.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    if args.initial:
        rows = rows[:1]
    results = [solve(folder / row["case"], row, args.threads, args.cpu) for row in rows]
    if not args.initial:
        assert (
            max(
                abs(results[-1][r + "_Nm"] / results[0][r + "_Nm"] - 1)
                for r in ("hs", "ls")
            )
            < 0.002
        )
    (folder / "comparison.json").write_text(json.dumps(results, indent=2))
    print(
        "PASS",
        len(results),
        "poses; both rotors within 1% of frozen reference",
        flush=True,
    )


if __name__ == "__main__":
    main()
