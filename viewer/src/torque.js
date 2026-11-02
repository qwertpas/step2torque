import * as THREE from "three";

export function torqueArrow(value, surfaces) {
  if (!Number.isFinite(value) || value === 0) return null;
  const rotor = surfaces.filter(surface => surface.rotating);
  if (!rotor.length) return null;
  let radius = 0, top = -Infinity;
  for (const surface of rotor) {
    const points = surface.geometry.attributes.position;
    for (let i = 0; i < points.count; i++) {
      radius = Math.max(radius, Math.hypot(points.getX(i), points.getY(i)));
      top = Math.max(top, points.getZ(i));
    }
  }
  radius *= .98;
  const sweep = Math.sign(value) * Math.PI * 2 / 3;
  const points = Array.from({ length: 65 }, (_, i) => {
    const angle = -Math.PI / 4 + sweep * (i / 64 - .5);
    return new THREE.Vector3(radius * Math.cos(angle), radius * Math.sin(angle), top + radius * .025);
  });
  const path = new THREE.CatmullRomCurve3(points), group = new THREE.Group();
  const material = new THREE.MeshBasicMaterial({ color: "#263244", transparent: true, depthTest: false, depthWrite: false });
  const halo = new THREE.MeshBasicMaterial({ color: "white", transparent: true, depthTest: false, depthWrite: false });
  // Keep the direction cue visible through the model and every section plane.
  const border = new THREE.Mesh(new THREE.TubeGeometry(path, 64, radius * .017, 8, false), halo);
  border.renderOrder = 6; group.add(border);
  const arc = new THREE.Mesh(new THREE.TubeGeometry(path, 64, radius * .009, 8, false), material);
  arc.renderOrder = 7; group.add(arc);
  const head = new THREE.Mesh(new THREE.ConeGeometry(radius * .045, radius * .16, 16), material);
  head.position.copy(path.getPoint(1));
  head.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), path.getTangent(1));
  head.renderOrder = 8; group.add(head);
  return group;
}
