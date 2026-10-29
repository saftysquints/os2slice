// os2slice panel preview: the selected part(s), oriented as they will print, on the chosen bed.
// Z is up (like the slicer). Each part is drawn in its filament's colour. Renders on change only.
// Copies are laid out here exactly as printing.copy_offsets places them on the plate.
import * as THREE from "./vendor/three.module.js";
import { STLLoader } from "./vendor/STLLoader.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const root = document.getElementById("panel");
const box = document.getElementById("preview");
const note = document.getElementById("preview-note");
const form = document.getElementById("printform");
const beds = JSON.parse(root.dataset.beds || "{}");
const LAYOUT = JSON.parse(root.dataset.layout || '{"gap":6,"brimGap":10,"margin":5}');

const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
const scene = new THREE.Scene();
scene.background = new THREE.Color(dark ? 0x1d2026 : 0xf3f5f7);
THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
camera.up.set(0, 0, 1);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
box.prepend(renderer.domElement);

scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.4));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(-150, -250, 400);
scene.add(sun);

const controls = new OrbitControls(camera, renderer.domElement);
controls.addEventListener("change", render);

const bedGroup = new THREE.Group();
scene.add(bedGroup);
let base = null; // THREE.Group: one copy of the part meshes, centred on the bed
let baseSize = null; // [x, y, z] of one copy, mm
let model = null; // THREE.Group of every copy (clones share base's geometry and materials)
let bedSize = [256, 256];

function render() {
  renderer.render(scene, camera);
}

function resize() {
  const w = box.clientWidth || 300;
  const h = box.clientHeight || 230;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  render();
}
new ResizeObserver(resize).observe(box);

function buildBed([w, d]) {
  bedGroup.clear();
  const plate = new THREE.Mesh(
    new THREE.PlaneGeometry(w, d),
    new THREE.MeshStandardMaterial({ color: dark ? 0x3a3f47 : 0xd9dde2, roughness: 0.9 }),
  );
  plate.position.z = -0.05;
  bedGroup.add(plate);
  const pts = [];
  for (let x = -w / 2; x <= w / 2 + 0.01; x += 10) pts.push(x, -d / 2, 0, x, d / 2, 0);
  for (let y = -d / 2; y <= d / 2 + 0.01; y += 10) pts.push(-w / 2, y, 0, w / 2, y, 0);
  const grid = new THREE.BufferGeometry();
  grid.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  bedGroup.add(new THREE.LineSegments(grid, new THREE.LineBasicMaterial({
    color: dark ? 0x555c66 : 0xb8bec6,
  })));
}

function frame() {
  // With parts: frame them (the bed stays as context). Without: show the whole bed.
  let target, r;
  if (model) {
    const b = new THREE.Box3().setFromObject(model);
    const size = b.getSize(new THREE.Vector3());
    target = b.getCenter(new THREE.Vector3());
    r = Math.max(size.length() * 1.5, 60);
  } else {
    target = new THREE.Vector3(0, 0, 0);
    r = Math.hypot(bedSize[0], bedSize[1]) * 1.05;
  }
  camera.position.set(target.x - r * 0.55, target.y - r * 0.95, target.z + r * 0.75);
  controls.target.copy(target);
  controls.update();
  render();
}

const setNote = (text) => { note.textContent = text; };

function currentBed() {
  // The panel's printer menu holds the printer name (no tray since the filament menu split).
  const printer = form.elements.printer ? form.elements.printer.value.split("|")[0] : "";
  return beds[printer] || [256, 256];
}

function shade(hex) {
  if (!hex || !/^#[0-9a-fA-F]{6}$/.test(hex)) return new THREE.Color("#e0a040");
  // Lift very dark filament a little so the shading still reads.
  const col = new THREE.Color(hex);
  const hsl = col.getHSL({});
  if (hsl.l < 0.22) col.setHSL(hsl.h, hsl.s, 0.22);
  return col;
}

function recolour() {
  if (!base) return;
  const colours = window.os2sliceColours ? window.os2sliceColours() : {};
  for (const m of base.children) m.material.color.copy(shade(colours[m.userData.part]));
  render();
}

// Port of printing.copy_offsets: the squarest grid that fits, centred, front-left first.
// Returns null when the copies don't fit.
function copyOffsets([w, d], copies, [bedW, bedD], gap) {
  const roomW = bedW - 2 * LAYOUT.margin;
  const roomD = bedD - 2 * LAYOUT.margin;
  let best = null;
  for (let cols = 1; cols <= copies; cols++) {
    const rows = Math.ceil(copies / cols);
    const gridW = cols * w + (cols - 1) * gap;
    const gridD = rows * d + (rows - 1) * gap;
    if (gridW <= roomW && gridD <= roomD) {
      const score = Math.max(gridW / roomW, gridD / roomD);
      if (!best || score < best.score) best = { score, cols, rows };
    }
  }
  if (!best) return null;
  const px = w + gap;
  const py = d + gap;
  const x0 = (-(best.cols - 1) * px) / 2;
  const y0 = (-(best.rows - 1) * py) / 2;
  return Array.from({ length: copies }, (_, n) => [
    x0 + (n % best.cols) * px, y0 + Math.floor(n / best.cols) * py,
  ]);
}

function copiesWanted() {
  const n = parseInt(form.elements.copies ? form.elements.copies.value : "1", 10);
  return Number.isInteger(n) && n >= 1 && n <= 25 ? n : 1;
}

// Place the copies of `base` on the bed and describe the result. No download needed.
function layout() {
  if (!base) return;
  const f = form.elements;
  const copies = copiesWanted();
  const brim = f.brim && f.brim.checked && !f.brim.disabled; // disabled: a resin printer
  const gap = LAYOUT.gap + (brim ? LAYOUT.brimGap : 0);
  const offsets = copyOffsets([baseSize[0], baseSize[1]], copies, bedSize, gap);
  if (model) scene.remove(model);
  model = new THREE.Group();
  for (const [x, y] of offsets || [[0, 0]]) {
    const copy = base.clone();
    copy.position.x += x;
    copy.position.y += y;
    model.add(copy);
  }
  scene.add(model);
  frame();
  const [sx, sy, sz] = baseSize;
  const dims = `${sx.toFixed(1)} × ${sy.toFixed(1)} × ${sz.toFixed(1)} mm`;
  const parts = base.children.length > 1 ? ` · ${base.children.length} parts` : "";
  const auto = f.orient.value === "auto" ? " · the slicer will pick the orientation" : "";
  let text = `${dims}${parts}${auto}`;
  if (sx > bedSize[0] || sy > bedSize[1]) text = `${dims} · larger than this printer's bed!`;
  else if (!offsets) text = `${dims} · ${copies} copies don't fit on this bed!`;
  else if (copies > 1) text += ` · ${copies} copies`;
  setNote(text);
}

let timer = 0;
let inflight = null;

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(refresh, 250);
}

async function refresh() {
  const bed = currentBed();
  if (bed[0] !== bedSize[0] || bed[1] !== bedSize[1] || bedGroup.children.length === 0) {
    bedSize = bed;
    buildBed(bedSize);
    frame();
  }
  const f = form.elements;
  if (!f.p.value && !f.face.value) {
    if (model) { scene.remove(model); model = null; render(); }
    base = null;
    setNote("Select a part to preview it.");
    return;
  }
  const extra = f.extra.value ? f.extra.value.split(",").map((x) => x.split(":")[0]) : [];
  const ids = extra.length ? [f.p.value, ...extra] : [""];
  const params = new URLSearchParams();
  for (const k of ["d", "wv", "wvid", "e", "c", "p", "face"]) params.set(k, f[k].value);
  params.set("orient", f.orient.value);
  if (extra.length) params.set("extra", extra.join(","));
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;
  setNote(extra.length ? `Loading ${ids.length} parts…` : "Loading preview…");
  try {
    const group = new THREE.Group();
    for (const id of ids) {
      const q = new URLSearchParams(params);
      if (id) q.set("only", id);
      const res = await fetch(`/panel/preview?${q}`, { signal, credentials: "same-origin" });
      if (!res.ok) throw new Error(`preview failed (${res.status})`);
      const geometry = new STLLoader().parse(await res.arrayBuffer());
      geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
        roughness: 0.55, metalness: 0.05,
      }));
      mesh.userData.part = id || f.p.value;
      group.add(mesh);
    }
    // os2slice centres the assembly on the bed (the grid of copies, too); mirror that.
    const b = new THREE.Box3().setFromObject(group);
    group.position.set(-(b.min.x + b.max.x) / 2, -(b.min.y + b.max.y) / 2, -b.min.z);
    const s = b.getSize(new THREE.Vector3());
    base = group;
    baseSize = [s.x, s.y, s.z];
    recolour();
    layout();
  } catch (err) {
    if (err.name !== "AbortError") setNote(`No preview: ${err.message}`);
  }
}

document.addEventListener("os2slice:selection", schedule);
document.addEventListener("os2slice:color", recolour);
form.addEventListener("change", (ev) => {
  if (["printer", "orient"].includes(ev.target.name)) schedule();
  else if (["copies", "brim"].includes(ev.target.name)) layout();
  else if (ev.target.dataset && ev.target.dataset.part) recolour();
});
// Typing or using the arrows in the copies box updates the plate straight away.
form.addEventListener("input", (ev) => { if (ev.target.name === "copies") layout(); });
resize();
refresh();
