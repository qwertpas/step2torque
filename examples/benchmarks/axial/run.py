"""3D scalar/virtual-work check of a released unrolled axial-flux motor.

The reference is native xfemm with the same geometry and material laws.
Two opposite poles turn anti-periodicity into ordinary periodicity. A short
periodic extrusion preserves the paper's 2D linear-motor approximation; this
does not validate finite annular end effects. See README.md for sources and boundary conditions.
"""

import argparse
import json
from pathlib import Path
import tempfile
import time

import netgen.meshing as nm
import ngsolve as ng
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from step2torque import _field, _torque
from step2torque._worker import device

ROOT = Path(__file__).resolve().parent


def straight_mesh(xy, triangles, domains, pairs, period):
    """Wrap the native stepped cut onto a straight periodic boundary.

    FEMM pairs vertices on its stepped cut. P2 also needs paired edges; an
    interior chord across a cut's corner has no counterpart on the other side.
    Splitting at a straight cut preserves the exact geometry and material areas.
    """
    left = pairs[np.arange(len(pairs)), np.argmin(xy[pairs, 0], axis=1)]
    cut = xy[left, 0].max()
    points, cells, labels = [], [], []
    for triangle, domain in zip(triangles, domains):
        polygon = xy[triangle].copy()
        polygon[abs(polygon[:, 0] - cut) < 1e-12, 0] = cut
        for sign in [-1, 1]:
            clipped = []
            for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
                inside_a, inside_b = sign * (a[0] - cut) >= 0, sign * (b[0] - cut) >= 0
                if inside_a:
                    clipped.append(a)
                if inside_a != inside_b:
                    clipped.append(a + (cut - a[0]) / (b[0] - a[0]) * (b - a))
            if len(clipped) < 3:
                continue
            clipped = np.array(clipped) + ([period, 0] if sign < 0 else [0, 0])
            for j in range(1, len(clipped) - 1):
                p = clipped[[0, j, j + 1]]
                if abs(np.linalg.det((p[1:] - p[0]).T)) < 1e-20:
                    continue
                cells.append(np.arange(len(points), len(points) + 3))
                points.extend(p)
                labels.append(domain)
    points = np.array(points)
    close = cKDTree(points).query_pairs(1e-10, output_type="ndarray")
    graph = coo_matrix((np.ones(len(close)), close.T), shape=(len(points), len(points)))
    _, mapping = connected_components(graph, directed=False)
    _, first = np.unique(mapping, return_index=True)
    result = points[first], mapping[np.array(cells)], np.array(labels)
    p = result[0][result[1]]
    q = xy[triangles]
    old_area = np.bincount(domains, abs(np.linalg.det(q[:, 1:] - q[:, :1])) / 2)
    new_area = np.bincount(result[2], abs(np.linalg.det(p[:, 1:] - p[:, :1])) / 2)
    assert np.allclose(old_area, new_area, rtol=1e-10, atol=1e-15)
    left = np.flatnonzero(abs(result[0][:, 0] - cut) < 1e-10)
    distances, right = cKDTree(result[0]).query(result[0][left] + [period, 0])
    assert max(distances) < 1e-10
    return *result, np.column_stack([left, right])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    source = json.loads((ROOT / "model.json").read_text())
    data = np.load(ROOT / "mesh.npz")
    period = source["half_period_m"]
    original, native_triangles, native_domains, native_pairs = straight_mesh(
        data["vertices_m"],
        data["triangles_0based"],
        data["block_label_0based"],
        data["periodic_pairs_0based"],
        period,
    )
    # Native paired nodes define the join; rounded coordinates lose matches
    # when a fine-mesh vertex happens to lie on a rounding threshold.
    pairs = native_pairs.copy()
    pairs[original[pairs[:, 0], 0] > original[pairs[:, 1], 0]] = pairs[
        original[pairs[:, 0], 0] > original[pairs[:, 1], 0]
    ][:, ::-1]
    assert (
        np.max(abs(original[pairs[:, 1]] - original[pairs[:, 0]] - [period, 0])) < 1e-10
    )
    points, triangles, domains = original.tolist(), [], []
    joined = dict(pairs)
    second = []
    for k, point in enumerate(original + [period, 0]):
        if k in joined:
            second.append(joined[k])
        else:
            second.append(len(points))
            points.append(point)
    second = np.array(second)
    for copy in range(2):
        mapping = np.arange(len(original)) if copy == 0 else second
        triangles.extend(mapping[native_triangles])
        domains.extend(native_domains + copy * len(source["labels"]))
    xy, triangles, domains = np.array(points), np.array(triangles), np.array(domains)
    n = len(xy)
    paired, matches = pairs[:, 0], second[pairs[:, 1]]
    rank = np.arange(n)
    rank[matches] = paired
    # Periodic quadrilateral faces need the same diagonal at both ends.
    triangles = np.take_along_axis(
        triangles, np.argsort(rank[triangles], axis=1), axis=1
    )
    depth = 0.0005
    xyz = np.column_stack([np.tile(xy, (2, 1)), np.repeat([0.0, depth], n)])
    native = nm.Mesh(dim=3)
    for x, y, z in xyz:
        native.Add(nm.MeshPoint(nm.Pnt(float(x), float(y), float(z))))
    materials, faces = {}, []
    for copy in range(2):
        for j, label in enumerate(source["labels"]):
            material = source["materials"][int(label[2]) - 1]
            name = (
                "air"
                if material["name"] == "Air"
                else f"body{j + copy * len(source['labels'])}"
            )
            law = dict(material["law"])
            if "br_T" in law:
                law["br"] = [0, (-1) ** copy * law.pop("br_T"), 0]
            materials[name] = law
            index = j + copy * len(source["labels"]) + 1
            native.SetMaterial(index, name)
            faces.append(
                [
                    native.Add(
                        nm.FaceDescriptor(surfnr=2 * index + s, domin=index, bc=s + 1)
                    )
                    for s in [0, 1]
                ]
            )
    for triangle, domain in zip(triangles, domains):
        a, b, c = map(int, triangle)
        for vertices in [
            [a, b, c, c + n],
            [a, b, b + n, c + n],
            [a, a + n, b + n, c + n],
        ]:
            p = xyz[vertices]
            if np.linalg.det((p[1:] - p[0]).T) < 0:
                vertices[0], vertices[1] = vertices[1], vertices[0]
            native.Add(
                nm.Element3D(index=int(domain) + 1, vertices=[v + 1 for v in vertices])
            )
        native.Add(nm.Element2D(index=faces[domain][0], vertices=[a + 1, c + 1, b + 1]))
        native.Add(
            nm.Element2D(
                index=faces[domain][1], vertices=[a + n + 1, b + n + 1, c + n + 1]
            )
        )
    edges = np.sort(
        np.concatenate(
            [triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]]
        ),
        axis=1,
    )
    edges, inverse, count = np.unique(
        edges, axis=0, return_inverse=True, return_counts=True
    )
    owners = np.full(len(edges), domains.max() + 1)
    np.minimum.at(owners, inverse, np.tile(domains, 3))
    outside_faces = {}
    for k in np.flatnonzero(count == 1):
        a, b = edges[k]
        if rank[a] > rank[b]:
            a, b = b, a
        outside = any(
            max(abs(xy[[a, b], 1] - y)) < 1e-12
            for y in [xy[:, 1].min(), xy[:, 1].max()]
        )
        bc, domain = (3 if outside else 4), int(owners[k])
        if (bc, domain) not in outside_faces:
            outside_faces[bc, domain] = native.Add(
                nm.FaceDescriptor(
                    surfnr=100 + len(outside_faces), domin=domain + 1, bc=bc
                )
            )
        index = outside_faces[bc, domain]
        native.Add(
            nm.Element2D(index=index, vertices=[int(a) + 1, int(b) + 1, int(b) + n + 1])
        )
        native.Add(
            nm.Element2D(
                index=index, vertices=[int(a) + 1, int(b) + n + 1, int(a) + n + 1]
            )
        )
    boundary_names = ["depth0", "depth1", "outer", "repeat"]
    for k in range(native.GetNFaceDescriptors()):
        native.SetBCName(k, boundary_names[native.FaceDescriptor(k + 1).bc - 1])
    for k in range(n):
        native.AddPointIdentification(
            k + 1, k + n + 1, 1, nm.IdentificationType.PERIODIC
        )
    for a, b in zip(paired, matches):
        for z in [0, n]:
            native.AddPointIdentification(
                int(a) + z + 1, b + z + 1, 2, nm.IdentificationType.PERIODIC
            )
    mesh = ng.Mesh(native)
    ng.SetNumThreads(4)
    space = ng.Periodic(ng.H1(mesh, order=2, dirichlet="outer"))
    print("field", mesh.ne, space.ndof, flush=True)
    solved = _field.solve(
        mesh,
        ng.CF((0, 0, 0)),
        materials,
        periodic=True,
        method=device("cpu" if args.cpu else "auto", space.ndof),
        stages=(0.25, 0.5, 1.0),
        tolerance=1e-9 * np.sqrt(depth / source["depth_m"]),
    )
    weight = np.clip((0.0115 - xyz[:, 1]) / 0.0015, 0, 1)
    velocity = ng.GridFunction(ng.VectorH1(mesh, order=1))
    velocity.components[0].vec.FV().NumPy()[:] = weight
    evaluator, _ = _torque.make_torque(
        mesh,
        space,
        [],
        1,
        0,
        0,
        velocity=velocity,
        spin=np.tile([1, 0, 0], (len(xyz), 1)),
    )
    force = evaluator(solved["potential"]) * source["depth_m"] / depth
    reference = 2 * source["half_period_force_N"]
    report = dict(
        computed_force_N=force,
        native_FEMM_force_N=reference,
        computed_Nm=force * source["radius_m"] * source["periods"],
        reference_Nm=reference * source["radius_m"] * source["periods"],
        relative_error=abs(force / reference - 1),
        elements=mesh.ne,
        dofs=space.ndof,
        elapsed_s=time.monotonic() - started,
        scope="Unrolled axial-flux reference; periodic depth; matched Bt=0 outer boundaries",
    )
    folder = Path(tempfile.mkdtemp(prefix="step2torque-axial-"))
    report["path"] = str(folder)
    native.Save(str(folder / "mesh.vol.gz"))
    solved["potential"].Save(str(folder / "field.sol"))
    (folder / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    assert report["relative_error"] < 0.01


if __name__ == "__main__":
    main()
