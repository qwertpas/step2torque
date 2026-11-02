"""STEP assembly -> an SI tetrahedral mesh and explicit physical coefficients."""

from collections import Counter
import json
import math

import netgen.occ as occ
from netgen.meshing import IdentificationType
import ngsolve as ng
import numpy as np

from .model import read_components
from ._source import frame
from . import _contact


def rotation(axis):
    axis = np.asarray(axis, float)
    cross, cosine = np.cross(axis, (0, 0, 1)), axis[2]
    if np.linalg.norm(cross) < 1e-12:
        return np.eye(3) if cosine > 0 else np.diag([1.0, -1.0, -1.0])
    skew = np.array(
        [[0, -cross[2], cross[1]], [cross[2], 0, -cross[0]], [-cross[1], cross[0], 0]]
    )
    return np.eye(3) + skew + skew @ skew / (1 + cosine)


def align(shape, axis, center):
    shape = shape.Move(occ.Vec(*(-np.asarray(center))))
    axis = np.asarray(axis, float)
    cross = np.cross(axis, (0, 0, 1))
    if np.linalg.norm(cross) < 1e-12:
        return (
            shape
            if axis[2] > 0
            else shape.Rotate(occ.Axis(occ.Pnt(0, 0, 0), occ.X), 180)
        )
    return shape.Rotate(
        occ.Axis(occ.Pnt(0, 0, 0), occ.Dir(*cross)),
        math.degrees(math.acos(np.clip(axis[2], -1, 1))),
    )


def coil_geometry(shape, direction, name):
    center = np.asarray(tuple(shape.center))
    # Snap the requested right-hand direction to parallel CAD cap planes.
    # STEP placements round direction cosines; sampling a near-parallel plane
    # instead would introduce a fictitious sheet at the winding boundary.
    planes = []
    for face in shape.faces:
        surface = face.surf
        normal = np.array(tuple(surface.Normal(0, 0)))
        other = np.array(tuple(surface.Normal(0.731, 0.419)))
        if (
            abs(float(normal @ direction)) > 1 - 1e-8
            and np.linalg.norm(np.cross(normal, other)) < 1e-12
        ):
            normal *= 1 if normal @ direction > 0 else -1
            planes.append((normal, np.array(tuple(surface.Value(0, 0)))))
    if len(planes) < 2:
        raise ValueError(
            f"{name}: coil direction must be normal to its winding annulus"
        )
    direction = planes[0][0]
    positions = [float(point @ direction) for _, point in planes]
    height = max(positions) - min(positions)
    center += direction * ((max(positions) + min(positions)) / 2 - center @ direction)
    local = align(shape, direction, center)
    u = rotation(direction) @ frame(direction)[0]
    local = local.Rotate(
        occ.Axis(occ.Pnt(0, 0, 0), occ.Z), -math.degrees(math.atan2(u[1], u[0]))
    )
    # OCCT bounding boxes include shape tolerances. Excitation boundaries need
    # the actual CAD surfaces: that padding creates spurious sheet currents.
    coordinates = np.array([tuple(v.p) for v in local.vertices])
    outer = float(np.linalg.norm(coordinates[:, :2], axis=1).max())
    inner2 = outer**2 - shape.mass / (math.pi * height)
    inner = math.sqrt(max(inner2, 0))
    ring = (
        (
            occ.Cylinder(occ.Pnt(0, 0, -height / 2), occ.Z, outer, height)
            - occ.Cylinder(occ.Pnt(0, 0, -height / 2), occ.Z, inner, height)
        )
        if inner > 0
        else None
    )
    base = dict(
        center=(center * 0.001).tolist(),
        axis=list(direction),
        height=height * 0.001,
        volume=shape.mass * 1e-9,
    )
    if inner > 0 and (local - ring).mass + (ring - local).mass <= max(
        1e-4, shape.mass * 1e-5
    ):
        return dict(
            base,
            geometry="circle",
            inner=inner * 0.001,
            outer=outer * 0.001,
            turn_length=math.pi * (inner + outer) * 0.001,
        )
    if len(coordinates):
        x = sorted(
            {round(abs(float(a)), 6) for a in coordinates[:, 0] if abs(a) > 1e-5}
        )
        y = sorted(
            {round(abs(float(a)), 6) for a in coordinates[:, 1] if abs(a) > 1e-5}
        )
        if len(x) == len(y) == 2:
            xi, xo = x
            yi, yo = y
            rect = occ.Box(
                occ.Pnt(-xo, -yo, -height / 2), occ.Pnt(xo, yo, height / 2)
            ) - occ.Box(occ.Pnt(-xi, -yi, -height), occ.Pnt(xi, yi, height))
            if (local - rect).mass + (rect - local).mass <= max(
                1e-4, shape.mass * 1e-5
            ):
                return dict(
                    base,
                    geometry="rectangle",
                    inner_x=xi * 0.001,
                    outer_x=xo * 0.001,
                    inner_y=yi * 0.001,
                    outer_y=yo * 0.001,
                    turn_length=2 * (xi + xo + yi + yo) * 0.001,
                )
    raise ValueError(
        f"{name}: coil must be a circular or rectangular annulus; check its direction"
    )


def periodic_count(bodies, requested):
    """Use symmetry only after checking geometry, material, phases and PM axes."""
    spin = occ.Axis(occ.Pnt(0, 0, 0), occ.Z)
    signatures, centers = [], []
    for shape, row in bodies:
        center = np.asarray(tuple(shape.center))
        centers.append(center)
        signatures.append(
            json.dumps(
                {
                    k: row.get(k)
                    for k in (
                        "kind",
                        "material",
                        "phase",
                        "turns",
                        "br",
                        "mur",
                        "rotating",
                    )
                },
                sort_keys=True,
            )
        )
    counts = Counter(
        (
            round(float(np.linalg.norm(c[:2])), 3),
            round(float(c[2]), 3),
            round(float(s.mass), 3),
            sig,
        )
        for (s, _), c, sig in zip(bodies, centers, signatures)
        if np.linalg.norm(c[:2]) > 1e-3
    )
    possible = math.gcd(*counts.values()) if counts else 1
    candidates = (
        [requested]
        if requested != "auto"
        else [n for n in range(possible, 1, -1) if possible % n == 0] + [1]
    )
    centers = np.array(centers)
    for sectors in candidates:
        if sectors == 1:
            return 1
        angle = 2 * math.pi / sectors
        rot = np.array(
            [
                [math.cos(angle), -math.sin(angle), 0],
                [math.sin(angle), math.cos(angle), 0],
                [0, 0, 1],
            ]
        )
        valid = True
        for i, (shape, row) in enumerate(bodies):
            target = centers[i] @ rot.T
            matches = [
                j
                for j in range(len(bodies))
                if signatures[j] == signatures[i]
                and np.linalg.norm(centers[j] - target) < 1e-4
            ]
            if len(matches) != 1:
                valid = False
                break
            j = matches[0]
            if (
                row["kind"] in ("magnet", "coil")
                and np.linalg.norm(
                    np.asarray(row["direction"]) @ rot.T - bodies[j][1]["direction"]
                )
                > 1e-7
            ):
                valid = False
                break
            rotated = shape.Rotate(spin, 360 / sectors)
            if (rotated - bodies[j][0]).mass > max(1e-5, shape.mass * 1e-7):
                valid = False
                break
        if valid:
            return sectors
    raise ValueError(
        "Requested symmetry does not preserve the CAD, materials and excitation"
    )


def wedge(radius, left, right, bottom, height):
    points = [
        occ.Pnt(radius * math.cos(a), radius * math.sin(a), bottom)
        for a in (left, (left + right) / 2, right)
    ]
    origin = occ.Pnt(0, 0, bottom)
    return occ.Face(
        occ.Wire(
            [
                occ.Segment(origin, points[0]),
                occ.ArcOfCircle(*points),
                occ.Segment(points[-1], origin),
            ]
        )
    ).Extrude(occ.Vec(0, 0, height))


def prepare(spec, folder, progress):
    """Cache canonical mesh once, so pause/resume retains the exact DOF map."""
    meta = folder / "mesh.json"
    if meta.exists():
        return ng.Mesh(str(folder / "mesh.vol.gz")), json.loads(meta.read_text())
    progress(stage="geometry")
    imported = list(read_components(folder / "assembly.step").values())
    rot = rotation(spec["axis"])
    bodies, coils = [], []
    spin = occ.Axis(occ.Pnt(0, 0, 0), occ.Z)
    for row in spec["components"]:
        row = dict(row)
        shape = align(imported[row["index"]].shape, spec["axis"], spec["center"])
        if row["kind"] == "magnet":
            if row.get("poles") is not None:
                bodies.extend(magnet_parts(shape, row))
                continue
            if isinstance(row["direction"], str):
                radial = np.array(tuple(shape.center)) * (1, 1, 0)
                if np.linalg.norm(radial) < 1e-9:
                    raise ValueError(
                        f"{row['name']}: radial magnet direction is undefined on the shaft axis"
                    )
                row["direction"] = (
                    radial
                    / np.linalg.norm(radial)
                    * (-1 if row["direction"] == "-radial" else 1)
                ).tolist()
            else:
                row["direction"] = (rot @ row["direction"]).tolist()
        elif row["kind"] == "coil":
            row["direction"] = (rot @ row["direction"]).tolist()
            coil = dict(coil_geometry(shape, row["direction"], row["name"]), **row)
            coil.pop("direction")
            rho = 1.724e-8 * (1 + 0.00393 * (coil["temperature"] - 20))
            factor = rho * coil["turn_length"] ** 2 / (coil["volume"] * coil["fill"])
            if coil["turns"] is None:
                coil["turns"] = max(1, round(math.sqrt(coil["resistance"] / factor)))
                row["turns"] = coil["turns"]
            if coil["resistance"] is None:
                coil["resistance"] = factor * coil["turns"] ** 2
            coils.append(coil)
        bodies.append((shape, row))
    sectors = periodic_count(bodies, spec["symmetry"])
    # Rotate every moving shape and its magnetization by mechanical angle.
    angle = math.radians(spec["angle"])
    rz = np.array(
        [
            [math.cos(angle), -math.sin(angle), 0],
            [math.sin(angle), math.cos(angle), 0],
            [0, 0, 1],
        ]
    )
    for i, (shape, row) in enumerate(bodies):
        if row["rotating"]:
            shape = shape.Rotate(spin, spec["angle"])
            if row["kind"] == "magnet":
                row["direction"] = (rz @ row["direction"]).tolist()
            bodies[i] = shape, row
    box = np.array([[tuple(p) for p in s.bounding_box] for s, _ in bodies])
    rmax = float(abs(box[:, :, :2]).max())
    bottom, top = float(box[:, 0, 2].min()), float(box[:, 1, 2].max())
    extent = max(top - bottom, rmax)
    radius = spec["air_radius"] or 8 * rmax
    if radius <= math.sqrt(2) * rmax:
        raise ValueError("air_radius must enclose all STEP components")
    zmin, zmax = bottom - (radius - rmax), top + (radius - rmax)
    far = max(6.0, extent / 10, radius / 8)
    face = spec["mesh_size"] or min(0.65, rmax / 16)
    bulk = 2 * face
    period = 2 * math.pi / sectors
    if sectors > 1:
        critical = sorted(
            {
                round(math.atan2(v.p.y, v.p.x) % period, 8)
                for s, _ in bodies
                for v in s.vertices
                if math.hypot(v.p.x, v.p.y) > 1e-6
            }
        )
        if not critical:
            critical = [0.0]
        gaps = [
            (critical[i], (critical[(i + 1) % len(critical)] - critical[i]) % period)
            for i in range(len(critical))
        ]
        cuts = [(a + g / 2) % period for a, g in sorted(gaps, key=lambda x: -x[1])]
    else:
        cuts = [0.0]
    materials, moving = {}, []
    for left in cuts:
        boundary = (
            wedge(radius, left, left + period, zmin, zmax - zmin)
            if sectors > 1
            else occ.Cylinder(occ.Pnt(0, 0, zmin), occ.Z, radius, zmax - zmin)
        )
        boundary.faces.name = "outer"
        parts, materials, moving = [], {}, []
        for shape, row in bodies:
            part = shape * boundary
            if part.mass < 1e-7:
                continue
            name = row.get("domain", f"body_{row['index']}")
            part.mat(name)
            part.maxh, part.faces.maxh = bulk, face
            parts.append(part)
            if row["kind"] == "iron":
                materials[name] = row["material"]
            elif row["kind"] == "magnet":
                materials[name] = dict(
                    mur=row["mur"], br=(row["br"] * np.array(row["direction"])).tolist()
                )
            else:
                materials[name] = dict(mur=1.0)
                next(c for c in coils if c["index"] == row["index"])["domain"] = name
            if row["rotating"]:
                moving.append(name)
        fused = occ.Fuse(parts)
        volume = sum(part.mass for part in parts)
        if volume - fused.mass > max(1e-5, volume * 1e-8):
            raise ValueError(
                "STEP components overlap; separate intersecting solids before solving"
            )
        air = boundary - fused
        air.mat("air")
        air.maxh = far
        shape = occ.Glue([air, *parts])
        if sectors == 1:
            break
        sides = [[], []]
        for f in shape.faces:
            x, y, _ = tuple(f.center)
            phi = math.atan2(y, x)
            for side, a in enumerate((left, left + period)):
                if abs(math.atan2(math.sin(phi - a), math.cos(phi - a))) < 1e-7:
                    f.name = f"side{side}"
                    sides[side].append(f)
        matches = occ.ListOfShapes(sides[0]).Identify(
            occ.ListOfShapes(sides[1]),
            "periodic",
            IdentificationType.PERIODIC,
            occ.Rotation(spin, 360 / sectors),
        )
        if matches and matches == len(sides[0]) == len(sides[1]):
            break
    else:
        raise ValueError("Could not form matching periodic faces from this STEP")
    progress(stage="meshing", sectors=sectors)
    native = occ.OCCGeometry(shape).GenerateMesh(
        maxh=far, grading=0.4, curvaturesafety=8, minedgelen=1e-6
    )
    native.Scale(0.001)
    native.SetGeometry(occ.OCCGeometry(shape.Scale(occ.Pnt(0, 0, 0), 0.001)))
    mesh, contact = _contact.split(
        ng.Mesh(native), spec["contact_gap"] * 0.001, materials, moving
    )
    native = mesh.ngmesh
    native.Save(str(folder / "mesh.vol.gz"))
    mesh = ng.Mesh(str(folder / "mesh.vol.gz"))
    info = dict(
        sectors=sectors,
        sector_start_rad=left,
        air_radius_mm=radius,
        materials=materials,
        moving=moving,
        coils=coils,
        surface_mm=face,
        bulk_mm=bulk,
        elements=mesh.ne,
        vertices=mesh.nv,
        contact=contact,
    )
    meta.write_text(json.dumps(info))
    return mesh, info


def magnet_parts(shape, row):
    """Resolve radial poles on CAD faces, so no tetrahedron crosses a pole jump.

    Piecewise uniform vectors approximate radial magnetization within 2.5°.
    Each pole gets at least four pieces; moving geometry rotates them together.
    """
    poles = row["poles"]
    pieces = max(4, math.ceil(360 / poles / 5))
    bottom, top = [tuple(p)[2] for p in shape.bounding_box]
    radius = 2 * max(math.hypot(p.x, p.y) for p in shape.bounding_box)
    start = math.radians(row["offset"]) - math.pi / poles
    delta = 2 * math.pi / (poles * pieces)
    for k in range(poles * pieces):
        left = start + k * delta
        part = shape * wedge(radius, left, left + delta, bottom - 1, top - bottom + 2)
        if part.mass < 1e-7:
            continue
        sign = (-1 if row["direction"] == "-radial" else 1) * (-1) ** (k // pieces)
        angle = left + delta / 2
        settings = dict(row)
        settings.update(
            domain=f"body_{row['index']}_{k}",
            direction=[sign * math.cos(angle), sign * math.sin(angle), 0.0],
        )
        yield part, settings
