"""WK4310: sources and assumptions in README.md."""

import argparse
from math import cos, sin, pi
from pathlib import Path
from step2torque import Motor

parser = argparse.ArgumentParser()
parser.add_argument("--ring", action="store_true", help="Use the bonded-ring rotor")
args = parser.parse_args()

here = Path(__file__).resolve().parent
motor = Motor(here / ("ring.step" if args.ring else "motor.step"))
motor.components["stator"].set_material(here.parents[1] / "materials/M19.csv")

phases = (
    1, -1, -3, 3, 2, -2, -1, 1, 3, -3, -2, 2,
    1, -1, -3, 3, 2, -2, -1, 1, 3, -3, -2, 2,
)
for k, phase in enumerate(phases):
    sign = 1 if phase > 0 else -1
    a = 2 * pi * k / len(phases)
    motor.components[f"coil{k}"].make_coil(
        phase=abs(phase), direction=(sign * cos(a), sign * sin(a), 0),
        turns=60, resistance=0.645,
    )

if args.ring:
    motor.components["magnet_ring"].make_magnet(
        direction="radial", poles=28, offset=-1.40525,
        br=0.68, mur=1.2,
    )
else:
    for k in range(28):
        motor.components[f"magnet{k}"].make_magnet(
            direction="radial" if k % 2 == 0 else "-radial", br=1.32,
        )

sol = motor.solve(angle=0, power=5.5, mesh_size=0.8, air_radius=98)
print(sol.status, sol.progress)
sol.wait()
print({"torque_Nm": sol.torque, "phase_A": sol.currents})
