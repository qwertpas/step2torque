import argparse
from pathlib import Path

from . import Plane, Viewer


def main():
    parser = argparse.ArgumentParser(
        description="A section of a saved step2torque field."
    )
    parser.add_argument("solution", help="Saved solution directory or .s2t file")
    parser.add_argument("output", type=Path, help="Output .png, .html or .s2t")
    parser.add_argument(
        "--plane", choices=("axial", "longitudinal", "oblique"), default="axial"
    )
    parser.add_argument(
        "--angle", type=float, default=0, help="Angle about the shaft (degrees)"
    )
    parser.add_argument(
        "--tilt",
        type=float,
        default=45,
        help="Oblique plane normal from the shaft (degrees)",
    )
    parser.add_argument(
        "--offset", type=float, default=0, help="Signed plane offset (mm)"
    )
    parser.add_argument("--metric", choices=("flux", "loading"))
    parser.add_argument(
        "--air", action="store_true", help="Colour air as well as solids"
    )
    parser.add_argument("--no-lines", action="store_true")
    parser.add_argument(
        "--density", type=float, default=180, help="Streamline density (10 to 500)"
    )
    parser.add_argument("--vmax", type=float, help="Colour scale maximum (T or %%)")
    parser.add_argument(
        "--resolution", type=int, default=800, help="PNG samples on the longer edge"
    )
    args = parser.parse_args()
    tilt = {"axial": 0, "longitudinal": 90, "oblique": args.tilt}[args.plane]
    plane = Plane(args.angle, tilt, args.offset)
    options = dict(
        plane=plane,
        metric=args.metric,
        air=args.air,
        lines=not args.no_lines,
        vmax=args.vmax,
        density=args.density,
    )
    view = Viewer(args.solution)
    if args.output.suffix.lower() == ".html":
        output = view.html(args.output, **options)
    elif args.output.suffix.lower() == ".png":
        output = view.png(args.output, resolution=args.resolution, **options)
    elif args.output.suffix.lower() == ".s2t":
        output = view.export(args.output, **options)
    else:
        parser.error("output must end in .png, .html or .s2t")
    print(output)


if __name__ == "__main__":
    main()
