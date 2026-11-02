import base64
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image
import pytest

from step2torque import Motor, Viewer
from step2torque.visualization.archive import read


@pytest.fixture
def setup_motor(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An unsolved preview must not mesh or solve a field")

    monkeypatch.setattr(Motor, "solve", forbidden)
    monkeypatch.setattr("step2torque._mesh.prepare", forbidden)
    monkeypatch.setattr("step2torque.solution.Solution._start", forbidden)
    motor = Motor(Path(__file__).resolve().parents[2] / "examples/simple/motor.step")
    for k, phase in enumerate(("A", "B", "C", "A", "B", "C")):
        a = math.radians(60 * k)
        motor.components[f"coil{k + 1}"].make_coil(
            phase=phase, turns=30, direction=(math.cos(a), math.sin(a), 0)
        )
    for k in range(4):
        motor.components[f"magnet{k + 1}"].make_magnet(
            direction="radial" if k % 2 == 0 else "-radial"
        )
    return motor


def test_motor_preview_exports_without_solving(setup_motor, tmp_path):
    current = {"A": 2, "B": -1, "C": -1}
    image = setup_motor.render_image(
        tmp_path / "setup.png", angle=37, current=current, dpi=90
    )
    html = setup_motor.render_html(tmp_path / "setup.html", angle=37, current=current)
    portable = setup_motor.export(tmp_path / "setup.s2t", angle=37, current=current)
    pixels = np.array(Image.open(image).convert("RGB"))
    assert pixels.shape == (630, 810, 3)
    assert np.count_nonzero(np.min(pixels, axis=2) < 200) > 30000
    payload = base64.b64decode(
        re.search(r'id="fieldData">(.*?)</script>', html.read_text()).group(1)
    )
    assert payload == portable.read_bytes()
    meta, arrays = read(portable)
    assert meta["solved"] is False
    assert meta["current_flow"] is True
    assert meta["angle_deg"] == 37
    assert meta["metric"] is None and meta["result"] == {}
    assert meta["units"] == {"position": "mm"}
    assert {"h", "cells", "material", "nodes", "bounds"}.isdisjoint(arrays)
    assert len(meta["coils"]) == 6
    for coil in meta["coils"]:
        assert coil["current_A"] == current[coil["phase"]]
        assert coil["turns"] == 30
    assert {s["name"] for s in meta["surfaces"] if s["rotating"]} == {
        "rotor",
        "magnet1",
        "magnet2",
        "magnet3",
        "magnet4",
    }
    for surface in meta["surfaces"]:
        assert len(arrays[surface["array"]]) > 0
        assert arrays[surface["array"]].max() < len(arrays["points"]) / 3
        if surface["kind"] == "magnet":
            assert surface["pole"]["rule"] == "radial"


def test_unsolved_roundtrip_and_field_errors(setup_motor, tmp_path):
    path = setup_motor.export(tmp_path / "motor.s2t", angle=-23)
    view = Viewer(path)
    assert not view.field.solved
    assert all(c["current_A"] == 1 for c in view.field.coils)
    again = view.export(tmp_path / "again.s2t")
    assert path.read_bytes() == again.read_bytes()
    view.png(tmp_path / "again.png", dpi=60)
    for operation in [
        lambda: view.sample([0, 0, 0]),
        lambda: view.section(),
        lambda: view.png(tmp_path / "flux.png", metric="flux"),
        lambda: view.html(tmp_path / "loading.html", metric="loading"),
    ]:
        with pytest.raises(ValueError, match="[Uu]nsolved|solved field"):
            operation()


def test_setup_uses_motor_coordinates_and_source_axes(setup_motor):
    setup_motor.axis = [0, 1, 0]
    setup_motor.center = [3, -2, 1]
    view = Viewer(setup_motor)
    coil = view.field.coils[0]
    points = np.array(coil["points"])
    original = np.array(tuple(setup_motor.components[coil["name"]].shape.center))
    expected = view.field.rotation @ (original - setup_motor.center)
    np.testing.assert_allclose(points.mean(axis=0), expected, atol=1e-9)
    area = np.cross(points, np.roll(points, -1, axis=0)).sum(axis=0)
    np.testing.assert_allclose(
        area / np.linalg.norm(area), view.field.rotation @ [1, 0, 0], atol=1e-12
    )


def test_preview_input_errors(setup_motor, tmp_path):
    for options in [
        dict(current="optimized"),
        dict(current={"A": 1}),
        dict(current=float("nan")),
        dict(angle=float("nan")),
    ]:
        with pytest.raises(ValueError):
            setup_motor.export(tmp_path / "bad.s2t", **options)
