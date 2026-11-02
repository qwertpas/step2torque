import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { toCreasedNormals, mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { sectionShape } from "../vendor/step-viewer/section-shape.js";
import { createField, dot, frame, sampleSection } from "./field.js";
import { streamlines } from "./lines.js";
import { readSolution, writeSolution, base64 } from "./file.js";
import { modelMaterial } from "./material.js";
import { currentFlow, amps } from "./current.js";

const $ = id => document.getElementById(id);

async function main() {
  const bytes = Uint8Array.from(atob($("fieldData").textContent.trim()), c => c.charCodeAt(0));
  const { meta, arrays } = await readSolution(bytes);
  const solved = meta.solved !== false;
  const field = solved ? createField(meta, arrays) : null;
  const plane = { ...meta.plane }, palette = colorTable(meta.colors);
  const stage = $("stage"), flat = $("flat"), context = flat.getContext("2d"), textureCanvas = document.createElement("canvas");
  let mode = meta.mode, section, curves = [], edges = [], scene, renderer, camera, controls, planeMesh, texture, surfaces, capSurfaces;
  let bounds = meta.bounds;
  let rotation = meta.angle_deg;
  let viewport, queued = false, dirty = true, density = meta.density, revision = 0, lastRenderMs = 0;
  let pending, active = false, workerReady = false, ticket = 0, interacting = false, finishTimer, settled = false, computeMs = 0;
  let textureWidth = 0, textureHeight = 0, renderWidth = 0, renderHeight = 0;
  const maxima = { flux: meta.flux_max, loading: 100 };
  maxima[meta.metric] = meta.vmax;
  const ui = { metric: meta.metric, air: meta.air, lines: meta.lines, current: Boolean(meta.current_flow) };
  const flow = currentFlow(meta.coils || [], meta.box);
  const labels = flow.children.map(loop => {
    const coil = loop.userData.coil, element = document.createElement("span");
    element.className = "phase-label"; element.textContent = String(coil.phase);
    element.style.color = flow.userData.phases.find(p => p.phase === coil.phase).color;
    $("phaseLabels").append(element);
    const center = new THREE.Vector3();
    for (const p of coil.points) center.add(new THREE.Vector3(...p));
    return { element, center: center.divideScalar(coil.points.length), coil };
  });
  const ray = new THREE.Raycaster(), pointer = new THREE.Vector2();
  const modelClip = new THREE.Plane();
  const flatView = { zoom: 1, x: 0, y: 0 };
  let drag;

  function settings() {
    for (const element of document.querySelectorAll("[data-field]")) element.hidden = !solved;
    $("rotationControl").hidden = solved;
    $("unsolved").hidden = solved;
    $("rotation").value = rotation;
    if (document.activeElement !== $("rotationValue")) $("rotationValue").value = rotation;
    const kind = $("section").value;
    $("angleControl").hidden = kind === "axial";
    $("tiltControl").hidden = kind !== "oblique";
    $("air").disabled = ui.metric === "loading";
    $("current").disabled = mode !== "3d" || !flow.children.length;
    $("currentControl").title = flow.children.length ? "Width shows |I|; arrows show conventional current. Visible through the model." : "This file has no saved winding paths.";
    flow.visible = mode === "3d" && ui.current && flow.children.length > 0;
    $("currentLegend").hidden = !flow.visible;
    $("phaseLabels").hidden = mode !== "3d" || (solved && !flow.visible);
    $("maxUnit").textContent = ui.metric === "loading" ? "%" : "T";
    $("maximum").value = maxima[ui.metric];
    const { n } = frame(plane), corners = [];
    for (const x of [meta.box[0][0], meta.box[1][0]]) for (const y of [meta.box[0][1], meta.box[1][1]]) for (const z of [meta.box[0][2], meta.box[1][2]]) corners.push(dot([x, y, z], n));
    $("offset").min = Math.min(...corners); $("offset").max = Math.max(...corners);
    for (const key of ["angle", "tilt", "offset"]) {
      $(key).value = plane[key];
      if (document.activeElement !== $(key + "Value")) $(key + "Value").value = +plane[key].toFixed(3);
    }
  }

  function outline(cut, caps = false) {
    const { u, v, n } = frame(cut);
    const transform = new THREE.Matrix4().makeBasis(new THREE.Vector3(...u), new THREE.Vector3(...v), new THREE.Vector3(...n));
    transform.setPosition(new THREE.Vector3(...n).multiplyScalar(cut.offset)).invert();
    return capSurfaces.map(surface => {
      const local = surface.mesh ? transform.clone().multiply(surface.mesh.matrix) : transform;
      const shape = sectionShape(surface.geometry, local);
      if (caps) {
        surface.cap.geometry.dispose();
        surface.cap.geometry = new THREE.BufferGeometry();
        surface.cap.geometry.setAttribute("position", new THREE.BufferAttribute(shape.fill, 3));
        surface.cap.geometry.computeVertexNormals();
        surface.cap.matrix.copy(transform).invert();
      }
      return shape.edges;
    });
  }

  function redraw() {
    if (!solved && dirty) {
      section = { plane: { ...plane } };
      edges = outline(plane, true);
      dirty = false; settled = true;
    }
    if (dirty) {
      pending = { id: ++ticket, plane: { ...plane }, bounds, resolution: interacting ? 180 : 600, preview: interacting, lines: ui.lines && !interacting, density, reference: meta.flux_max };
      dirty = false;
      settled = false;
      sendSection();
      return;
    }
    if (!section) return;
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const start = performance.now();
      if (solved) paintTexture(textureCanvas, section, curves, edges, ui, maxima[ui.metric], palette, meta.materials);
      if (mode === "2d") drawFlat(); else drawThree();
      if (solved) drawLegend($("legend").getContext("2d"), 0, 0, 380, 108, ui.metric, maxima[ui.metric], palette);
      revision++;
      lastRenderMs = performance.now() - start;
      $("busy").hidden = true;
    });
  }

  function sendSection() {
    if (!workerReady || active || !pending) return;
    active = true;
    worker.postMessage(pending);
    pending = undefined;
  }

  let worker;
  if (solved) {
    const workerUrl = URL.createObjectURL(new Blob([__FIELD_WORKER__], { type: "text/javascript" }));
    worker = new Worker(workerUrl);
    worker.onmessage = ({ data }) => {
      if (data.ready) {
        workerReady = true;
        URL.revokeObjectURL(workerUrl);
      } else {
        active = false;
        // Show completed previews during a sweep; only its latest full result
        // may settle. Each image keeps its own plane, so colours stay aligned.
        if (data.id === ticket || (data.preview && interacting)) {
          section = { ...data.section, plane: data.plane, preview: data.preview };
          curves = data.curves;
          edges = outline(data.plane, true);
          computeMs = data.computeMs;
          settled = !data.preview;
          redraw();
        }
      }
      sendSection();
    };
    worker.onerror = error => { $("busy").hidden = false; $("busy").textContent = error.message; };
    worker.postMessage({ type: "init", meta, arrays });
  }

  function finishSection() {
    if (!interacting) return;
    clearTimeout(finishTimer);
    interacting = false;
    dirty = true;
    redraw();
  }

  function drawFlat() {
    const dpr = Math.min(devicePixelRatio || 1, 2), w = Math.round(stage.clientWidth * dpr), h = Math.round(stage.clientHeight * dpr);
    flat.width = w; flat.height = h;
    context.fillStyle = "white"; context.fillRect(0, 0, w, h);
    viewport = fit(section.box, w, h, 45 * dpr, 65 * dpr);
    viewport.x += flatView.x * dpr - viewport.w * (flatView.zoom - 1) / 2;
    viewport.y += flatView.y * dpr - viewport.h * (flatView.zoom - 1) / 2;
    viewport.w *= flatView.zoom; viewport.h *= flatView.zoom;
    context.drawImage(textureCanvas, viewport.x, viewport.y, viewport.w, viewport.h);
    axes(context, section.box, viewport, dpr);
  }

  function setupThree() {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true });
    renderer.setClearColor(0xffffff); renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.localClippingEnabled = true;
    $("three").appendChild(renderer.domElement);
    scene = new THREE.Scene();
    scene.add(flow);
    scene.add(new THREE.HemisphereLight(0xf4f7ff, 0x505565, 1.6));
    const light = new THREE.DirectionalLight(0xffffff, 2.5);
    light.position.set(-60, -90, 140);
    scene.add(light);
    const fill = new THREE.DirectionalLight(0xffffff, .8);
    fill.position.set(100, 40, 70);
    scene.add(fill);
    const box = new THREE.Box3(new THREE.Vector3(...meta.box[0]), new THREE.Vector3(...meta.box[1]));
    const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length();
    camera = new THREE.PerspectiveCamera(34, 1, size / 1000, size * 30);
    camera.up.set(0, 0, 1);
    camera.position.copy(center).add(new THREE.Vector3(.6, -1, .85).normalize().multiplyScalar(size * 1.35));
    controls = new OrbitControls(camera, renderer.domElement); controls.target.copy(center);
    controls.enableDamping = false; controls.minDistance = size * .15; controls.maxDistance = size * 12;
    controls.addEventListener("change", renderScene); controls.update(); controls.saveState();
    for (const surface of surfaces) {
      const material = modelMaterial(surface); material.clippingPlanes = [modelClip];
      surface.mesh = new THREE.Mesh(surface.geometry, material);
      scene.add(surface.mesh);
    }
    // Periodic boundaries can split one winding across two component names.
    // Weld all copper for section extraction so these cuts close into solids.
    capSurfaces = surfaces.filter(s => s.kind !== "coil");
    const coils = surfaces.filter(s => s.kind === "coil");
    if (coils.length) {
      const geometry = mergeGeometries(coils.map(s => s.geometry)); geometry.computeBoundingBox();
      capSurfaces.push({ kind: "coil", geometry });
    }
    for (const surface of capSurfaces) {
      surface.cap = new THREE.Mesh(new THREE.BufferGeometry(), modelMaterial(surface));
      surface.cap.matrixAutoUpdate = false;
      scene.add(surface.cap);
    }
    if (!solved) return;
    texture = makeTexture();
    planeMesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide, transparent: true, alphaTest: .02, depthWrite: true, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2 }));
    planeMesh.renderOrder = 1; scene.add(planeMesh);
  }

  function makeTexture() {
    const image = new THREE.CanvasTexture(textureCanvas);
    image.colorSpace = THREE.SRGBColorSpace;
    image.minFilter = THREE.LinearFilter; image.magFilter = THREE.LinearFilter; image.generateMipmaps = false;
    textureWidth = textureCanvas.width; textureHeight = textureCanvas.height;
    return image;
  }

  function renderScene() {
    if (!renderer || mode !== "3d" || !section) return;
    const { n } = frame(section.plane), offset = section.plane.offset;
    const side = dot(n, camera.position.toArray()) >= offset ? -1 : 1;
    // Keep the half behind the cut so opaque solids cannot hide the field.
    // Float32 clipping must not discard fragments of a coincident end face.
    const tolerance = Math.hypot(...meta.box[1].map((x, i) => x - meta.box[0][i])) * 1e-6;
    modelClip.set(new THREE.Vector3(...n).multiplyScalar(side), -side * offset + tolerance);
    renderer.render(scene, camera);
    for (const label of labels) {
      const p = label.center.clone().project(camera);
      label.element.style.left = (p.x + 1) / 2 * stage.clientWidth + "px";
      label.element.style.top = (1 - p.y) / 2 * stage.clientHeight + "px";
      label.element.hidden = p.z < -1 || p.z > 1;
    }
  }

  function drawThree() {
    if (renderWidth !== stage.clientWidth || renderHeight !== stage.clientHeight) {
      renderWidth = stage.clientWidth; renderHeight = stage.clientHeight;
      renderer.setSize(renderWidth, renderHeight);
      camera.aspect = renderWidth / renderHeight;
    }
    camera.zoom = Math.min(1, camera.aspect); camera.updateProjectionMatrix();
    if (!solved) { renderScene(); return; }
    const { u, v, n } = frame(section.plane), [left, bottom, right, top] = section.box;
    const matrix = new THREE.Matrix4().makeBasis(new THREE.Vector3(...u), new THREE.Vector3(...v), new THREE.Vector3(...n));
    planeMesh.quaternion.setFromRotationMatrix(matrix);
    planeMesh.position.set(...n).multiplyScalar(section.plane.offset).addScaledVector(new THREE.Vector3(...u), (left + right) / 2).addScaledVector(new THREE.Vector3(...v), (bottom + top) / 2);
    planeMesh.scale.set(right - left, top - bottom, 1);
    // WebGL texture storage is immutable after upload. A new section aspect
    // needs fresh storage, otherwise pixels from the previous cut survive.
    if (textureWidth !== textureCanvas.width || textureHeight !== textureCanvas.height) {
      texture.dispose();
      texture = makeTexture();
      planeMesh.material.map = texture;
    }
    texture.needsUpdate = true;
    renderScene();
  }

  surfaces = meta.surfaces.map(surface => {
    const triangles = arrays[surface.array], positions = new Float32Array(triangles.length * 3 * meta.sectors);
    let cursor = 0;
    for (let j = 0; j < meta.sectors; j++) {
      const a = j * Math.PI * 2 / meta.sectors, c = Math.cos(a), s = Math.sin(a);
      for (const index of triangles) {
        const p = index * 3, x = arrays.points[p], y = arrays.points[p + 1];
        positions[cursor++] = c * x - s * y; positions[cursor++] = s * x + c * y; positions[cursor++] = arrays.points[p + 2];
      }
    }
    let geometry = new THREE.BufferGeometry(); geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry = toCreasedNormals(geometry, THREE.MathUtils.degToRad(35));
    geometry.computeBoundingBox();
    return { ...surface, geometry };
  });
  setupThree();
  function rotateMotor() {
    for (const surface of surfaces) if (surface.rotating) {
      const angle = rotation * Math.PI / 180;
      surface.mesh.rotation.z = angle; surface.mesh.updateMatrix();
      for (const material of [surface.mesh.material, surface.cap.material]) {
        if (material.userData.poleRotation) material.userData.poleRotation.value = angle;
      }
    }
  }
  if (!solved) {
    rotateMotor(); document.title = "Motor setup";
    $("saveSolution").textContent = "Save setup";
    $("currentLegend").title = "Preview phase currents; default 1 A per phase";
  }
  flat.hidden = mode !== "2d"; $("three").hidden = mode !== "3d";
  $("mode2d").setAttribute("aria-pressed", mode === "2d"); $("mode3d").setAttribute("aria-pressed", mode === "3d");
  $("section").value = plane.tilt % 180 === 0 ? "axial" : plane.tilt === 90 ? "longitudinal" : "oblique";
  if ($("section").value === "axial") plane.angle = 0;
  $("metric").value = ui.metric; $("air").checked = ui.air; $("lines").checked = ui.lines;
  $("current").checked = ui.current;
  for (const phase of flow.userData.phases) {
    const row = document.createElement("div"), swatch = document.createElement("span"), value = document.createElement("span");
    swatch.style.background = phase.color;
    value.textContent = `Phase ${phase.phase}  ${amps(phase.current_A)}`;
    row.append(swatch, value); $("currentLegend").append(row);
  }
  $("density").value = density;
  $("metric").querySelector('[value="loading"]').disabled = !meta.materials.some(m => m.reference_T);
  settings();
  for (const id of ["rotation", "rotationValue"]) $(id).addEventListener("input", () => {
    if (solved || $(id).value === "" || !$(id).validity.valid) return;
    const value = Number($(id).value);
    if (!Number.isFinite(value) || value < -180 || value > 180) return;
    rotation = value; rotateMotor(); settings(); dirty = true; redraw();
  });
  $("section").addEventListener("change", () => {
    plane.tilt = { axial: 0, longitudinal: 90, oblique: 45 }[$("section").value]; plane.offset = 0;
    if ($("section").value === "axial") plane.angle = 0;
    clearTimeout(finishTimer); interacting = false;
    settings(); dirty = true; redraw();
  });
  for (const key of ["angle", "tilt", "offset"]) for (const id of [key, key + "Value"]) $(id).addEventListener("input", () => {
    if ($(id).value === "" || $(id).validity.badInput) return;
    const value = Number($(id).value);
    if (!Number.isFinite(value) || (key === "tilt" && (value < 0 || value > 180))) return;
    plane[key] = value;
    if ($(id).type === "range") {
      interacting = true;
      clearTimeout(finishTimer);
      finishTimer = setTimeout(finishSection, 160);
    }
    settings(); dirty = true; redraw();
  });
  for (const key of ["angle", "tilt", "offset"]) {
    $(key).addEventListener("change", finishSection);
    $(key).addEventListener("pointerup", finishSection);
    $(key).addEventListener("pointercancel", finishSection);
  }
  for (const key of ["metric", "air", "lines", "current"]) $(key).addEventListener("change", () => {
    ui[key] = key === "metric" ? $(key).value : $(key).checked;
    if (key === "lines" && ui.lines && !curves.length) dirty = true;
    settings(); redraw();
  });
  $("maximum").addEventListener("input", () => {
    if ($("maximum").value === "" || $("maximum").validity.badInput) return;
    const value = Number($("maximum").value); if (!Number.isFinite(value) || value <= 0) return;
    maxima[ui.metric] = value; redraw();
  });
  $("density").addEventListener("input", () => {
    if ($("density").value === "" || $("density").validity.badInput) return;
    const value = Number($("density").value); if (!Number.isFinite(value) || value < 10 || value > 500) return;
    density = value; dirty = true; redraw();
  });
  for (const m of ["2d", "3d"]) $("mode" + m).addEventListener("click", () => {
    mode = m; flat.hidden = m !== "2d"; $("three").hidden = m !== "3d";
    if (m === "3d" && bounds) { bounds = null; dirty = true; Object.assign(flatView, { zoom: 1, x: 0, y: 0 }); }
    $("mode2d").setAttribute("aria-pressed", m === "2d"); $("mode3d").setAttribute("aria-pressed", m === "3d");
    settings(); redraw();
  });
  function fitView() {
    if (bounds) { bounds = null; dirty = true; }
    Object.assign(flatView, { zoom: 1, x: 0, y: 0 });
    controls.reset(); redraw();
  }
  $("fit").addEventListener("click", fitView);
  flat.addEventListener("dblclick", fitView);
  flat.addEventListener("wheel", event => {
    event.preventDefault();
    const rect = flat.getBoundingClientRect(), old = flatView.zoom;
    flatView.zoom = Math.max(.5, Math.min(30, old * Math.exp(-event.deltaY * .0015)));
    const ratio = flatView.zoom / old, dpr = flat.width / rect.width;
    const cx = (viewport.x + viewport.w / 2) / dpr, cy = (viewport.y + viewport.h / 2) / dpr;
    flatView.x += (event.clientX - rect.left - cx) * (1 - ratio);
    flatView.y += (event.clientY - rect.top - cy) * (1 - ratio);
    drawFlat();
  }, { passive: false });
  flat.addEventListener("pointerdown", event => { drag = { x: event.clientX, y: event.clientY }; flat.setPointerCapture(event.pointerId); });
  flat.addEventListener("pointermove", event => {
    if (!drag) return;
    flatView.x += event.clientX - drag.x; flatView.y += event.clientY - drag.y;
    drag = { x: event.clientX, y: event.clientY }; drawFlat();
  });
  flat.addEventListener("pointerup", event => { drag = undefined; flat.releasePointerCapture(event.pointerId); });
  flat.addEventListener("pointercancel", () => { drag = undefined; });
  stage.addEventListener("pointermove", event => {
    if (!section || drag || event.target === $("legend")) return;
    const cut = section.plane, rect = stage.getBoundingClientRect(), { u, v, n } = frame(cut);
    let point;
    if (mode === "2d") {
      const x = (event.clientX - rect.left) * flat.width / rect.width, y = (event.clientY - rect.top) * flat.height / rect.height;
      const a = section.box[0] + (x - viewport.x) / viewport.w * (section.box[2] - section.box[0]);
      const b = section.box[3] - (y - viewport.y) / viewport.h * (section.box[3] - section.box[1]);
      point = u.map((value, i) => value * a + v[i] * b + n[i] * cut.offset);
    } else {
      pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
      ray.setFromCamera(pointer, camera);
      if (flow.visible) {
        const hit = ray.intersectObjects(flow.children, true)[0];
        if (hit) {
          const coil = hit.object.parent.userData.coil;
          $("status").textContent = `${coil.name} · Phase ${coil.phase} · ${amps(coil.current_A)} · ${coil.turns} turns`;
          return;
        }
      }
      if (!solved) {
        const hit = ray.intersectObjects(surfaces.map(s => s.mesh))[0];
        const surface = hit && surfaces.find(s => s.mesh === hit.object);
        $("status").textContent = surface ? `${surface.name} · ${surface.kind} · ${surface.rotating ? "Rotor" : "Stator"}` : "";
        return;
      }
      const hit = ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(...n), -cut.offset), new THREE.Vector3());
      if (!hit) return; point = hit.toArray();
    }
    const sample = field.sample(...point);
    $("status").textContent = sample ? `${meta.materials[sample.material].name} · ${Math.hypot(sample.bx, sample.by, sample.bz).toFixed(3)} T${Number.isFinite(sample.loading) ? " · " + sample.loading.toFixed(1) + "%" : ""} · ${point.map(x => x.toFixed(1)).join(", ")} mm` : "Outside solved domain";
  });
  stage.addEventListener("pointerleave", () => { $("status").textContent = ""; });
  function savedData() {
    return writeSolution({ ...meta, plane: { ...plane }, metric: ui.metric, air: ui.air, lines: ui.lines, current_flow: ui.current, density, vmax: maxima[ui.metric], mode, bounds, angle_deg: solved ? meta.angle_deg : rotation }, arrays);
  }
  $("saveSolution").addEventListener("click", async () => {
    $("saveSolution").disabled = true;
    try {
      const saved = await savedData();
      const url = URL.createObjectURL(new Blob([saved], { type: "application/octet-stream" })), link = document.createElement("a");
      link.href = url; link.download = "motor.s2t"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { $("saveSolution").disabled = false; }
  });
  $("exportHtml").addEventListener("click", async () => {
    const clone = document.documentElement.cloneNode(true);
    for (const button of clone.querySelectorAll("button")) button.disabled = false;
    for (const id of ["three", "currentLegend", "phaseLabels"]) clone.querySelector("#" + id).replaceChildren();
    clone.querySelector("#fieldData").textContent = base64(await savedData());
    clone.querySelector("#busy").hidden = false;
    const url = URL.createObjectURL(new Blob(["<!doctype html>\n" + clone.outerHTML], { type: "text/html" })), link = document.createElement("a");
    link.href = url; link.download = "motor.html"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("export").addEventListener("click", async () => {
    $("export").disabled = true;
    await new Promise(resolve => requestAnimationFrame(resolve));
    try {
      if (!solved) {
        renderScene();
        const canvas = document.createElement("canvas"), source = renderer.domElement;
        canvas.width = source.width; canvas.height = source.height;
        const ctx = canvas.getContext("2d"), scale = canvas.width / stage.clientWidth;
        ctx.drawImage(source, 0, 0);
        ctx.font = `600 ${11 * scale}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        for (const label of labels) if (!label.element.hidden) {
          const p = label.center.clone().project(camera), text = String(label.coil.phase);
          const x = (p.x + 1) / 2 * canvas.width, y = (1 - p.y) / 2 * canvas.height;
          const width = ctx.measureText(text).width + 10 * scale, height = 20 * scale;
          ctx.fillStyle = "white"; ctx.fillRect(x - width / 2, y - height / 2, width, height);
          ctx.strokeStyle = label.element.style.color; ctx.strokeRect(x - width / 2, y - height / 2, width, height);
          ctx.fillStyle = label.element.style.color; ctx.fillText(text, x, y);
        }
        ctx.font = `${12 * scale}px system-ui`; ctx.textAlign = "left";
        if (flow.visible) for (const [i, phase] of flow.userData.phases.entries()) {
          const y = (30 + 24 * i) * scale;
          ctx.fillStyle = "white"; ctx.fillRect(18 * scale, y - 12 * scale, 180 * scale, 24 * scale);
          ctx.fillStyle = phase.color; ctx.fillRect(27 * scale, y - scale, 18 * scale, 3 * scale);
          ctx.fillStyle = "#263244"; ctx.fillText(`Phase ${phase.phase}  ${amps(phase.current_A)}`, 52 * scale, y);
        }
        ctx.textAlign = "right"; ctx.fillStyle = "#526173";
        ctx.fillText(`Unsolved · ${rotation}°`, canvas.width - 20 * scale, 30 * scale);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png")), url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url; link.download = "motor-setup.png"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        return;
      }
      const high = sampleSection(field, plane, 1000, bounds), canvas = document.createElement("canvas"), image = document.createElement("canvas");
      paintTexture(image, high, ui.lines ? streamlines(high, density, meta.flux_max) : [], outline(plane), ui, maxima[ui.metric], palette, meta.materials);
      canvas.width = 1600; canvas.height = Math.max(700, Math.min(1800, 1480 * image.height / image.width + 140));
      const ctx = canvas.getContext("2d"); ctx.fillStyle = "white"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      const view = fit(high.box, canvas.width, canvas.height, 70, 100);
      ctx.drawImage(image, view.x, view.y, view.w, view.h); axes(ctx, high.box, view, 1.5);
      drawLegend(ctx, canvas.width - 400, canvas.height - 100, 360, 100, ui.metric, maxima[ui.metric], palette);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png")), url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `motor-${ui.metric}.png`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { $("export").disabled = false; }
  });
  new ResizeObserver(() => {
    stage.style.height = Math.max(320, innerHeight - $("toolbar").offsetHeight) + "px";
    if (section) { if (mode === "2d") drawFlat(); else drawThree(); }
  }).observe($("toolbar"));
  window.addEventListener("resize", () => { if (section) redraw(); });
  window.addEventListener("pagehide", () => { worker?.terminate(); controls.dispose(); renderer.dispose(); }, { once: true });
  // A small read-only inspection surface also supports numeric parity checks.
  window.motorField = { solved, sample: field?.sample, get rotation() { return rotation; }, get components() { return surfaces.map(s => ({ name: s.name, rotating: Boolean(s.rotating), matrix: s.mesh.matrix.toArray() })); }, get plane() { return { ...plane }; }, get revision() { return revision; }, get renderMs() { return lastRenderMs; }, get computeMs() { return computeMs; }, get settled() { return settled && !active && !pending && !queued; }, get section() { return section; }, get lineCount() { return curves.length; }, get edgeCount() { return edges.reduce((n, e) => n + e.length / 6, 0); } };
  redraw();
}

function colorTable(colors) {
  const values = colors.map(color => [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16))), table = [];
  for (let i = 0; i < 256; i++) {
    const position = i / 255 * (values.length - 1), a = Math.min(values.length - 2, Math.floor(position)), t = position - a;
    table.push(values[a].map((value, j) => Math.round(value * (1 - t) + values[a + 1][j] * t)));
  }
  return table;
}

function paintTexture(canvas, section, lines, edges, ui, maximum, palette, materials) {
  const { nx, ny, magnitude, loading, material, box } = section;
  // Keep geometry outlines and lines crisp while the field raster stays finite.
  const scale = Math.max(1, (section.preview ? 600 : 1200) / Math.max(nx, ny));
  canvas.width = Math.round(nx * scale); canvas.height = Math.round(ny * scale);
  const raster = document.createElement("canvas"); raster.width = nx; raster.height = ny;
  const pixels = new ImageData(nx, ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i, id = material[k], value = ui.metric === "flux" ? magnitude[k] : loading[k];
    if (id < 0 || !Number.isFinite(value) || (ui.metric === "flux" && !ui.air && materials[id].kind === "air")) continue;
    const color = palette[Math.min(255, Math.max(0, Math.round(value / maximum * 255)))], p = ((ny - 1 - j) * nx + i) * 4;
    pixels.data[p] = color[0]; pixels.data[p + 1] = color[1]; pixels.data[p + 2] = color[2]; pixels.data[p + 3] = 255;
  }
  raster.getContext("2d").putImageData(pixels, 0, 0);
  const ctx = canvas.getContext("2d"); ctx.imageSmoothingEnabled = false; ctx.drawImage(raster, 0, 0, canvas.width, canvas.height);
  const map = p => [(p[0] - box[0]) / (box[2] - box[0]) * canvas.width, (box[3] - p[1]) / (box[3] - box[1]) * canvas.height];
  if (ui.lines) {
    ctx.strokeStyle = "#202936ab"; ctx.lineWidth = .72 * Math.max(canvas.width, canvas.height) / 1000; ctx.beginPath();
    for (const line of lines) {
      line.forEach((p, i) => { const q = map(p); if (i) ctx.lineTo(...q); else ctx.moveTo(...q); });
    }
    ctx.stroke();
    // One restrained direction marker per sufficiently long curve.
    ctx.fillStyle = "#263243b0";
    for (const line of lines) {
      if (line.length < 30) continue;
      const i = Math.floor(line.length * .55), a = map(line[i]), b = map(line[i + 1]);
      const angle = Math.atan2(b[1] - a[1], b[0] - a[0]), r = Math.max(canvas.width, canvas.height) / 400;
      ctx.beginPath(); ctx.moveTo(a[0] + r * Math.cos(angle), a[1] + r * Math.sin(angle));
      ctx.lineTo(a[0] + r * Math.cos(angle + 2.5), a[1] + r * Math.sin(angle + 2.5));
      ctx.lineTo(a[0] + r * Math.cos(angle - 2.5), a[1] + r * Math.sin(angle - 2.5)); ctx.closePath(); ctx.fill();
    }
  }
  ctx.strokeStyle = "#303642"; ctx.lineWidth = .8 * Math.max(canvas.width, canvas.height) / 1000; ctx.beginPath();
  for (const segments of edges) for (let i = 0; i < segments.length; i += 6) { ctx.moveTo(...map([segments[i], segments[i + 1]])); ctx.lineTo(...map([segments[i + 3], segments[i + 4]])); }
  ctx.stroke();
}

function fit(box, width, height, padding, bottom) {
  const w = box[2] - box[0], h = box[3] - box[1], scale = Math.min((width - 2 * padding) / w, (height - padding - bottom) / h);
  return { x: (width - w * scale) / 2, y: (height - bottom + padding - h * scale) / 2, w: w * scale, h: h * scale };
}

function axes(ctx, box, view, dpr) {
  ctx.font = `${11 * dpr}px system-ui`; ctx.fillStyle = "#788493"; ctx.textAlign = "center";
  const tick = (length) => { const raw = length / 5, base = 10 ** Math.floor(Math.log10(raw)), t = raw / base; return base * (t < 2 ? 2 : t < 5 ? 5 : 10); };
  const sx = tick(box[2] - box[0]), sy = tick(box[3] - box[1]);
  for (let x = Math.ceil(box[0] / sx) * sx; x <= box[2]; x += sx) ctx.fillText(+x.toPrecision(5), view.x + (x - box[0]) / (box[2] - box[0]) * view.w, view.y + view.h + 18 * dpr);
  ctx.textAlign = "right";
  for (let y = Math.ceil(box[1] / sy) * sy; y <= box[3]; y += sy) ctx.fillText(+y.toPrecision(5), view.x - 8 * dpr, view.y + (box[3] - y) / (box[3] - box[1]) * view.h + 4 * dpr);
  ctx.fillText("mm", view.x + view.w, view.y + view.h + 34 * dpr);
}

function drawLegend(ctx, x, y, width, height, metric, maximum, palette) {
  ctx.clearRect(x, y, width, height); ctx.fillStyle = "#fffffff0"; ctx.fillRect(x, y, width, height);
  ctx.font = `${height * .23}px system-ui`; ctx.fillStyle = "#47566a"; ctx.textAlign = "left";
  ctx.fillText(metric === "flux" ? "Flux density |B| · T" : "Steel loading · %", x + 12, y + height * .25);
  const left = x + 12, w = width - 24;
  for (let i = 0; i < 256; i++) { ctx.fillStyle = `rgb(${palette[i].join(",")})`; ctx.fillRect(left + w * i / 256, y + height * .4, w / 256 + .5, height * .2); }
  ctx.font = `${height * .21}px system-ui`; ctx.fillStyle = "#647186";
  for (let i = 0; i <= 4; i++) { ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center"; ctx.fillText(+(maximum * i / 4).toFixed(2), left + w * i / 4, y + height * .9); }
}

main().catch(error => { $("busy").textContent = error.message; console.error(error); });
