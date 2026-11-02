"""RK4 projected streamlines with separation proportional to 1 / |B_parallel|.

This is the same placement rule as web/lines.js. Interpolation here only guides
curves on the sampled cut; the saved field keeps material discontinuities.
"""

import math

import numpy as np


def streamlines(x, y, vx, vy, material, density=180, reference=None):
    nx, ny = len(x), len(y)
    width, height = x[-1] - x[0], y[-1] - y[0]
    span = max(width, height)
    strength = np.where(material >= 0, np.hypot(vx, vy), 0)
    peak = float(np.nanmax(strength))
    if peak < 1e-12:
        return []
    peak = reference if reference is not None else peak
    if not math.isfinite(density) or not 10 <= density <= 500:
        raise ValueError("density must be between 10 and 500")
    base, maximum = span / density, span / 6
    step = min(span / max(nx, ny) * 0.8, base * 0.4)
    columns = math.ceil(width / base) + 1
    rows = math.ceil(height / base) + 1
    buckets = {}
    radius = math.ceil(maximum / base) + 1
    offsets = sorted(
        [
            (a, b, math.hypot(a, b))
            for a in range(-radius, radius + 1)
            for b in range(-radius, radius + 1)
        ],
        key=lambda p: p[2],
    )
    vx, vy, material = vx.ravel(), vy.ravel(), material.ravel()
    xf, yf = (nx - 1) / width, (ny - 1) / height

    def velocity(a, b):
        xx, yy = (a - x[0]) * xf, (b - y[0]) * yf
        if xx < 0 or yy < 0 or xx >= nx - 1 or yy >= ny - 1:
            return None
        i, j = int(xx), int(yy)
        tx, ty = xx - i, yy - j
        k = j * nx + i
        ids = (k, k + 1, k + nx, k + nx + 1)
        if any(material[q] < 0 for q in ids):
            return None
        weights = ((1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty)
        a = sum(float(vx[q]) * w for q, w in zip(ids, weights))
        b = sum(float(vy[q]) * w for q, w in zip(ids, weights))
        length = math.hypot(a, b)
        return (a / length, b / length, length) if length > peak * 0.001 else None

    def spacing(value):
        return base * peak / value

    def bucket(a, b):
        return math.floor((a - x[0]) / base), math.floor((b - y[0]) / base)

    def near(a, b, distance):
        if distance >= math.hypot(width, height):
            return bool(buckets)
        if distance > maximum:
            square = distance**2
            return any(
                (px - a) ** 2 + (py - b) ** 2 < square
                for points in buckets.values()
                for px, py in points
            )
        i, j = bucket(a, b)
        limit, square = distance / base + math.sqrt(2), distance**2
        for dx, dy, r in offsets:
            if r > limit:
                break
            cx, cy = i + dx, j + dy
            if cx < 0 or cy < 0 or cx >= columns or cy >= rows:
                continue
            for px, py in buckets.get(cy * columns + cx, ()):
                if (px - a) ** 2 + (py - b) ** 2 < square:
                    return True
        return False

    def trace(seed, sign):
        a, b = seed
        line, visited, last = [], set(), -1
        for _ in range(math.ceil(span * 5 / step)):
            v1 = velocity(a, b)
            if v1 is None or near(a, b, spacing(v1[2]) * 0.45):
                break
            cx, cy = bucket(a, b)
            key = cy * columns + cx
            if key != last and key in visited:
                break
            visited.add(key)
            last = key
            line.append((a, b))
            dt = step * sign
            v2 = velocity(a + dt * v1[0] / 2, b + dt * v1[1] / 2)
            if v2 is None:
                break
            v3 = velocity(a + dt * v2[0] / 2, b + dt * v2[1] / 2)
            if v3 is None:
                break
            v4 = velocity(a + dt * v3[0], b + dt * v3[1])
            if v4 is None:
                break
            a += dt / 6 * (v1[0] + 2 * v2[0] + 2 * v3[0] + v4[0])
            b += dt / 6 * (v1[1] + 2 * v2[1] + 2 * v3[1] + v4[1])
        return line

    sx, sy = math.ceil(width / base * 1.5), math.ceil(height / base * 1.5)
    seeds = []
    for j in range(sy):
        for i in range(sx):
            a, b = x[0] + (i + 0.5) / sx * width, y[0] + (j + 0.5) / sy * height
            v = velocity(a, b)
            if v:
                seeds.append((a, b, v[2]))
    seeds.sort(key=lambda p: -p[2])
    output = []
    for a, b, value in seeds:
        if near(a, b, spacing(value)):
            continue
        line = trace((a, b), -1)[::-1] + trace((a, b), 1)[1:]
        if len(line) * step < base * 3:
            continue
        previous = None
        for p in line:
            if (
                previous
                and math.hypot(p[0] - previous[0], p[1] - previous[1]) < base * 0.25
            ):
                continue
            cx, cy = bucket(*p)
            buckets.setdefault(cy * columns + cx, []).append(p)
            previous = p
        output.append(np.asarray(line))
    return output
