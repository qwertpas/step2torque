"""The public model: STEP names and physical properties. Lengths are mm."""

from collections import Counter
import hashlib
import math
from pathlib import Path
import tempfile

import numpy as np
from netgen.occ import OCCGeometry

from .materials import material


def vector(value, name="direction"):
    axes = {
        "+x": (1, 0, 0),
        "-x": (-1, 0, 0),
        "+y": (0, 1, 0),
        "-y": (0, -1, 0),
        "+z": (0, 0, 1),
        "-z": (0, 0, -1),
    }
    if isinstance(value, str):
        if value not in axes:
            raise ValueError(f"{name}: use +x, -x, +y, -y, +z, -z, or a 3-vector")
        value = axes[value]
    a = np.asarray(value, dtype=float)
    if a.shape != (3,) or not np.isfinite(a).all() or np.linalg.norm(a) == 0:
        raise ValueError(f"{name}: supply a nonzero 3-vector")
    return (a / np.linalg.norm(a)).tolist()


def positive(value, name):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


class Component:
    """One named CAD component. Configuration methods return this component."""

    def __init__(self, name, shape, index):
        self.name, self.shape, self.index = name, shape, index
        self.settings = dict(
            kind="iron", material=material("steel"), rotating="rotor" in name.lower()
        )
        # Names identify missing excitation, never its direction or magnitude.
        if "magnet" in name.lower():
            self.settings.update(
                kind="magnet", rotating=True, direction=None, br=1.2, mur=1.05
            )
        elif "coil" in name.lower() or "winding" in name.lower():
            self.settings.update(kind="coil", direction=None)

    def set_material(self, name, *, rotating=None):
        """Assign steel, 1J22, air, a B-H CSV, or {'mur': relative_permeability}."""
        self.settings.update(kind="iron", material=material(name))
        if rotating is not None:
            self.settings["rotating"] = bool(rotating)
        return self

    def make_coil(
        self,
        phase=1,
        *,
        direction=None,
        turns=None,
        resistance=None,
        fill=0.6,
        temperature=20,
    ):
        """Circular/rectangular annulus; direction is its right-hand axis, resistance is ohms.

        Supply turns or resistance; the other is calculated from the CAD window,
        copper fill and temperature. Coils sharing a phase are in series.
        """
        if not isinstance(phase, (int, str)) or isinstance(phase, bool):
            raise ValueError(f"{self.name}: phase must be an integer or a name")
        if not 0 < fill <= 1:
            raise ValueError(f"{self.name}: fill must be between 0 and 1")
        if not math.isfinite(temperature) or temperature < -200:
            raise ValueError(f"{self.name}: invalid copper temperature")
        self.settings = dict(
            kind="coil",
            rotating=False,
            phase=phase,
            direction=vector(direction) if direction is not None else None,
            turns=None if turns is None else positive(turns, "turns"),
            resistance=None
            if resistance is None
            else positive(resistance, "resistance"),
            fill=float(fill),
            temperature=float(temperature),
        )
        return self

    def make_magnet(
        self, *, direction=None, br=1.2, mur=1.05, rotating=True, poles=None, offset=0
    ):
        """Uniform direction, or a radial multipole ring (poles, offset in degrees).

        Without poles, radial / -radial uses the body center. A ring's first
        north pole is centered on +x around the shaft, shifted by offset.
        """
        radial = isinstance(direction, str) and direction in ("radial", "-radial")
        if poles is not None and (
            not isinstance(poles, int)
            or isinstance(poles, bool)
            or poles < 2
            or poles % 2
            or not radial
        ):
            raise ValueError(
                f"{self.name}: poles requires an even count and radial direction"
            )
        if not math.isfinite(offset):
            raise ValueError(f"{self.name}: offset must be finite degrees")
        if offset and poles is None:
            raise ValueError(f"{self.name}: offset requires poles")
        if direction is not None and not radial:
            direction = vector(direction)
        self.settings = dict(
            kind="magnet",
            rotating=bool(rotating),
            direction=direction,
            br=positive(br, "br"),
            mur=positive(mur, "mur"),
            poles=poles,
            offset=float(offset),
        )
        return self

    def __repr__(self):
        s = self.settings
        return f"{self.name!r}: {s['kind']}" + (
            " (direction required)" if s.get("direction", True) is None else ""
        )


def read_components(path):
    path = Path(path)
    # Netgen dispatches by a case-sensitive suffix; CAD exporters often use STEP.
    if path.suffix != path.suffix.lower():
        with tempfile.TemporaryDirectory() as folder:
            lower = Path(folder) / "assembly.step"
            lower.write_bytes(path.read_bytes())
            geometry = OCCGeometry(str(lower))
    else:
        geometry = OCCGeometry(str(path))
    rows = []

    def visit(node, parents):
        if node.is_assembly:
            for child in node.children:
                visit(child, [*parents, node.name] if node.name else parents)
            return
        solids = list(node.shape.solids)
        if not solids:
            return
        # Native solid names are more useful than an exporter-generated PRODUCT.
        if len(solids) > 1 and all(s.name for s in solids):
            rows.extend((s.name, "/".join([*parents, s.name]), s) for s in solids)
        else:
            name = solids[0].name if len(solids) == 1 and solids[0].name else node.name
            name = name or f"body{len(rows) + 1}"
            rows.append((name, "/".join([*parents, name]), node.shape))

    visit(geometry.GetAssemblyTree(), [])
    if not rows:
        raise ValueError("STEP contains no solid components")
    counts = Counter(name for name, _, _ in rows)
    result = {}
    for name, full, shape in rows:
        key = full if counts[name] > 1 else name
        if key in result:
            raise ValueError(
                f"Duplicate STEP occurrence {key!r}; give the CAD occurrences distinct names"
            )
        result[key] = Component(key, shape, len(result))
    return result


class Motor:
    """Motor('assembly.step', axis='+z', center=(0,0,0)). CAD lengths are mm.

    components is a dict of the actual CAD component names. Iron defaults to
    steel. Rotor-named components rotate; magnets rotate by default.
    """

    def __init__(
        self, step, *, axis="+z", center=(0, 0, 0), symmetry="auto", contact_gap=0.01
    ):
        self.path = Path(step).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"STEP file not found: {self.path}")
        self.axis = vector(axis, "rotation axis")
        self.center = np.asarray(center, dtype=float).tolist()
        if len(self.center) != 3 or not np.isfinite(self.center).all():
            raise ValueError("center must be a 3-vector in mm")
        if symmetry != "auto" and (not isinstance(symmetry, int) or symmetry < 1):
            raise ValueError("symmetry must be 'auto' or a positive sector count")
        self.symmetry = symmetry
        if (
            not isinstance(contact_gap, (int, float))
            or not math.isfinite(contact_gap)
            or contact_gap < 0
        ):
            raise ValueError("contact_gap must be nonnegative mm")
        self.contact_gap = float(contact_gap)
        self.components = read_components(self.path)
        self._step_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def solve(self, *, angle=0, current="optimized", power=5.5, **options):
        """Start a background solve. angle is mechanical degrees from the STEP."""
        from .solution import solve_position

        return solve_position(
            self, angle=angle, current=current, power=power, **options
        )

    def render_image(self, path, *, angle=0, current=None, **options):
        """Preview the configured motor as PNG; default current is 1 A per phase."""
        from .visualization import Viewer

        return Viewer(self, angle=angle, current=current).png(path, **options)

    def render_html(self, path, *, angle=0, current=None, **options):
        """Preview CAD, winding phases and mechanical rotation without solving."""
        from .visualization import Viewer

        return Viewer(self, angle=angle, current=current).html(path, **options)

    def export(self, path, *, angle=0, current=None, **options):
        """Export an unsolved .s2t setup; default current is 1 A per phase."""
        from .visualization import Viewer

        return Viewer(self, angle=angle, current=current).export(path, **options)

    def _spec(self):
        rows = []
        for c in self.components.values():
            s = dict(c.settings)
            if s["kind"] in ("coil", "magnet") and s.get("direction") is None:
                raise ValueError(f"{c.name}: {s['kind']} direction is required")
            if s["kind"] == "coil" and "turns" not in s:
                raise ValueError(
                    f"{c.name}: call make_coil(phase=..., turns=..., direction=...)"
                )
            if s["kind"] == "coil" and s["turns"] is None and s["resistance"] is None:
                raise ValueError(f"{c.name}: supply turns or resistance")
            rows.append(dict(name=c.name, index=c.index, **s))
        if not any(r["rotating"] for r in rows):
            raise ValueError(
                "No rotating components; use set_material(..., rotating=True)"
            )
        return dict(
            axis=self.axis,
            center=self.center,
            symmetry=self.symmetry,
            contact_gap=self.contact_gap,
            components=rows,
        )

    def __repr__(self):
        return (
            "Motor("
            + self.path.name
            + ")\n"
            + "\n".join(f"  {c!r}" for c in self.components.values())
        )
