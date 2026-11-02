# Examples

## Minimal motor

A BLDC inrunner with six tooth coils and four curved magnets. Two opposite
coils share each phase. Run `python3 examples/simple/run.py` with the included
[motor.step](simple/motor.step). [Sources and assumptions](simple/README.md).

Recorded solve time: **1 min 11 s on GPU**, with 8 CPU threads.

<img src="simple/model-3d.png" height="260" alt="3D inrunner model"> <img src="simple/field.png" height="260" alt="Inrunner field">

```python
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
```

`solve()` returns immediately. `status`, `progress`, `pause()` and `resume()`
work while it solves; `wait()` is optional.

## Benchmarks

The step2torque simulator is benchmarked on publicly available models as a sanity check. Detailed sources, assumptions and results are in each benchmark README.

| CAD | Model and reproduction | Original source | Difference | Solve time | Why included |
|---|---|---|---:|---:|---|
| [<img src="benchmarks/lrk/model-3d.png" width="240" alt="LRK motor CAD">](benchmarks/lrk/model-3d.png) | [FEMM LRK motor](benchmarks/lrk/README.md) · [run.py](benchmarks/lrk/run.py) | [FEMM Sliding Band Benchmark](https://www.femm.info/doku/doku.php?id=SlidingBandBenchmark) | -0.305% | 55 s<br>CPU, 8 CPU threads | Tests nonlinear radial PM torque with released geometry, materials and winding currents. |
| [<img src="benchmarks/axial/model-3d.png" width="240" alt="Unrolled axial-flux motor CAD">](benchmarks/axial/model-3d.png) | [Unrolled axial-flux motor](benchmarks/axial/README.md) · [run.py](benchmarks/axial/run.py) | [Nyitrai–Orosz paper](https://www.pp.bme.hu/eecs/article/view/17755) · [Original Ārtap model](https://raw.githubusercontent.com/artap-framework/artap/96f4484f448649dc2a8aa9772c03f39f2cb62372/examples/EM_optimization_with_FEMM/AF_AntiPeriodic.fem) | -0.542% | 55 s<br>CPU, 4 CPU threads | Tests axial PM cogging force, nonlinear steel and periodic boundaries. |
| [<img src="benchmarks/analytic/sphere-model-3d.png" width="240" alt="PM sphere and coil CAD">](benchmarks/analytic/sphere-model-3d.png) | [PM sphere and coil](benchmarks/analytic/README.md) · [run.py](benchmarks/analytic/run.py) | [Dipole torque](https://www.feynmanlectures.caltech.edu/II_15.html#Ch15-S1) | -0.529% | 1 min 14 s<br>GPU, 8 CPU threads | Tests the full STEP interface, current direction and mechanical-angle response against an analytic solution. |
| [<img src="benchmarks/analytic/ring-model-3d.png" width="240" alt="Two-pole magnet ring CAD">](benchmarks/analytic/ring-model-3d.png) | [Two-pole magnet ring](benchmarks/analytic/README.md) · [run.py](benchmarks/analytic/run.py) | [Dipole torque](https://www.feynmanlectures.caltech.edu/II_15.html#Ch15-S1) | -0.0275% | 1 min 03 s<br>GPU, 8 CPU threads | Tests radial pole assignment, shaft orientation and pole offset. |
| [<img src="benchmarks/spheres/model-3d.png" width="240" alt="Two offset magnetized spheres CAD">](benchmarks/spheres/model-3d.png) | [Exact 3D sphere interaction](benchmarks/spheres/README.md) · [run.py](benchmarks/spheres/run.py) | [Edwards et al. exact sphere solution](https://digitalcommons.usu.edu/physics_facpub/2033/) | -0.101% | 2 min 24 s<br>GPU, 4 CPU threads | Tests exact finite-magnet torque in a 3D field with offset centers and nonparallel magnetizations. |
| [<img src="benchmarks/team20/model-3d.png" width="240" alt="TEAM 20 yoke, pole and winding CAD cutaway">](benchmarks/team20/model-3d.png) | [TEAM 20 3D static force](benchmarks/team20/README.md) · [run.py](benchmarks/team20/run.py) | [Nakata et al. measurements](https://www.integratedsoft.com/documents/tech_3ax.pdf#page=2) | +0.58% to +6.24%<br>4 currents | 4 min 24 s initial solve<br>GPU, 4 CPU threads | Tests measured 3D force with nonlinear steel, unequal pole/yoke depths and finite winding ends. |
| [<img src="benchmarks/reluctance/model-3d.png" width="240" alt="Nonlinear iron rotor and coils CAD">](benchmarks/reluctance/model-3d.png) | [Nonlinear iron rotor](benchmarks/reluctance/README.md) · [run.py](benchmarks/reluctance/run.py) | [Ellipsoid demagnetization](https://doi.org/10.1103/PhysRev.67.351) | +0.214% | 2 min 11 s<br>GPU, 8 CPU threads | Tests nonlinear reluctance torque below and above magnetic saturation. |
| [<img src="benchmarks/gear/model-3d.png" width="240" alt="Axial-field magnetic gear CAD">](benchmarks/gear/model-3d.png) | [Axial-field magnetic gear](benchmarks/gear/README.md) · [run.py](benchmarks/gear/run.py) | [COMSOL magnetic gear](https://www.comsol.com/model/axial-field-magnetic-gear-in-3d-22301) | <0.78% over 7 poses | 2 min 1 s initial pose<br>GPU, 8 CPU threads | Tests two rotor torques, axial gaps and finite 3D fringing fields. |

## Commercial case studies

6 radial flux motors are evaluated in step2torque and compared to the torque calculated from their catalog specifications. Comparisons use **5.5 W total copper loss at 20°C**. Catalog torques are converted to this loss using the current and resistance assumptions, as detailed in each case README.

| Model / field | Motor and sources | Catalog equivalent, N·m | Simulated, N·m | Difference | Solve time\* |
|---|---|---:|---:|---:|---:|
| <img src="commercial/tq/model-3d.png" width="240" alt="tq 3D model"> <img src="commercial/tq/field.png" width="240" alt="tq field"> | [TQ ILM50x14](commercial/tq/README.md) · [Original source](https://www.tq-group.com/filedownloads/files/products/robodrive/extended_data-sheets/DRVA_DB-ILM-Kits_ILM50x14_Rev201.pdf) | 0.31895 | 0.31612 | -0.9% | 2 min 58 s<br>GPU, 4 CPU threads |
| <img src="commercial/mosrac/model-3d.png" width="240" alt="mosrac 3D model"> <img src="commercial/mosrac/field.png" width="240" alt="mosrac field"> | [Mosrac U5019](commercial/mosrac/README.md) · [Original source](https://www.mosrac.com/products/frameless-direct-drive-torque-motors/u50-series.html) | 0.25952 | 0.24661 | -5.0% | 2 min 55 s<br>GPU, 4 CPU threads |
| <img src="commercial/kollmorgen/model-3d.png" width="240" alt="kollmorgen 3D model"> <img src="commercial/kollmorgen/field.png" width="240" alt="kollmorgen field"> | [Kollmorgen TBM2G-05013A](commercial/kollmorgen/README.md) · [Original source](https://www.kollmorgen.com/sites/default/files/2025-12/TBM2G-KM_SG_00396_RevB_EN.pdf) | 0.19656 | 0.20251 | +3.0% | 7 min 28 s<br>GPU, 4 CPU threads |
| <img src="commercial/cubemars/model-3d.png" width="240" alt="cubemars 3D model"> <img src="commercial/cubemars/field.png" width="240" alt="cubemars field"> | [CubeMars RI60 KV120](commercial/cubemars/README.md) · [Original source](https://www.cubemars.com/goods-857-RI60%2BKV120.html) | 0.20184 | 0.19805 | -1.9% | 4 min 14 s<br>GPU, 4 CPU threads |
| <img src="commercial/steadywin/model-3d.png" width="240" alt="steadywin 3D model"> <img src="commercial/steadywin/field.png" width="240" alt="steadywin field"> | [Steadywin WK4310: segmented magnets](commercial/steadywin/README.md) · [Original source](https://steadywin-motor.com/products/wholesale-mini-size-11mm-dc-brushless-frameless-motor-for-gimbal-camera) | 0.16791 | 0.18038 | +7.4% | 1 min 52 s<br>GPU, 8 CPU threads |
| <img src="commercial/steadywin/ring-model-3d.png" width="240" alt="steadywin 3D model"> <img src="commercial/steadywin/ring.png" width="240" alt="steadywin field"> | [Steadywin WK4310CH: bonded ring](commercial/steadywin/README.md) · [Original source](https://steadywin-motor.com/products/wholesale-mini-size-11mm-dc-brushless-frameless-motor-for-gimbal-camera) | 0.08395 | 0.08083 | -3.7% | 1 min 40 s<br>GPU, 8 CPU threads |

\* Recorded wall times on an AMD EPYC 7J13 / NVIDIA A100 40 GB.

### Parameter uncertainty
<img src="commercial/torque.png" height="400" alt="Torque versus envelope volume with parameter uncertainty">


Inputs were perturbed within engineering ranges—e.g. ±0.03 mm gaps, ±0.05–0.10 mm hidden steel dimensions and ±5% magnet remanence (±10% bonded)—then combined low/high cases were solved at the original five positions ([ranges and results](commercial/uncertainty.json)). Blue dots retain the nominal torque at 5.5 W copper loss, bars show sampled min/max means, orange diamonds mark catalog equivalents, and volume uses the full cylindrical envelope including the bore.
