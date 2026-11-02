import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { readSolution } from "../src/file.js";

const file = path.resolve(process.argv[2] || "../build/site/index.html");
const output = path.resolve("../build/qa/site");
await fs.mkdir(output, { recursive: true });
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "motor-site-"));
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, acceptDownloads: true });
const errors = [], network = [], report = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
page.on("request", request => { if (request.url().startsWith("http")) network.push(request.url()); });
const settle = frame => frame.waitForFunction(() => window.motorField?.settled, { timeout: 120000 });

try {
  await page.goto("file://" + file);
  const frame = await page.locator("#viewer").elementHandle().then(element => element.contentFrame());
  const examples = await page.locator("#example option").evaluateAll(options => options.map(o => ({ file: o.value, label: o.textContent })));
  assert.ok(examples.length > 0);
  for (const example of examples) {
    await page.selectOption("#example", example.file);
    await frame.waitForURL(url => url.pathname.endsWith("/" + example.file));
    await settle(frame);
    if (!await frame.evaluate(() => motorField.solved)) {
      assert.ok(await frame.locator("#rotationControl").isVisible());
      assert.ok(await frame.locator("#current").isChecked());
      assert.equal(await frame.locator("#metric").isVisible(), false);
      assert.equal(await frame.locator(".phase-label").count(), 6);
      report.push({ label: example.label, solved: false });
      console.log(example.label, "geometry and phase labels");
      continue;
    }
    const state = await frame.evaluate(() => {
      const s = motorField.section;
      let finite = 0, maximum = 0;
      for (let i = 0; i < s.magnitude.length; i++) if (s.material[i] >= 0 && Number.isFinite(s.magnitude[i])) { finite++; maximum = Math.max(maximum, s.magnitude[i]); }
      return { plane: motorField.plane, box: s.box, lines: motorField.lineCount, edges: motorField.edgeCount, finite, maximum_T: maximum };
    });
    assert.ok(state.finite > 0 && state.edges > 0 && state.lines > 0, JSON.stringify({ example, state }));
    await page.screenshot({ path: path.join(output, path.basename(example.file, ".html") + ".png") });
    report.push({ label: example.label, ...state });
    console.log(example.label, state.finite, "samples,", state.lines, "lines");
    if (/analytic|reluctance/.test(example.file)) {
      assert.equal(await frame.locator("#mode2d").getAttribute("aria-pressed"), "true");
      assert.equal(await frame.locator("#angleControl").isVisible(), false);
      await frame.locator("#mode3d").click(); await settle(frame);
      assert.notDeepEqual(await frame.evaluate(() => motorField.section.box), state.box, "3D must restore the full coil context");
    }
  }

  const mosrac = examples.find(example => example.file.includes("mosrac"));
  await page.selectOption("#example", mosrac.file);
  await frame.waitForURL(url => url.pathname.endsWith("/" + mosrac.file)); await settle(frame);
  await frame.selectOption("#metric", "loading");
  await frame.locator("#lines").uncheck();
  for (const position of [0, 7, 9.65]) {
    await frame.locator("#offsetValue").fill(String(position)); await settle(frame);
    await page.screenshot({ path: path.join(output, `mosrac-copper-${position}.png`) });
  }
  await frame.locator("#offsetValue").fill("12"); await settle(frame);
  await page.screenshot({ path: path.join(output, "mosrac-poles.png") });

  // A browser export must retain the entire solved field and current view.
  await frame.selectOption("#section", "longitudinal");
  await frame.locator("#angleValue").fill("37");
  await frame.locator("#offsetValue").fill("1.25"); await settle(frame);
  const before = await frame.evaluate(() => motorField.sample(28, 0, 0));
  const downloadPromise = page.waitForEvent("download");
  await frame.locator("#saveSolution").click();
  const saved = path.join(output, "browser.s2t");
  await (await downloadPromise).saveAs(saved);
  const bytes = await fs.readFile(saved), reopened = await readSolution(bytes);
  assert.deepEqual(reopened.meta.plane, { angle: 37, tilt: 90, offset: 1.25 });
  assert.equal(reopened.meta.metric, "loading");
  const originalHtml = await fs.readFile(path.join(path.dirname(file), mosrac.file), "utf8");
  const original = await readSolution(Buffer.from(originalHtml.match(/id="fieldData">(.*?)<\/script>/s)[1], "base64"));
  for (const name of Object.keys(original.arrays)) assert.deepEqual(reopened.arrays[name], original.arrays[name], name);

  await page.locator("#open").setInputFiles(saved);
  await frame.waitForURL("about:srcdoc"); await settle(frame);
  assert.equal(await page.locator("#message").textContent(), "browser.s2t");
  assert.deepEqual(await frame.evaluate(() => motorField.plane), reopened.meta.plane);
  assert.deepEqual(await frame.evaluate(() => motorField.sample(28, 0, 0)), before);
  await page.screenshot({ path: path.join(output, "local-solution.png") });
  const invalid = path.join(temporary, "invalid.s2t");
  await fs.writeFile(invalid, "not a solution");
  await page.locator("#open").setInputFiles(invalid);
  await page.waitForFunction(() => document.querySelector("#message").textContent.includes("Invalid"));
  assert.deepEqual(await frame.evaluate(() => motorField.plane), reopened.meta.plane);
  await page.selectOption("#example", mosrac.file);
  await frame.waitForURL(url => url.pathname.endsWith("/" + mosrac.file)); await settle(frame);
  await page.setViewportSize({ width: 430, height: 780 });
  await page.waitForTimeout(150);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.ok(await frame.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  const clipped = await frame.evaluate(async () => {
    const canvas = document.querySelector("#three canvas"), image = new Image();
    image.src = canvas.toDataURL(); await image.decode();
    const flat = document.createElement("canvas"); flat.width = image.width; flat.height = image.height;
    const ctx = flat.getContext("2d"); ctx.drawImage(image, 0, 0);
    let count = 0;
    for (const x of [0, flat.width - 1]) for (const value of ctx.getImageData(x, 0, 1, flat.height).data) if (value < 245) count++;
    return count;
  });
  assert.equal(clipped, 0, "Mobile fit crops the model at the screen edges");
  await page.screenshot({ path: path.join(output, "mobile.png") });
  assert.deepEqual(errors, []); assert.deepEqual(network, []);
  await fs.writeFile(path.join(output, "site.json"), JSON.stringify({ examples: report, errors, network, savedBytes: bytes.length }, null, 2) + "\n");
  console.log("All examples, copper sections, browser export, local load, invalid file and mobile checks passed offline.");
} finally {
  await browser.close(); await fs.rm(temporary, { recursive: true });
}
