from math import cos, radians, sin
from pathlib import Path
from step2torque import Motor

motor = Motor(Path(__file__).with_name("motor.step"))
motor.components["stator"].set_material("1J22")

# Six tooth coils, 60 degrees apart, connected as three phases.
for k, phase in enumerate(("A", "B", "C", "A", "B", "C")):
    angle = radians(60 * k)
    motor.components[f"coil{k+1}"].make_coil(
        phase=phase, turns=30, direction=(cos(angle), sin(angle), 0)
    )

# Four curved magnets alternate north-out and south-out.
for k in range(4):
    motor.components[f"magnet{k+1}"].make_magnet(
        direction="radial" if k % 2 == 0 else "-radial"
    )

sol = motor.solve(power=5.5)  # Optimizes currents at 5.5 W copper loss.
print(sol.status)
sol.wait()
print(sol.torque)  # N·m about the shaft (+z).
