"""Planes in motor coordinates: shaft = z; distances are millimetres."""

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class Plane:
    """An oriented section. tilt is the normal's angle from the shaft, in degrees.

    At tilt=90, angle is the radial direction visible horizontally in the cut.
    offset is signed distance from the motor origin along the plane normal.
    """

    angle: float = 0
    tilt: float = 0
    offset: float = 0

    def __post_init__(self):
        if not all(math.isfinite(x) for x in (self.angle, self.tilt, self.offset)):
            raise ValueError("Plane angle, tilt and offset must be finite")
        if not 0 <= self.tilt <= 180:
            raise ValueError("Plane tilt must be between 0 and 180 degrees")

    @classmethod
    def axial(cls, z=0):
        """A section perpendicular to the shaft, at z mm."""
        return cls(offset=z)

    @classmethod
    def longitudinal(cls, angle=0, offset=0):
        """A plane through the shaft at angle degrees, optionally offset in mm."""
        return cls(angle=angle, tilt=90, offset=offset)

    @property
    def frame(self):
        a, t = np.radians([self.angle, self.tilt])
        u = np.array([np.cos(a), np.sin(a), 0.0])
        n = np.array([np.sin(t) * np.sin(a), -np.sin(t) * np.cos(a), np.cos(t)])
        v = np.cross(n, u)
        return u, v, n

    def bounds(self, box):
        """The plane's intersection with an axis-aligned box, as u/v bounds."""
        box = np.asarray(box)
        corners = np.array(
            [[x, y, z] for x in box[:, 0] for y in box[:, 1] for z in box[:, 2]]
        )
        u, v, n = self.frame
        d = corners @ n - self.offset
        intersections = []
        for i in range(8):
            for bit in (1, 2, 4):
                j = i ^ bit
                if j <= i or d[i] * d[j] > 0:
                    continue
                if abs(d[i] - d[j]) < 1e-12:
                    intersections.extend((corners[i], corners[j]))
                else:
                    intersections.append(
                        corners[i] + d[i] / (d[i] - d[j]) * (corners[j] - corners[i])
                    )
        if not intersections:
            # Keep a stable view when a plane is moved beyond the geometry.
            intersections = corners
        projected = np.asarray(intersections) @ np.array([u, v]).T
        low, high = projected.min(axis=0), projected.max(axis=0)
        margin = max(np.max(high - low) * 0.035, 0.01)
        return (
            *[float(x) for x in (low - margin)],
            *[float(x) for x in (high + margin)],
        )
