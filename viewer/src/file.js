const types = { "<f4": Float32Array, "<u4": Uint32Array, "<i4": Int32Array, "<u2": Uint16Array };

export async function readSolution(bytes) {
  try {
    const raw = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
    const decoder = new TextDecoder(), view = new DataView(raw);
    if (decoder.decode(new Uint8Array(raw, 0, 8)) !== "S2TFIELD") throw new Error();
    const length = view.getUint32(8, true), start = Math.ceil((12 + length) / 4) * 4;
    const meta = JSON.parse(decoder.decode(new Uint8Array(raw, 12, length)));
    if (meta.format !== "step2torque-field" || meta.version !== 1 || !["2d", "3d"].includes(meta.mode)) throw new Error();
    const data = raw.slice(start), arrays = {};
    for (const [name, a] of Object.entries(meta.arrays)) {
      if (!types[a.type] || !Number.isInteger(a.offset) || !Number.isInteger(a.length) || a.offset < 0 || a.length < 0) throw new Error();
      arrays[name] = new types[a.type](data, a.offset, a.length);
    }
    if (!arrays.points?.length || arrays.points.length % 3 || !arrays.cells?.length || arrays.cells.length % 4 || arrays.h?.length !== arrays.cells.length * 3 || arrays.material?.length !== arrays.cells.length / 4 || !arrays.bounds?.length || !arrays.nodes?.length || !meta.materials?.length) throw new Error();
    return { meta, arrays };
  } catch {
    throw new Error("Invalid step2torque solution file");
  }
}

export function base64(bytes) {
  const parts = [];
  for (let i = 0; i < bytes.length; i += 32768) parts.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
  return btoa(parts.join(""));
}

export async function writeSolution(meta, arrays) {
  const text = new TextEncoder().encode(JSON.stringify(meta));
  const header = new Uint8Array(Math.ceil((12 + text.length) / 4) * 4);
  header.set(new TextEncoder().encode("S2TFIELD"));
  new DataView(header.buffer).setUint32(8, text.length, true); header.set(text, 12);
  const size = Math.max(...Object.entries(meta.arrays).map(([name, a]) => a.offset + arrays[name].byteLength));
  const body = new Uint8Array(size);
  for (const [name, a] of Object.entries(meta.arrays)) body.set(new Uint8Array(arrays[name].buffer, arrays[name].byteOffset, arrays[name].byteLength), a.offset);
  return new Uint8Array(await new Response(new Blob([header, body]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());
}
