# step2torque

Nonlinear 3D motor torque from a named STEP assembly.

[Open the browser viewer](https://qwertpas.github.io/step2torque/)

| Model | Flux density |
|---|---|
| <img src="examples/simple/model-3d.png" width="400" alt="Motor CAD view"> | <img src="examples/simple/field.png" width="400" alt="Motor flux section"> |

```sh
python3 -m pip install step2torque
```

```python
from step2torque import Motor

motor = Motor("motor.step")
print(motor.components)  # CAD component names
motor.components["stator"].set_material("1J22")
motor.components["coil"].make_coil(phase=1, turns=72, direction="+z")
motor.components["magnet"].make_magnet(direction="radial", br=1.2)

sol = motor.solve(power=5.5)  # returns immediately; optimizes currents
print(sol.status, sol.progress)
sol.wait()                  # optional blocking wait
print(sol.torque, sol.currents)  # N·m, instantaneous phase A
print(sol.flux_density([[0, 0, 0]]))  # B in tesla, points in CAD mm
```

`sol.pause()` requests a saved field and releases the worker at the next safe
boundary. `sol.resume()` continues. `Solution.open(sol.path)` reconnects later.
`sol.error` contains a concise failure reason. Failed solves never return torque.
Runs are saved automatically; `sol.path` is the persistent checkpoint folder.

Render and share the solved field:

```python
sol.render_image("field.png")
sol.render_html("motor.html")
sol.export("motor.s2t")
```

Open the HTML directly, or load the `.s2t` file in the browser viewer.

Use `plane=Plane.axial(z=0)` or `Plane.longitudinal(angle=30)` to select a section
in Python (`from step2torque import Plane`). Distances are mm relative to the
motor center, with the shaft along z.

`motor.solve(angle=10, current={1: 2.0})` rotates moving parts 10 mechanical
degrees from the STEP and uses fixed current. Use `current={}` for cogging.
Multiphase currents sum to zero by default; `balanced=False` permits independent
phase drives. Coils sharing a phase are in series; resistance is per coil.

Coils are circular or rectangular annuli; direction is their right-hand axis.
Supply turns or resistance; the other uses CAD dimensions, 60% copper fill
and 20°C. Magnets accept a global vector, `"radial"`, or `"-radial"`.
For a multipole ring, use `make_magnet(direction="radial", poles=20)`;
`offset=5` shifts its pole pattern by 5 mechanical degrees.
Other components default to steel. Rotor-named
components and magnets rotate. `Motor(..., axis="+z", center=(0,0,0))` changes
the shaft axis and center in mm. Periodic symmetry is detected automatically.
Separate touching iron parts use a 10 µm surface contact. `Motor(...,
contact_gap=0.01)` sets this gap in mm; use zero for perfect magnetic contact.

CPU acceleration is included. `python3 -m pip install 'step2torque[cuda]'` enables NVIDIA
cuDSS; `device="auto"` selects an available device. Optional solve arguments:
`threads=8`, `mesh_size=0.6` (surface mm), `output="saved-solve"`.
`air_radius=100` (mm) changes the outer air boundary for convergence checks.

[Examples, benchmarks and commercial case studies](examples/README.md)

