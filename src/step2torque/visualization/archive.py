"""Single portable field file: gzip(magic, JSON header, aligned arrays)."""

from dataclasses import asdict
import gzip
import json
from pathlib import Path
import struct

import numpy as np

from .appearance import magnet_style

MAGIC = b"S2TFIELD"
TYPES = {"<f4", "<u4", "<i4", "<u2"}


def hierarchy(points, cells, leaf_size=12):
    """An AABB tree of tetrahedra, built once at export, queried in the browser."""
    vertices = points[cells]
    low, high = vertices.min(axis=1), vertices.max(axis=1)
    centers = (low + high) / 2
    order = np.arange(len(cells), dtype=np.uint32)
    bounds, nodes = [], []

    def build(start, stop):
        ids = order[start:stop]
        lo, hi = low[ids].min(axis=0), high[ids].max(axis=0)
        index = len(nodes)
        bounds.append([*lo, *hi])
        nodes.append([0, 0])
        if stop - start <= leaf_size:
            nodes[index] = [-start - 1, stop - start]
        else:
            axis = int(np.argmax(np.ptp(centers[ids], axis=0)))
            middle = (stop - start) // 2
            order[start:stop] = ids[np.argpartition(centers[ids, axis], middle)]
            left = build(start, start + middle)
            right = build(start + middle, stop)
            nodes[index] = [left, right]
        return index

    build(0, len(cells))
    # Float32 bounds must round outwards, otherwise a thin tetra can be missed.
    box = np.asarray(bounds, dtype=np.float32)
    box[:, :3] = np.nextafter(box[:, :3], -np.inf)
    box[:, 3:] = np.nextafter(box[:, 3:], np.inf)
    return order, box, np.asarray(nodes, dtype=np.int32)


def pack(
    field, plane, *, metric, air, lines, vmax, flux_max, density, mode, colors, bounds
):
    if mode not in ("2d", "3d"):
        raise ValueError("mode must be 2d or 3d")
    if not field.solved and mode != "3d":
        raise ValueError("Unsolved setups use the 3D view")
    metric = ("flux" if metric is None else metric) if field.solved else metric
    if not field.solved and metric is not None:
        raise ValueError("Flux and steel loading require a solved field")
    if field.solved and metric not in ("flux", "loading"):
        raise ValueError("metric must be flux or loading")
    if metric == "loading" and not any(m["reference_T"] for m in field.materials):
        raise ValueError("Steel loading needs a nonlinear steel B-H curve")
    if vmax is not None and (not np.isfinite(vmax) or vmax <= 0):
        raise ValueError("vmax must be positive")
    if not np.isfinite(density) or not 10 <= density <= 500:
        raise ValueError("density must be between 10 and 500")
    if bounds is not None:
        bounds = np.asarray(bounds, float)
        if (
            bounds.shape != (4,)
            or not np.isfinite(bounds).all()
            or np.any(bounds[2:] <= bounds[:2])
        ):
            raise ValueError("bounds must be finite (left, bottom, right, top)")
        bounds = bounds.tolist()
    high = (vmax if vmax is not None else (100 if metric == "loading" else flux_max)) if field.solved else None
    arrays, parts, offset = {}, [], 0

    def add(name, values, dtype):
        nonlocal offset
        values = np.ascontiguousarray(values, dtype=dtype)
        padding = (-offset) % 4
        if padding:
            parts.append(bytes(padding))
            offset += padding
        arrays[name] = dict(offset=offset, length=values.size, type=values.dtype.str)
        parts.append(values.tobytes())
        offset += values.nbytes

    add("points", field.points, "<f4")
    if field.solved:
        h = field.vertex_fields()
        order, boxes, nodes = hierarchy(field.points, field.cells)
        add("cells", field.cells[order], "<u4")
        add("h", h[order], "<f4")
        add("material", field.cell_material[order], "<u2")
        add("bounds", boxes, "<f4")
        add("nodes", nodes, "<i4")
    surfaces = []
    for i, surface in enumerate(field.surfaces):
        name = f"surface{i}"
        add(name, surface["triangles"], "<u4")
        surfaces.append(
            dict(
                name=surface["name"],
                kind=surface["kind"],
                array=name,
                pole=magnet_style(field, surface),
                rotating=surface.get("rotating", False),
            )
        )
    meta = dict(
        format="step2torque-field",
        version=1,
        solved=field.solved,
        units=dict(position="mm", h="A/m", b="T") if field.solved else dict(position="mm"),
        arrays=arrays,
        materials=field.materials,
        surfaces=surfaces,
        coils=field.coils,
        current_flow=getattr(field, "meta", {}).get("current_flow", False),
        sectors=field.sectors,
        sector_start_rad=field.info["sector_start_rad"],
        box=field.bounds.tolist(),
        world_center_mm=field.spec["center"],
        world_to_motor=field.rotation.tolist(),
        plane=asdict(plane),
        metric=metric,
        air=bool(air),
        lines=bool(lines) if field.solved else False,
        vmax=high,
        flux_max=flux_max,
        density=density,
        colors=colors,
        angle_deg=field.spec["angle"],
        axis=field.spec["axis"],
        result=getattr(field, "result", {}),
        mode=mode,
        bounds=bounds,
    )
    if not field.solved:
        meta["components"] = field.spec["components"]
    header = json.dumps(meta, separators=(",", ":"), allow_nan=False).encode()
    prefix = MAGIC + struct.pack("<I", len(header)) + header
    prefix += bytes((-len(prefix)) % 4)
    return gzip.compress(prefix + b"".join(parts), compresslevel=6, mtime=0)


def read(path):
    try:
        raw = gzip.decompress(Path(path).read_bytes())
        if raw[:8] != MAGIC:
            raise ValueError("Not a step2torque solution file")
        size = struct.unpack_from("<I", raw, 8)[0]
        meta = json.loads(raw[12 : 12 + size])
        if meta["format"] != "step2torque-field" or meta["version"] != 1:
            raise ValueError("Unsupported solution format")
        data = raw[(12 + size + 3) // 4 * 4 :]
        arrays = {}
        for name, entry in meta["arrays"].items():
            if entry["type"] not in TYPES or entry["offset"] < 0 or entry["length"] < 0:
                raise ValueError("Invalid solution arrays")
            arrays[name] = np.frombuffer(
                data, dtype=entry["type"], count=entry["length"], offset=entry["offset"]
            )
        return meta, arrays
    except (
        OSError,
        EOFError,
        KeyError,
        TypeError,
        struct.error,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("Invalid step2torque solution file") from error
