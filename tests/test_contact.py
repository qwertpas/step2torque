"""Surface contacts against an analytic circuit and the public STEP workflow."""

import json

import netgen.occ as occ
from netgen.meshing import IdentificationType
import ngsolve as ng
import numpy as np
import pytest
from scipy.optimize import brentq

from step2torque import Motor, _contact, _field
from step2torque.materials import MU0, material


@pytest.mark.parametrize(
    "nonlinear,tangent", [(False, 0.0), (True, 0.0), (False, 500.0)]
)
def test_contact_circuit(nonlinear, tangent):
    lower = occ.Box(occ.Pnt(0, 0, 0), occ.Pnt(1, 1, 1))
    upper = occ.Box(occ.Pnt(0, 0, 1), occ.Pnt(1, 1, 2))
    lower.mat("iron0")
    upper.mat("iron1")
    lower.faces.name = upper.faces.name = "wall"
    lower.faces.Min(occ.Z).name = upper.faces.Max(occ.Z).name = "outer"
    shape = occ.Glue([lower, upper])
    if tangent:
        left, right = [], []
        for face in shape.faces:
            if abs(face.center.x) < 1e-9:
                left.append(face)
            elif abs(face.center.x - 1) < 1e-9:
                right.append(face)
        occ.ListOfShapes(left).Identify(
            occ.ListOfShapes(right),
            "periodic",
            IdentificationType.PERIODIC,
            occ.Translation(occ.Vec(1, 0, 0)),
        )
    native = occ.OCCGeometry(shape).GenerateMesh(maxh=0.7)
    native.Scale(0.001)
    original = ng.Mesh(native)
    law = material("kede_1j21") if nonlinear else dict(mur=1000.0)
    materials = dict(iron0=law, iron1=law)
    mesh, contact = _contact.split(original, 1e-5, materials)
    assert contact and contact["faces"]
    assert mesh.ne == original.ne
    applied = 12000.0 if nonlinear else 4000.0
    h = (
        (lambda b: np.interp(b, law["b"], law["h"]))
        if nonlinear
        else (lambda b: b / (MU0 * 1000))
    )
    exact = brentq(lambda b: 0.002 * h(b) + 1e-5 * b / MU0 - applied * 0.002, 0.0, 3.5)
    solved = _field.solve(
        mesh,
        ng.CF((tangent, 0, applied)),
        materials,
        periodic=bool(tangent),
        contact=contact,
    )
    expected = ng.CF((MU0 * 1000 * tangent, 0, exact))
    volume = ng.Integrate(ng.CF(1), mesh)
    error = ng.Integrate((solved["B"] - expected) ** 2, mesh) / volume
    assert np.sqrt(error) < 1e-8
    final = solved["diagnostics"]["newton"][-1]
    assert final["hessian_floor"] == 0 and final["decrement_sqrt_j"] < 1e-9


def test_partial_contact_mesh():
    lower = occ.Box(occ.Pnt(-1, -1, -1), occ.Pnt(1, 1, 0))
    upper = occ.Box(occ.Pnt(-1, -1, 0), occ.Pnt(1, 1, 1))
    lower.mat("iron0")
    upper.mat("iron1")
    outer = occ.Box(occ.Pnt(-3, -3, -3), occ.Pnt(3, 3, 3))
    air = outer - occ.Fuse([lower, upper])
    air.mat("air")
    air.faces.name = "outer"
    native = occ.OCCGeometry(occ.Glue([air, lower, upper])).GenerateMesh(maxh=2)
    native.Scale(0.001)
    original = ng.Mesh(native)
    materials = dict(iron0=dict(mur=100.0), iron1=dict(mur=100.0))
    mesh, contact = _contact.split(original, 1e-5, materials)
    assert contact and mesh.nv > original.nv
    assert ng.Integrate(ng.CF(1), mesh) == pytest.approx(
        ng.Integrate(ng.CF(1), original), rel=1e-12
    )
    tets = mesh.ngmesh.Elements3D().NumPy()["nodes"]
    faces = np.sort(
        np.concatenate(
            [tets[:, f] for f in [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]]]
        ),
        axis=1,
    )
    unique, count = np.unique(faces, axis=0, return_counts=True)
    boundary = set(
        map(tuple, np.sort(mesh.ngmesh.Elements2D().NumPy()["nodes"], axis=1))
    )
    assert not set(map(tuple, unique[count == 1])) - boundary
    assert count.max() == 2
    assert set(mesh.GetMaterials()) == {"air", "iron0", "iron1"}
    unchanged, perfect = _contact.split(original, 0.0, materials)
    assert unchanged is original and perfect is None


def test_rotational_contact_traces():
    from step2torque._mesh import wedge

    crop = wedge(6, 0, np.pi / 2, -1, 4)
    parts = []
    for i in range(2):
        ring = occ.Cylinder(occ.Pnt(0, 0, i), occ.Z, 5, 1) - occ.Cylinder(
            occ.Pnt(0, 0, i), occ.Z, 3, 1
        )
        ring = ring * crop
        ring.mat(f"iron{i}")
        ring.faces.name = "wall"
        ring.faces.Min(occ.Z).name = "outer" if i == 0 else "wall"
        ring.faces.Max(occ.Z).name = "outer" if i == 1 else "wall"
        parts.append(ring)
    shape = occ.Glue(parts)
    sides = [[], []]
    for face in shape.faces:
        if abs(face.center.y) < 1e-9:
            face.name = "side0"
            sides[0].append(face)
        elif abs(face.center.x) < 1e-9:
            face.name = "side1"
            sides[1].append(face)
    occ.ListOfShapes(sides[0]).Identify(
        occ.ListOfShapes(sides[1]),
        "periodic",
        IdentificationType.PERIODIC,
        occ.Rotation(occ.Axis(occ.Pnt(0, 0, 0), occ.Z), 90),
    )
    native = occ.OCCGeometry(shape).GenerateMesh(maxh=0.8)
    native.Scale(0.001)
    materials = dict(iron0=dict(mur=1000.0), iron1=dict(mur=1000.0))
    mesh, contact = _contact.split(ng.Mesh(native), 1e-5, materials)
    pairs = {a.nr: b.nr for a, b, _ in mesh.ngmesh.GetIdentifications()}
    left, right = set(), set()
    for face in mesh.ngmesh.Elements2D().NumPy():
        name = mesh.GetBoundaries()[face["index"] - 1]
        if name == "side0":
            left.add(tuple(sorted(pairs[int(v)] for v in face["nodes"])))
        elif name == "side1":
            right.add(tuple(sorted(face["nodes"])))
    assert left == right and left
    solved = _field.solve(
        mesh, ng.CF((0, 0, 4000)), materials, periodic=True, contact=contact
    )
    assert solved["diagnostics"]["newton"][-1]["decrement_sqrt_j"] < 1e-9


def test_step_contacts_and_current_search(tmp_path):
    shapes = []
    for j, z in enumerate((-1.0, 0.0)):
        iron = occ.Cylinder(occ.Pnt(0, 0, z), occ.Z, 4, 1) - occ.Cylinder(
            occ.Pnt(0, 0, z), occ.Z, 2, 1
        )
        iron.name = f"iron{j}"
        shapes.append(iron)
    for j, z in enumerate((-0.25, 1.75)):
        coil = occ.Cylinder(occ.Pnt(0, 0, z), occ.Z, 6, 0.5) - occ.Cylinder(
            occ.Pnt(0, 0, z), occ.Z, 5, 0.5
        )
        coil.name = f"coil{j}"
        shapes.append(coil)
    magnet = occ.Sphere(occ.Pnt(0, 0, 4), 1)
    magnet.name = "magnet"
    path = tmp_path / "contact.step"
    occ.Compound([*shapes, magnet]).WriteStep(str(path))
    motor = Motor(path, axis="+y")
    for j in range(2):
        motor.components[f"iron{j}"].set_material(dict(mur=100.0))
        motor.components[f"coil{j}"].make_coil(
            phase=j, turns=50, resistance=0.1, direction="+z"
        )
    motor.components["magnet"].make_magnet(direction="+x")
    sol = motor.solve(
        power=0.1, balanced=False, mesh_size=1.2, air_radius=25, threads=2, device="cpu"
    ).wait(timeout=180)
    info = json.loads((sol.path / "mesh.json").read_text())
    assert info["contact"]["gap"] == pytest.approx(1e-5)
    assert info["contact"]["faces"] and len(info["contact"]["interfaces"]) == 1
    assert sol.result["optimization_converged"]
    assert sum(0.1 * i**2 for i in sol.currents.values()) == pytest.approx(0.1)
    assert np.isfinite(sol.flux_density([3, 0, -0.5])).all()
    fixed = motor.solve(
        current=sol.currents,
        balanced=False,
        mesh_size=1.2,
        air_radius=25,
        threads=2,
        device="cpu",
    ).wait(timeout=120)
    assert fixed.torque == pytest.approx(sol.torque, rel=1e-5, abs=1e-10)
