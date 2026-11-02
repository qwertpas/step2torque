import * as THREE from "three";

const colors = ["#852cc0", "#008b83", "#df6500", "#2675c7", "#b12d67", "#647b13"];

export function amps(value) {
  return `${Math.abs(value) < 1e-9 ? "0" : value.toLocaleString("en-US", { maximumSignificantDigits: 3, signDisplay: "always" })} A`;
}

export function currentFlow(coils, box) {
  const group = new THREE.Group();
  const phases = [...new Set(coils.map(coil => coil.phase))];
  const maximum = Math.max(...coils.map(coil => Math.abs(coil.current_A)), 0);
  const size = Math.hypot(...box[1].map((x, i) => x - box[0][i]));
  const zero = Math.max(1e-12, maximum * 1e-10);
  group.userData.phases = phases.map((phase, i) => {
    const current_A = coils.find(coil => coil.phase === phase).current_A;
    return { phase, current_A, color: Math.abs(current_A) > zero ? colors[i % colors.length] : "#929aa5" };
  });
  for (const coil of coils) {
    const loop = new THREE.Group();
    loop.userData.coil = coil;
    const points = coil.points.map(p => new THREE.Vector3(...p));
    // Reverse traversal for negative current; the exported path follows +axis.
    if (coil.current_A < 0) points.reverse();
    const path = new THREE.CurvePath();
    for (let i = 0; i < points.length; i++) path.add(new THREE.LineCurve3(points[i], points[(i + 1) % points.length]));
    const active = Math.abs(coil.current_A) > zero;
    const ratio = active ? Math.abs(coil.current_A) / maximum : 0;
    const radius = size * Math.max(.00045, .0028 * ratio);
    const color = group.userData.phases[phases.indexOf(coil.phase)].color;
    const material = new THREE.MeshBasicMaterial({ color, transparent: true, depthTest: false, depthWrite: false });
    const halo = new THREE.MeshBasicMaterial({ color: "white", transparent: true, depthTest: false, depthWrite: false });
    // Draw over the opaque solids and the field plane, without clipping. This
    // makes the whole winding readable even where its copper has been cut away.
    const segments = Math.max(96, points.length);
    const border = new THREE.Mesh(new THREE.TubeGeometry(path, segments, radius + size * .0007, 6, true), halo);
    border.renderOrder = 3; loop.add(border);
    const wire = new THREE.Mesh(new THREE.TubeGeometry(path, segments, radius, 6, true), material);
    wire.renderOrder = 4; loop.add(wire);
    if (active) {
      // Rectangles have four corner points. Keep each whole arrowhead in the
      // middle of a straight side, with clearance from both bends.
      const markers = points.length === 4
        ? path.curves.map(side => ({ point: side.getPoint(.5), tangent: side.getTangent(.5), span: side.getLength() }))
        : [1 / 6, 1 / 2, 5 / 6].map(t => ({ point: path.getPointAt(t), tangent: path.getTangentAt(t), span: path.getLength() }));
      for (const { point, tangent, span } of markers) {
        const length = Math.min(path.getLength() * .055, size * .018, span * .35);
        const arrow = new THREE.Mesh(new THREE.ConeGeometry(length * .3, length, 12), material);
        arrow.position.copy(point);
        arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), tangent);
        arrow.renderOrder = 5; loop.add(arrow);
      }
    }
    group.add(loop);
  }
  return group;
}
