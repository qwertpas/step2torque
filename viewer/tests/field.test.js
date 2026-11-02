import assert from "node:assert/strict";
import test from "node:test";
import { createField, curve, frame, MU0, sampleSection } from "../src/field.js";
import { streamlines } from "../src/lines.js";

function fixture(law = { mur: 1 }, sectors = 1) {
  const meta = { sectors, sector_start_rad: 0, materials: [law], box: [[0, 0, 0], [1, 1, 1]] };
  const arrays = {
    points: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]), cells: new Uint32Array([0, 1, 2, 3]),
    h: new Float32Array([100, 200, 300, 200, 200, 300, 100, 300, 300, 100, 200, 400]),
    material: new Uint16Array([0]), bounds: new Float32Array([0, 0, 0, 1, 1, 1]), nodes: new Int32Array([-1, 1]),
  };
  return createField(meta, arrays);
}

test("affine field interpolation, remanence and outside-domain mask", () => {
  const field = fixture({ mur: 1.05, br: [.7, 0, 0] });
  const sample = field.sample(.1, .2, .3);
  assert.ok(Math.abs(sample.hx - 110) < 1e-10);
  assert.ok(Math.abs(sample.hy - 220) < 1e-10);
  assert.ok(Math.abs(sample.hz - 330) < 1e-10);
  assert.ok(Math.abs(sample.bx - (.7 + MU0 * 1.05 * 110)) < 1e-12);
  assert.equal(field.sample(1, 1, 1), null);
  assert.equal(field.sample(-1, 0, 0), null);
});

test("periodic sector folding rotates B back to the complete motor", () => {
  const field = fixture({ mur: 1 }, 4);
  const a = field.sample(.2, .1, .3), b = field.sample(-.1, .2, .3);
  assert.ok(Math.abs(a.bx - b.by) < 1e-12);
  assert.ok(Math.abs(a.by + b.bx) < 1e-12);
  assert.ok(Math.abs(a.bz - b.bz) < 1e-12);
});

test("nonlinear steel uses the full vector and saved curve saturation reference", () => {
  const law = { h: [0, 100, 1000], b: [0, 1, 2], reference_T: 2 - MU0 * 1000 };
  const sample = fixture(law).sample(.1, .2, .3), h = Math.hypot(110, 220, 330);
  const b = 1 + (h - 100) / 900;
  assert.ok(Math.abs(Math.hypot(sample.bx, sample.by, sample.bz) - b) < 1e-12);
  assert.ok(Math.abs(sample.loading - (b - MU0 * h) / law.reference_T * 100) < 1e-10);
  assert.equal(curve(law.h, law.b, 2000), 2 + MU0 * 1000);
});

test("axial, shaft and oblique sections retain orthonormal bases", () => {
  for (const plane of [{ angle: 0, tilt: 0, offset: .1 }, { angle: 37, tilt: 90, offset: .1 }, { angle: 71, tilt: 43, offset: .1 }]) {
    const { u, v, n } = frame(plane);
    for (const axis of [u, v, n]) assert.ok(Math.abs(Math.hypot(...axis) - 1) < 1e-14);
    const section = sampleSection(fixture(), plane, 60);
    assert.ok(section.magnitude.some(Number.isFinite));
    assert.ok(section.material.some(x => x === -1));
  }
});

test("planar streamlines cross steel, PM and air; a normal-only field has no lines", () => {
  const size = 100, section = { box: [-1, -1, 1, 1], nx: size, ny: size, vx: new Float32Array(size**2).fill(1), vy: new Float32Array(size**2), magnitude: new Float32Array(size**2).fill(1), material: new Int32Array(size**2) };
  for (let j = 0; j < size; j++) for (let i = 0; i < size; i++) section.material[j * size + i] = Math.floor(i / 34);
  const lines = streamlines(section);
  assert.ok(lines.some(line => line[0][0] < -.9 && line.at(-1)[0] > .9));
  for (const line of lines) assert.ok(line.every(p => Math.abs(p[1] - line[0][1]) < 1e-12));
  section.vx.fill(0);
  assert.deepEqual(streamlines(section), []);
});

test("four times the flux density places about four times as many lines", () => {
  const size = 120, section = { box: [-1, -1, 1, 1], nx: size, ny: size, vx: new Float32Array(size**2), vy: new Float32Array(size**2), material: new Int32Array(size**2) };
  for (let j = 0; j < size; j++) for (let i = 0; i < size; i++) section.vx[j * size + i] = j < size / 2 ? 1 : 4;
  const lines = streamlines(section, 80), low = lines.filter(line => line[0][1] < -.1).length, high = lines.filter(line => line[0][1] > .1).length;
  assert.ok(high > low * 2.8 && high < low * 5, `${high} strong-field lines / ${low} weak-field lines`);
});

test("very weak fields do not acquire an artificial minimum line density", () => {
  const size = 70, section = { box: [-1, -1, 1, 1], nx: size, ny: size, vx: new Float32Array(size**2).fill(.04), vy: new Float32Array(size**2), material: new Int32Array(size**2) };
  assert.ok(streamlines(section, 80, 4).length <= 2);
});
