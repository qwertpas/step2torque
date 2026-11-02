"""V3 exact affine-P2 air virtual-work torque; no PDE or physical-law changes.

The symmetric tensor M = div(v)/2 I - sym(grad(v)) is constant on each
affine tetrahedron for the verified P1 mesh velocity. H = sum(I_i h_i)-grad(phi)
is affine, so the tetrahedral second barycentric moment V*(1+delta_ab)/20
integrates H.M.H exactly. Sparse evaluation retains both source cross terms.
"""

import hashlib
import json
import math
import os
import time
from pathlib import Path
import ngsolve as ng
import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

VERSION = "analytic-air-volume-v3.1-guarded"
MU0 = 4e-7 * math.pi
REFERENCE = np.array(
    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
)


def evaluator_hash():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256(VERSION.encode())
    for name in ["_torque.py", "_motion.py", "_source.py"]:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    digest.update(ng.__version__.encode())
    return digest.hexdigest()


def _undeformed(mesh):
    if mesh.deformation is not None:
        raise ValueError("Virtual-work operators require an undeformed reference mesh.")


def mesh_signature(mesh):
    _undeformed(mesh)
    digest = hashlib.sha256()
    digest.update(np.asarray(mesh.ngmesh.Coordinates()).tobytes())
    digest.update(mesh.ngmesh.Elements3D().NumPy().tobytes())
    digest.update(
        repr((mesh.GetMaterials(), mesh.ngmesh.GetIdentifications())).encode()
    )
    return digest.hexdigest()


def space_signature(space):
    _undeformed(space.mesh)
    digest = hashlib.sha256(
        repr(
            (space.type, space.globalorder, space.ndof, space.dim, space.is_complex)
        ).encode()
    )

    # Native configuration captures basis flags (e.g. nodalp2) that leave
    # type/order/DOF maps unchanged. Mesh objects are bound independently by
    # mesh_signature rather than Python repr addresses.
    def configuration(value):
        if isinstance(value, ng.FESpace):
            digest.update(b"FESpace")
            configuration(value.__getstate__())
        elif isinstance(value, ng.Mesh):
            digest.update(b"separately-bound-mesh")
        elif type(value).__name__ == "Flags":
            configuration(value.__getstate__())
        elif isinstance(value, bytes):
            digest.update(str(len(value)).encode() + b":" + value)
        elif isinstance(value, (tuple, list)):
            digest.update(type(value).__name__.encode() + str(len(value)).encode())
            for item in value:
                configuration(item)
        elif isinstance(value, (str, int, float, bool, type(None))):
            digest.update(repr(value).encode())
        else:
            raise ValueError(
                "Unsupported native FE configuration state: " + str(type(value))
            )

    configuration(space)
    first = next(iter(space.Elements(ng.VOL)))
    fe = space.GetFE(first)
    reference = np.vstack(
        [
            REFERENCE,
            *[
                (REFERENCE[a] + REFERENCE[b])[None, :] / 2
                for a in range(4)
                for b in range(a)
            ],
        ]
    )
    for point in reference:
        digest.update(np.asarray(fe.CalcShape(*point)).tobytes())
    digest.update(np.asarray(space.FreeDofs(), dtype=bool).tobytes())
    return digest.hexdigest()


def source_signature(unit):
    digest = hashlib.sha256(space_signature(unit.space).encode())
    digest.update(unit.vec.FV().NumPy().tobytes())
    return digest.hexdigest()


def cache_key(meshpath, space, units, pairs, tip, magnet_radius):
    digest = hashlib.sha256(evaluator_hash().encode())
    digest.update(Path(meshpath).read_bytes())
    digest.update(mesh_signature(space.mesh).encode())
    digest.update(space_signature(space).encode())
    digest.update(repr((space.ndof, pairs, tip, magnet_radius)).encode())
    for unit in units:
        digest.update(source_signature(unit).encode())
    return digest.hexdigest()


def _evaluator(active, matrix, loads, constants, reference_space, expected_space):
    verified_spaces = {id(reference_space): reference_space}

    def evaluate(potential, currents=()):
        _undeformed(reference_space.mesh)
        _undeformed(potential.space.mesh)
        if id(potential.space) not in verified_spaces:
            if space_signature(potential.space) != expected_space:
                raise ValueError(
                    "Virtual-work potential FE/periodic/Dirichlet map differs from cached space."
                )
            if mesh_signature(potential.space.mesh) != mesh_signature(
                reference_space.mesh
            ):
                raise ValueError(
                    "Virtual-work potential mesh differs from cached mesh."
                )
            verified_spaces[id(potential.space)] = potential.space
        if len(currents) != len(loads):
            raise ValueError("Supply one current per virtual-work source field.")
        values = potential.vec.FV().NumPy()[active]
        currents = np.asarray(currents, dtype=float)
        return float(
            values @ (matrix @ values)
            - 2 * currents @ loads @ values
            + currents @ constants @ currents
        )

    def derivative(potential, currents, changes, current_map):
        values = potential.vec.FV().NumPy()[active]
        currents = np.asarray(currents)
        dphi = np.array([g.vec.FV().NumPy()[active] for g in changes])
        return 2 * dphi @ (matrix @ values - loads.T @ currents) + 2 * np.asarray(
            current_map
        ).T @ (constants @ currents - loads @ values)

    evaluate.derivative = derivative
    return evaluate


def make_torque(
    mesh,
    space,
    units,
    pairs,
    tip,
    magnet_radius,
    cache=None,
    key=None,
    velocity=None,
    spin=None,
):
    started = time.monotonic()
    if mesh.ngmesh.GetCurveOrder() != 1:
        raise ValueError("V3 requires an affine tetrahedral mesh.")
    _undeformed(mesh)
    meshkey = mesh_signature(mesh)
    spacekey = space_signature(space)
    sourcekeys = [source_signature(u) for u in units]
    motionkey = repr((pairs, tip, magnet_radius))
    if cache is not None:
        cache = Path(cache)
        if not isinstance(key, str) or not key:
            raise ValueError(
                "Virtual-work cache requires strict mesh/source/evaluator key."
            )
        if cache.exists():
            with np.load(cache, allow_pickle=False) as saved:
                if saved["key"].item() != key or saved["ndof"].item() != space.ndof:
                    raise ValueError("Virtual-work cache key or space mismatch.")
                if (
                    saved["mesh_signature"].item() != meshkey
                    or saved["space_signature"].item() != spacekey
                ):
                    raise ValueError(
                        "Virtual-work cached mesh or complete FE map differs."
                    )
                if json.loads(saved["source_signatures"].item()) != sourcekeys:
                    raise ValueError(
                        "Virtual-work cached source FE map or values differ."
                    )
                if saved["motion_signature"].item() != motionkey:
                    raise ValueError(
                        "Virtual-work cached sector normalization or radial motion differs."
                    )
                active = saved["active"]
                loads = saved["loads"]
                constants = saved["constants"]
                if len(loads) != len(units):
                    raise ValueError("Virtual-work cache source-count mismatch.")
                matrix = csr_matrix(
                    (saved["data"], saved["indices"], saved["indptr"]),
                    shape=tuple(saved["shape"]),
                )
                info = json.loads(saved["info"].item())
                if info["evaluator_hash"] != evaluator_hash():
                    raise ValueError(
                        "Virtual-work evaluator changed; start a new solve"
                    )
            info.update(cache_hit=True, cache_load_s=time.monotonic() - started)
            return _evaluator(active, matrix, loads, constants, space, spacekey), info

    from ._motion import radial_velocity as make_velocity

    velocity = (
        make_velocity(mesh, tip, magnet_radius) if velocity is None else velocity
    )  # Audits exact material motion.
    cells = mesh.ngmesh.Elements3D().NumPy()
    air = np.array([m == "air" for m in mesh.GetMaterials()])
    elements = np.flatnonzero(air[cells["index"] - 1])
    if not len(elements):
        raise ValueError("Virtual work requires air tetrahedra.")
    xyz = np.asarray(mesh.ngmesh.Coordinates())[: mesh.nv]
    nodes = cells["nodes"] - 1
    # Exact rigid or fixed P1 cells have zero symmetric work tensor. Excluding
    # them analytically prevents numerical skew-gradient roundoff creating a
    # large, physically null sparse graph in the far field.
    vv = np.column_stack([c.vec.FV().NumPy() for c in velocity.components])
    spin = (
        np.column_stack([-xyz[:, 1], xyz[:, 0], np.zeros(mesh.nv)])
        if spin is None
        else spin
    )
    fixed = np.all(vv[nodes[elements]] == 0, axis=(1, 2))
    rigid = np.all(vv[nodes[elements]] == spin[nodes[elements]], axis=(1, 2))
    all_air = len(elements)
    elements = elements[~(fixed | rigid)]
    from ._source import current_cells

    work_source_cells = [
        int(current_cells(mesh, unit)[elements].sum()) for unit in units
    ]
    centers = xyz[nodes[elements]].mean(axis=1)
    mapped = np.repeat(mesh(*centers[:1].T), len(elements))
    mapped["nr"] = elements
    for name in ["x", "y", "z"]:
        mapped[name] = 0.25
    jac = np.asarray(ng.specialcf.JacobianMatrix(3)(mapped)).reshape(-1, 3, 3)
    inverse = np.linalg.inv(jac)
    volume = abs(np.linalg.det(jac)) / 6
    gradv = np.asarray(ng.grad(velocity)(mapped)).reshape(-1, 3, 3)
    tensor = 0.5 * np.trace(gradv, axis1=1, axis2=2)[:, None, None] * np.eye(
        3
    ) - 0.5 * (gradv + gradv.transpose(0, 2, 1))
    tensor *= pairs * MU0 * volume[:, None, None] / 20
    before = time.monotonic()
    dofs = []
    gradients = []
    for i, number in enumerate(elements):
        eid = ng.ElementId(ng.VOL, int(number))
        fe = space.GetFE(eid)
        if fe.ndof not in (4, 10):
            raise ValueError(
                "V3 exact integration is qualified only for affine P1/P2 H1 tetrahedra."
            )
        dofs.append(space.GetDofNrs(eid))
        gradients.append(
            np.array([np.asarray(fe.CalcDShape(*r)) for r in REFERENCE]) @ inverse[i]
        )
    dofs = np.asarray(dofs, dtype=np.int64)
    gradients = np.asarray(gradients)
    if np.any(dofs < 0):
        raise ValueError("Virtual-work element has inactive native DOFs.")
    extraction_s = time.monotonic() - before

    # Probe actual FE ordering, periodic DOF map and Jacobian orientation.
    sample = np.arange(len(elements))
    probe = ng.GridFunction(space)
    probe.vec.FV().NumPy()[:] = np.sin(np.arange(space.ndof) * 0.6180339887498949)
    check_points = mapped[sample].copy()
    bary = np.random.default_rng(74123).dirichlet(np.ones(4), size=len(sample))
    for j, name in enumerate(["x", "y", "z"]):
        check_points[name] = bary[:, j + 1]
    predicted = np.einsum(
        "na,naic,ni->nc", bary, gradients[sample], probe.vec.FV().NumPy()[dofs[sample]]
    )
    actual = np.asarray(ng.grad(probe)(check_points)).reshape(-1, 3)
    derivative_error = float(
        np.linalg.norm(predicted - actual) / max(np.linalg.norm(actual), 1e-30)
    )
    if derivative_error > 1e-10:
        raise ValueError(
            f"Virtual-work native derivative mapping mismatch: {derivative_error}"
        )
    vertices = np.repeat(mapped, 4)
    for j, name in enumerate(["x", "y", "z"]):
        vertices[name] = np.tile(REFERENCE[:, j], len(elements))
    sources = []
    source_errors = []
    source_max = []
    source_curl_max = []
    for unit in units:
        values = np.asarray(unit(vertices)).reshape(len(elements), 4, 3)
        predicted = np.einsum("na,nac->nc", bary, values[sample])
        actual = np.asarray(unit(check_points)).reshape(-1, 3)
        error = float(
            np.linalg.norm(predicted - actual) / max(np.linalg.norm(actual), 1.0)
        )
        if error > 1e-10:
            raise ValueError(f"Virtual-work source not affine: {error}")
        source_errors.append(error)
        sources.append(values)
        source_max.append(float(abs(values).max()))
        source_curl_max.append(
            float(
                np.linalg.norm(
                    np.asarray(ng.curl(unit)(mapped)).reshape(-1, 3), axis=1
                ).max()
            )
        )

    def moments(a, b):
        return np.einsum(
            "eic,ecd,ejd->eij", a.sum(axis=1), tensor, b.sum(axis=1), optimize=True
        ) + np.einsum("eaic,ecd,eajd->eij", a, tensor, b, optimize=True)

    before = time.monotonic()
    blocks = moments(gradients, gradients)
    active = np.unique(dofs)
    local = np.searchsorted(active, dofs)
    rows = np.broadcast_to(local[:, :, None], blocks.shape).ravel()
    columns = np.broadcast_to(local[:, None, :], blocks.shape).ravel()
    matrix = coo_matrix(
        (blocks.ravel(), (rows, columns)), shape=(len(active), len(active))
    ).tocsr()
    matrix.eliminate_zeros()
    loads = np.zeros((len(units), len(active)))
    constants = np.zeros((len(units), len(units)))
    if units:
        sources = np.stack(sources, axis=2)
        local_loads = moments(gradients, sources)
        for i in range(len(units)):
            loads[i] = np.bincount(
                local.ravel(),
                weights=local_loads[:, :, i].ravel(),
                minlength=len(active),
            )
        constants = moments(sources, sources).sum(axis=0)
    info = dict(
        evaluator_version=VERSION,
        evaluator_hash=evaluator_hash(),
        method="Analytic air-volume virtual work; exact affine P2 tetrahedral moments",
        all_air_elements=all_air,
        work_elements=len(elements),
        full_dofs=space.ndof,
        active_dofs=len(active),
        matrix_nonzeros=matrix.nnz,
        derivative_relative_error=derivative_error,
        source_relative_errors=source_errors,
        derivative_checked_elements=len(sample),
        work_source_max_abs=source_max,
        work_source_curl_max_norm=source_curl_max,
        work_source_cells=work_source_cells,
        source_constants=constants.tolist(),
        extraction_s=extraction_s,
        assembly_s=time.monotonic() - before,
        total_build_s=time.monotonic() - started,
        cache_hit=False,
        physical_radian_velocity=True,
        tetrahedral_polynomial_order=2,
    )
    if cache is not None:
        temporary = cache.with_name(cache.name + f".{os.getpid()}.tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(
                stream,
                key=key,
                ndof=space.ndof,
                active=active,
                mesh_signature=meshkey,
                space_signature=spacekey,
                source_signatures=json.dumps(sourcekeys),
                motion_signature=motionkey,
                data=matrix.data,
                indices=matrix.indices,
                indptr=matrix.indptr,
                shape=matrix.shape,
                loads=loads,
                constants=constants,
                info=json.dumps(info),
            )
        temporary.replace(cache)
    return _evaluator(active, matrix, loads, constants, space, spacekey), info
