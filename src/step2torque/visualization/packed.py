"""Read and sample a portable solution without extracting any sidecar files."""

from pathlib import Path

import numpy as np

from .archive import read
from .native import constitutive, rotate


class PackedField:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.meta, arrays = read(path)
        self.points = arrays["points"].reshape(-1, 3)
        self.cells = arrays["cells"].reshape(-1, 4)
        self.h = arrays["h"].reshape(-1, 4, 3)
        self.cell_material = arrays["material"]
        self.boxes = arrays["bounds"].reshape(-1, 6)
        self.nodes = arrays["nodes"].reshape(-1, 2)
        self.materials = self.meta["materials"]
        self.bounds = np.array(self.meta["box"])
        self.sectors = self.meta["sectors"]
        self.info = dict(sector_start_rad=self.meta["sector_start_rad"])
        self.spec = dict(
            axis=self.meta["axis"],
            center=self.meta["world_center_mm"],
            angle=self.meta["angle_deg"],
        )
        self.rotation = np.array(self.meta["world_to_motor"])
        self.result = self.meta["result"]
        self.surfaces = [
            dict(
                name=s["name"],
                kind=s["kind"],
                pole=s.get("pole"),
                triangles=arrays[s["array"]].reshape(-1, 3),
            )
            for s in self.meta["surfaces"]
        ]
        vertices = self.points[self.cells].astype(float)
        self.origins = vertices[:, 0]
        self.inverse = np.linalg.inv(
            (vertices[:, 1:] - vertices[:, :1]).transpose(0, 2, 1)
        )

    def sample(self, points_mm, *, world=False):
        points = np.asarray(points_mm, float)
        single = points.ndim == 1
        points = np.atleast_2d(points)
        if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
            raise ValueError("Supply finite 3D points in mm")
        points = (
            (points - self.spec["center"]) @ self.rotation.T if world else points.copy()
        )
        turns = np.zeros(len(points))
        if self.sectors > 1:
            period = 2 * np.pi / self.sectors
            turns = (
                np.floor(
                    (
                        np.arctan2(points[:, 1], points[:, 0])
                        - self.info["sector_start_rad"]
                    )
                    / period
                )
                * period
            )
            points = rotate(points, -turns)
        h = np.full((len(points), 3), np.nan)
        domains = np.full(len(points), -1, dtype=np.int32)
        stack = [(0, np.arange(len(points)))]
        while stack:
            node, ids = stack.pop()
            lo, hi = self.boxes[node, :3], self.boxes[node, 3:]
            ids = ids[
                (domains[ids] < 0)
                & np.all(points[ids] >= lo - 1e-6, axis=1)
                & np.all(points[ids] <= hi + 1e-6, axis=1)
            ]
            if not len(ids):
                continue
            a, b = self.nodes[node]
            if a >= 0:
                stack.extend([(a, ids), (b, ids)])
                continue
            cells = np.arange(-a - 1, -a - 1 + b)
            uvw = np.einsum(
                "cij,pcj->pci",
                self.inverse[cells],
                points[ids, None] - self.origins[cells],
            )
            weights = np.concatenate([1 - uvw.sum(axis=2, keepdims=True), uvw], axis=2)
            inside = np.all(weights >= -2e-7, axis=2)
            valid = inside.any(axis=1)
            chosen = inside[valid].argmax(axis=1)
            found = cells[chosen]
            h[ids[valid]] = np.einsum(
                "pi,pij->pj", weights[valid, chosen], self.h[found]
            )
            domains[ids[valid]] = self.cell_material[found]
        b, loading = constitutive(h, domains, self.materials)
        b, h = rotate(b, turns), rotate(h, turns)
        if world:
            b, h = b @ self.rotation, h @ self.rotation
        return {
            k: v[0] if single else v
            for k, v in dict(b=b, h=h, material=domains, loading=loading).items()
        }

    def vertex_fields(self):
        return self.h
