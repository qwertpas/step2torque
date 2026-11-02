"""40 mm transverse-flux motor; sources and assumptions in README.md."""

from pathlib import Path
from step2torque import Motor

motor = Motor(Path(__file__).with_name("motor.step"))

for phase in "ABC":
    motor.components[f"Phase {phase} - 65 turn foil pack"].make_coil(
        phase=phase, turns=65, direction="+z",
        resistance=0.3706134192773899, fill=6 / 7, temperature=120,
    )
    for cup in (1, 2):
        motor.components[f"{phase}{cup} - simple one-sided cup"].set_material(
            "kede_1j21"
        )
    # Two aligned rows per phase, each with 40 alternating radial magnets.
    for row in (1, 2):
        for pole in range(1, 41):
            motor.components[f"Phase {phase} PM row{row} pole{pole:02d}"].make_magnet(
                direction="radial" if pole % 2 else "-radial", br=1.2,
            )

if __name__ == "__main__":
    # Previous sampled weak pose: 75.9375 electrical degrees / 20 pole pairs.
    sol = motor.solve(angle=75.9375 / 20, power=5.5, mesh_size=0.4, air_radius=65)
    print(sol.status, sol.progress)
    sol.wait()
    print({"torque_Nm": sol.torque, "phase_A": sol.currents})
