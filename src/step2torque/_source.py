"""The original continuous annular lifting field, in an arbitrary coil frame."""

from functools import cache

import numpy as np
import ngsolve as ng


@cache
def edge_factor():
    """Check the installed NGSolve edge orientation against constant fields."""
    from netgen.occ import Box, Pnt, OCCGeometry

    mesh = ng.Mesh(OCCGeometry(Box(Pnt(0, 0, 0), Pnt(1, 1, 1))).GenerateMesh(maxh=1))
    space = ng.HCurl(mesh, order=0)
    nodes = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2)
    points = np.asarray(mesh.ngmesh.Coordinates())
    expected = points[nodes[:, 1]] - points[nodes[:, 0]]
    field = ng.GridFunction(space)
    actual = []
    for axis in np.eye(3):
        field.Set(ng.CF(tuple(axis)))
        actual.append(field.vec.FV().NumPy().copy())
    actual = np.array(actual).T
    factor = float(np.sum(actual * expected) / np.sum(expected**2))
    if abs(abs(factor) - 1) > 1e-10 or np.max(abs(actual - factor * expected)) > 1e-10:
        raise RuntimeError(
            "Installed NGSolve has unsupported winding edge normalization"
        )
    return factor


def frame(axis):
    n = np.asarray(axis, float)
    n /= np.linalg.norm(n)
    u = np.cross((0, 0, 1) if abs(n[2]) < 0.9 else (1, 0, 0), n)
    u /= np.linalg.norm(u)
    return np.array([u, np.cross(n, u), n])


def annular_source(mesh, coil, ampere_turns=1, order=8):
    """Integrate Hs·dl on native HCurl edges, splitting at all window surfaces.

    Curl(Hs) is uniform azimuthal current inside an ideal circular annulus.
    Geometry and center are metres. Axis follows the current right-hand rule.
    """
    if coil.get("geometry") == "rectangle":
        return rectangular_source(mesh, coil, ampere_turns, order=order)
    ri, ro, height = coil["inner"], coil["outer"], coil["height"]
    basis = frame(coil["axis"])
    space = ng.HCurl(mesh, order=0)
    points = (np.asarray(mesh.ngmesh.Coordinates()) - coil["center"]) @ basis.T
    nodes = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2)
    if space.ndof != len(nodes):
        raise ValueError("Winding source requires one HCurl degree of freedom per edge")
    starts = points[nodes[:, 0]]
    delta = points[nodes[:, 1]] - starts
    indices = np.flatnonzero(delta[:, 2] != 0)
    a, d = starts[indices], delta[indices]
    tz0, tz1 = (-height / 2 - a[:, 2]) / d[:, 2], (height / 2 - a[:, 2]) / d[:, 2]
    lo, hi = np.maximum(0, np.minimum(tz0, tz1)), np.minimum(1, np.maximum(tz0, tz1))
    active = hi > lo
    indices, a, d, lo, hi = (
        indices[active],
        a[active],
        d[active],
        lo[active],
        hi[active],
    )
    aa = np.sum(d[:, :2] ** 2, axis=1)
    bb = 2 * np.sum(a[:, :2] * d[:, :2], axis=1)
    cc = np.sum(a[:, :2] ** 2, axis=1)
    cuts = [lo, hi]
    for radius in (ri, ro):
        discriminant = bb**2 - 4 * aa * (cc - radius**2)
        valid = (aa > 0) & (discriminant >= 0)
        root = np.sqrt(np.maximum(discriminant, 0))
        for side in (-1, 1):
            t = np.divide(-bb + side * root, 2 * aa, out=lo.copy(), where=valid)
            cuts.append(np.clip(t, lo, hi))
    cuts = np.sort(np.column_stack(cuts), axis=1)
    q, w = np.polynomial.legendre.leggauss(order)
    integral = np.zeros(len(indices))
    for first, last in zip(cuts.T[:-1], cuts.T[1:]):
        middle, half = (first + last) / 2, (last - first) / 2
        for point, weight in zip(q, w):
            xy = a[:, :2] + (middle + half * point)[:, None] * d[:, :2]
            radius = np.sqrt(np.sum(xy**2, axis=1))
            integral += half * weight * np.clip((ro - radius) / (ro - ri), 0, 1)
    field = ng.GridFunction(space, name="winding_source")
    field.vec.FV().NumPy()[indices] = (
        edge_factor() * ampere_turns / height * d[:, 2] * integral
    )
    return field


def rectangular_source(mesh, coil, ampere_turns=1, order=8):
    """Closed rectangular turns: native edge integrals of their spanning cuts.

    Gauss points stay inside the copper pack, so Stokes' law confines the
    discrete current to copper even at STEP placements with rounded coordinates.
    """
    basis = frame(coil["axis"])
    points = (np.asarray(mesh.ngmesh.Coordinates()) - coil["center"]) @ basis.T
    space = ng.HCurl(mesh, order=0)
    nodes = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2)
    a, b = points[nodes[:, 0]], points[nodes[:, 1]]
    values = np.zeros(len(nodes))
    q, w = np.polynomial.legendre.leggauss(order)
    for z, wz in zip(q * coil["height"] / 2, w):
        z0, z1 = a[:, 2] - z, b[:, 2] - z
        crossing = np.flatnonzero(((z0 < 0) & (z1 >= 0)) | ((z1 < 0) & (z0 >= 0)))
        t = -z0[crossing] / (z1[crossing] - z0[crossing])
        xy = a[crossing, :2] + t[:, None] * (b[crossing, :2] - a[crossing, :2])
        sign = np.sign(z1[crossing] - z0[crossing])
        for fraction, wf in zip((q + 1) / 2, w):
            x = coil["inner_x"] + fraction * (coil["outer_x"] - coil["inner_x"])
            y = coil["inner_y"] + fraction * (coil["outer_y"] - coil["inner_y"])
            inside = (abs(xy[:, 0]) <= x) & (abs(xy[:, 1]) <= y)
            values[crossing[inside]] += ampere_turns * wz * wf / 4 * sign[inside]
    field = ng.GridFunction(space, name="winding_source")
    field.vec.FV().NumPy()[:] = edge_factor() * values
    return field


def curl_norm(mesh, field, names):
    return float(
        np.sqrt(
            ng.Integrate(
                ng.curl(field) * ng.curl(field), mesh, definedon=mesh.Materials(names)
            )
        )
    )


def cell_edges(mesh):
    """Reuse topology while the worker retains its immutable mesh."""
    if hasattr(mesh, "_current_edges"):
        return mesh._current_edges
    # Netgen node indices are 32-bit. Pair keys exceed that range on fine meshes.
    edges = mesh.ngmesh._getWireframe().NumPy().reshape(-1, 2).astype(np.int64)
    low, high = edges.min(axis=1), edges.max(axis=1)
    keys = low * mesh.nv + high
    order = np.argsort(keys)
    keys = keys[order]
    nodes = mesh.ngmesh.Elements3D().NumPy()["nodes"].astype(np.int64) - 1
    indices, signs = [], []
    for a, b in ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)):
        a, b = nodes[:, a], nodes[:, b]
        index = order[
            np.searchsorted(keys, np.minimum(a, b) * mesh.nv + np.maximum(a, b))
        ]
        indices.append(index)
        signs.append(np.where(edges[index, 0] == a, 1.0, -1.0))
    mesh._current_edges = np.array(indices).T, np.array(signs).T
    return mesh._current_edges


def current_cells(mesh, field):
    """Cells with nonzero edge circulation, excluding floating-point cancellation.

    Curl in a tiny tetrahedron amplifies roundoff by inverse area. Ampere's
    integral law on its faces tests the same source without that amplification.
    """
    indices, signs = cell_edges(mesh)
    values = field.vec.FV().NumPy()
    edge = values[indices] * signs
    maximum = abs(values).max()

    active = np.zeros(mesh.ne, dtype=bool)
    for a, b, c in ((0, 3, 1), (0, 4, 2), (1, 5, 2), (3, 5, 4)):
        x, y, z = edge[:, a], edge[:, b], -edge[:, c]
        bound = 64 * np.finfo(float).eps * (abs(x) + abs(y) + abs(z) + maximum)
        active |= abs(x + y + z) > bound
    return active
