"""Material data is serialized into each run; external paths are unnecessary."""

import csv
from pathlib import Path

import numpy as np

MU0 = 4e-7 * np.pi


def material(name):
    if isinstance(name, dict):
        if set(name) != {"mur"} or not np.isfinite(name["mur"]) or name["mur"] <= 0:
            raise ValueError(
                "Linear material: use {'mur': positive_relative_permeability}"
            )
        return dict(name="linear", mur=float(name["mur"]))
    key = str(name).lower()
    if key in ("air", "copper", "aluminum"):
        return dict(name=key, mur=1.0)
    files = {
        "steel": "rotor.csv",
        "1018": "rotor.csv",
        "dt4": "dt4.csv",
        "1j22": "1j22.csv",
        "kede_1j21": "kede_1j21.csv",
    }
    path = Path(__file__).parent / files[key] if key in files else Path(name)
    if not path.is_file():
        raise ValueError(
            f"Unknown material {name!r}; use steel, 1J22, DT4, air, a B-H CSV, or {{'mur': ...}}"
        )
    with path.open(newline="") as stream:
        data = list(csv.DictReader(stream))
    try:
        h = [float(row["H_A_per_m"]) for row in data]
        b = [float(row["B_T"]) for row in data]
    except (KeyError, ValueError) as e:
        raise ValueError(f"{path.name}: CSV needs H_A_per_m,B_T columns") from e
    if (
        len(h) < 2
        or h[0] != 0
        or b[0] != 0
        or not np.isfinite([h, b]).all()
        or np.any(np.diff(h) <= 0)
        or np.any(np.diff(b) <= 0)
    ):
        raise ValueError(
            f"{path.name}: B-H data must start at 0,0 and increase strictly"
        )
    return dict(name=str(name), h=h, b=b)
