import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { readSolution } from "../src/file.js";

const file = path.resolve(process.argv[2] || "../build/site/examples/simple.html");
const output = path.resolve("../build/qa/current");
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "motor-current-"));
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
const errors = [], network = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
page.on("request", request => { if (request.url().startsWith("http")) network.push(request.url()); });
const settle = () => page.waitForFunction(() => window.motorField?.settled, { timeout: 120000 });
const masks = () => page.evaluate(() => {
  const canvas = document.querySelector("#three canvas"), copy = document.createElement("canvas");
  copy.width = canvas.width; copy.height = canvas.height;
  const ctx = copy.getContext("2d"); ctx.drawImage(canvas, 0, 0);
  const pixels = ctx.getImageData(0, 0, copy.width, copy.height).data;
  const colors = [[0, 139, 131], [223, 101, 0]], masks = colors.map(() => []);
  for (let i = 0; i < pixels.length; i += 4) for (let j = 0; j < colors.length; j++) {
    if (colors[j].every((value, k) => value === pixels[i + k])) masks[j].push(i / 4);
  }
  return { width: copy.width, masks };
});

try {
  await page.goto("file://" + file); await settle();
  // Section controls can wrap the toolbar. Hold the canvas size fixed while
  // checking that only the section changes, then reload normally for mobile.
  await page.addStyleTag({ content: "#stage{height:900px!important}" }); await settle();
  assert.ok(await page.locator("#current").isEnabled());
  assert.equal(await page.locator("#current").isChecked(), false);
  assert.equal(await page.locator("#currentLegend").isVisible(), false);
  const before = await page.locator("#three canvas").screenshot();
  await page.locator("#current").check(); await settle();
  assert.equal(before.equals(await page.locator("#three canvas").screenshot()), false);
  assert.equal(await page.locator("#currentLegend").innerText(), "Phase A 0 A\nPhase B +3.39 A\nPhase C -3.39 A");
  const original = await masks();
  assert.ok(original.masks.every(mask => mask.length > 2000));
  await page.screenshot({ path: path.join(output, "simple.png") });
  const rect = await page.locator("#three canvas").boundingBox();
  const pixel = original.masks[0][Math.floor(original.masks[0].length / 2)];
  await page.mouse.move(rect.x + pixel % original.width + .5, rect.y + Math.floor(pixel / original.width) + .5);
  assert.match(await page.locator("#status").innerText(), /coil\d+ · Phase [BC] · [+-]3.39 A · 30 turns/);

  const overlap = [];
  for (const [kind, position] of [["axial", 6], ["axial", -6], ["longitudinal", 2], ["oblique", 1]]) {
    await page.selectOption("#section", kind);
    if (kind !== "axial") await page.locator("#angleValue").fill("37");
    await page.locator("#offsetValue").fill(String(position)); await settle();
    const actual = await masks();
    const match = original.masks.map((mask, i) => {
      const expected = new Set(mask);
      return actual.masks[i].filter(pixel => expected.has(pixel)).length / Math.max(mask.length, actual.masks[i].length);
    });
    assert.ok(match.every(value => value > .99), `Clipped winding at ${kind} ${position}: ${match}`);
    overlap.push({ kind, position, match });
  }
  await page.screenshot({ path: path.join(output, "oblique.png") });
  const orbitBefore = await page.locator("#three canvas").screenshot();
  await page.mouse.move(750, 500); await page.mouse.down(); await page.mouse.move(900, 550, { steps: 8 }); await page.mouse.up();
  assert.equal(orbitBefore.equals(await page.locator("#three canvas").screenshot()), false);
  assert.ok((await masks()).masks.every(mask => mask.length > 1000));
  await page.locator("#fit").click(); await settle();
  await page.locator("#mode2d").click(); await settle();
  assert.equal(await page.locator("#current").isEnabled(), false);
  assert.equal(await page.locator("#currentLegend").isVisible(), false);
  await page.locator("#mode3d").click(); await settle();
  assert.ok(await page.locator("#current").isChecked());
  await page.locator("#current").uncheck(); await settle();
  assert.equal(await page.locator("#currentLegend").isVisible(), false);
  await page.locator("#current").check(); await settle();

  const document = await fs.readFile(file, "utf8");
  const originalData = await readSolution(Buffer.from(document.match(/id="fieldData">(.*?)<\/script>/s)[1], "base64"));
  const download = page.waitForEvent("download");
  await page.locator("#saveSolution").click();
  const saved = path.join(temporary, "current.s2t"); await (await download).saveAs(saved);
  const bytes = await fs.readFile(saved), reopened = await readSolution(bytes);
  assert.equal(reopened.meta.current_flow, true);
  assert.equal(reopened.meta.coils.length, 6, "Periodic sectors must not duplicate physical coils");
  assert.deepEqual(reopened.meta.coils, originalData.meta.coils);
  for (const name of Object.keys(originalData.arrays)) assert.deepEqual(reopened.arrays[name], originalData.arrays[name], name);
  const reload = path.join(temporary, "reopened.html");
  await fs.writeFile(reload, document.replace(/(id="fieldData">)(.*?)(<\/script>)/s, (_, start, _data, end) => start + bytes.toString("base64") + end));
  await page.goto("file://" + reload); await settle();
  assert.ok(await page.locator("#current").isChecked());
  assert.ok(await page.locator("#currentLegend").isVisible());
  await page.setViewportSize({ width: 430, height: 780 }); await settle();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: path.join(output, "mobile.png") });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("file://" + path.join(path.dirname(file), "mosrac.html")); await settle();
  await page.locator("#current").check(); await settle();
  assert.equal(await page.locator("#currentLegend").innerText(), "Phase 1 +0.0276 A\nPhase 3 -2.09 A\nPhase 2 +2.07 A");
  await page.screenshot({ path: path.join(output, "mosrac.png") });
  await page.goto("file://" + path.join(path.dirname(file), "double-sphere.html")); await settle();
  assert.equal(await page.locator("#currentLegend").isVisible(), false);
  assert.deepEqual(errors, []); assert.deepEqual(network, []);
  await fs.writeFile(path.join(output, "report.json"), JSON.stringify({ overlap, errors, network }, null, 2) + "\n");
  console.log("Current toggle, unclipped loops, orbit, hover, zero/reversed currents, save/reload and mobile passed offline.");
} finally {
  await browser.close(); await fs.rm(temporary, { recursive: true });
}
