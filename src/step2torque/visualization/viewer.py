"""Read a solved field, sample arbitrary sections and export PNG, HTML or .s2t."""

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .native import NativeField, rotate
from .packed import PackedField
from .lines import streamlines
from .plane import Plane

COLORS = [
    "#13388e",
    "#006cff",
    "#00d4ff",
    "#00d974",
    "#6ee600",
    "#ffff00",
    "#ff9b00",
    "#ff1600",
    "#de00ba",
]


@dataclass
class Section:
    plane: Plane
    x: np.ndarray
    y: np.ndarray
    b: np.ndarray
    material: np.ndarray
    loading: np.ndarray
    outlines: np.ndarray


class Viewer:
    """Read a Solution, saved directory or portable .s2t file. No solve is launched.

    Plane coordinates are relative to the saved motor center and shaft axis.
    sample(..., world=True) accepts original CAD coordinates instead.
    """

    def __init__(self, solution):
        path = getattr(solution, "path", solution)
        if isinstance(solution, (NativeField, PackedField)):
            self.field = solution
        elif Path(path).is_file():
            self.field = PackedField(path)
        else:
            self.field = NativeField(solution)
        if isinstance(self.field, PackedField):
            self.flux_max = self.field.meta["flux_max"]
        else:
            ids = np.arange(
                0, len(self.field.cells), max(1, len(self.field.cells) // 8000)
            )
            points = self.field.points[self.field.cells[ids]].mean(axis=1)
            samples = self.field.sample(points)
            self.flux_max = default_max(
                self.field, np.linalg.norm(samples["b"], axis=1), samples["material"]
            )

    @classmethod
    def from_field(cls, mesh, h, materials, *, sectors=1, sector_start=0):
        """View an existing NGSolve H field, including periodic benchmarks.

        mesh coordinates are SI metres; H is in A/m. The display uses mm.
        """
        return cls(
            NativeField.from_field(
                mesh, h, materials, sectors=sectors, sector_start=sector_start
            )
        )

    def sample(self, points_mm, *, world=False):
        return self.field.sample(points_mm, world=world)

    def section(self, plane=None, *, resolution=600, bounds=None):
        """Sample the full B vector on a plane, including all solved materials.

        bounds=(left, bottom, right, top) uses plane coordinates in mm.
        resolution is the number of samples on the longer edge.
        """
        plane = plane or Plane.axial()
        if not isinstance(resolution, int) or resolution < 16:
            raise ValueError("resolution must be an integer of at least 16")
        bounds = plane.bounds(self.field.bounds) if bounds is None else bounds
        left, bottom, right, top = bounds
        if not np.isfinite(bounds).all() or right <= left or top <= bottom:
            raise ValueError("bounds must be finite (left, bottom, right, top)")
        width, height = right - left, top - bottom
        nx, ny = (
            max(16, round(resolution * width / max(width, height))),
            max(16, round(resolution * height / max(width, height))),
        )
        x, y = np.linspace(left, right, nx), np.linspace(bottom, top, ny)
        xx, yy = np.meshgrid(x, y)
        u, v, n = plane.frame
        points = xx[..., None] * u + yy[..., None] * v + plane.offset * n
        samples = self.field.sample(points.reshape(-1, 3))
        outlines = section_edges(self.field, plane)
        return Section(
            plane,
            x,
            y,
            samples["b"].reshape(ny, nx, 3),
            samples["material"].reshape(ny, nx),
            samples["loading"].reshape(ny, nx),
            outlines,
        )

    def png(
        self,
        path,
        *,
        plane=None,
        metric="flux",
        air=False,
        lines=True,
        vmax=None,
        resolution=800,
        bounds=None,
        dpi=180,
        density=180,
    ):
        """Export a clean section with |B| (T) or steel loading (%).

        Streamlines trace B projected into this plane; colour always uses |B|.
        """
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.collections import LineCollection
        from matplotlib.colors import LinearSegmentedColormap
        from matplotlib.figure import Figure

        if metric not in ("flux", "loading"):
            raise ValueError("metric must be flux or loading")
        if metric == "loading" and not any(
            m["reference_T"] for m in self.field.materials
        ):
            raise ValueError("Steel loading needs a nonlinear steel B-H curve")
        if vmax is not None and (not np.isfinite(vmax) or vmax <= 0):
            raise ValueError("vmax must be positive")
        s = self.section(plane, resolution=resolution, bounds=bounds)
        magnitude = np.linalg.norm(s.b, axis=2)
        values = magnitude if metric == "flux" else s.loading
        kinds = np.array([m["kind"] for m in self.field.materials])
        if not air and metric == "flux":
            values = np.where(
                (s.material >= 0) & (kinds[np.maximum(s.material, 0)] != "air"),
                values,
                np.nan,
            )
        high = (
            vmax
            if vmax is not None
            else (100 if metric == "loading" else self.flux_max)
        )
        width, height = s.x[-1] - s.x[0], s.y[-1] - s.y[0]
        fig = Figure(
            figsize=(7.6, max(3.5, min(11, 6.6 * height / width))),
            dpi=dpi,
            layout="constrained",
        )
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        cmap = LinearSegmentedColormap.from_list("motor_flux", COLORS, N=256)
        image = ax.pcolormesh(
            s.x,
            s.y,
            values,
            cmap=cmap,
            vmin=0,
            vmax=high,
            shading="nearest",
            rasterized=True,
        )
        if lines:
            u, v, _ = s.plane.frame
            bx, by = s.b @ u, s.b @ v
            paths = streamlines(
                s.x, s.y, bx, by, s.material, density=density, reference=self.flux_max
            )
            if paths:
                ax.add_collection(
                    LineCollection(paths, colors="#242933bb", linewidths=0.42)
                )
        if len(s.outlines):
            ax.add_collection(
                LineCollection(s.outlines, colors="#303642", linewidths=0.5)
            )
        ax.set(
            xlim=(s.x[0], s.x[-1]),
            ylim=(s.y[0], s.y[-1]),
            aspect="equal",
            xlabel="mm",
            ylabel="mm",
        )
        ax.tick_params(labelsize=8, length=2, color="#68707c")
        for spine in ax.spines.values():
            spine.set_visible(False)
        above = np.any(values > high * (1 + 1e-10))
        bar = fig.colorbar(
            image,
            ax=ax,
            fraction=0.04,
            pad=0.035,
            shrink=0.85,
            extend="max" if above else "neither",
        )
        bar.set_label(
            "Flux density |B| (T)" if metric == "flux" else "Steel loading (%)", size=9
        )
        bar.ax.tick_params(labelsize=8, length=2)
        bar.outline.set_visible(False)
        fig.savefig(
            path,
            facecolor="white",
            bbox_inches="tight",
            pad_inches=0.08,
            metadata={
                "Description": f"Plane {asdict(s.plane)}; lines = in-plane B. Steel loading = |B-mu0 H| / max_curve(B-mu0 H)."
            },
        )
        return Path(path).resolve()

    def html(
        self,
        path,
        *,
        plane=None,
        metric="flux",
        air=False,
        lines=True,
        vmax=None,
        density=180,
        mode="3d",
        bounds=None,
    ):
        """Export one offline HTML with the 3D field and adjustable section."""
        from .html import write_html

        return write_html(
            self.field,
            path,
            plane or Plane.axial(),
            metric=metric,
            air=air,
            lines=lines,
            vmax=vmax,
            flux_max=self.flux_max,
            density=density,
            mode=mode,
            bounds=bounds,
        )

    def export(
        self,
        path,
        *,
        plane=None,
        metric="flux",
        air=False,
        lines=True,
        vmax=None,
        density=180,
        mode="3d",
        bounds=None,
    ):
        """Export one .s2t file with the complete 3D field and material laws."""
        from .archive import pack

        data = pack(
            self.field,
            plane or Plane.axial(),
            metric=metric,
            air=air,
            lines=lines,
            vmax=vmax,
            flux_max=self.flux_max,
            density=density,
            mode=mode,
            colors=COLORS,
            bounds=bounds,
        )
        path = Path(path).resolve()
        path.write_bytes(data)
        return path


def default_max(field, magnitude, domains):
    solid = np.array([m["kind"] != "air" for m in field.materials])
    values = magnitude[
        (domains >= 0) & solid[np.maximum(domains, 0)] & np.isfinite(magnitude)
    ]
    return (
        max(0.1, float(np.ceil(np.percentile(values, 99.5) * 10) / 10))
        if len(values)
        else 1.0
    )


def section_edges(field, plane):
    """Intersect material surface triangles; half-open crossings avoid doubles."""
    u, v, n = plane.frame
    result = []
    for surface in field.surfaces:
        triangles = field.points[surface["triangles"]]
        for sector in range(field.sectors):
            vertices = rotate(
                triangles.reshape(-1, 3), sector * 2 * np.pi / field.sectors
            ).reshape(-1, 3, 3)
            d = vertices @ n - plane.offset
            mask = (d.min(axis=1) < 0) & (d.max(axis=1) >= 0)
            vertices, d = vertices[mask], d[mask]
            segments = np.empty((len(vertices), 2, 3))
            count = np.zeros(len(vertices), dtype=int)
            for a, b in ((0, 1), (1, 2), (2, 0)):
                cut = (d[:, a] < 0) != (d[:, b] < 0)
                ids = np.flatnonzero(cut)
                t = d[cut, a] / (d[cut, a] - d[cut, b])
                segments[ids, count[ids]] = vertices[cut, a] + t[:, None] * (
                    vertices[cut, b] - vertices[cut, a]
                )
                count[ids] += 1
            if len(segments):
                segments = segments @ np.array([u, v]).T
                result.append(
                    segments[
                        np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1) > 1e-9
                    ]
                )
    return np.concatenate(result) if result else np.empty((0, 2, 2))
