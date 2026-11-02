import json

import netgen.occ as occ
import ngsolve as ng
import pytest


@pytest.fixture
def saved_field(tmp_path):
    """A prescribed affine H field with air, nonlinear steel, magnet and a coil.

    This is an exact interpolation fixture, not a new physical motor solve.
    """
    parts = []
    for a, b, name in [
        (-4, -2, "air"),
        (-2, 0, "body_0"),
        (0, 2, "body_1"),
        (2, 4, "body_2"),
    ]:
        part = occ.Box(occ.Pnt(a, -2, -2), occ.Pnt(b, 2, 2))
        part.mat(name)
        parts.append(part)
    shape = occ.Glue(parts)
    shape.faces.name = "outer"
    native = occ.OCCGeometry(shape).GenerateMesh(maxh=1.2)
    native.Scale(0.001)
    native.Save(str(tmp_path / "mesh.vol.gz"))
    mesh = ng.Mesh(str(tmp_path / "mesh.vol.gz"))
    space = ng.H1(mesh, order=2, dirichlet="outer")
    phi = ng.GridFunction(space)
    phi.Set(-100 * ng.x - 200 * ng.y - 300 * ng.z - 50000 * ng.x**2)
    phi.Save(str(tmp_path / "best.sol"))
    source = ng.GridFunction(ng.HCurl(mesh, order=0))
    source.Set(ng.CF((0, 1, 0)))
    source.Save(str(tmp_path / "source0.sol"))
    materials = {
        "body_0": dict(h=[0, 100, 1000, 10000], b=[0, 0.5, 1.5, 2]),
        "body_1": dict(mur=1.05, br=[0.5, 0.1, 0]),
        "body_2": dict(mur=1),
    }
    spec = dict(
        axis=[0, 0, 1],
        center=[0, 0, 0],
        phases=[1],
        angle=0,
        components=[
            dict(index=0, name="steel", kind="iron"),
            dict(index=1, name="PM", kind="magnet"),
            dict(index=2, name="winding", kind="coil"),
        ],
    )
    info = dict(
        sectors=1,
        sector_start_rad=0,
        materials=materials,
        coils=[dict(
            name="winding", turns=10, phase=1, domain="body_2",
            geometry="circle", center=[0.003, 0, 0], axis=[1, 0, 0],
            inner=0.0005, outer=0.0015,
        )],
    )
    for name, data in [
        ("run.json", spec),
        ("mesh.json", info),
        ("result.json", dict(phases=[1], currents_A=[2], angle_deg=0)),
    ]:
        (tmp_path / name).write_text(json.dumps(data))
    return tmp_path
