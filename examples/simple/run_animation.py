from concurrent.futures import ProcessPoolExecutor
from math import cos, radians, sin
from multiprocessing import get_context
import os
from pathlib import Path

from PIL import Image
from step2torque import Motor

threads = min(4, os.cpu_count() or 1)
motor = Motor(Path(__file__).with_name("motor.step"))
motor.components["stator"].set_material("1J22")

# Six tooth coils, 60 degrees apart, connected as three phases.
for k, phase in enumerate(("A", "B", "C", "A", "B", "C")):
    angle = radians(60 * k)
    motor.components[f"coil{k + 1}"].make_coil(
        phase=phase, turns=30, direction=(cos(angle), sin(angle), 0)
    )

# Four curved magnets alternate north-out and south-out.
for k in range(4):
    motor.components[f"magnet{k + 1}"].make_magnet(
        direction="radial" if k % 2 == 0 else "-radial"
    )


def frame(k):
    # Two pole pairs: one electrical cycle is 180 mechanical degrees.
    sol = motor.solve(angle=180 * k / 24, power=5.5, threads=threads)
    sol.wait()
    path = sol.path / "frame.png"
    sol.render_image(path, vmax=3, bounds=(-22, -22, 22, 22), resolution=400, dpi=100)
    print(f"Frame {k + 1}/24", flush=True)
    with Image.open(path) as image:
        return image.convert("RGB")


if __name__ == "__main__":
    # Share CPU cores between frames; the solver uses the GPU when available.
    os.environ.update(
        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1"
    )
    with ProcessPoolExecutor(
        max_workers=min(24, (os.cpu_count() or 1) // threads),
        mp_context=get_context("spawn"),
    ) as pool:
        frames = list(pool.map(frame, range(24)))

    # A shared palette keeps the colours steady throughout the loop.
    palette = frames[0].quantize()
    frames = [
        image.quantize(palette=palette, dither=Image.Dither.NONE) for image in frames
    ]
    path = Path(__file__).with_name("animation.gif")
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=320, loop=0, disposal=2
    )
    print(path)
