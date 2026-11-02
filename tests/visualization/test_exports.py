import base64
import json
import re
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from step2torque import Plane, Solution, Viewer
from step2torque.visualization.native import MU0
from step2torque.visualization.lines import streamlines
from step2torque.visualization.archive import read


def test_field_materials_and_outside(saved_field):
    view = Viewer(saved_field)
    points = np.array(
        [[-3, 0.25, 0.5], [-1, 0.25, 0.5], [1, 0.25, 0.5], [3, 0.25, 0.5]]
    )
    samples = view.sample(points)
    expected_h = np.array(
        [[-200, 220, 300], [0, 220, 300], [200, 220, 300], [400, 220, 300]]
    )
    np.testing.assert_allclose(samples["h"], expected_h, atol=1e-9)
    np.testing.assert_allclose(
        samples["b"][[0, 3]], MU0 * expected_h[[0, 3]], atol=1e-12
    )
    np.testing.assert_allclose(
        samples["b"][2], MU0 * 1.05 * expected_h[2] + [0.5, 0.1, 0], atol=1e-12
    )
    magnitude = np.linalg.norm(expected_h[1])
    expected_b = 0.5 + (magnitude - 100) / 900
    np.testing.assert_allclose(np.linalg.norm(samples["b"][1]), expected_b, atol=1e-12)
    np.testing.assert_allclose(
        samples["loading"][1],
        (expected_b - MU0 * magnitude) / (2 - MU0 * 10000) * 100,
        atol=1e-10,
    )
    assert np.isnan(samples["loading"][[0, 2, 3]]).all()
    assert samples["material"].tolist() == [0, 1, 2, 3]
    outside = view.sample([20, 0, 0])
    assert outside["material"] == -1
    assert np.isnan(outside["b"]).all()


def test_world_coordinates_and_arbitrary_sections(saved_field):
    spec = json.loads((saved_field / "run.json").read_text())
    spec.update(axis=[0, 1, 0], center=[30, -20, 10])
    (saved_field / "run.json").write_text(json.dumps(spec))
    view = Viewer(saved_field)
    local = np.array([[1, 0.2, 0.3], [-1, -0.4, 0.5]])
    world = local @ view.field.rotation + spec["center"]
    local_b = view.sample(local)["b"]
    np.testing.assert_allclose(
        view.sample(world, world=True)["b"], local_b @ view.field.rotation, atol=1e-11
    )
    for plane in [Plane.axial(0.7), Plane.longitudinal(37), Plane(71, 43, 0.35)]:
        u, v, n = plane.frame
        np.testing.assert_allclose(np.cross(u, v), n, atol=1e-14)
        section = view.section(plane, resolution=90)
        assert section.b.shape[:2] == (len(section.y), len(section.x))
        assert len(section.outlines) > 10
        assert np.isfinite(section.b).any()


def test_png_and_html_end_to_end(saved_field, tmp_path):
    view = Viewer(saved_field)
    for metric in ("flux", "loading"):
        png = view.png(
            tmp_path / f"{metric}.png",
            plane=Plane(29, 65, 0.1),
            metric=metric,
            resolution=130,
        )
        image = Image.open(png)
        assert image.width > 500 and image.height > 300
        assert np.asarray(image).std() > 20
    output = view.html(tmp_path / "field.html", plane=Plane.longitudinal(19))
    document = output.read_text()
    portable = tmp_path / "field.s2t"
    portable.write_bytes(
        base64.b64decode(re.search(r'id="fieldData">(.*?)</script>', document).group(1))
    )
    exported = view.export(tmp_path / "export.s2t", plane=Plane.longitudinal(19))
    assert exported.read_bytes() == portable.read_bytes()
    metadata, arrays = read(portable)
    assert metadata["plane"]["angle"] == 19
    assert metadata["units"] == dict(position="mm", h="A/m", b="T")
    assert metadata["surfaces"]
    cells, points = arrays["cells"].reshape(-1, 4), arrays["points"].reshape(-1, 3)
    h = arrays["h"].reshape(-1, 4, 3)
    expected = points[cells].astype(float)
    expected[..., 0] = 100 + expected[..., 0] * 100
    expected[..., 1], expected[..., 2] = 220, 300
    np.testing.assert_allclose(h, expected, atol=2e-4)
    assert "<script src=" not in document
    assert "<link" not in document
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "step2torque.visualization",
            str(saved_field),
            str(tmp_path / "cli.png"),
            "--plane",
            "longitudinal",
            "--angle",
            "41",
            "--resolution",
            "80",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cli.png").is_file()


def test_portable_solution_roundtrip(saved_field, tmp_path):
    native = Viewer(saved_field)
    path = native.export(tmp_path / "motor.s2t", bounds=(-2, -1, 2, 1), mode="2d")
    view = Viewer(path)
    assert view.field.meta["bounds"] == [-2, -1, 2, 1]
    ids = np.arange(0, len(native.field.cells), 3)
    points = native.field.points[native.field.cells[ids]].mean(axis=1)
    expected = native.sample(points)
    result = view.sample(points)
    np.testing.assert_array_equal(result["material"], expected["material"])
    np.testing.assert_allclose(result["b"], expected["b"], atol=2e-6)
    np.testing.assert_allclose(result["loading"], expected["loading"], atol=2e-4)
    assert view.sample([1000, 0, 0])["material"] == -1
    view.png(tmp_path / "portable.png", plane=Plane(20, 50, 0.2), resolution=80)
    view.html(tmp_path / "portable.html")
    path.write_bytes(b"not a solution")
    with pytest.raises(ValueError, match="solution"):
        Viewer(path)


def test_main_solution_exports(saved_field, tmp_path):
    (saved_field / "status.json").write_text('{"status":"completed"}')
    sol = Solution.open(saved_field)
    assert sol.render_image(tmp_path / "main.png", resolution=80).is_file()
    assert sol.render_html(tmp_path / "main.html").is_file()
    data = sol.export(tmp_path / "main.s2t")
    np.testing.assert_allclose(
        Viewer(data).sample([1, 0.25, 0.5])["b"],
        Viewer(saved_field).sample([1, 0.25, 0.5])["b"],
        atol=2e-6,
    )
    (saved_field / "status.json").write_text('{"status":"paused"}')
    with pytest.raises(ValueError, match="completes"):
        sol.export(tmp_path / "paused.s2t")


def test_incomplete_and_invalid_inputs(tmp_path, saved_field):
    with pytest.raises(ValueError, match="missing"):
        Viewer(tmp_path / "absent")
    view = Viewer(saved_field)
    with pytest.raises(ValueError, match="finite"):
        view.sample([float("nan"), 0, 0])
    with pytest.raises(ValueError, match="positive"):
        view.png(tmp_path / "bad.png", vmax=0)
    with pytest.raises(ValueError, match="tilt"):
        Plane(tilt=181)


def test_flux_weighted_line_spacing():
    x = y = np.linspace(-1, 1, 70)
    vx = np.where(np.meshgrid(x, y)[1] < 0, 1.0, 4.0)
    lines = streamlines(
        x, y, vx, np.zeros_like(vx), np.zeros_like(vx, dtype=int), density=60
    )
    low = sum(line[0, 1] < -0.1 for line in lines)
    high = sum(line[0, 1] > 0.1 for line in lines)
    assert 2.8 * low < high < 5 * low


def test_saved_current_mapping_and_direct_benchmark_field(saved_field):
    result = json.loads((saved_field / "result.json").read_text())
    result["currents_A"] = {"1": 2}
    (saved_field / "result.json").write_text(json.dumps(result))
    view = Viewer(saved_field)
    materials = {m["domain"]: m for m in view.field.materials}
    direct = Viewer.from_field(view.field.mesh, view.field.h, materials)
    points = [[-3, 0.25, 0.5], [-1, 0.25, 0.5], [1, 0.25, 0.5], [3, 0.25, 0.5]]
    np.testing.assert_allclose(
        direct.sample(points)["b"], view.sample(points)["b"], atol=1e-12
    )
    section = direct.section(Plane.axial(0.15), resolution=100)
    assert len(section.outlines) > 10
    assert {s["kind"] for s in direct.field.surfaces} == {"iron", "magnet", "coil"}


def test_weak_fields_have_sparse_lines():
    x = y = np.linspace(-1, 1, 70)
    vx = np.full((70, 70), 0.04)
    assert (
        len(
            streamlines(
                x,
                y,
                vx,
                np.zeros_like(vx),
                np.zeros_like(vx, dtype=int),
                density=80,
                reference=4,
            )
        )
        <= 2
    )


def test_periodic_reference_mesh_has_no_artificial_cut_outline():
    import netgen.occ as occ
    import ngsolve as ng

    shape = occ.Box(occ.Pnt(-4, 0, -2), occ.Pnt(4, 4, 2))
    shape.mat("steel")
    native = occ.OCCGeometry(shape).GenerateMesh(maxh=2)
    native.Scale(0.001)
    view = Viewer.from_field(
        ng.Mesh(native),
        ng.CF((0, 0, 100)),
        {"steel": dict(kind="iron", mur=1)},
        sectors=2,
    )
    section = view.section(Plane.axial(), resolution=60)
    assert len(section.outlines) > 0
    assert not np.any(np.all(np.abs(section.outlines[:, :, 1]) < 1e-7, axis=1))
