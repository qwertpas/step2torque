"""Configured CAD and winding paths, without a field solve or volume mesh."""

import math
from pathlib import Path

import numpy as np
from netgen.meshing import MeshingStep
from netgen.occ import OCCGeometry

from .._mesh import align, coil_geometry, rotation
from .native import coil_paths, rotate


class MotorGeometry:
    solved = False

    def __init__(self, motor, *, angle=0, current=None):
        if not math.isfinite(angle):
            raise ValueError("angle must be finite mechanical degrees")
        self.spec = dict(motor._spec(), angle=(angle + 180) % 360 - 180)
        self.rotation = rotation(motor.axis)
        self.sectors, self.info = 1, dict(sector_start_rad=0)
        self.meta = dict(current_flow=True)
        self.result = {}
        self.materials, self.surfaces, points, coils = [], [], [], []
        boxes = np.array(
            [
                [tuple(c.shape.bounding_box[0]), tuple(c.shape.bounding_box[1])]
                for c in motor.components.values()
            ]
        )
        display_size = np.max(boxes[:, 1].max(axis=0) - boxes[:, 0].min(axis=0))
        phases = list(
            dict.fromkeys(
                c["phase"] for c in self.spec["components"] if c["kind"] == "coil"
            )
        )
        if current is None:
            currents = dict.fromkeys(phases, 1.0)
        elif isinstance(current, (int, float)):
            currents = dict.fromkeys(phases, float(current))
        elif isinstance(current, dict) and set(current) == set(phases):
            currents = dict(current)
        else:
            raise ValueError(f"Supply preview current for every phase: {phases}")
        if not all(
            isinstance(i, (int, float)) and math.isfinite(i) for i in currents.values()
        ):
            raise ValueError("Preview currents must be finite amps")
        cursor = 0
        for row in self.spec["components"]:
            shape = align(motor.components[row["name"]].shape, motor.axis, motor.center)
            # Only tessellate CAD faces. No volume elements or field are built.
            mesh = OCCGeometry(shape).GenerateMesh(
                maxh=display_size / 80,
                curvaturesafety=16,
                perfstepsend=MeshingStep.MESHSURFACE,
            )
            vertices = np.array([tuple(p.p) for p in mesh.Points()])
            ids = np.array(
                mesh.Elements2D().NumPy()["nodes"] - 1 + cursor, dtype=np.uint32
            )
            points.append(vertices)
            cursor += len(vertices)
            pole = None
            if row["kind"] == "coil":
                coil = dict(
                    coil_geometry(shape, self.rotation @ row["direction"], row["name"]),
                    **row,
                )
                if coil["turns"] is None:
                    rho = 1.724e-8 * (1 + 0.00393 * (coil["temperature"] - 20))
                    factor = (
                        rho * coil["turn_length"] ** 2 / (coil["volume"] * coil["fill"])
                    )
                    coil["turns"] = max(
                        1, round(math.sqrt(coil["resistance"] / factor))
                    )
                coils.append(coil)
            elif row["kind"] == "magnet":
                direction = row["direction"]
                if isinstance(direction, str):
                    radius = np.linalg.norm(vertices[:, :2], axis=1)
                    pole = dict(
                        rule="radial",
                        radius=float((radius.min() + radius.max()) / 2),
                        sign=-1 if direction == "-radial" else 1,
                        count=row.get("poles") or 0,
                        phase=math.radians(row.get("offset", 0)),
                    )
                else:
                    pole = dict(
                        rule="plane",
                        center=list(shape.center),
                        vector=(self.rotation @ direction).tolist(),
                    )
                pole.update(period=0, start=0)
            self.surfaces.append(
                dict(
                    name=row["name"],
                    kind=row["kind"],
                    rotating=row["rotating"],
                    pole=pole,
                    triangles=ids,
                )
            )
            self.materials.append(
                dict(name=row["name"], kind=row["kind"], reference_T=None)
            )
        self.points = np.concatenate(points)
        # Fit all rotor angles, including noncircular and off-center parts.
        radius = np.linalg.norm(self.points[:, :2], axis=1).max()
        self.bounds = np.array(
            [
                [-radius, -radius, self.points[:, 2].min()],
                [radius, radius, self.points[:, 2].max()],
            ]
        )
        self.coils = coil_paths(coils, currents)


def write_image(field, path, *, dpi=180):
    """A 3D setup image with input currents, phase labels and magnet poles."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import to_rgba_array
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    fig = Figure(figsize=(9, 7), dpi=dpi)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1], projection="3d", computed_zorder=False)
    ax.view_init(elev=36, azim=-59)
    low, high = field.bounds
    ax.set(xlim=(low[0], high[0]), ylim=(low[1], high[1]), zlim=(low[2], high[2]))
    ax.set_box_aspect(high - low)
    angle = math.radians(field.spec["angle"])
    faces, facecolors = [], []
    for surface in field.surfaces:
        triangles = field.points[surface["triangles"]]
        triangles = pole_triangles(surface, triangles)
        colors = surface_colors(surface, triangles.mean(axis=1))
        if surface.get("rotating"):
            triangles = rotate(triangles.reshape(-1, 3), angle).reshape(-1, 3, 3)
        rgba = np.broadcast_to(to_rgba_array(colors), (len(triangles), 4))
        # Remove hidden back faces before Matplotlib's depth sorting; otherwise
        # large rear triangles can paint over the visible surface of a ring.
        clip = (
            np.concatenate([triangles, np.ones((*triangles.shape[:2], 1))], axis=2)
            @ ax.get_proj().T
        )
        projected = clip[:, :, :2] / clip[:, :, 3:]
        a, b = projected[:, 1] - projected[:, 0], projected[:, 2] - projected[:, 0]
        front = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0] > 0
        faces.append(triangles[front])
        facecolors.append(rgba[front])
    ax.add_collection3d(
        Poly3DCollection(
            np.concatenate(faces),
            facecolors=np.concatenate(facecolors),
            linewidth=0,
            antialiased=False,
            shade=True,
            zorder=1,
        )
    )
    palette = ["#852cc0", "#008b83", "#df6500", "#2675c7", "#b12d67", "#647b13"]
    phases = list(dict.fromkeys(c["phase"] for c in field.coils))
    maximum = max((abs(c["current_A"]) for c in field.coils), default=0)
    size = np.linalg.norm(field.bounds[1] - field.bounds[0])
    handles = []
    for coil in field.coils:
        points = np.array(coil["points"])
        color = palette[phases.index(coil["phase"]) % len(palette)]
        center = points.mean(axis=0)
        ax.text(
            *center,
            str(coil["phase"]),
            color=color,
            ha="center",
            va="center",
            fontsize=9,
            bbox=dict(facecolor="white", edgecolor=color, boxstyle="round,pad=.25"),
            zorder=5,
        )
        if not field.meta.get("current_flow", True):
            continue
        if coil["current_A"] < 0:
            points = points[::-1]
        active = abs(coil["current_A"]) > max(1e-12, maximum * 1e-10)
        if not active:
            color = "#929aa5"
        width = 0.4 + 2.5 * abs(coil["current_A"]) / maximum if maximum else 0.4
        closed = np.vstack([points, points[0]])
        ax.plot(*closed.T, color="white", linewidth=width + 1.4, zorder=3)
        ax.plot(*closed.T, color=color, linewidth=width, zorder=4)
        if active:
            normal = np.cross(points[1] - points[0], points[2] - points[1])
            normal /= np.linalg.norm(normal)
            edges = range(4) if len(points) == 4 else [16, 48, 80]
            perimeter = np.linalg.norm(np.diff(closed, axis=0), axis=1).sum()
            for i in edges:
                first, last = closed[i], closed[i + 1]
                direction = (last - first) / np.linalg.norm(last - first)
                length = min(
                    perimeter * 0.055,
                    size * 0.018,
                    np.linalg.norm(last - first) * 0.35 if len(points) == 4 else size,
                )
                center = (first + last) / 2
                side = np.cross(normal, direction) * length * 0.3
                head = [
                    center + direction * length * 0.6,
                    center - direction * length * 0.4 + side,
                    center - direction * length * 0.4 - side,
                ]
                ax.add_collection3d(
                    Poly3DCollection([head], facecolors=color, linewidth=0, zorder=4)
                )
    for i, phase in enumerate(phases):
        current = next(c["current_A"] for c in field.coils if c["phase"] == phase)
        handles.append(
            Line2D(
                [],
                [],
                color=palette[i % len(palette)],
                label=f"Phase {phase}  {current:+.3g} A",
            )
        )
    if handles:
        fig.legend(handles=handles, loc="upper left", frameon=False, fontsize=9)
    fig.text(
        0.97,
        0.97,
        f"Unsolved · {field.spec['angle']:g}°",
        ha="right",
        va="top",
        size=9,
        color="#526173",
    )
    ax.set_axis_off()
    path = Path(path).resolve()
    fig.savefig(path, facecolor="white")
    return path


def pole_triangles(surface, triangles):
    """Refine faces crossing a pole boundary before assigning flat PNG colours."""
    if not surface.get("pole"):
        return triangles
    finished = []
    for _ in range(5):
        corners = surface_colors(surface, triangles.reshape(-1, 3)).reshape(-1, 3)
        centers = surface_colors(surface, triangles.mean(axis=1))
        split = np.any(corners != centers[:, None], axis=1)
        finished.append(triangles[~split])
        if not split.any():
            break
        a, b, c = triangles[split].transpose(1, 0, 2)
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        triangles = np.concatenate(
            [
                np.stack(v, axis=1)
                for v in [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
            ]
        )
    else:
        finished.append(triangles)
    return np.concatenate(finished)


def surface_colors(surface, points):
    colors = dict(iron="#9da5ae", coil="#bd733e", magnet="#6d78b3")
    pole = surface.get("pole")
    if not pole:
        return colors[surface["kind"]]
    if pole["rule"] == "plane":
        side = (points - pole["center"]) @ pole["vector"]
    else:
        polarity = np.full(len(points), pole["sign"], dtype=float)
        if pole["count"]:
            angle = (
                np.arctan2(points[:, 1], points[:, 0])
                - pole["phase"]
                + np.pi / pole["count"]
            )
            sectors = np.floor((angle % (2 * np.pi)) / (2 * np.pi / pole["count"]))
            polarity *= 1 - 2 * (sectors % 2)
        side = (np.linalg.norm(points[:, :2], axis=1) - pole["radius"]) * polarity
    return np.where(side >= 0, "#cf4548", "#356ec6")
