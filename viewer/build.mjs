import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.dirname(fileURLToPath(import.meta.url));
const packageDir = path.resolve(root, "../src/step2torque/visualization");
const output = path.resolve(root, "../build/site");
const options = { absWorkingDir: root, bundle: true, minify: true, format: "iife", write: false };
const licenses = await Promise.all(["three", "three-mesh-bvh"].map(async name =>
  name + "\n" + await fs.readFile(path.join(root, "node_modules", name, "LICENSE"), "utf8")));
const worker = await build({ ...options, entryPoints: ["src/section-worker.js"] });
const viewer = await build({
  ...options, entryPoints: ["src/viewer.js"], legalComments: "inline",
  define: { __FIELD_WORKER__: JSON.stringify(worker.outputFiles[0].text) },
  banner: { js: "/*!\n" + licenses.join("\n") + "\n*/" },
});
const runtime = viewer.outputFiles[0].text;
await fs.writeFile(path.join(packageDir, "viewer.js"), runtime);

const template = (await fs.readFile(path.join(packageDir, "viewer.html"), "utf8"))
  .replace("__SCRIPT__", () => runtime.replaceAll("</script", "<\\/script"));
const catalog = JSON.parse(await fs.readFile(path.join(root, "examples/catalog.json"), "utf8"));
await fs.rm(output, { recursive: true, force: true });
await fs.mkdir(path.join(output, "examples"), { recursive: true });
const examples = [];
for (const item of catalog) {
  const file = "examples/" + item.id + ".html";
  const data = await fs.readFile(path.join(root, "examples", item.path));
  await fs.writeFile(path.join(output, file), template.replace("__DATA__", () => data.toString("base64")));
  examples.push({ label: item.label, file });
}

const site = await build({ ...options, entryPoints: ["src/site.js"] });
const values = {
  EXAMPLES: JSON.stringify(examples).replaceAll("<", "\\u003c"),
  TEMPLATE: Buffer.from(template.replace("__DATA__", "__SOLUTION__")).toString("base64"),
  SCRIPT: site.outputFiles[0].text.replaceAll("</script", "<\\/script"),
};
const html = (await fs.readFile(path.join(root, "src/site.html"), "utf8"))
  .replace(/__(EXAMPLES|TEMPLATE|SCRIPT)__/g, (_, name) => values[name]);
await fs.writeFile(path.join(output, "index.html"), html);
await fs.writeFile(path.join(output, ".nojekyll"), "");
console.log(path.join(output, "index.html"));
