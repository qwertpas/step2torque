import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { chromium } from "playwright";
import { readSolution, writeSolution, base64 } from "../src/file.js";

const file = path.resolve(process.argv[2] || "../build/site/examples/mosrac.html");
const output = path.resolve("../build/qa/browser");
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, acceptDownloads: true });
const errors = [], network = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", request => { if (request.url().startsWith("http")) network.push(request.url()); });
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
await page.goto("file://" + file);
const settle = target => target.waitForFunction(() => window.motorField?.settled, { timeout: 120000 });
const visibleField = target => target.evaluate(async () => {
  const image = new Image(); image.src = document.querySelector("#three canvas").toDataURL(); await image.decode();
  const canvas = document.createElement("canvas"); canvas.width = image.width; canvas.height = image.height;
  const ctx = canvas.getContext("2d"); ctx.drawImage(image, 0, 0);
  const rgba = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let green = 0;
  for (let i = 0; i < rgba.length; i += 4) if (rgba[i + 1] > 80 && rgba[i + 1] > rgba[i] * 1.3 && rgba[i + 1] > rgba[i + 2] * 1.15) green++;
  return green;
});
await settle(page);
assert.equal(await page.locator("#busy").isVisible(), false);
console.log("initial", await page.evaluate(() => ({ cells: motorField.section.nx * motorField.section.ny, lines: motorField.lineCount, edges: motorField.edgeCount, renderMs: motorField.renderMs })));
if (process.argv[3]) {
  const reference = JSON.parse(await fs.readFile(process.argv[3], "utf8"));
  const comparison = await page.evaluate(reference => {
    let maximumB = 0, maximumH = 0, missing = 0, domains = 0;
    for (let i = 0; i < reference.points.length; i++) {
      const sample = motorField.sample(...reference.points[i]);
      if (!sample) { missing++; continue; }
      for (let j = 0; j < 3; j++) {
        maximumB = Math.max(maximumB, Math.abs([sample.bx, sample.by, sample.bz][j] - reference.b[i][j]));
        maximumH = Math.max(maximumH, Math.abs([sample.hx, sample.hy, sample.hz][j] - reference.h[i][j]));
      }
      if (sample.material !== reference.material[i]) domains++;
    }
    return { points: reference.points.length, maximumB_T: maximumB, maximumH_Am: maximumH, missing, domains };
  }, reference);
  console.log("native parity", comparison);
  assert.equal(comparison.missing, 0); assert.equal(comparison.domains, 0);
  assert.ok(comparison.maximumB_T < 2e-4, JSON.stringify(comparison));
  await fs.writeFile(path.join(output, "parity-result.json"), JSON.stringify(comparison, null, 2) + "\n");
}
await page.screenshot({ path: path.join(output, "view-3d.png") });
assert.ok(await visibleField(page) > 200, "Opaque model hides the axial field");
await page.mouse.move(750, 550); await page.mouse.down(); await page.mouse.move(750, 300, { steps: 8 }); await page.mouse.up();
assert.ok(await visibleField(page) > 200, "Opaque model hides the field when viewed from the opposite side");
await page.locator("#fit").click(); await settle(page);
assert.equal(await page.locator("#angleControl").isVisible(), false);
// Switching from a square axial texture to a short shaft section must look
// exactly like opening that shaft section directly, without old axial pixels.
await page.selectOption("#section", "longitudinal"); await settle(page);
assert.ok(await visibleField(page) > 200, "Opaque model hides the longitudinal field");
assert.equal(await page.locator("#angleControl").isVisible(), true);
const shaftAfterSwitch = await page.locator("#three canvas").screenshot();
const document = await fs.readFile(file, "utf8");
const directFile = path.join(await fs.mkdtemp(path.join(os.tmpdir(), "motor-section-")), "direct.html");
const encoded = document.match(/id="fieldData">(.*?)<\/script>/s)[1];
const data = await readSolution(Buffer.from(encoded, "base64"));
const directData = await writeSolution({ ...data.meta, plane: { angle: 0, tilt: 90, offset: 0 } }, data.arrays);
await fs.writeFile(directFile, document.replace(/(id="fieldData">)(.*?)(<\/script>)/s, (_, start, _bytes, end) => start + base64(directData) + end));
const direct = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
await direct.goto("file://" + directFile); await settle(direct);
assert.equal(shaftAfterSwitch.equals(await direct.locator("#three canvas").screenshot()), true, "Switched longitudinal texture differs from a fresh longitudinal view");
await page.screenshot({ path: path.join(output, "view-longitudinal-3d.png") });
await direct.close(); await fs.rm(path.dirname(directFile), { recursive: true });
await page.selectOption("#section", "axial"); await settle(page);
// A continuous slider sweep should accept inputs and paint coarse sections
// while the expensive field/streamline work runs off the UI thread.
const sweep = await page.evaluate(async () => {
  const slider = document.getElementById("offset"), initial = motorField.revision;
  let gap = 0, before = performance.now(), previews = 0;
  for (let i = 0; i < 24; i++) {
    slider.value = i / 10; slider.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 20));
    const now = performance.now(); gap = Math.max(gap, now - before); before = now;
    if (Math.max(motorField.section.nx, motorField.section.ny) === 180) previews++;
  }
  slider.dispatchEvent(new Event("change", { bubbles: true }));
  return { maxInputGapMs: gap, previews, rendered: motorField.revision - initial };
});
await settle(page);
assert.ok(sweep.previews > 0 && sweep.rendered > 0, JSON.stringify(sweep));
assert.equal(await page.evaluate(() => motorField.plane.offset), 2.3);
assert.equal(await page.evaluate(() => Math.max(motorField.section.nx, motorField.section.ny)), 600);
console.log("slider sweep", sweep, "final compute ms", await page.evaluate(() => motorField.computeMs));
await page.locator("#offsetValue").fill("0"); await settle(page);
const orbitBefore = await page.locator("#three canvas").screenshot();
await page.mouse.move(750, 550); await page.mouse.down(); await page.mouse.move(890, 590, { steps: 8 }); await page.mouse.up();
assert.equal(orbitBefore.equals(await page.locator("#three canvas").screenshot()), false);
await page.locator("#fit").click();
await page.locator("#mode2d").click();
await page.waitForTimeout(150);
await page.screenshot({ path: path.join(output, "view-2d.png") });
const flatBefore = await page.locator("#flat").screenshot();
await page.mouse.move(750, 550); await page.mouse.wheel(0, -300); await page.waitForTimeout(100);
assert.equal(flatBefore.equals(await page.locator("#flat").screenshot()), false);
await page.locator("#fit").click(); await page.waitForTimeout(100);
assert.equal(flatBefore.equals(await page.locator("#flat").screenshot()), true);
const fullLines = await page.evaluate(() => motorField.lineCount);
await page.locator("#density").fill("90"); await page.locator("#density").dispatchEvent("input");
await settle(page);
assert.ok(await page.evaluate(count => motorField.lineCount < count && motorField.lineCount > 0, fullLines));
const fixedLines = await page.evaluate(() => motorField.lineCount);
await page.locator("#maximum").fill("3"); await page.locator("#maximum").dispatchEvent("input");
await page.waitForTimeout(100);
assert.equal(await page.evaluate(() => motorField.lineCount), fixedLines);
await page.locator("#density").fill("180"); await page.locator("#density").dispatchEvent("input");
await page.locator("#maximum").fill("2"); await page.locator("#maximum").dispatchEvent("input");
await page.locator("#offsetValue").fill(""); await page.locator("#offsetValue").pressSequentially("-1.25");
await settle(page);
assert.equal(await page.evaluate(() => motorField.plane.offset), -1.25);
for (const kind of ["longitudinal", "oblique", "axial"]) {
  await page.selectOption("#section", kind);
  await settle(page);
  if (kind !== "axial") {
    await page.locator("#angleValue").fill("37");
    await page.locator("#angleValue").dispatchEvent("input");
    if (kind === "oblique") {
      await page.locator("#tiltValue").fill("53"); await page.locator("#tiltValue").dispatchEvent("input");
      await page.locator("#mode3d").click();
    }
  }
  await page.locator("#offsetValue").fill("1.25"); await page.locator("#offsetValue").dispatchEvent("input");
  await settle(page);
  const state = await page.evaluate(() => ({ plane: motorField.plane, displayedPlane: motorField.section.plane, lines: motorField.lineCount, edges: motorField.edgeCount, renderMs: motorField.renderMs }));
  console.log(kind, state); assert.equal(state.plane.offset, 1.25); assert.ok(state.edges > 0);
  assert.deepEqual(state.displayedPlane, state.plane);
  await page.screenshot({ path: path.join(output, `view-${kind}.png`) });
}
await page.selectOption("#metric", "loading");
await page.locator("#mode2d").click();
await page.waitForTimeout(150);
assert.equal(await page.locator("#maxUnit").textContent(), "%");
await page.screenshot({ path: path.join(output, "view-loading.png") });
await page.selectOption("#metric", "flux");
await page.locator("#air").check(); await page.locator("#lines").uncheck();
await page.waitForTimeout(150);
await page.locator("#lines").check();
await page.locator("#air").uncheck();
const downloadPromise = page.waitForEvent("download");
await page.locator("#export").click();
const download = await downloadPromise;
await download.saveAs(path.join(output, "html-export.png"));
assert.ok((await fs.stat(path.join(output, "html-export.png"))).size > 10000);
await page.setViewportSize({ width: 430, height: 780 }); await page.waitForTimeout(150);
assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
await page.screenshot({ path: path.join(output, "view-mobile.png") });
assert.deepEqual(errors, []); assert.deepEqual(network, []);
await browser.close();
console.log("Offline HTML, sections, heatmaps, 2D/3D views and PNG download passed.");
