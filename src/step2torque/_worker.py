"""One worker per Solution. Main flow: mesh -> sources -> torque -> field states."""

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

import ngsolve as ng
import numpy as np

from . import _contact, _currents, _field, _mesh, _motion, _source, _torque
from .materials import MU0
from .solution import write_json, implementation


class Paused(Exception):
    pass


def device(requested, ndof):
    if requested == "cpu" or (requested == "auto" and ndof < 30000):
        return "direct"
    try:
        import cupy
        import nvmath.sparse.advanced  # noqa: F401

        available = cupy.cuda.runtime.getDeviceCount() > 0
    except (ImportError, RuntimeError):
        available = False
    if requested == "cuda" and not available:
        raise ValueError(
            "CUDA unavailable; install step2torque[cuda] on an NVIDIA CUDA system"
        )
    return "cuda" if available else "direct"


def run(folder):
    started = time.monotonic()
    spec = json.loads((folder / "run.json").read_text())
    if spec["implementation"] != implementation():
        raise ValueError(
            "Solver version changed; start a new solve to keep results consistent"
        )
    if (
        hashlib.sha256((folder / "assembly.step").read_bytes()).hexdigest()
        != spec["step_sha256"]
    ):
        raise ValueError("Saved STEP changed; start a new solve")
    state_path = folder / "search.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else dict(trials=[], active=None)
    )
    status = dict(
        status="running",
        pid=os.getpid(),
        stage="starting",
        positions=1,
        states=len(state["trials"]),
    )

    def progress(**values):
        status.update(values, elapsed_s=time.monotonic() - started)
        write_json(folder / "status.json", status)
        if (folder / "pause").exists():
            status.update(status="paused")
            write_json(folder / "status.json", status)
            raise Paused

    progress()
    threads = spec["threads"] or min(
        8,
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count() or 1,
    )
    ng.SetNumThreads(1)
    mesh, info = _mesh.prepare(spec, folder, progress)
    ng.SetNumThreads(threads)
    phases, coils = spec["phases"], info["coils"]
    space = ng.H1(mesh, order=2, dirichlet="outer")
    periodic = info["sectors"] > 1
    if periodic:
        space = ng.Periodic(space)
    method = device(spec["device"], space.ndof)
    progress(
        stage="sources",
        elements=mesh.ne,
        dofs=space.ndof,
        threads=threads,
        device="cuda" if method == "cuda" else "cpu",
    )
    units = []
    magnetic = "|".join(
        n
        for n, m in info["materials"].items()
        if "h" in m or "br" in m or m.get("mur", 1) != 1
    )
    source_audit = []
    for i, coil in enumerate(coils):
        unit = ng.GridFunction(ng.HCurl(mesh, order=0))
        path = folder / f"source{i}.sol"
        if path.exists():
            unit.Load(str(path))
        else:
            unit = _source.annular_source(mesh, coil)
            unit.Save(str(path))
        curl = _source.curl_norm(mesh, unit, magnetic) if magnetic else 0.0
        if curl > 1e-7:
            raise ValueError(
                f"{coil['name']}: winding source intersects magnetic material; check the coil axis and winding window"
            )
        source_audit.append(
            dict(
                component=coil["name"],
                magnetic_curl=curl,
                all_air_curl=_source.curl_norm(mesh, unit, "air"),
            )
        )
        units.append(unit)
    optimized = spec["current"] == "optimized"
    if optimized:
        phase_map, resistance = _currents.mapping(
            phases, coils, spec["power"], spec["balanced"]
        )
    else:
        phase_map = np.eye(len(phases))
        resistance = np.array(
            [sum(c["resistance"] for c in coils if c["phase"] == p) for p in phases]
        )
    coil_map = np.array(
        [c["turns"] * phase_map[phases.index(c["phase"])] for c in coils]
    )
    dimension = phase_map.shape[1]
    modal_units = []
    for mode in range(dimension):
        unit = ng.GridFunction(ng.HCurl(mesh, order=0))
        for i, physical in enumerate(units):
            unit.vec.data += float(coil_map[i, mode]) * physical.vec
        modal_units.append(unit)
    parameters = [ng.Parameter(0.0) for _ in range(dimension)]
    source = sum((p * u for p, u in zip(parameters, modal_units)), ng.CF((0, 0, 0)))
    progress(stage="torque_operator")
    velocity, spin = _motion.velocity(mesh, info["moving"], units, periodic=periodic)
    torque, torque_info = _torque.make_torque(
        mesh,
        space,
        units,
        info["sectors"],
        0,
        0,
        cache=folder / "torque.npz",
        key=spec["step_sha256"],
        velocity=velocity,
        spin=spin,
    )
    if any(torque_info["work_source_cells"]):
        raise ValueError("Winding current intersects the torque work region")
    # The all-air support diagnostic remains visible, even when outside motion.
    write_json(
        folder / "diagnostics.json", dict(sources=source_audit, torque=torque_info)
    )
    progress(stage="initializing")
    vacuum = []
    u, v = space.TnT()
    with ng.TaskManager():
        a = ng.BilinearForm(space, symmetric=True)
        a += MU0 * ng.grad(u) * ng.grad(v) * ng.dx
        a.Assemble()
        matrix = (
            _contact.add_contact(mesh, space, a.mat, info["contact"])
            if info["contact"]
            else a.mat
        )
        if method == "cuda":
            from ._cuda import CuDSS

            inverse = CuDSS(matrix, space.FreeDofs())
        else:
            inverse = matrix.Inverse(space.FreeDofs(), inverse="sparsecholesky")
        for unit in modal_units:
            f = ng.LinearForm(space)
            f += MU0 * unit * ng.grad(v) * ng.dx
            f.Assemble()
            phi = ng.GridFunction(space)
            phi.vec.data = inverse * f.vec
            vacuum.append(phi)
    if method == "cuda":
        inverse.close()
    del inverse
    initial = ng.GridFunction(space)
    checkpoint_file = folder / "checkpoint.sol"
    if checkpoint_file.exists():
        initial.Load(str(checkpoint_file))
        previous = (
            state["active"]["direction"]
            if state["active"]
            else state["trials"][-1]["direction"]
        )
        for p, value in zip(parameters, previous):
            p.Set(float(value))

    def checkpoint(phi, record):
        # Saving only on request avoids a multi-MB disk write at every Newton step.
        if (folder / "pause").exists():
            phi.Save(str(checkpoint_file))
            write_json(state_path, state)
        progress(stage="solving", states=len(state["trials"]), **record)

    max_states = 20 if dimension <= 2 else 2 * dimension + 20
    predictor = {}

    def states():
        while True:
            if state["active"] is None:
                if not optimized:
                    candidate = (spec["current"], None) if not state["trials"] else None
                elif len(state["trials"]) >= max_states:
                    candidate = None
                else:
                    candidate = _currents.next_direction(state["trials"], dimension)
                if candidate is None:
                    return
                direction, beta = candidate
                state["active"] = dict(direction=direction, beta_deg=beta)
                write_json(state_path, state)
            values = state["active"]["direction"]
            if predictor:
                # The field loop also adds its vacuum change. Replace that part
                # with the accepted nonlinear tangent prediction before yielding.
                for new, old, change, linear in zip(
                    values, predictor["values"], predictor["changes"], vacuum
                ):
                    predictor["phi"].vec.data += (new - old) * (change.vec - linear.vec)
            yield values

    def accepted(index, result):
        direction = np.array(state["active"]["direction"])
        physical_ni = coil_map @ direction if coils else []
        value = torque(result["potential"], physical_ni)
        phase_currents = phase_map @ direction
        copper = float(resistance @ phase_currents**2)
        if optimized and abs(copper - spec["power"]) > 1e-8:
            raise RuntimeError("Current search violated the copper loss constraint")
        trial = dict(
            state["active"],
            torque_Nm=value,
            currents_A=phase_currents.tolist(),
            copper_loss_W=copper,
            newton=result["diagnostics"]["newton"],
        )
        if optimized and dimension >= 2:
            test = space.TestFunction()
            changes = []
            with ng.TaskManager():
                for unit in modal_units:
                    f = ng.LinearForm(space)
                    for mu, measure in result["tangent_materials"]:
                        f += ng.InnerProduct(mu * unit, ng.grad(test)) * measure
                    f.Assemble()
                    change = ng.GridFunction(space)
                    change.vec.data = result["tangent_inverse"] * f.vec
                    changes.append(change)
            gradient = torque.derivative(
                result["potential"], physical_ni, changes, coil_map
            )
            predictor.update(
                phi=result["potential"], values=direction.tolist(), changes=changes
            )
            trial["gradient"] = gradient.tolist()
            if dimension == 2:
                beta = math.radians(trial["beta_deg"])
                trial["slope"] = float(gradient @ [-math.sin(beta), math.cos(beta)])
        if not state["trials"] or value > max(t["torque_Nm"] for t in state["trials"]):
            result["potential"].Save(str(folder / "best.sol"))
        state["trials"].append(trial)
        state["active"] = None
        result["potential"].Save(str(checkpoint_file))
        write_json(state_path, state)
        progress(
            stage="current_search" if optimized else "solving",
            states=len(state["trials"]),
            best_torque_Nm=max(t["torque_Nm"] for t in state["trials"]),
        )

    _field.solve(
        mesh,
        source,
        info["materials"],
        initial=initial,
        periodic=periodic,
        method=method,
        parameters=parameters,
        states=states(),
        vacuum=vacuum,
        callback=accepted,
        checkpoint=checkpoint,
        contact=info["contact"],
    )
    best = max(state["trials"], key=lambda t: t["torque_Nm"])
    result = dict(
        best,
        phases=phases,
        angle_deg=spec["angle"],
        elements=mesh.ne,
        dofs=space.ndof,
        device="cuda" if method == "cuda" else "cpu",
        threads=threads,
        elapsed_s=time.monotonic() - started,
        states=len(state["trials"]),
        optimization_converged=(
            not optimized
            or _currents.next_direction(state["trials"], dimension) is None
        ),
    )
    write_json(folder / "result.json", result)
    progress(status="completed", stage="completed", torque_Nm=best["torque_Nm"])


def main():
    folder = Path(sys.argv[1])
    try:
        run(folder)
    except Paused:
        pass
    except Exception as error:
        traceback.print_exc()
        message = str(error)
        if "ALLOC_FAILED" in message:
            message = (
                "GPU memory exhausted; use device='cpu' or fewer concurrent solves"
            )
        write_json(
            folder / "status.json",
            dict(status="failed", error=message, pid=os.getpid()),
        )


if __name__ == "__main__":
    main()
