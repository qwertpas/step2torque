import { createField, sampleSection } from "./field.js";
import { streamlines } from "./lines.js";

let field, section, cached;

self.onmessage = ({ data }) => {
  if (data.type === "init") {
    field = createField(data.meta, data.arrays);
    self.postMessage({ ready: true });
    return;
  }
  const start = performance.now();
  const key = JSON.stringify([data.plane, data.resolution, data.bounds]);
  if (key !== cached) {
    section = sampleSection(field, data.plane, data.resolution, data.bounds);
    cached = key;
  }
  const curves = data.lines ? streamlines(section, data.density, data.reference) : [];
  self.postMessage({ id: data.id, section, curves, plane: data.plane, preview: data.preview, computeMs: performance.now() - start });
};
