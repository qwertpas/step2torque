"""P1 air motion: exact rigid rotation in moving solids, zero in fixed solids."""

import numpy as np
import ngsolve as ng
from ._source import current_cells


def velocity(mesh, moving, units, axis=(0, 0, 1), center=(0, 0, 0), periodic=False):
    points = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
    spin = np.cross(axis, points - center)
    cells = mesh.ngmesh.Elements3D().NumPy()
    nodes, domains = cells["nodes"] - 1, cells["index"] - 1
    names = mesh.GetMaterials()
    rotating = np.array([name in moving for name in names])
    fixed = np.array([name != "air" and name not in moving for name in names])
    fixed_nodes = np.unique(nodes[fixed[domains]])
    moving_nodes = np.unique(nodes[rotating[domains]])
    if np.intersect1d(fixed_nodes, moving_nodes).size:
        raise ValueError(
            "Rotating and stationary solids touch; the motor needs an air gap"
        )
    # The original source has a known affine-mesh mismatch in coil-adjacent air.
    # Keep that fixed-source support outside virtual motion, as in V3.1.
    source_cells = np.zeros(mesh.ne, dtype=bool)
    for unit in units:
        source_cells |= current_cells(mesh, unit)
    fixed_nodes = np.union1d(fixed_nodes, np.unique(nodes[source_cells]))
    if np.intersect1d(fixed_nodes, moving_nodes).size:
        raise ValueError(
            "Coil current intersects moving material; check coil direction and CAD winding window"
        )
    space = ng.H1(mesh, order=1, dirichlet="outer")
    if periodic:
        space = ng.Periodic(space)
    weight = ng.GridFunction(space)
    free = space.FreeDofs()
    # Periodic meshes identify vertex DOFs; coordinates are not a DOF index.
    vertex_dofs = np.array(
        [space.GetDofNrs(ng.NodeId(ng.VERTEX, i))[0] for i in range(mesh.nv)]
    )
    for node in fixed_nodes:
        free[int(vertex_dofs[node])] = False
    for node in moving_nodes:
        free[int(vertex_dofs[node])] = False
        weight.vec[int(vertex_dofs[node])] = 1
    u, v = space.TnT()
    a = ng.BilinearForm(space, symmetric=True)
    a += ng.grad(u) * ng.grad(v) * ng.dx
    with ng.TaskManager():
        a.Assemble()
        weight.vec.data -= a.mat.Inverse(free, inverse="sparsecholesky") * (
            a.mat * weight.vec
        )
    values = weight.vec.FV().NumPy()[vertex_dofs, None] * spin
    values[fixed_nodes] = 0
    values[moving_nodes] = spin[moving_nodes]
    result = ng.GridFunction(ng.VectorH1(mesh, order=1))
    for j, component in enumerate(result.components):
        dofs = [
            component.space.GetDofNrs(ng.NodeId(ng.VERTEX, i))[0]
            for i in range(mesh.nv)
        ]
        component.vec.FV().NumPy()[dofs] = values[:, j]
    return result, spin


def radial_velocity(mesh, tip, magnet_radius):
    """Unchanged V3.1 radial law for reproduction of historical motor states."""
    points = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
    radius = np.hypot(points[:, 0], points[:, 1])
    weight = np.clip((radius - tip) / (magnet_radius - tip), 0, 1)
    weight = np.where(radius > 0.025, np.clip((0.045 - radius) / 0.02, 0, 1), weight)
    result = ng.GridFunction(ng.VectorH1(mesh, order=1))
    result.components[0].vec.FV().NumPy()[:] = -weight * points[:, 1]
    result.components[1].vec.FV().NumPy()[:] = weight * points[:, 0]
    return result
