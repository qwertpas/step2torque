// Trace B in the section. Local separation is proportional to 1 / |B_parallel|,
// so a high-flux neck packs lines together instead of enforcing even spacing.
export function streamlines(section, density = 180, reference = null) {
  const { nx, ny, vx, vy, material, box } = section;
  const width = box[2] - box[0], height = box[3] - box[1], span = Math.max(width, height);
  let peak = 0;
  for (let i = 0; i < vx.length; i++) if (material[i] >= 0) peak = Math.max(peak, Math.hypot(vx[i], vy[i]));
  if (peak < 1e-12) return [];
  peak = reference ?? peak;
  const base = span / density, maximum = span / 6, step = Math.min(span / Math.max(nx, ny) * .8, base * .4);
  const columns = Math.ceil(width / base) + 1, rows = Math.ceil(height / base) + 1, buckets = new Map();
  const offsets = [], radius = Math.ceil(maximum / base) + 1;
  for (let y = -radius; y <= radius; y++) for (let x = -radius; x <= radius; x++) offsets.push([x, y, Math.hypot(x, y)]);
  offsets.sort((a, b) => a[2] - b[2]);

  function velocity(x, y) {
    const xx = (x - box[0]) / width * (nx - 1), yy = (y - box[1]) / height * (ny - 1);
    if (xx < 0 || yy < 0 || xx >= nx - 1 || yy >= ny - 1) return null;
    const i = Math.floor(xx), j = Math.floor(yy), tx = xx - i, ty = yy - j, k = j * nx + i;
    const ids = [k, k + 1, k + nx, k + nx + 1];
    if (ids.some(id => material[id] < 0)) return null;
    const weights = [(1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty];
    let a = 0, b = 0;
    for (let q = 0; q < 4; q++) { a += vx[ids[q]] * weights[q]; b += vy[ids[q]] * weights[q]; }
    const strength = Math.hypot(a, b);
    return strength > peak * .001 ? [a / strength, b / strength, strength] : null;
  }
  const spacing = strength => base * peak / strength;
  const bucket = (x, y) => [Math.floor((x - box[0]) / base), Math.floor((y - box[1]) / base)];
  function near(x, y, distance) {
    if (distance >= Math.hypot(width, height)) return buckets.size > 0;
    if (distance > maximum) {
      for (const points of buckets.values()) for (let k = 0; k < points.length; k += 2) {
        if ((points[k] - x)**2 + (points[k + 1] - y)**2 < distance**2) return true;
      }
      return false;
    }
    const [i, j] = bucket(x, y), limit = distance / base + Math.SQRT2;
    for (const [dx, dy, r] of offsets) {
      if (r > limit) break;
      const a = i + dx, b = j + dy;
      if (a < 0 || b < 0 || a >= columns || b >= rows) continue;
      const points = buckets.get(b * columns + a);
      if (!points) continue;
      for (let k = 0; k < points.length; k += 2) if ((points[k] - x)**2 + (points[k + 1] - y)**2 < distance**2) return true;
    }
    return false;
  }
  function trace(seed, sign) {
    let [x, y] = seed;
    const line = [], visited = new Set();
    let last = -1;
    for (let i = 0; i < Math.ceil(span * 5 / step); i++) {
      const a = velocity(x, y); if (!a || near(x, y, spacing(a[2]) * .45)) break;
      const [cx, cy] = bucket(x, y), key = cy * columns + cx;
      if (key !== last && visited.has(key)) break;
      visited.add(key); last = key; line.push([x, y]);
      const dt = step * sign;
      const b = velocity(x + dt * a[0] / 2, y + dt * a[1] / 2); if (!b) break;
      const c = velocity(x + dt * b[0] / 2, y + dt * b[1] / 2); if (!c) break;
      const d = velocity(x + dt * c[0], y + dt * c[1]); if (!d) break;
      x += dt / 6 * (a[0] + 2 * b[0] + 2 * c[0] + d[0]);
      y += dt / 6 * (a[1] + 2 * b[1] + 2 * c[1] + d[1]);
    }
    return line;
  }
  // Strong regions seed first. We then permit those same lines to spread out
  // in the yoke or air; weak regions do not crowd out a concentrated flux path.
  const seeds = [], sx = Math.ceil(width / base * 1.5), sy = Math.ceil(height / base * 1.5);
  for (let j = 0; j < sy; j++) for (let i = 0; i < sx; i++) {
    const x = box[0] + (i + .5) / sx * width, y = box[1] + (j + .5) / sy * height, v = velocity(x, y);
    if (v) seeds.push([x, y, v[2]]);
  }
  seeds.sort((a, b) => b[2] - a[2]);
  const output = [];
  for (const [x, y, strength] of seeds) {
    if (near(x, y, spacing(strength))) continue;
    const before = trace([x, y], -1), after = trace([x, y], 1), line = before.reverse().concat(after.slice(1));
    if (line.length * step < base * 3) continue;
    let previous;
    for (const p of line) {
      if (previous && Math.hypot(p[0] - previous[0], p[1] - previous[1]) < base * .25) continue;
      const [i, j] = bucket(...p), key = j * columns + i;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(...p); previous = p;
    }
    output.push(line);
  }
  return output;
}
