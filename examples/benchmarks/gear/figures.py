"""Render the CAD, stored torque comparisons, and an optional saved field."""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import netgen.occ as occ
import ngsolve as ng
import numpy as np

from run import ROOT, MU0, materials, parts, space
from step2torque import _field


def model():
    cut = occ.Box(occ.Pnt(0, -60, -1), occ.Pnt(60, 0, 40))
    triangles, colors = [], []
    for _, solid, _, sign in parts():
        solid = solid - cut
        if solid.mass < 1e-8:
            continue
        data = solid._webgui_data()["Bezier_trig_points"]
        xyz = np.stack([np.asarray(p).reshape(-1, 4)[:, :3] for p in data[:3]], axis=1)
        normal = np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0])
        normal /= np.maximum(np.linalg.norm(normal, axis=1)[:, None], 1e-12)
        shade = 0.62 + 0.38 * abs(normal @ np.array([0.35, -0.45, 0.82]))
        color = "#d35050" if sign > 0 else "#437ebc" if sign < 0 else "#b9bfc5"
        triangles.extend(xyz)
        colors.extend(shade[:, None] * to_rgb(color))
    fig = plt.figure(figsize=(6, 4.5))
    ax = fig.add_subplot(projection="3d")
    ax.add_collection3d(
        Poly3DCollection(triangles, facecolors=colors, linewidths=0, antialiased=False)
    )
    ax.set(xlim=(-53, 53), ylim=(-53, 53), zlim=(-5, 46))
    ax.set_box_aspect((106, 106, 51), zoom=1.25)
    ax.set_proj_type("ortho")
    ax.view_init(elev=26, azim=-52)
    ax.set_axis_off()
    fig.subplots_adjust(0, 0, 1, 1)
    fig.savefig(ROOT / "model-3d.png", dpi=180)
    plt.close(fig)


def torque():
    rows = np.genfromtxt(
        ROOT / "comparison.csv", delimiter=",", names=True, dtype=None, encoding="utf8"
    )
    reference = np.genfromtxt(
        ROOT / "reference.csv", delimiter=",", names=True, dtype=None, encoding="utf8"
    )
    native = np.genfromtxt(ROOT / "comsol.csv", delimiter=",", names=True)
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    for ax, rotor, title in zip(axes, ("hs", "ls"), ("High-speed", "Low-speed")):
        ax.plot(
            native["ls_deg"],
            native[rotor + "_Nm"],
            "o-",
            c=".55",
            ms=3,
            lw=1,
            label="COMSOL saved transient",
        )
        ax.plot(
            rows["ls_deg"],
            rows[rotor + "_Nm"],
            "o-",
            c="#007d8a",
            label="step2torque frozen poses",
        )
        ax.plot(
            reference["ls_deg"],
            reference[rotor + "_Nm"],
            "D",
            c="#d56b16",
            ms=5,
            label="Independent frozen A reference",
        )
        ax.set_ylabel("Torque (N·m)")
        ax.set_title(title, loc="left", fontsize=10)
        ax.grid(alpha=0.2)
    axes[0].legend(loc="best", fontsize=8)
    axes[-1].set_xlabel("Low-speed rotor angle (°)")
    fig.tight_layout()
    fig.savefig(ROOT / "torque.png")
    plt.close(fig)


def field(folder):
    mesh = ng.Mesh(str(folder / "mesh.vol.gz"))
    phi = ng.GridFunction(space(mesh))
    phi.Load(str(folder / "potential.sol"))
    h = -ng.grad(phi)
    magnitude = ng.sqrt(h * h + 1e-8)
    fields = {}
    for name, law in materials().items():
        if "h" in law:
            b, _ = _field.law(law, magnitude, 0)
            fields[name] = b / magnitude * h
        else:
            fields[name] = MU0 * law["mur"] * h + ng.CF(tuple(law.get("br", (0, 0, 0))))
    bfield = mesh.MaterialCF(fields, default=MU0 * h)
    theta = np.pi / 14
    c, s = np.cos(theta), np.sin(theta)
    x = np.linspace(-65, 65, 520)
    z = np.linspace(-5, 44, 300)
    xx, zz = np.meshgrid(x, z)
    mapped = mesh((xx * c / 1000).ravel(), (xx * s / 1000).ravel(), (zz / 1000).ravel())
    b = np.asarray(bfield(mapped)).reshape(len(z), len(x), 3)
    bs, bp = c * b[:, :, 0] + s * b[:, :, 1], -s * b[:, :, 0] + c * b[:, :, 1]
    total = np.linalg.norm(b, axis=2)
    labels = np.asarray(
        mesh.MaterialCF(
            {
                n: 0 if n == "air" else 2 if "magnet" in n else 3 if "pole" in n else 1
                for n in mesh.GetMaterials()
            }
        )(mapped)
    ).reshape(xx.shape)
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True, sharey=True)
    image = axes[0].pcolormesh(
        x, z, total, vmin=0, vmax=2.5, cmap="magma", rasterized=True, shading="auto"
    )
    fig.colorbar(image, ax=axes[0], label="|B| (T)", pad=0.015, extend="max")
    keep = np.sqrt(bs**2 + b[:, :, 2] ** 2) >= 0.035
    axes[0].streamplot(
        x,
        z,
        np.where(keep, bs, np.nan),
        np.where(keep, b[:, :, 2], np.nan),
        color="white",
        linewidth=0.45,
        density=1.6,
        arrowsize=0.55,
    )
    image = axes[1].pcolormesh(
        x, z, bp, vmin=-0.35, vmax=0.35, cmap="RdBu_r", rasterized=True, shading="auto"
    )
    fig.colorbar(image, ax=axes[1], label="B azimuthal (T)", pad=0.015, extend="both")
    for ax in axes:
        for kind in (1, 2, 3):
            ax.contour(
                xx, zz, labels == kind, levels=[0.5], colors=".5", linewidths=0.4
            )
        ax.set_aspect("equal")
        ax.set_ylabel("z (mm)")
    axes[-1].set_xlabel("Position in radial plane (mm)")
    fig.tight_layout()
    fig.savefig(ROOT / "field.png")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--field", type=Path, help="initial pose folder produced by run.py"
    )
    args = parser.parse_args()
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "savefig.dpi": 180,
        }
    )
    model()
    torque()
    if args.field:
        field(args.field)


if __name__ == "__main__":
    main()
