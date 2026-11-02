"""North/south display sides from saved remanence, in motor coordinates."""

import numpy as np


def magnet_style(field, surface):
    if surface["kind"] != "magnet":
        return None
    if "pole" in surface:
        return surface["pole"]
    ids = [
        i
        for i, m in enumerate(field.materials)
        if m["name"] == surface["name"] and m["kind"] == "magnet"
    ]
    rows = {c["name"]: c for c in field.spec.get("components", [])}
    component = rows.get(surface["name"], {})
    points = field.points[np.unique(surface["triangles"])]
    radius = np.linalg.norm(points[:, :2], axis=1)
    style = dict(
        period=2 * np.pi / field.sectors if field.sectors > 1 else 0,
        start=field.info["sector_start_rad"],
    )
    if component.get("poles"):
        return dict(
            style,
            rule="radial",
            radius=float((radius.min() + radius.max()) / 2),
            sign=-1 if component["direction"] == "-radial" else 1,
            count=component["poles"],
            phase=np.radians(
                component.get("offset", 0)
                + (field.spec["angle"] if component["rotating"] else 0)
            ),
        )
    vectors = np.array([field.materials[i]["br"] for i in ids])
    if not len(vectors) or not np.allclose(vectors, vectors[0], atol=1e-10):
        raise ValueError("Give each distinct magnet a separate material display name")
    tets = field.points[field.cells[np.isin(field.cell_material, ids)]]
    weights = np.abs(np.linalg.det(tets[:, 1:] - tets[:, :1]))
    center = np.average(tets.mean(axis=1), axis=0, weights=weights)
    br = vectors[0]
    if component.get("direction") in ("radial", "-radial"):
        return dict(
            style,
            rule="radial",
            radius=float((radius.min() + radius.max()) / 2),
            sign=1 if component["direction"] == "radial" else -1,
            count=0,
            phase=0,
        )
    return dict(style, rule="plane", center=center.tolist(), vector=br.tolist())
