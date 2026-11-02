"""V3.1 scalar-potential equations and Newton/energy line search.

H = Hs - grad(phi). Each material contributes B(H), its differential tangent,
and coenergy. Linear air/PM matrices are assembled once; only iron is updated.
"""

import time

import ngsolve as ng
import numpy as np

from .materials import MU0
from ._kernel import Kernel


def law(data, h, floor):
    fields, flux = np.asarray(data["h"]), np.asarray(data["b"])
    fields = np.r_[fields, 1e8]
    flux = np.r_[flux, flux[-1] + MU0 * (fields[-1] - fields[-2])]
    curve = ng.BSpline(2, [fields[0], *fields.tolist(), fields[-1]], flux.tolist())
    b, differential = curve(h), curve.Differentiate()(h)
    secant = b / h
    differential = ng.IfPos(differential - floor * secant, differential, floor * secant)
    return b, differential


def solve(
    mesh,
    source,
    materials,
    *,
    space=None,
    initial=None,
    periodic=False,
    method="direct",
    parameters=(),
    states=None,
    vacuum=(),
    callback=None,
    checkpoint=None,
    tolerance=1e-9,
    maxit=200,
    stages=(1,),
    hessian_floor=0.1,
    contact=None,
):
    """Solve fixed or generated current states; callback can use the final tangent."""
    if space is None:
        space = ng.H1(mesh, order=2, dirichlet="outer")
        if periodic:
            space = ng.Periodic(space)
    phi = ng.GridFunction(space, name="potential")
    if initial is not None:
        if initial.space.ndof != space.ndof:
            raise ValueError("Checkpoint does not match the mesh")
        phi.vec.data = initial.vec
    phi.vec.data = ng.Projector(space.FreeDofs(), True) * phi.vec
    factor, floor = ng.Parameter(1.0), ng.Parameter(hessian_floor)
    h = factor * source - ng.grad(phi)
    hn = ng.sqrt(h * h + 1e-8)
    direction = ng.OuterProduct(h, h) / hn**2
    u, v = space.TnT()
    linear = ng.BilinearForm(space, symmetric=True)
    load = ng.LinearForm(space)
    linear += 0 * ng.grad(u) * ng.grad(v) * ng.dx
    iron, constants, tangents, fluxes = False, [], [], {}
    # Group equal laws. Avoid a compiled branch for every individual magnet.
    groups = {}
    for name in mesh.GetMaterials():
        data = materials.get(name, dict(mur=1.0))
        key = (
            (tuple(data["h"]), tuple(data["b"]))
            if "h" in data
            else float(data.get("mur", 1))
        )
        groups.setdefault(key, []).append(name)
    for key, names in groups.items():
        nonlinear = isinstance(key, tuple)
        region = "|".join(names)
        rules = {ng.ET.TET: ng.IntegrationRule(ng.ET.TET, 6 if nonlinear else 2)}
        measure = ng.dx(definedon=mesh.Materials(region), intrules=rules)
        if nonlinear:
            b, differential = law(dict(h=key[0], b=key[1]), hn, floor)
            secant = b / hn
            flux = secant * h
            mu = secant * ng.Id(3) + (differential - secant) * direction
        else:
            permeability = MU0 * key
            axis = mesh.MaterialCF(
                {
                    n: ng.CF(tuple(materials.get(n, {}).get("br", (0, 0, 0))))
                    for n in names
                },
                default=(0, 0, 0),
            )
            flux = permeability * h + factor * axis
            mu = permeability * ng.Id(3)
        mu, flux = (c.Compile(maxderiv=0) for c in (mu, flux))
        fluxes.update({n: flux for n in names})
        tangents.append((mu, measure))
        if nonlinear:
            iron = True
        else:
            linear += permeability * ng.grad(u) * ng.grad(v) * measure
            load += (
                (permeability * factor * source + factor * axis) * ng.grad(v) * measure
            )
            constant = (permeability - MU0) / 2 * factor**2 * (
                source * source
            ) + factor**2 * (axis * source)
            constants.append(constant.Compile(maxderiv=0) * measure)
    bfield = mesh.MaterialCF(fluxes, default=MU0 * h)
    old, correction = phi.vec.CreateVector(), phi.vec.CreateVector()
    inverse, history, previous = None, [], [p.Get() for p in parameters]
    started = time.monotonic()

    def energy():
        return line_energy()

    def line_energy():
        linear_residual.data = linear_matrix * phi.vec
        return (
            form.energy()
            + 0.5 * float(ng.InnerProduct(phi.vec, linear_residual))
            - float(ng.InnerProduct(load.vec, phi.vec))
            + constant_energy
        )

    result = None
    with ng.TaskManager():
        linear.Assemble()
        linear_matrix = linear.mat
        if contact is not None:
            from ._contact import add_contact

            linear_matrix = add_contact(mesh, space, linear_matrix, contact)
        linear_residual = phi.vec.CreateVector()
        form = Kernel(
            mesh, space, source, materials, phi, factor, floor, parameters, linear_matrix
        )
        residual = form.residual
        for case, currents in enumerate(states if states is not None else [None]):
            first_record = len(history)
            if currents is not None:
                for parameter, value, before, unit in zip(
                    parameters, currents, previous, vacuum
                ):
                    phi.vec.data = phi.vec + (value - before) * unit.vec
                    parameter.Set(float(value))
                previous = list(currents)
            previous_level = 1.0
            for level in stages:
                phi.vec.data = (level / previous_level) * phi.vec
                previous_level = level
                factor.Set(level)
                floor.Set(hessian_floor)
                load.Assemble()
                constant_energy = (
                    float(ng.Integrate(sum(constants), mesh)) if constants else 0.0
                )
                current_energy = line_energy()
                for iteration in range(maxit):
                    if checkpoint:
                        checkpoint(
                            phi,
                            dict(
                                case=case,
                                excitation=level,
                                iteration=iteration,
                                elapsed_s=time.monotonic() - started,
                            ),
                        )
                    before = time.monotonic()
                    form.Assemble()
                    residual.Assemble()
                    form.mat.AsVector().data += linear_matrix.AsVector()
                    linear_residual.data = linear_matrix * phi.vec
                    residual.vec.data += load.vec - linear_residual
                    assembled = time.monotonic()
                    if inverse is None:
                        if method == "cuda":
                            from ._cuda import CuDSS

                            inverse = CuDSS(form.mat, space.FreeDofs())
                        else:
                            inverse = form.mat.Inverse(
                                space.FreeDofs(), inverse="sparsecholesky"
                            )
                    elif iron:
                        inverse.Update()
                    correction.data = inverse * residual.vec
                    decrement2 = float(ng.InnerProduct(correction, residual.vec))
                    if not np.isfinite(decrement2) or decrement2 < 0:
                        raise RuntimeError(
                            "Magnetic tangent solve was not positive and finite"
                        )
                    decrement = np.sqrt(decrement2)
                    record = dict(
                        case=case,
                        stage=level,
                        iteration=iteration + 1,
                        decrement_sqrt_j=float(decrement),
                        hessian_floor=float(floor.Get()),
                        assembly_s=assembled - before,
                        factor_s=time.monotonic() - assembled,
                    )
                    history.append(record)
                    if decrement < tolerance:
                        if floor.Get() > 0:
                            floor.Set(0)
                            continue
                        break
                    old.data = phi.vec
                    step = 1.0
                    roundoff = 20 * np.finfo(float).eps * max(1.0, abs(current_energy))
                    for _ in range(25):
                        phi.vec.data = old + step * correction
                        candidate = line_energy()
                        if (
                            np.isfinite(candidate)
                            and candidate
                            <= current_energy - 1e-4 * step * decrement2 + roundoff
                        ):
                            current_energy = candidate
                            record.update(step=step, energy_j=candidate)
                            break
                        step /= 2
                    else:
                        phi.vec.data = old
                        raise RuntimeError(
                            "Magnetic solve could not find a decreasing energy step"
                        )
                    if hessian_floor:
                        if step < 0.25:
                            floor.Set(max(hessian_floor, 4 * floor.Get()))
                        else:
                            next_floor = floor.Get() / 2
                            floor.Set(0 if next_floor < 1e-4 else next_floor)
                else:
                    raise RuntimeError(
                        f"Magnetic solve did not converge after {maxit} iterations (error {decrement:.3g})"
                    )
            result = dict(
                mesh=mesh,
                potential=phi,
                H_source=source,
                H=h,
                B=bfield,
                energy_function=energy,
                tangent_space=space,
                tangent_inverse=inverse,
                tangent_materials=tangents,
                diagnostics=dict(
                    newton=history[first_record:],
                    elapsed_s=time.monotonic() - started,
                    ndof=space.ndof,
                    elements=mesh.ne,
                    contrast_coenergy_j=energy(),
                ),
            )
            if callback:
                callback(case, result)
    return result
