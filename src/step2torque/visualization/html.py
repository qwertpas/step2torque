"""One offline HTML, or the static viewer shell, using the same .s2t payload."""

import base64
from pathlib import Path
import re

from .archive import pack
from .viewer import COLORS


def write_html(
    field, path, plane, *, metric, air, lines, vmax, flux_max, density, mode, bounds
):
    data = pack(
        field,
        plane,
        metric=metric,
        air=air,
        lines=lines,
        vmax=vmax,
        flux_max=flux_max,
        density=density,
        mode=mode,
        colors=COLORS,
        bounds=bounds,
    )
    return write_page(path, data=data)


def write_page(path, *, data=b""):
    root = Path(__file__).parent
    values = dict(
        DATA=base64.b64encode(data).decode(),
        SCRIPT=(root / "viewer.js").read_text().replace("</script", "<\\/script"),
    )
    document = re.sub(
        r"__(DATA|SCRIPT)__",
        lambda m: values[m[1]],
        (root / "viewer.html").read_text(),
    )
    path = Path(path).resolve()
    path.write_text(document)
    return path
