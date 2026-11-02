"""Runtime mesh and material data for the compiled affine P2 assembly."""

from types import SimpleNamespace

import ngsolve as ng
import numpy as np

from ._assembly import Assembly
from .materials import MU0


class Kernel:
    def __init__(
        self,
        mesh,
        space,
        source,
        materials,
        potential,
        factor,
        floor,
        parameters,
        matrix,
    ):
        self.source, self.phi = source, potential
        self.factor, self.floor, self.parameters = factor, floor, parameters
        self.previous = None
        self.mat = matrix.CreateMatrix()
        self.values = np.asarray(self.mat.CSR()[0])
        vector = potential.vec.CreateVector()
        self.residual = SimpleNamespace(vec=vector, Assemble=lambda: None)
        self.residual_values = vector.FV().NumPy()
        curves, domains, keys = [], [], {}
        for name in mesh.GetMaterials():
            data = materials.get(name, {})
            if "h" not in data:
                domains.append(-1)
                continue
            key = tuple(data["h"]), tuple(data["b"])
            if key not in keys:
                keys[key] = len(curves)
                h = np.r_[data["h"], 1e8]
                b = np.r_[data["b"], data["b"][-1] + MU0 * (h[-1] - h[-2])]
                curves.append((h, b))
            domains.append(keys[key])
        cells = mesh.ngmesh.Elements3D().NumPy()
        labels = np.asarray(domains, dtype=np.int32)[cells["index"] - 1]
        elements = np.flatnonzero(labels >= 0)
        coordinates = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
        centers = coordinates[cells["nodes"][elements] - 1].mean(axis=1)
        if mesh.ngmesh.GetCurveOrder() != 1 or mesh.deformation is not None:
            raise ValueError("Compiled assembly requires undeformed affine tetrahedra")
        mapped = (
            np.repeat(mesh(*centers[:1].T), len(elements)) if len(elements) else None
        )
        if mapped is not None:
            mapped["nr"] = elements
            for name in ("x", "y", "z"):
                mapped[name] = 0.25
            jacobian = np.asarray(ng.specialcf.JacobianMatrix(3)(mapped)).reshape(
                -1, 3, 3
            )
            inverse, volume = np.linalg.inv(jacobian), abs(np.linalg.det(jacobian))
        else:
            inverse, volume = np.empty((0, 3, 3)), np.empty(0)
        rule = ng.IntegrationRule(ng.ET.TET, 6)
        points = np.array([p.point for p in rule])
        fe = space.GetFE(ng.ElementId(ng.VOL, 0))
        reference = np.array([fe.CalcDShape(*p) for p in points])
        dofs = np.array(
            [space.GetDofNrs(ng.ElementId(ng.VOL, int(e))) for e in elements],
            dtype=np.int32,
        ).reshape(-1, 10)
        if reference.shape != (len(rule), 10, 3) or np.any(dofs < 0):
            raise ValueError("Compiled assembly requires the native P2 tetrahedron map")
        _, columns, pointers = matrix.CSR()
        self.native = Assembly(
            np.asarray(pointers, dtype=np.int64),
            np.asarray(columns, dtype=np.int32),
            dofs,
            labels[elements],
            inverse,
            volume,
            reference,
            np.array([p.weight for p in rule]),
            points,
            curves,
            ng.GetNumThreads(),
        )
        self.vertices = np.repeat(mapped, 4) if mapped is not None else None
        if self.vertices is not None:
            vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
            for j, name in enumerate(("x", "y", "z")):
                self.vertices[name] = np.tile(vertices[:, j], len(elements))

    def sync(self):
        current = tuple(p.Get() for p in self.parameters)
        if current != self.previous:
            if self.vertices is not None:
                self.native.set_source(
                    np.ascontiguousarray(self.source(self.vertices), dtype=np.float64)
                )
            self.previous = current

    def Assemble(self):
        self.sync()
        self.native.assemble(
            self.phi.vec.FV().NumPy(),
            self.factor.Get(),
            self.floor.Get(),
            self.values,
            self.residual_values,
        )

    def energy(self):
        self.sync()
        return self.native.energy(self.phi.vec.FV().NumPy(), self.factor.Get())
