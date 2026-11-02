"""Small physical end-to-end checks, including real worker checkpoint/restart."""

import json
import math
import time

import netgen.occ as occ
import numpy as np
import psutil
import pytest

from step2torque import Motor, Solution, solve_position
from step2torque._mesh import coil_geometry
from step2torque.materials import MU0
from step2torque._source import rectangular_source, current_cells


def test_saturated_sphere_against_demagnetizing_solution(tmp_path):
    from scipy.optimize import brentq
    from step2torque.materials import material

    shapes = []
    for k, z in enumerate([-10, 10]):
        coil = occ.Cylinder(occ.Pnt(0, 0, z - 0.5), occ.Z, 21, 1) - occ.Cylinder(
            occ.Pnt(0, 0, z - 0.5), occ.Z, 19, 1
        )
        coil.name = f"coil{k}"
        shapes.append(coil)
    iron = occ.Sphere(occ.Pnt(0, 0, 0), 1)
    iron.name = "rotor"
    path = tmp_path / "saturation.step"
    occ.Compound([*shapes, iron]).WriteStep(str(path))
    motor = Motor(path)
    motor.components["rotor"].set_material("1J22", rotating=True)
    for k in range(2):
        motor.components[f"coil{k}"].make_coil(turns=100, direction="+z")
    sol = motor.solve(current=400, mesh_size=1.2, threads=2, device="cpu").wait(
        timeout=120
    )
    q, w = np.polynomial.legendre.leggauss(32)
    radius, z = (0.020 + 0.001 * q)[:, None], (0.010 + 0.0005 * q)[None, :]
    applied = (
        MU0
        * 40000
        * np.sum(w[:, None] * w[None, :] * radius**2 / (radius**2 + z**2) ** 1.5)
        / 4
    )
    data = material("1J22")

    def flux(h):
        return np.interp(h, data["h"], data["b"]) + MU0 * max(h - data["h"][-1], 0)

    # A sphere has demagnetizing factor 1/3, independent of the field solver.
    h = brentq(lambda h: h + (flux(h) / MU0 - h) / 3 - applied / MU0, 0, 1e7)
    assert h > 1e5  # Exercise the saturated part of the supplied B-H curve.
    assert sol.flux_density([0, 0, 0])[2] == pytest.approx(flux(h), rel=0.01)


@pytest.fixture
def motor(tmp_path):
    coil = occ.Cylinder(occ.Pnt(0, 0, -2), occ.Z, 10, 4) - occ.Cylinder(
        occ.Pnt(0, 0, -2), occ.Z, 8, 4
    )
    magnet = occ.Sphere(occ.Pnt(0, 0, 0), 2)
    coil.name, magnet.name = "coil", "magnet"
    path = tmp_path / "motor.step"
    occ.Compound([coil, magnet]).WriteStep(str(path))
    m = Motor(path, axis="+y")
    m.components["coil"].make_coil(turns=50, direction="+z", resistance=0.1)
    m.components["magnet"].make_magnet(direction="+x", mur=1.0)
    return m


def test_step_names_and_errors(motor):
    assert list(motor.components) == ["coil", "magnet"]
    motor.components["coil"].make_coil(turns=50)
    with pytest.raises(ValueError, match="coil: coil direction is required"):
        motor.solve()
    with pytest.raises(ValueError, match="Unknown material"):
        motor.components["magnet"].set_material("unobtainium")
    motor.components["coil"].make_coil(direction="+z")
    with pytest.raises(ValueError, match="coil: supply turns or resistance"):
        motor.solve()


def test_rectangular_winding_from_cad():
    shape = occ.Box(occ.Pnt(-2, -4, -5), occ.Pnt(2, 4, 5)) - occ.Box(
        occ.Pnt(-3, -2, -3), occ.Pnt(3, 2, 3)
    )
    c = coil_geometry(shape, [1, 0, 0], "winding")
    assert c["geometry"] == "rectangle"
    assert c["height"] == pytest.approx(0.004, abs=1e-9)


def test_large_mesh_current_support():
    from ngsolve.meshes import MakeStructured3DMesh
    from netgen.meshing import Mesh, MeshPoint, Pnt, Element3D
    import ngsolve as ng

    original = MakeStructured3DMesh(hexes=False, nx=47)
    # CAD mesh numbering has no spatial order. Permuting a regular mesh keeps
    # its geometry exact while exposing 32-bit pair-key collisions reliably.
    order = np.random.default_rng(1).permutation(original.nv)
    inverse = np.argsort(order)
    native = Mesh(dim=3)
    for p in np.asarray(original.ngmesh.Coordinates())[order]:
        native.Add(MeshPoint(Pnt(*p)))
    for cell in original.ngmesh.Elements3D().NumPy()["nodes"]:
        native.Add(Element3D(index=1, vertices=(inverse[cell - 1] + 1).tolist()))
    native.SetMaterial(1, "air")
    mesh = ng.Mesh(native)
    coil = dict(
        axis=[0, 0, 1],
        center=[0.5, 0.5, 0.5],
        height=0.3,
        inner_x=0.12,
        outer_x=0.25,
        inner_y=0.12,
        outer_y=0.25,
    )
    field = rectangular_source(mesh, coil)
    active = current_cells(mesh, field)
    points = np.asarray(mesh.ngmesh.Coordinates())
    cells = mesh.ngmesh.Elements3D().NumPy()["nodes"] - 1
    centers = points[cells].mean(axis=1) - 0.5
    assert mesh.nv > 65536 and active.any()
    # Ampere's law confines current to tetrahedra intersecting the copper pack.
    assert np.all(
        abs(centers[active]) <= np.array([0.25, 0.25, 0.15]) + math.sqrt(3) / 47
    )


def test_rectangular_current_stays_in_copper(tmp_path):
    coil = occ.Box(occ.Pnt(-10, -10, -2), occ.Pnt(10, 10, 2)) - occ.Box(
        occ.Pnt(-8, -8, -3), occ.Pnt(8, 8, 3)
    )
    coil.name = "coil"
    pm = occ.Sphere(occ.Pnt(0, 0, 0), 2)
    pm.name = "magnet"
    path = tmp_path / "rectangular.step"
    occ.Compound([coil, pm]).WriteStep(str(path))
    motor = Motor(path, axis="+y")
    motor.components["coil"].make_coil(turns=50, direction="+z", resistance=0.1)
    motor.components["magnet"].make_magnet(direction="+x", mur=1.0)
    sol = motor.solve(current=1.0, mesh_size=0.7, device="cpu", threads=2).wait(
        timeout=120
    )
    q, w = np.polynomial.legendre.leggauss(32)
    a = (0.009 + 0.001 * q)[:, None]
    z = (0.002 * q)[None, :]
    field = 2 * MU0 * 50 * a**2 / (math.pi * (a**2 + z**2) * np.sqrt(2 * a**2 + z**2))
    b = float(np.sum(w[:, None] * w[None, :] * field) / 4)
    exact = -1.2 * (4 * math.pi / 3 * 0.002**3) / MU0 * b
    assert sol.torque == pytest.approx(exact, rel=0.01)
    diagnostics = json.loads((sol.path / "diagnostics.json").read_text())
    assert diagnostics["torque"]["work_source_cells"] == [0]


def test_fixed_current_and_angle(motor):
    # Uppercase suffixes are common in manufacturer CAD downloads.
    upper = motor.path.with_suffix(".STEP")
    upper.write_bytes(motor.path.read_bytes())
    motor = Motor(upper, axis="+y")
    # Resistance alone identifies turns using the CAD copper window.
    volume = math.pi * (0.010**2 - 0.008**2) * 0.004
    resistance = 1.724e-8 * (50 * math.pi * 0.018) ** 2 / (0.6 * volume)
    motor.components["coil"].make_coil(direction="+z", resistance=resistance)
    motor.components["magnet"].make_magnet(direction="+x", mur=1.0)
    started = time.monotonic()
    sol = solve_position(motor, current=1.0, mesh_size=1.0, threads=2, device="cpu")
    assert time.monotonic() - started < 1
    sol.wait(timeout=120)
    pid = json.loads((sol.path / "worker.json").read_text())["pid"]
    until = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < until:
        time.sleep(0.01)
    assert not psutil.pid_exists(pid)
    # A PM sphere in harmonic applied B has the dipole torque m x B.
    # The analytic coil field integrates all radii/heights of the uniform pack.
    b = MU0 * 50 / (2 * 0.002) * (math.asinh(0.010 / 0.002) - math.asinh(0.008 / 0.002))
    exact = -1.2 * (4 * math.pi / 3 * 0.002**3) / MU0 * b
    assert sol.torque == pytest.approx(exact, rel=0.01)
    assert sol.currents == {1: 1.0}
    assert sol.result["copper_loss_W"] == pytest.approx(resistance)
    b = sol.flux_density([0, 0, 0])
    assert 0.7 < b[0] < 0.9
    assert b[2] > 0.002
    assert sol.flux_density([[0, 0, 0], [0, 0, 3]]).shape == (2, 3)
    with pytest.raises(ValueError, match="outside"):
        sol.flux_density([1e6, 0, 0])
    # Zero current is a separate solve, not a reused field/result cache.
    off = motor.solve(
        current={}, angle=12, mesh_size=0.5, device="cpu", threads=2
    ).wait(timeout=120)
    assert abs(off.torque) < abs(exact) * 0.005
    assert off.result["angle_deg"] == 12


def test_multipole_ring_against_dipole_torque(tmp_path):
    shapes = []
    for k, z in enumerate([-10, 10]):
        coil = occ.Cylinder(occ.Pnt(0, 0, z - 0.5), occ.Z, 21, 1) - occ.Cylinder(
            occ.Pnt(0, 0, z - 0.5), occ.Z, 19, 1
        )
        coil.name = f"coil{k}"
        shapes.append(coil)
    ring = occ.Cylinder(occ.Pnt(0, -0.5, 0), occ.Y, 2, 1) - occ.Cylinder(
        occ.Pnt(0, -0.5, 0), occ.Y, 1.5, 1
    )
    ring.name = "magnet"
    shapes.append(ring)
    path = tmp_path / "ring.step"
    occ.Compound(shapes).WriteStep(str(path))
    motor = Motor(path, axis="+y")
    for k in range(2):
        motor.components[f"coil{k}"].make_coil(turns=100, direction="+z")
    motor.components["magnet"].make_magnet(
        direction="radial", poles=2, offset=15, mur=1
    )
    sol = motor.solve(current=1, angle=30, mesh_size=0.6, threads=2, device="cpu").wait(
        timeout=300
    )
    q, w = np.polynomial.legendre.leggauss(32)
    radius = (0.020 + 0.001 * q)[:, None]
    z = (0.010 + 0.0005 * q)[None, :]
    b = (
        MU0
        * 100
        * np.sum(w[:, None] * w[None, :] * radius**2 / (radius**2 + z**2) ** 1.5)
        / 4
    )
    # Integrating the two radial poles gives 2/pi of the uniform dipole moment.
    exact = -2 / math.pi * 1.2 * (ring.mass * 1e-9) / MU0 * b * math.cos(math.pi / 4)
    assert sol.torque == pytest.approx(exact, rel=0.01)


def test_pause_resume_preserves_field(motor):
    sol = motor.solve(
        current="optimized", power=0.1, mesh_size=0.6, device="cpu", threads=2
    )
    until = time.monotonic() + 120
    while sol.progress.get("stage") != "solving" and not sol.done:
        assert time.monotonic() < until
        time.sleep(0.005)
    assert not sol.done
    sol = Solution.open(sol.path)
    sol.pause().wait(timeout=120)
    assert sol.status == "paused"
    assert (sol.path / "checkpoint.sol").exists()
    saved = json.loads((sol.path / "search.json").read_text())
    resumed = Solution.open(sol.path)
    resumed.resume().wait(timeout=120)
    assert resumed.status == "completed"
    assert resumed.torque > 0
    assert resumed.result["copper_loss_W"] == pytest.approx(0.1)
    assert resumed.result["states"] == 2
    assert saved["active"] is not None or saved["trials"]
    assert np.isfinite(resumed.torque)


def test_user_inputs_reject_bad_current(motor):
    with pytest.raises(ValueError, match="every phase"):
        motor.solve(current={2: 0})
    with pytest.raises(ValueError, match="positive"):
        motor.solve(power=-1)
    with pytest.raises(ValueError, match="device"):
        motor.solve(device="invented")
    motor.path.write_bytes(motor.path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="STEP file changed"):
        motor.solve(current=1.0)


def test_three_phase_optimization_reproduces_fixed_current(tmp_path):
    shapes = []
    for j, z in enumerate((-4, 0, 4)):
        c = occ.Cylinder(occ.Pnt(0, 0, z - 0.5), occ.Z, 10, 1) - occ.Cylinder(
            occ.Pnt(0, 0, z - 0.5), occ.Z, 8, 1
        )
        c.name = f"coil{j}"
        shapes.append(c)
    pm = occ.Sphere(occ.Pnt(0, 0, 0), 1.5)
    pm.name = "magnet"
    path = tmp_path / "three_phase.step"
    occ.Compound([*shapes, pm]).WriteStep(str(path))
    motor = Motor(path, axis="+y")
    for j in range(3):
        motor.components[f"coil{j}"].make_coil(
            phase=j, turns=50, direction="+z", resistance=0.1
        )
    motor.components["magnet"].make_magnet(direction="+x", mur=1.0)
    sol = motor.solve(power=0.3, mesh_size=1.0, threads=2, device="cpu").wait(
        timeout=180
    )
    assert sum(sol.currents.values()) == pytest.approx(0, abs=1e-12)
    assert sum(0.1 * i**2 for i in sol.currents.values()) == pytest.approx(0.3)
    # Mirror coils have equal coupling. The balanced maximum drives the center
    # coil at -sqrt(2) A and the outer two at +1/sqrt(2) A.
    assert sol.currents[1] == pytest.approx(-math.sqrt(2), rel=0.01)
    assert sol.result["optimization_converged"]
    fixed = motor.solve(
        current=sol.currents, mesh_size=1.0, threads=2, device="cpu"
    ).wait(timeout=120)
    assert fixed.torque == pytest.approx(sol.torque, rel=1e-5, abs=1e-10)
