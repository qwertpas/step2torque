import { readSolution, base64 } from "./file.js";

const $ = id => document.getElementById(id);
const examples = JSON.parse($("examples").textContent), frame = $("viewer");
const template = new TextDecoder().decode(Uint8Array.from(atob($("template").textContent), c => c.charCodeAt(0)));
let revision = 0;

for (const item of examples) $("example").add(new Option(item.label, item.file));
function showExample() {
  revision++;
  $("message").textContent = "";
  frame.title = $("example").selectedOptions[0].textContent;
  frame.src = $("example").value;
  frame.removeAttribute("srcdoc");
}
$("example").addEventListener("change", showExample);
$("open").addEventListener("change", async () => {
  const file = $("open").files[0]; if (!file) return;
  const current = ++revision;
  $("message").textContent = "Opening…";
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    await readSolution(bytes);
    if (current !== revision) return;
    frame.srcdoc = template.replace("__SOLUTION__", base64(bytes));
    frame.title = file.name;
    $("example").selectedIndex = -1;
    $("message").textContent = file.name;
  } catch (error) {
    if (current === revision) $("message").textContent = error.message;
  }
  $("open").value = "";
});
showExample();
