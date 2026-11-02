"""A subprocess owns numerical state; this object reads its small status file."""

import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import psutil

from .model import positive


def implementation():
    digest = hashlib.sha256(version("ngsolve").encode())
    root = Path(__file__).parent
    for path in sorted(root.rglob("*")):
        if path.suffix in (".py", ".csv"):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, allow_nan=False))
    temporary.replace(path)


class Solution:
    """Live solve: status, progress, torque (N m), currents (A), pause(), resume().

    wait(timeout=None) is optional. Solution.open(path) reconnects after Python
    exits. Pausing saves the field at the next safe numerical boundary.
    """

    def __init__(self, path):
        self.path = Path(path).resolve()
        self._process = None

    @classmethod
    def open(cls, path):
        solution = cls(path)
        if not (solution.path / "run.json").is_file():
            raise ValueError(f"No saved solve at {solution.path}")
        return solution

    def _state(self):
        state = json.loads((self.path / "status.json").read_text())
        if state["status"] in ("queued", "running") and (self.path / "pause").exists():
            state["status"] = "pausing"
        if state["status"] in ("queued", "running", "pausing"):
            stopped = self._process is not None and self._process.poll() is not None
            if self._process is None:
                worker = json.loads((self.path / "worker.json").read_text())
                try:
                    process = psutil.Process(worker["pid"])
                    stopped = (
                        process.status() == psutil.STATUS_ZOMBIE
                        or process.create_time() != worker["started_at"]
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    stopped = True
            if stopped:
                lines = (
                    (self.path / "worker.log").read_text(errors="replace").splitlines()
                )
                state.update(
                    status="failed",
                    error=lines[-1]
                    if lines
                    else "Solver process exited before completion",
                )
        return state

    @property
    def status(self):
        return self._state()["status"]

    @property
    def done(self):
        return self.status in ("completed", "failed", "cancelled")

    @property
    def progress(self):
        return {k: v for k, v in self._state().items() if k not in ("pid", "error")}

    @property
    def error(self):
        return self._state().get("error")

    @property
    def result(self):
        if self.status == "failed":
            raise RuntimeError(self.error)
        p = self.path / "result.json"
        return json.loads(p.read_text()) if p.exists() else None

    @property
    def torque(self):
        result = self.result
        return result["torque_Nm"] if result else None

    @property
    def currents(self):
        result = self.result
        if result is None:
            return None
        phases = json.loads((self.path / "run.json").read_text())["phases"]
        return dict(zip(phases, result["currents_A"]))

    def flux_density(self, points_mm):
        """Return B in tesla at a point or array of points in global CAD mm."""
        if self.result is None:
            raise ValueError("Flux density is available after the solve completes")
        from ._results import flux_density

        return flux_density(self.path, points_mm)

    def render_image(self, path, **options):
        """Render a field cross section to PNG after completion."""
        return self._viewer().png(path, **options)

    def render_html(self, path, **options):
        """Write one interactive HTML file that opens offline in a browser."""
        return self._viewer().html(path, **options)

    def export(self, path, **options):
        """Write one portable .s2t solution for the browser viewer."""
        return self._viewer().export(path, **options)

    def _viewer(self):
        if self.status != "completed":
            raise ValueError("Visualization is available after the solve completes")
        from .visualization import Viewer

        return Viewer(self)

    def wait(self, timeout=None):
        """Wait for completion or pause; return self, or raise on failure/timeout."""
        start = time.monotonic()
        while self.status not in ("completed", "paused", "failed", "cancelled"):
            if timeout is not None and time.monotonic() - start >= timeout:
                raise TimeoutError("Solve still running")
            time.sleep(0.05)
        if self.status == "failed":
            raise RuntimeError(self.error)
        return self

    def pause(self):
        """Request a checkpoint and worker exit. Poll status for 'paused'."""
        if self.status in ("queued", "running", "pausing"):
            (self.path / "pause").touch()
        return self

    def resume(self):
        """Continue a paused/interrupted solve from saved field and current search."""
        if self.status not in ("paused", "failed"):
            raise ValueError(f"Cannot resume a {self.status} solve")
        (self.path / "pause").unlink(missing_ok=True)
        self._start()
        return self

    def _start(self):
        write_json(self.path / "status.json", dict(status="queued", stage="starting"))
        log = (self.path / "worker.log").open("ab")
        env = dict(
            os.environ,
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
        )
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "step2torque._worker", str(self.path)],
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
            )
            try:
                started = psutil.Process(self._process.pid).create_time()
            except psutil.NoSuchProcess:
                started = None
            write_json(
                self.path / "worker.json",
                dict(pid=self._process.pid, started_at=started),
            )
            # Reap finished workers even when callers only poll status.
            threading.Thread(target=self._process.wait, daemon=True).start()
        finally:
            log.close()

    def __repr__(self):
        status = self.status
        torque = self.torque if status == "completed" else None
        return f"Solution({status}, torque={torque}, path={str(self.path)!r})"


def solve_position(
    motor,
    *,
    angle=0,
    current="optimized",
    power=5.5,
    balanced=True,
    mesh_size=None,
    air_radius=None,
    threads=None,
    device="auto",
    output=None,
):
    """Start a solve immediately; return a live Solution.

    angle: mechanical degrees from STEP; current: 'optimized', scalar A for
    one phase, or {phase: instantaneous_amps}. power: total copper watts for
    optimization. balanced: require sum(phase currents)=0 for multiphase coils.
    mesh_size: optional surface size in mm; air_radius: outer air radius in mm.
    device: auto, cpu, or cuda.
    """
    spec = motor._spec()
    if not math.isfinite(angle):
        raise ValueError("angle must be finite mechanical degrees")
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("device must be auto, cpu, or cuda")
    if threads is not None and (not isinstance(threads, int) or threads < 1):
        raise ValueError("threads must be a positive integer")
    if mesh_size is not None:
        positive(mesh_size, "mesh_size")
    if air_radius is not None:
        positive(air_radius, "air_radius")
    phases = list(
        dict.fromkeys(c["phase"] for c in spec["components"] if c["kind"] == "coil")
    )
    if isinstance(current, str):
        if current != "optimized":
            raise ValueError("current must be 'optimized', amps, or {phase: amps}")
        if not phases:
            raise ValueError(
                "Current optimization needs a coil; use current={} for cogging torque"
            )
        positive(power, "power")
    else:
        if isinstance(current, (int, float)):
            if current == 0:
                current = dict.fromkeys(phases, 0.0)
            elif len(phases) != 1:
                raise ValueError("For multiple phases, supply current={phase: amps}")
            else:
                current = {phases[0]: current}
        elif isinstance(current, dict) and not current:
            current = dict.fromkeys(phases, 0.0)
        if not isinstance(current, dict) or set(current) != set(phases):
            raise ValueError(f"Supply current for every phase: {phases}")
        if not all(
            isinstance(i, (int, float)) and math.isfinite(i) for i in current.values()
        ):
            raise ValueError("Phase currents must be finite amps")
        if balanced and len(phases) > 1 and abs(sum(current.values())) > 1e-9:
            raise ValueError(
                "Phase currents must sum to zero; use balanced=False for independent phases"
            )
        current = [float(current[p]) for p in phases]
    spec.update(
        angle=float(angle),
        current=current,
        phases=phases,
        power=float(power),
        balanced=bool(balanced),
        mesh_size=mesh_size,
        air_radius=air_radius,
        threads=threads,
        device=device,
    )
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "step2torque"
    )
    if output is None:
        cache.mkdir(parents=True, exist_ok=True)
    path = (
        Path(output).expanduser().resolve()
        if output
        else Path(tempfile.mkdtemp(prefix="solve-", dir=cache))
    )
    if output:
        path.mkdir(parents=True, exist_ok=True)
        if any(path.iterdir()):
            raise ValueError(f"Output directory must be empty: {path}")
    shutil.copyfile(motor.path, path / "assembly.step")
    spec["step_sha256"] = hashlib.sha256(
        (path / "assembly.step").read_bytes()
    ).hexdigest()
    if spec["step_sha256"] != motor._step_hash:
        raise ValueError("STEP file changed; reload Motor before solving")
    spec["implementation"] = implementation()
    write_json(path / "run.json", spec)
    solution = Solution(path)
    solution._start()
    return solution
