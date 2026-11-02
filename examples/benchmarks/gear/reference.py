"""Independent curl-conforming vector-potential and Maxwell-stress reference.

Uses NGSolve's symbolic assembly, not step2torque's kernel, scalar solver,
motion, or torque implementation. Shares the audited CAD/material inputs.
"""

import argparse
from collections import deque
import json
from pathlib import Path
import time

import ngsolve as ng
import numpy as np

from run import ROOT, MU0, materials
from step2torque._worker import device


def maxwell_gap(mesh, field, rotor, order):
    """Exact FE-polynomial Maxwell torque averaged over the complete gap.

    Clip air tetrahedra to the two axial gap planes before quadrature. This
    avoids sampling across either FE jumps or piecewise integration weights.
    """
    from scipy.spatial import ConvexHull

    low, high, sign = (0.010, 0.013, 1) if rotor == "hs" else (0.023, 0.026, -1)
    coordinates = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
    cells = mesh.ngmesh.Elements3D().NumPy()
    points = coordinates[cells["nodes"] - 1]
    air = np.array(mesh.GetMaterials())[cells["index"] - 1] == "air"
    selected = np.flatnonzero(
        air
        & (points[:, :, 2].min(1) < high - 1e-14)
        & (points[:, :, 2].max(1) > low + 1e-14)
    )
    tetrahedra, owners = [], []
    for number in selected:
        tetra = points[number]
        if tetra[:, 2].min() >= low and tetra[:, 2].max() <= high:
            tetrahedra.append(tetra)
            owners.append(number)
            continue
        polygon = [p for p in tetra if low - 1e-14 <= p[2] <= high + 1e-14]
        for i in range(4):
            for j in range(i):
                a, b = tetra[i], tetra[j]
                for z in (low, high):
                    if (a[2] - z) * (b[2] - z) < 0:
                        polygon.append(a + (b - a) * (z - a[2]) / (b[2] - a[2]))
        polygon = np.unique(np.round(polygon, 14), axis=0)
        hull = ConvexHull(polygon)
        center = polygon.mean(0)
        for face in hull.simplices:
            tetrahedra.append(np.vstack([center, polygon[face]]))
            owners.append(number)
    tetrahedra = np.asarray(tetrahedra)
    determinant = abs(
        np.linalg.det((tetrahedra[:, 1:] - tetrahedra[:, :1]).transpose(0, 2, 1))
    )
    mapped = np.repeat(mesh(*points[selected[:1]].mean(1).T), len(owners))
    mapped["nr"] = owners
    for key in ("x", "y", "z"):
        mapped[key] = 0
    origin = np.column_stack([coordinate(mapped) for coordinate in (ng.x, ng.y, ng.z)])
    inverse = np.linalg.inv(
        np.asarray(ng.specialcf.JacobianMatrix(3)(mapped)).reshape(-1, 3, 3)
    )
    torque = 0.0
    for point in ng.IntegrationRule(ng.ET.TET, order):
        xyz = tetrahedra[:, 0] + np.einsum(
            "i,nij->nj", np.array(point.point), tetrahedra[:, 1:] - tetrahedra[:, :1]
        )
        local = np.einsum("nij,nj->ni", inverse, xyz - origin)
        assert local.min() > -1e-8 and local.sum(1).max() < 1 + 1e-8
        for j, key in enumerate(("x", "y", "z")):
            mapped[key] = local[:, j]
        b = np.asarray(field(mapped)).reshape(-1, 3)
        torque += point.weight * np.sum(
            determinant * (xyz[:, 0] * b[:, 1] - xyz[:, 1] * b[:, 0]) * b[:, 2]
        )
    return dict(
        order=order,
        torque_Nm=float(sign * torque / ((high - low) * MU0)),
        integration_tetrahedra=len(owners),
        gap_volume_m3=float(determinant.sum() / 6),
    )


def gauge(space, mesh):
    # Exact tree gauge for the remaining lowest-order gradients. nograds=True
    # removes higher-order gradients. All boundary vertices share one root.
    parent = list(range(mesh.nv))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        a, b = find(a), find(b)
        if a == b:
            return False
        parent[b] = a
        return True

    free = space.FreeDofs()
    for edge in mesh.edges:
        dof = space.GetDofNrs(edge)[0]
        if not free[dof]:
            union(*[v.nr for v in edge.vertices])
    root = find(
        next(
            v.nr
            for e in mesh.edges
            if not free[space.GetDofNrs(e)[0]]
            for v in e.vertices
        )
    )
    boundary = {i for i in range(mesh.nv) if find(i) == root}
    adjacent = [[] for _ in range(mesh.nv)]
    for edge in mesh.edges:
        dof = space.GetDofNrs(edge)[0]
        a, b = [v.nr for v in edge.vertices]
        adjacent[a].append((b, dof))
        adjacent[b].append((a, dof))
    visited = set(boundary)
    queue = deque(boundary)
    pinned = 0
    while queue:
        for neighbor, dof in adjacent[queue.popleft()]:
            if neighbor not in visited:
                assert free[dof]
                free[dof] = False
                visited.add(neighbor)
                queue.append(neighbor)
                pinned += 1
    assert len(visited) == mesh.nv
    return pinned


def solve(mesh, order, method):
    space = ng.HCurl(mesh, order=order, nograds=True, dirichlet="outer")
    pinned = gauge(space, mesh)
    print(
        json.dumps(dict(stage="vector_field", ndof=space.ndof, method=method)),
        flush=True,
    )
    a = ng.GridFunction(space, name="vector_potential")
    b = ng.curl(a)
    bn = ng.sqrt(b * b + 1e-24)
    laws = materials()
    u, v = space.TnT()
    linear = ng.BilinearForm(space, symmetric=True)
    linear += 0 * ng.curl(u) * ng.curl(v) * ng.dx
    load = ng.LinearForm(space)
    for name in mesh.GetMaterials():
        if name == "ls_iron":
            continue
        law = laws.get(name, dict(mur=1))
        measure = ng.dx(definedon=mesh.Materials(name))
        linear += (1 / (MU0 * law["mur"])) * ng.curl(u) * ng.curl(v) * measure
        load += (
            (1 / (MU0 * law["mur"]))
            * ng.CF(tuple(law.get("br", [0, 0, 0])))
            * ng.curl(v)
            * measure
        )
    curve = np.loadtxt(ROOT / "bh.csv", delimiter=",", skiprows=1)
    h, knots = curve.T
    slopes = np.diff(h) / np.diff(knots)
    H, energy, differential = slopes[0] * bn, 0.5 * slopes[0] * bn**2, ng.CF(slopes[0])
    for k in range(1, len(slopes)):
        t = bn - knots[k]
        delta = slopes[k] - slopes[k - 1]
        H += ng.IfPos(t, delta * t, 0)
        energy += ng.IfPos(t, 0.5 * delta * t**2, 0)
        differential += ng.IfPos(t, delta, 0)
    secant = H / bn
    tensor = (
        secant * ng.Id(3) + (differential - secant) * ng.OuterProduct(b, b) / bn**2
    ).Compile()
    measure = ng.dx(
        definedon=mesh.Materials("ls_iron"),
        intrules={ng.ET.TET: ng.IntegrationRule(ng.ET.TET, 8)},
    )
    nonlinear = ng.BilinearForm(space, symmetric=True)
    nonlinear += 0 * ng.curl(u) * ng.curl(v) * ng.dx
    nonlinear += (tensor * ng.curl(u)) * ng.curl(v) * measure
    residual = ng.LinearForm(space)
    residual += (-secant * b * ng.curl(v)).Compile() * measure
    work = energy.Compile() * measure
    history = []
    correction, before, rlin = [a.vec.CreateVector() for _ in range(3)]

    with ng.TaskManager():
        linear.Assemble()
        load.Assemble()

        def value():
            rlin.data = linear.mat * a.vec
            return float(
                ng.Integrate(work, mesh)
                + 0.5 * ng.InnerProduct(a.vec, rlin)
                - ng.InnerProduct(load.vec, a.vec)
            )

        current = value()
        inverse = None
        for iteration in range(60):
            nonlinear.Assemble()
            nonlinear.mat.AsVector().data += linear.mat.AsVector()
            residual.Assemble()
            rlin.data = linear.mat * a.vec
            residual.vec.data += load.vec - rlin
            if inverse is None:
                if method == "cuda":
                    from step2torque._cuda import CuDSS

                    inverse = CuDSS(nonlinear.mat, space.FreeDofs())
                else:
                    inverse = nonlinear.mat.Inverse(
                        space.FreeDofs(), inverse="sparsecholesky"
                    )
            else:
                inverse.Update()
            correction.data = inverse * residual.vec
            decrement2 = float(ng.InnerProduct(correction, residual.vec))
            assert decrement2 >= 0 and np.isfinite(decrement2)
            record = dict(iteration=iteration, decrement_sqrt_j=decrement2**0.5)
            history.append(record)
            print(json.dumps(record), flush=True)
            if decrement2**0.5 < 1e-9:
                break
            before.data = a.vec
            step = 1
            for _ in range(30):
                a.vec.data = before + step * correction
                candidate = value()
                if candidate < current - 1e-4 * step * decrement2 + 50 * np.finfo(
                    float
                ).eps * max(1, abs(current)):
                    current = candidate
                    record["step"] = step
                    break
                step /= 2
            else:
                raise RuntimeError("Vector-potential energy line search failed")
        else:
            raise RuntimeError("Vector-potential Newton did not converge")
    if method == "cuda":
        inverse.cleanup()
    return a, dict(
        newton=history, ndof=space.ndof, pinned_tree_edges=pinned, energy_j=current
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="one pose folder produced by run.py")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()
    ng.SetNumThreads(args.threads)
    config = json.loads((args.folder / "input.json").read_text())
    assert config["boundary"] == "insulation"
    mesh = ng.Mesh(str(args.folder / "mesh.vol.gz"))
    started = time.monotonic()
    method = device("cpu" if args.cpu else "auto", mesh.ne)
    potential, diagnostics = solve(mesh, 2, method)
    potential.Save(str(args.folder / "reference.sol"))
    result = dict(input=config, diagnostics=diagnostics, method=method)
    for rotor in ("hs", "ls"):
        values = [maxwell_gap(mesh, ng.curl(potential), rotor, q) for q in (4, 6)]
        assert abs(values[0]["torque_Nm"] - values[1]["torque_Nm"]) < 1e-10
        result[rotor + "_Nm"] = values[-1]["torque_Nm"]
        result[rotor + "_quadrature"] = values
    result["elapsed_s"] = time.monotonic() - started
    (args.folder / "reference.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
