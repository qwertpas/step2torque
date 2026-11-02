"""The saved mesh, P2 potential and HCurl source basis are the field format.

No step2torque imports, solver process, CAD reload, or assembly is needed.
"""

import json
from pathlib import Path
import re

import ngsolve as ng
import numpy as np

MU0 = 4e-7 * np.pi


class NativeField:
    def __init__(self, solution):
        self.path = Path(getattr(solution, "path", solution)).resolve()
        required = ["run.json", "mesh.json", "result.json", "mesh.vol.gz", "best.sol"]
        missing = [n for n in required if not (self.path / n).is_file()]
        if missing:
            raise ValueError(f"Saved solution is missing {', '.join(missing)}")
        self.spec, self.info, self.result = [
            json.loads((self.path / n).read_text()) for n in required[:3]
        ]
        self.mesh = ng.Mesh(str(self.path / "mesh.vol.gz"))
        space = ng.H1(self.mesh, order=2, dirichlet="outer")
        self.sectors = self.info["sectors"]
        if self.sectors > 1:
            space = ng.Periodic(space)
        self.potential = ng.GridFunction(space)
        self.potential.Load(str(self.path / "best.sol"))
        self.h = -ng.grad(self.potential)
        self.sources = []
        saved = self.result["currents_A"]
        currents = (
            {p: saved[str(p)] for p in self.spec["phases"]}
            if isinstance(saved, dict)
            else dict(zip(self.spec["phases"], saved))
        )
        for i, coil in enumerate(self.info["coils"]):
            unit = ng.GridFunction(ng.HCurl(self.mesh, order=0))
            unit.Load(str(self.path / f"source{i}.sol"))
            self.sources.append(unit)
            self.h += coil["turns"] * currents[coil["phase"]] * unit
        self._prepare()

    @classmethod
    def from_field(cls, mesh, h, materials, *, sectors=1, sector_start=0):
        """Read an already loaded H coefficient on an SI tetrahedral mesh.

        Used by the periodic reference benchmarks, whose fields are saved
        separately from the public Solution format. No solve is performed.
        Material entries may add name/kind for geometry and winding outlines.
        """
        field = cls.__new__(cls)
        field.mesh, field.h = mesh, h
        field.sectors = sectors
        field.spec = dict(axis=[0, 0, 1], center=[0, 0, 0], angle=0, components=[])
        field.info = dict(
            sectors=sectors, sector_start_rad=sector_start, materials=materials
        )
        field._prepare(cell_surfaces=True)
        return field

    def _prepare(self, cell_surfaces=False):
        self.materials = []
        components = {c["index"]: c for c in self.spec["components"]}
        for name in self.mesh.GetMaterials():
            law = dict(self.info["materials"].get(name, {"mur": 1.0}))
            match = re.match(r"body_(\d+)(?:_|$)", name)
            component = components.get(int(match[1]), {}) if match else {}
            kind = component.get(
                "kind",
                law.get(
                    "kind", "iron" if "h" in law else "magnet" if "br" in law else "air"
                ),
            )
            reference = (
                float(np.max(np.array(law["b"]) - MU0 * np.array(law["h"])))
                if "h" in law
                else None
            )
            self.materials.append(
                dict(
                    law,
                    domain=name,
                    name=component.get("name", law.get("name", name)),
                    kind=kind,
                    reference_T=reference,
                )
            )
        self.points = np.array([tuple(p.p) for p in self.mesh.ngmesh.Points()]) * 1000
        elements = self.mesh.ngmesh.Elements3D().NumPy()
        if np.any(elements["curved"]) or elements["nodes"].shape[1] != 4:
            raise ValueError("Field export requires an affine tetrahedral mesh")
        self.cells = np.array(elements["nodes"] - 1, dtype=np.uint32)
        self.cell_material = np.array(elements["index"] - 1, dtype=np.uint16)
        axis = np.asarray(self.spec["axis"], float)
        cross, cosine = np.cross(axis, (0, 0, 1)), axis[2]
        if np.linalg.norm(cross) < 1e-12:
            self.rotation = np.eye(3) if cosine > 0 else np.diag([1.0, -1.0, -1.0])
        else:
            x, y, z = cross
            skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
            self.rotation = np.eye(3) + skew + skew @ skew / (1 + cosine)
        self.surfaces = self._cell_surfaces() if cell_surfaces else self._surfaces()
        vertices = (
            np.concatenate([s["triangles"].ravel() for s in self.surfaces])
            if self.surfaces
            else self.cells.ravel()
        )
        solid = self.points[np.unique(vertices)]
        if self.sectors > 1:
            solid = np.concatenate(
                [
                    rotate(solid, j * 2 * np.pi / self.sectors)
                    for j in range(self.sectors)
                ]
            )
        self.bounds = np.array([solid.min(axis=0), solid.max(axis=0)])

    def _cell_surfaces(self):
        """Reference meshes may omit internal 2D faces. Recover them from tets."""
        groups, lookup, domains = [], {}, []
        for m in self.materials:
            key = (m["name"], m["kind"])
            if m["kind"] == "air":
                domains.append(-1)
                continue
            if key not in lookup:
                lookup[key] = len(groups)
                groups.append(dict(name=key[0], kind=key[1]))
            domains.append(lookup[key])
        owners = np.array(domains)[self.cell_material]
        solid = owners >= 0
        cells, owners = self.cells[solid], owners[solid]
        if not len(cells):
            return []
        faces = np.concatenate(
            [cells[:, ids] for ids in ([0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3])]
        )
        owners = np.tile(owners, 4)
        keys = np.sort(faces, axis=1)
        order = np.lexsort(keys.T[::-1])
        keys, faces, owners = keys[order], faces[order], owners[order]
        same = np.all(keys[1:] == keys[:-1], axis=1) & (owners[1:] == owners[:-1])
        hidden = np.r_[same, False] | np.r_[False, same]
        if self.sectors > 1:
            vertices = self.points[faces]
            start = self.info["sector_start_rad"]
            for angle in (start, start + 2 * np.pi / self.sectors):
                normal = np.array([-np.sin(angle), np.cos(angle), 0])
                # Rotational cut caps become internal after repeating the sector.
                hidden |= np.all(np.abs(vertices @ normal) < 1e-7, axis=1)
        return [
            dict(group, triangles=faces[(owners == i) & ~hidden])
            for i, group in enumerate(groups)
        ]

    def _surfaces(self):
        groups, group_index = [], {}
        for m in self.materials:
            key = (m["name"], m["kind"])
            if m["kind"] == "air":
                continue
            if key not in group_index:
                group_index[key] = len(groups)
                groups.append(dict(name=m["name"], kind=m["kind"], triangles=[]))
        domain_group = [
            group_index.get((m["name"], m["kind"]), -1) for m in self.materials
        ]
        faces = self.mesh.ngmesh.Elements2D().NumPy()
        for index in np.unique(faces["index"]):
            descriptor = self.mesh.ngmesh.FaceDescriptor(int(index))
            if descriptor.bcname in ("side0", "side1"):
                continue
            a = domain_group[descriptor.domin - 1] if descriptor.domin else -1
            b = domain_group[descriptor.domout - 1] if descriptor.domout else -1
            if a == b:
                continue
            triangles = np.array(
                faces["nodes"][faces["index"] == index] - 1, dtype=np.uint32
            )
            if a >= 0:
                groups[a]["triangles"].append(triangles)
            if b >= 0:
                groups[b]["triangles"].append(triangles[:, ::-1])
        return [
            dict(g, triangles=np.concatenate(g["triangles"]))
            for g in groups
            if g["triangles"]
        ]

    def sample(self, points_mm, *, world=False):
        """B, H, domain IDs, steel loading; outside the mesh is NaN / -1.

        Inputs and returned vectors use motor coordinates unless world=True.
        """
        points = np.asarray(points_mm, float)
        single = points.ndim == 1
        points = np.atleast_2d(points)
        if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
            raise ValueError("Supply finite 3D points in mm")
        local = (
            (points - self.spec["center"]) @ self.rotation.T if world else points.copy()
        )
        turns = np.zeros(len(local))
        if self.sectors > 1:
            period = 2 * np.pi / self.sectors
            turns = (
                np.floor(
                    (
                        np.arctan2(local[:, 1], local[:, 0])
                        - self.info["sector_start_rad"]
                    )
                    / period
                )
                * period
            )
            local = rotate(local, -turns)
        mapped = self.mesh(*(local * 0.001).T)
        valid = mapped["nr"].ravel() >= 0
        h = np.full((len(local), 3), np.nan)
        domains = np.full(len(local), -1, dtype=np.int32)
        if valid.any():
            inside = mapped[valid]
            h[valid] = np.asarray(self.h(inside)).reshape(-1, 3)
            domains[valid] = self.cell_material[mapped["nr"].ravel()[valid]]
        b, loading = constitutive(h, domains, self.materials)
        b, h = rotate(b, turns), rotate(h, turns)
        if world:
            b, h = b @ self.rotation, h @ self.rotation
        return {
            k: v[0] if single else v
            for k, v in dict(b=b, h=h, material=domains, loading=loading).items()
        }

    def vertex_fields(self, batch=20000):
        """H is affine within each tetrahedron. Four interior samples recover it.

        Sampling inside each cell preserves both sides of every material jump.
        No averaging across iron, air or permanent magnet interfaces occurs.
        """
        output = np.empty((len(self.cells), 4, 3), dtype=np.float32)
        for start in range(0, len(self.cells), batch):
            vertices = self.points[self.cells[start : start + batch]]
            interior = 0.4 * vertices + 0.15 * vertices.sum(axis=1, keepdims=True)
            mapped = self.mesh(*(interior.reshape(-1, 3) * 0.001).T)
            if np.any(mapped["nr"] < 0):
                raise ValueError("Cannot locate an interior tetrahedron sample")
            values = np.asarray(self.h(mapped)).reshape(-1, 4, 3)
            output[start : start + batch] = (
                values - 0.15 * values.sum(axis=1, keepdims=True)
            ) / 0.4
        return output


def rotate(vectors, angle):
    result = np.array(vectors, copy=True)
    c, s = np.cos(angle), np.sin(angle)
    x, y = vectors[:, 0], vectors[:, 1]
    result[:, 0], result[:, 1] = c * x - s * y, s * x + c * y
    return result


def constitutive(h, domains, materials):
    b = MU0 * h
    loading = np.full(len(h), np.nan)
    for j in np.unique(domains):
        if j < 0:
            continue
        m = materials[j]
        mask = domains == j
        if "h" in m:
            magnitude = np.sqrt(np.sum(h[mask] ** 2, axis=1) + 1e-8)
            value = np.interp(magnitude, m["h"], m["b"])
            value += MU0 * np.maximum(magnitude - m["h"][-1], 0)
            b[mask] = h[mask] * (value / magnitude)[:, None]
            loading[mask] = (
                np.linalg.norm(b[mask] - MU0 * h[mask], axis=1) / m["reference_T"] * 100
            )
        else:
            b[mask] = MU0 * m.get("mur", 1) * h[mask] + m.get("br", [0, 0, 0])
    return b, loading
