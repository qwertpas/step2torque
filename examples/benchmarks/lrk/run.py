"""Packaged 3D scalar equations and torque versus FEMM's released LRK motor.

Extrude the released mesh with axial periodicity: the specified 2D reference
has infinite length, so free 3D end faces would be a different physical problem.
Slot currents are lifted by exact edge circulations, independently of the
package's annular-coil source. The analytic cases separately check STEP import and annular-coil sources.
"""

import argparse
import json
import math
from pathlib import Path
import re
import tempfile
import time

import netgen.meshing as nm
import ngsolve as ng
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import factorized

from step2torque import _field, _source, _torque
from step2torque._worker import device
from step2torque.materials import MU0

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    folder = Path(tempfile.mkdtemp(prefix="step2torque-lrk-"))
    fixture = ROOT
    native_text = (fixture / "model.fem").read_text()
    data = np.load(fixture / "mesh.npz")
    xy, triangles, domains, areas = (
        data[k]
        for k in ["vertices_m", "triangles_0based", "block_label_0based", "area_m2"]
    )
    source = json.loads((fixture / "model.json").read_text())
    blocks = re.findall(r"<BeginBlock>(.*?)<EndBlock>", native_text, re.S)
    mats = [
        {k: v.strip().strip('"') for k, v in re.findall(r"<(\w+)>\s*=\s*([^\n]+)", b)}
        for b in blocks
    ]
    lines = native_text.splitlines()
    start = next(i for i, s in enumerate(lines) if s.startswith("[NumBlockLabels]"))
    labels = np.array(
        [
            list(map(float, line.split()))
            for line in lines[start + 1 : start + 1 + int(lines[start].split("=")[1])]
        ]
    )
    currents = np.zeros(len(labels))
    for c in source["coil_normalization"]:
        currents[c["label"]] = c["NI_A"] / sum(areas[domains == c["label"]])
    local = np.stack(
        [triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]], axis=1
    )
    edges, inv = np.unique(
        np.sort(local.reshape(-1, 2), axis=1), axis=0, return_inverse=True
    )
    delta1 = xy[triangles[:, 1]] - xy[triangles[:, 0]]
    delta2 = xy[triangles[:, 2]] - xy[triangles[:, 0]]
    orientation = np.sign(delta1[:, 0] * delta2[:, 1] - delta1[:, 1] * delta2[:, 0])
    signs = np.where(local[:, :, 0] < local[:, :, 1], 1.0, -1.0) * orientation[:, None]
    incidence = sparse.csr_matrix(
        (signs.ravel(), (np.repeat(np.arange(len(triangles)), 3), inv)),
        shape=(len(triangles), len(edges)),
    )
    target = currents[domains] * areas
    inverse = factorized((incidence @ incidence.T).tocsc())
    edge_values = incidence.T @ inverse(target)
    edge_values += incidence.T @ inverse(target - incidence @ edge_values)
    circulation_error = float(np.max(abs(incidence @ edge_values - target)))
    print("source circulation error A", circulation_error, flush=True)
    del inverse

    mesh = nm.Mesh(dim=3)
    # An arbitrary axial period represents the same infinite-length motor.
    # Keep its tetrahedra well conditioned, then scale torque to the published
    # 10 mm depth. A 10 mm single cell would have an aspect ratio near 1000.
    depth = 0.0001
    n = len(xy)
    xyz = np.column_stack([np.tile(xy, (2, 1)), np.repeat([0.0, depth], n)])
    for x, y, z in xyz:
        mesh.Add(nm.MeshPoint(nm.Pnt(float(x), float(y), float(z))))
    faces = []
    for k in range(len(labels)):
        name = (
            "air" if mats[int(labels[k, 2]) - 1]["BlockName"] == "Air" else f"body{k}"
        )
        mesh.SetMaterial(k + 1, name)
        faces.append(
            [
                mesh.Add(nm.FaceDescriptor(surfnr=2 * k + s + 1, domin=k + 1, bc=s + 1))
                for s in [0, 1]
            ]
        )
    for triangle, domain in zip(np.sort(triangles, axis=1), domains):
        a, b, c = map(int, triangle)
        for vertices in [
            [a, b, c, c + n],
            [a, b, b + n, c + n],
            [a, a + n, b + n, c + n],
        ]:
            p = xyz[vertices]
            if np.linalg.det((p[1:] - p[0]).T) < 0:
                vertices[0], vertices[1] = vertices[1], vertices[0]
            mesh.Add(
                nm.Element3D(index=int(domain) + 1, vertices=[v + 1 for v in vertices])
            )
        mesh.Add(nm.Element2D(index=faces[domain][0], vertices=[a + 1, c + 1, b + 1]))
        mesh.Add(
            nm.Element2D(
                index=faces[domain][1], vertices=[a + n + 1, b + n + 1, c + n + 1]
            )
        )
    counts = np.bincount(inv, minlength=len(edges))
    owner = np.full(len(edges), len(triangles))
    np.minimum.at(owner, inv, np.repeat(np.arange(len(triangles)), 3))
    outside = {}
    for k in np.flatnonzero(counts == 1):
        domain = int(domains[owner[k]])
        if domain not in outside:
            outside[domain] = mesh.Add(
                nm.FaceDescriptor(surfnr=100 + domain, domin=domain + 1, bc=3)
            )
        a, b = map(int, edges[k])
        mesh.Add(
            nm.Element2D(index=outside[domain], vertices=[a + 1, b + 1, b + n + 1])
        )
        mesh.Add(
            nm.Element2D(index=outside[domain], vertices=[a + 1, b + n + 1, a + n + 1])
        )
    boundary_names = ["axial0", "axial1", "outer"]
    for k in range(mesh.GetNFaceDescriptors()):
        mesh.SetBCName(k, boundary_names[mesh.FaceDescriptor(k + 1).bc - 1])
    for k in range(n):
        mesh.AddPointIdentification(k + 1, k + n + 1, 1, nm.IdentificationType.PERIODIC)
    mesh.Save(str(folder / "mesh.vol.gz"))
    mesh = ng.Mesh(str(folder / "mesh.vol.gz"))
    ng.SetNumThreads(8)
    # All projected tetrahedron edges are original triangle edges or points.
    wire = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2)
    a, b = wire[:, 0] % n, wire[:, 1] % n
    lookup = {tuple(e): v for e, v in zip(edges, edge_values)}
    values = np.array(
        [
            0.0
            if x == y
            else (1 if x < y else -1) * lookup[tuple(sorted((int(x), int(y))))]
            for x, y in zip(a, b)
        ]
    )
    impressed = ng.GridFunction(ng.HCurl(mesh, order=0))
    impressed.vec.FV().NumPy()[:] = _source.edge_factor() * values
    impressed.Save(str(folder / "source.sol"))
    materials = {}
    curves = {c["name"]: c for c in source["processed_BH"]}
    for k, label in enumerate(labels):
        m = mats[int(label[2]) - 1]
        if m["BlockName"] in curves:
            c = curves[m["BlockName"]]
            law = dict(h=c["H_A_per_m"], b=c["B_T"])
        else:
            law = dict(mur=1 + float(m["LamFill"]) * (float(m["Mu_x"]) - 1))
            if float(m["H_c"]):
                angle = math.radians(label[5])
                br = MU0 * law["mur"] * float(m["H_c"])
                law["br"] = [br * math.cos(angle), br * math.sin(angle), 0.0]
        materials["air" if m["BlockName"] == "Air" else f"body{k}"] = law
    # FEMM's outer A=0 means zero normal B: a natural scalar boundary.
    # Fix one potential value to remove the arbitrary additive constant.
    space = ng.Periodic(ng.H1(mesh, order=2))
    free = space.FreeDofs()
    free[next(i for i in range(mesh.nv) if free[i])] = False
    method = device("cpu" if args.cpu else "auto", space.ndof)
    print("field", mesh.ne, space.ndof, method, flush=True)
    solved = _field.solve(
        mesh,
        impressed,
        materials,
        space=space,
        periodic=True,
        method=method,
        tolerance=1e-9 * math.sqrt(depth / source["depth_m"]),
        stages=(0.25, 0.5, 1.0),
    )
    solved["potential"].Save(str(folder / "field.sol"))
    radius = np.linalg.norm(xyz[:, :2], axis=1)
    weight = 1 - np.clip((radius - 0.0128) / 0.0002, 0, 1)
    velocity = ng.GridFunction(ng.VectorH1(mesh, order=1))
    velocity.components[0].vec.FV().NumPy()[:] = -weight * xyz[:, 1]
    velocity.components[1].vec.FV().NumPy()[:] = weight * xyz[:, 0]
    evaluator, audit = _torque.make_torque(
        mesh,
        space,
        [impressed],
        1,
        0,
        0,
        velocity=velocity,
    )
    value = evaluator(solved["potential"], [1.0]) * source["depth_m"] / depth
    report = dict(
        computed_Nm=value,
        published_Nm=0.0554274,
        relative_error=abs(value / 0.0554274 - 1),
        source_circulation_error_A=circulation_error,
        elements=mesh.ne,
        dofs=space.ndof,
        elapsed_s=time.monotonic() - started,
        final=solved["diagnostics"]["newton"][-1],
        work_source_cells=audit["work_source_cells"],
        path=str(folder),
        scope="Packaged 3D scalar/torque, axial periodic extension of released 2D radial motor; independent slot-source lift",
    )
    (folder / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    assert not any(audit["work_source_cells"])
    assert report["relative_error"] < 0.01


if __name__ == "__main__":
    main()
