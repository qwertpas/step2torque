"""Zero-thickness air-gap contact: B_n = (mu0 / gap) * [phi]."""

import ngsolve as ng
import numpy as np
import pyngcore as core
import netgen.meshing as nm
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .materials import MU0


def add_contact(mesh, space, matrix, contact):
    """Add the exact P2 surface mass matrix on paired, coincident traces.

    The mesh is continuous away from the contact. Its physical contact rim
    shares vertex/edge DOFs, so the jump vanishes at the rim. Rotational
    sector cuts retain independent, periodically identified contact traces.
    This is a physical Robin term, with no penalty or stabilization factor.
    """
    gap = float(contact["gap"])
    if gap <= 0:
        raise ValueError("Contact gap must be positive")
    points = np.asarray(mesh.ngmesh.Coordinates())
    cells = mesh.ngmesh.Elements3D().NumPy()["nodes"] - 1
    rule = ng.IntegrationRule(ng.ET.TRIG, 4)
    q = np.array([p.point[:2] for p in rule])
    weights = np.array([p.weight for p in rule])
    rows, columns, entries = [], [], []
    for face in contact["faces"]:
        triangle = points[face["vertices"]]
        locations = (
            triangle[0]
            + q[:, :1] * (triangle[1] - triangle[0])
            + q[:, 1:] * (triangle[2] - triangle[0])
        )
        jacobian = np.linalg.norm(
            np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        )
        traces, dofs = [], []
        for side, sign in (("minus_cell", 1), ("plus_cell", -1)):
            cell = int(face[side])
            element = ng.ElementId(ng.VOL, cell)
            xyz = points[cells[cell]]
            reference = np.linalg.solve((xyz[:3] - xyz[3]).T, (locations - xyz[3]).T).T
            fe = space.GetFE(element)
            shape = np.array([fe.CalcShape(*p) for p in reference])
            shape[abs(shape) < 1e-12] = 0
            traces.append(sign * shape)
            dofs.extend(space.GetDofNrs(element))
        unique, inverse = np.unique(dofs, return_inverse=True)
        jump = np.zeros((len(rule), len(unique)))
        for j, values in enumerate(np.concatenate(traces, axis=1).T):
            jump[:, inverse[j]] += values
        local = MU0 / gap * jacobian * (jump.T * weights) @ jump
        i, j = np.nonzero(local)
        rows.extend(unique[i])
        columns.extend(unique[j])
        entries.extend(local[i, j])
    values, cols, ptr = (np.asarray(x) for x in matrix.CSR())
    volume_rows = np.repeat(np.arange(matrix.height), np.diff(ptr).astype(np.int64))
    # Fill native arrays directly; Python conversion of the full volume graph
    # would cost more than assembling the contact itself.
    count = len(values)
    i, j = core.Array_I_S(count + len(rows)), core.Array_I_S(count + len(rows))
    data = core.Array_D_S(count + len(rows))
    i.NumPy()[:count], i.NumPy()[count:] = volume_rows, rows
    j.NumPy()[:count], j.NumPy()[count:] = cols, columns
    data.NumPy()[:count], data.NumPy()[count:] = values, entries
    return ng.la.SparseMatrixd.CreateFromCOO(i, j, data, matrix.height, matrix.width)


def split(mesh, gap, materials, moving=()):
    """Separate potential traces on actual, conforming iron mating faces."""
    if not gap:
        return mesh, None
    names = mesh.GetMaterials()
    iron = {
        name
        for name, law in materials.items()
        if "h" in law or ("br" not in law and law.get("mur", 1) > 1)
    }
    descriptors = [nm.FaceDescriptor(fd) for fd in mesh.ngmesh.FaceDescriptors()]
    selected = {}
    for i, fd in enumerate(descriptors, 1):
        if not fd.domin or not fd.domout:
            continue
        a, b = names[fd.domin - 1], names[fd.domout - 1]
        if a != b and a in iron and b in iron and (a in moving) == (b in moving):
            selected[i] = tuple(sorted((a, b)))
    if not selected:
        return mesh, None
    tets = mesh.ngmesh.Elements3D().NumPy()["nodes"] - 1
    boundary = mesh.ngmesh.Elements2D().NumPy().copy()
    triangles, indices = boundary["nodes"] - 1, boundary["index"]
    contact = np.isin(indices, list(selected))
    local = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    faces = tets[:, local].reshape(-1, 3)
    corners = np.argsort(faces, axis=1)
    occurrences = (4 * np.arange(mesh.ne)[:, None, None] + local).reshape(-1, 3)
    occurrences = np.take_along_axis(occurrences, corners, axis=1)
    keys = face_keys(faces)
    order = np.argsort(keys)
    keys, occurrences = keys[order], occurrences[order]
    positions = np.searchsorted(keys, face_keys(triangles))
    if not np.all(keys[positions] == face_keys(triangles)):
        raise ValueError("Contact mesh has an unmatched surface face")
    same = keys[1:] == keys[:-1]
    if np.any(same[:-1] & same[1:]):
        raise ValueError("Contact mesh has a nonmanifold face")
    paired = np.flatnonzero(same)
    cut = positions[contact]
    if not np.all(same[cut]):
        raise ValueError("Contact must have iron on both sides")
    joined = paired[~np.isin(paired, cut)]
    a, b = occurrences[joined].ravel(), occurrences[joined + 1].ravel()
    graph = coo_matrix(
        (np.ones(len(a), dtype=np.int8), (a, b)), shape=(4 * mesh.ne, 4 * mesh.ne)
    ).tocsr()
    _, labels = connected_components(graph, directed=False)
    _, first = np.unique(labels, return_index=True)
    nodes = tets.ravel()[first]
    _, original = np.unique(nodes, return_index=True)
    copies = np.isin(nodes, triangles[contact])
    copies[original] = False
    points = np.asarray(mesh.ngmesh.Coordinates())
    new_points = np.concatenate([points, points[nodes[copies]]])
    numbers = nodes.copy()
    numbers[copies] = mesh.nv + np.arange(copies.sum())
    new_tets = numbers[labels].reshape(-1, 4)

    owners = order[positions] // 4

    def trace(cell, vertices):
        return new_tets[cell, np.argmax(tets[cell, :, None] == vertices, axis=0)]

    new_triangles = np.array([trace(ci, tri) for ci, tri in zip(owners, triangles)])
    # Interior contact edges need independent P2 edge DOFs even when both
    # endpoints lie on the physical rim. One conforming bisection supplies them.
    edges, tied = {}, set()
    for si in np.flatnonzero(contact):
        pos = positions[si]
        lo, hi = order[pos] // 4, order[pos + 1] // 4
        tri = triangles[si]
        equal = trace(lo, tri) == trace(hi, tri)
        for i, j in ((0, 1), (0, 2), (1, 2)):
            edge = tuple(sorted((int(tri[i]), int(tri[j]))))
            key = selected[int(indices[si])], edge
            edges[key] = edges.get(key, 0) + 1
            if equal[i] and equal[j]:
                tied.add(key)
    refine = sorted({edge for pair, edge in tied if edges[pair, edge] == 2})
    if refine:
        return split(bisect(mesh, refine), gap, materials, moving)

    interfaces, pairs, records = [], {}, []
    extra_triangles, extra_indices = [], []
    domains = mesh.ngmesh.Elements3D().NumPy()["index"]
    for index, pair in selected.items():
        if pair not in pairs:
            pairs[pair] = len(interfaces)
            interfaces.append(dict(parts=list(pair), area_m2=0.0))
        fd = descriptors[index - 1]
        other = nm.FaceDescriptor(fd)
        other.domin, other.domout = fd.domout, 0
        fd.domout = 0
        k = pairs[pair]
        fd.bcname = f"contact_{k}_minus"
        other.bc = len(descriptors) + 1
        other.bcname = f"contact_{k}_plus"
        descriptors.append(other)
        for si in np.flatnonzero(indices == index):
            pos = positions[si]
            lo, hi = int(order[pos] // 4), int(order[pos + 1] // 4)
            if domains[lo] != fd.domin:
                lo, hi = hi, lo
            tri = triangles[si]
            lower, upper = trace(lo, tri), trace(hi, tri)
            new_triangles[si] = lower
            extra_triangles.append(upper)
            extra_indices.append(len(descriptors))
            xyz = points[tri]
            interfaces[k]["area_m2"] += float(
                np.linalg.norm(np.cross(xyz[1] - xyz[0], xyz[2] - xyz[0])) / 2
            )
            records.append(
                dict(
                    interface=k,
                    minus_cell=lo,
                    plus_cell=hi,
                    vertices=lower.tolist(),
                    plus_vertices=upper.tolist(),
                )
            )
    # Match each periodic boundary triangle's two traces independently. This
    # works for rotational or translated sectors without assuming a face normal.
    surface_lookup = {tuple(sorted(tri)): i for i, tri in enumerate(triangles)}
    identifications = set()
    groups = {}
    for a, b, ident in mesh.ngmesh.GetIdentifications():
        groups.setdefault(ident, {})[a.nr - 1] = b.nr - 1
    for ident, mapping in groups.items():
        for si, tri in enumerate(triangles):
            target = [mapping.get(int(i), -1) for i in tri]
            other = surface_lookup.get(tuple(sorted(target)))
            if other is None:
                continue
            for j, node in enumerate(target):
                k = list(triangles[other]).index(node)
                identifications.add(
                    (int(new_triangles[si, j]), int(new_triangles[other, k]), ident)
                )
    native = rebuild(
        mesh,
        new_points,
        new_tets,
        domains,
        np.concatenate([new_triangles, extra_triangles]),
        np.r_[indices, extra_indices],
        descriptors,
        identifications,
    )
    return native, dict(gap=gap, faces=records, interfaces=interfaces)


def face_keys(vertices):
    dtype = np.dtype([(name, np.int32) for name in ("a", "b", "c")])
    return (
        np.ascontiguousarray(np.sort(vertices, axis=1), dtype=np.int32)
        .view(dtype)
        .ravel()
    )


def rebuild(
    mesh, points, tets, domains, triangles, indices, descriptors, identifications
):
    native = nm.Mesh(dim=3)
    for fd in descriptors:
        native.Add(fd)
        native.SetBCName(fd.bc - 1, fd.bcname)
    for p in points:
        native.Add(nm.MeshPoint(nm.Pnt(*p)))
    for tet, domain in zip(tets, domains):
        native.Add(nm.Element3D(index=int(domain), vertices=(tet + 1).tolist()))
    for tri, index in zip(triangles, indices):
        native.Add(nm.Element2D(index=int(index), vertices=(tri + 1).tolist()))
    for i, name in enumerate(mesh.GetMaterials(), 1):
        native.SetMaterial(i, name)
    for a, b, ident in sorted(identifications):
        native.AddPointIdentification(
            a + 1, b + 1, ident, mesh.ngmesh.GetIdentificationType(ident)
        )
    return ng.Mesh(native)


def bisect(mesh, edges):
    points = np.asarray(mesh.ngmesh.Coordinates()).tolist()
    cells = mesh.ngmesh.Elements3D().NumPy()
    tets, domains = cells["nodes"] - 1, cells["index"].copy()
    surfaces = mesh.ngmesh.Elements2D().NumPy()
    triangles, indices = surfaces["nodes"] - 1, surfaces["index"].copy()
    for a, b in edges:
        c = len(points)
        points.append(((np.array(points[a]) + points[b]) / 2).tolist())
        for volume in (True, False):
            nodes, labels = (tets, domains) if volume else (triangles, indices)
            hit = np.any(nodes == a, axis=1) & np.any(nodes == b, axis=1)
            first, second = nodes[hit].copy(), nodes[hit].copy()
            first[first == a], second[second == b] = c, c
            nodes = np.concatenate([nodes[~hit], first, second])
            labels = np.r_[labels[~hit], labels[hit], labels[hit]]
            if volume:
                tets, domains = nodes, labels
            else:
                triangles, indices = nodes, labels
    identifications = {
        (a.nr - 1, b.nr - 1, ident) for a, b, ident in mesh.ngmesh.GetIdentifications()
    }
    return rebuild(
        mesh,
        points,
        tets,
        domains,
        triangles,
        indices,
        [nm.FaceDescriptor(fd) for fd in mesh.ngmesh.FaceDescriptors()],
        identifications,
    )
