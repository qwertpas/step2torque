# Visualization

```python
from step2torque import Plane, Viewer

sol.render_image("field.png", plane=Plane.axial(z=0))
sol.render_image("loading.png", plane=Plane.longitudinal(angle=30), metric="loading")
sol.render_html("motor.html")
sol.export("motor.s2t")

view = Viewer("motor.s2t")  # Also accepts a Solution or saved solve directory.
view.png("another-section.png", plane=Plane(angle=30, tilt=55, offset=2))
view.html("another-view.html")
```

Open the HTML directly, or load the `.s2t` file in the
[browser viewer](https://qwertpas.github.io/step2torque/). Each contains the complete
3D field and material laws; recipients need no CAD, checkpoints or Python.
The viewer works offline and can save another `.s2t` with the current view.
Solver continuation uses the original checkpoint directory.

Planes use mm relative to the motor center, with the shaft along z. `angle` is
rotation about that axis; `tilt` is the normal's angle from it, in degrees.
`bounds=(left, bottom, right, top)` sets an initial detail crop. Fit or 3D restores
the full model extent. `view.sample(points, world=True)` accepts original CAD mm.

Colour shows full 3D |B| in tesla, or steel polarization relative to the saved
B–H curve's maximum polarization, in percent. Streamlines follow the in-plane B
component through steel, magnets and air; their spacing decreases as flux grows.
`density=` changes line spacing against a fixed flux reference. A 3D field may
leave the section plane. Red magnet sides are north; blue sides are south.

For separately saved reference fields, use
`Viewer.from_field(mesh, h, materials, sectors=1, sector_start=0)`.
The NGSolve mesh uses metres and H uses A/m. Material entries carry the saved
constitutive law plus `name` and `kind`; give distinct magnets distinct names.
Exports preserve material discontinuities using four affine H values per tetrahedron.
Curved meshes are rejected.

```sh
step2torque-view saved-solve field.html
step2torque-view motor.s2t flux.png --plane longitudinal --angle 30
```
