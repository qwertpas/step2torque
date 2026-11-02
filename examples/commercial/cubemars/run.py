"""Approximate commercial geometry; sources and assumptions in README.md."""

from math import cos, sin, pi
from pathlib import Path
from step2torque import Motor

here = Path(__file__).resolve().parent
motor = Motor(here / "motor.step")
motor.components["stator"].set_material(here.parents[1] / "materials/M19.csv")

phases = (
    1, -1, -3, 3, 2, -2, -1, 1, 3, -3, -2, 2,
    1, -1, -3, 3, 2, -2, -1, 1, 3, -3, -2, 2,
)
for k, phase in enumerate(phases):
    sign = 1 if phase > 0 else -1
    a = 2 * pi * k / len(phases)
    motor.components[f"coil{k}"].make_coil(
        phase=abs(phase),
        direction=(sign * cos(a), sign * sin(a), 0),
        turns=27,
        resistance=0.16875,
        fill=0.29048281557,
    )

for k in range(28):
    direction = "-radial" if k % 2 == 0 else "radial"
    motor.components[f"magnet{k}"].make_magnet(
        direction=direction,
        br=1.32,
    )

sol = motor.solve(angle=0, power=5.5, mesh_size=0.8, air_radius=120)
print(sol.status, sol.progress)
sol.wait()
print({"torque_Nm": sol.torque, "phase_A": sol.currents})
