import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import { currentFlow } from "../src/current.js";

test("winding arrows obey the right-hand rule and signed current", () => {
  const points = [[-2, -1, 0], [2, -1, 0], [2, 1, 0], [-2, 1, 0]];
  const coils = [2, -1, 0].map((current_A, phase) => ({ phase, current_A, points }));
  const flow = currentFlow(coils, [[-3, -3, -3], [3, 3, 3]]);
  for (const [i, coil] of coils.entries()) {
    const arrows = flow.children[i].children.filter(mesh => mesh.geometry.type === "ConeGeometry");
    assert.equal(arrows.length > 0, coil.current_A !== 0);
    for (const arrow of arrows) {
      const direction = new THREE.Vector3(0, 1, 0).applyQuaternion(arrow.quaternion);
      assert.ok(arrow.position.clone().cross(direction).z * coil.current_A > 0);
    }
  }
  const radius = i => flow.children[i].children.find(mesh => mesh.geometry.type === "TubeGeometry" && mesh.material.color.getHex() !== 0xffffff).geometry.parameters.radius;
  assert.ok(Math.abs(radius(0) / radius(1) - 2) < 1e-12);
});

test("rectangular arrowheads stay between bends, including narrow sides", () => {
  const points = [[-.1, -10, 0], [.1, -10, 0], [.1, 10, 0], [-.1, 10, 0]];
  const midpoints = [[0, -10, 0], [.1, 0, 0], [0, 10, 0], [-.1, 0, 0]].map(p => new THREE.Vector3(...p));
  for (const current_A of [1, -1]) {
    const flow = currentFlow([{ phase: 1, current_A, points }], [[-20, -20, -20], [20, 20, 20]]);
    const arrows = flow.children[0].children.filter(mesh => mesh.geometry.type === "ConeGeometry");
    assert.equal(arrows.length, 4);
    for (const arrow of arrows) {
      assert.ok(midpoints.some(point => point.distanceTo(arrow.position) < 1e-12));
      const { height, radius } = arrow.geometry.parameters;
      const clearance = Math.min(...points.map(p => arrow.position.distanceTo(new THREE.Vector3(...p))));
      assert.ok(clearance > Math.hypot(height / 2, radius), "Arrowhead overlaps a corner");
    }
  }
});
