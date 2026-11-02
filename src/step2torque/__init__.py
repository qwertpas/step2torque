"""Load a STEP, configure its components, and solve without blocking.

motor = Motor("motor.step")
motor.components["coil"].make_coil(phase=1, turns=72, direction="+z")
motor.components["magnet"].make_magnet(direction="radial")
solution = motor.solve(current="optimized", power=5.5)
solution.wait()
print(solution.torque)  # N m
"""

from .model import Motor
from .solution import Solution, solve_position
from .visualization import Plane, Viewer

__all__ = ["Motor", "Solution", "solve_position", "Plane", "Viewer"]
__version__ = "0.1.0"
