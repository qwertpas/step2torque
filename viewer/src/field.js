// The field is affine H per tetrahedron followed by the original material law.
// Coordinates: mm; H: A/m; B: T. Material discontinuities stay discontinuous.
export const MU0 = 4e-7 * Math.PI;

export function createField(meta, arrays) {
  const { points, cells, h, material, bounds, nodes } = arrays;
  const count = cells.length / 4;
  const inverse = new Float64Array(count * 9);
  const weights = new Float64Array(4);
  const stack = new Int32Array(128);
  let last = -1;
  for (let i = 0; i < count; i++) {
    const a = cells[i * 4] * 3, b = cells[i * 4 + 1] * 3, c = cells[i * 4 + 2] * 3, d = cells[i * 4 + 3] * 3;
    const ax = points[b] - points[a], ay = points[b + 1] - points[a + 1], az = points[b + 2] - points[a + 2];
    const bx = points[c] - points[a], by = points[c + 1] - points[a + 1], bz = points[c + 2] - points[a + 2];
    const cx = points[d] - points[a], cy = points[d + 1] - points[a + 1], cz = points[d + 2] - points[a + 2];
    const det = ax * (by * cz - bz * cy) - bx * (ay * cz - az * cy) + cx * (ay * bz - az * by);
    if (!Number.isFinite(det) || det === 0) throw new Error("Degenerate field tetrahedron");
    inverse.set([
      (by * cz - bz * cy) / det, (bz * cx - bx * cz) / det, (bx * cy - by * cx) / det,
      (cy * az - cz * ay) / det, (cz * ax - cx * az) / det, (cx * ay - cy * ax) / det,
      (ay * bz - az * by) / det, (az * bx - ax * bz) / det, (ax * by - ay * bx) / det,
    ], i * 9);
  }
  function inside(i, x, y, z) {
    const a = cells[i * 4] * 3, m = i * 9;
    x -= points[a]; y -= points[a + 1]; z -= points[a + 2];
    const u = inverse[m] * x + inverse[m + 1] * y + inverse[m + 2] * z;
    const v = inverse[m + 3] * x + inverse[m + 4] * y + inverse[m + 5] * z;
    const w = inverse[m + 6] * x + inverse[m + 7] * y + inverse[m + 8] * z;
    const s = 1 - u - v - w, eps = 2e-7;
    if (u < -eps || v < -eps || w < -eps || s < -eps) return false;
    weights[0] = s; weights[1] = u; weights[2] = v; weights[3] = w;
    return true;
  }
  function locate(x, y, z) {
    if (last >= 0 && inside(last, x, y, z)) return last;
    let length = 1;
    stack[0] = 0;
    while (length) {
      const node = stack[--length], p = node * 6, eps = 1e-6;
      if (x < bounds[p] - eps || y < bounds[p + 1] - eps || z < bounds[p + 2] - eps || x > bounds[p + 3] + eps || y > bounds[p + 4] + eps || z > bounds[p + 5] + eps) continue;
      const a = nodes[node * 2], b = nodes[node * 2 + 1];
      if (a < 0) {
        const start = -a - 1;
        for (let i = start; i < start + b; i++) if (inside(i, x, y, z)) { last = i; return i; }
      } else {
        stack[length++] = a; stack[length++] = b;
      }
    }
    return -1;
  }
  function sample(x, y, z) {
    let turn = 0;
    if (meta.sectors > 1) {
      const period = 2 * Math.PI / meta.sectors;
      turn = Math.floor((Math.atan2(y, x) - meta.sector_start_rad) / period) * period;
      const c = Math.cos(turn), s = Math.sin(turn), before = x;
      x = c * x + s * y; y = -s * before + c * y;
    }
    const i = locate(x, y, z);
    if (i < 0) return null;
    let hx = 0, hy = 0, hz = 0;
    for (let j = 0; j < 4; j++) {
      const k = i * 12 + j * 3;
      hx += weights[j] * h[k]; hy += weights[j] * h[k + 1]; hz += weights[j] * h[k + 2];
    }
    const id = material[i], law = meta.materials[id];
    let bx, by, bz, loading = NaN;
    if (law.h) {
      const magnitude = Math.sqrt(hx * hx + hy * hy + hz * hz + 1e-8);
      const value = curve(law.h, law.b, magnitude), scale = value / magnitude;
      bx = hx * scale; by = hy * scale; bz = hz * scale;
      loading = Math.hypot(bx - MU0 * hx, by - MU0 * hy, bz - MU0 * hz) / law.reference_T * 100;
    } else {
      const mu = MU0 * (law.mur ?? 1), br = law.br ?? [0, 0, 0];
      bx = mu * hx + br[0]; by = mu * hy + br[1]; bz = mu * hz + br[2];
    }
    const c = Math.cos(turn), s = Math.sin(turn), b0 = bx, h0 = hx;
    bx = c * bx - s * by; by = s * b0 + c * by;
    hx = c * hx - s * hy; hy = s * h0 + c * hy;
    return { bx, by, bz, hx, hy, hz, material: id, loading };
  }
  return { sample, count, meta };
}

export function curve(h, b, value) {
  const end = h.length - 1;
  if (value >= h[end]) return b[end] + MU0 * (value - h[end]);
  let low = 0, high = end;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (h[middle] > value) high = middle; else low = middle;
  }
  return b[low] + (b[high] - b[low]) * (value - h[low]) / (h[high] - h[low]);
}

export function frame(plane) {
  const a = plane.angle * Math.PI / 180, t = plane.tilt * Math.PI / 180;
  const u = [Math.cos(a), Math.sin(a), 0];
  const n = [Math.sin(t) * Math.sin(a), -Math.sin(t) * Math.cos(a), Math.cos(t)];
  const v = [-Math.cos(t) * Math.sin(a), Math.cos(t) * Math.cos(a), Math.sin(t)];
  return { u, v, n };
}

export function planeBounds(plane, box) {
  const { u, v, n } = frame(plane), corners = [], d = [], intersections = [];
  for (const x of [box[0][0], box[1][0]]) for (const y of [box[0][1], box[1][1]]) for (const z of [box[0][2], box[1][2]]) {
    const p = [x, y, z]; corners.push(p); d.push(dot(p, n) - plane.offset);
  }
  for (let i = 0; i < 8; i++) for (const bit of [1, 2, 4]) {
    const j = i ^ bit;
    if (j <= i || d[i] * d[j] > 0) continue;
    if (Math.abs(d[i] - d[j]) < 1e-12) intersections.push(corners[i], corners[j]);
    else intersections.push(corners[i].map((x, k) => x + d[i] / (d[i] - d[j]) * (corners[j][k] - x)));
  }
  const values = (intersections.length ? intersections : corners).map(p => [dot(p, u), dot(p, v)]);
  const left = Math.min(...values.map(p => p[0])), bottom = Math.min(...values.map(p => p[1]));
  const right = Math.max(...values.map(p => p[0])), top = Math.max(...values.map(p => p[1]));
  const margin = Math.max((right - left) * .035, (top - bottom) * .035, .01);
  return [left - margin, bottom - margin, right + margin, top + margin];
}

export function sampleSection(field, plane, resolution = 360, bounds = null) {
  const box = bounds || planeBounds(plane, field.meta.box), { u, v, n } = frame(plane);
  const [left, bottom, right, top] = box, width = right - left, height = top - bottom;
  const nx = Math.max(16, Math.round(resolution * width / Math.max(width, height)));
  const ny = Math.max(16, Math.round(resolution * height / Math.max(width, height)));
  const size = nx * ny, magnitude = new Float32Array(size), vx = new Float32Array(size), vy = new Float32Array(size), loading = new Float32Array(size), material = new Int32Array(size), normal = new Float32Array(size);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const x = left + width * i / (nx - 1), y = bottom + height * j / (ny - 1), k = j * nx + i;
    const sample = field.sample(u[0] * x + v[0] * y + n[0] * plane.offset, u[1] * x + v[1] * y + n[1] * plane.offset, u[2] * x + v[2] * y + n[2] * plane.offset);
    if (!sample) { magnitude[k] = loading[k] = NaN; material[k] = -1; continue; }
    magnitude[k] = Math.hypot(sample.bx, sample.by, sample.bz);
    vx[k] = sample.bx * u[0] + sample.by * u[1] + sample.bz * u[2];
    vy[k] = sample.bx * v[0] + sample.by * v[1] + sample.bz * v[2];
    normal[k] = sample.bx * n[0] + sample.by * n[1] + sample.bz * n[2];
    loading[k] = sample.loading; material[k] = sample.material;
  }
  return { box, nx, ny, magnitude, vx, vy, loading, material, normal };
}

export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
