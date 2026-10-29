// The print panel with an FDM printer and a resin printer: node panel_resin.test.js <page> <panel.js>
//
// tests/test_panel_js.py renders the whole panel (server._panel_body) with an A1 Mini first
// and a Form 4, then this switches between them and checks what the user would see and
// what the form would send. Prints PASS/FAIL per check; exits 1 when any fails.
"use strict";
const fs = require("fs");
const { JSDOM } = require("jsdom");

const [pagePath, scriptPath] = process.argv.slice(2);
const dom = new JSDOM(fs.readFileSync(pagePath, "utf8"), {
  runScripts: "outside-only",
  url: "https://os2slice.test/panel",
});
const w = dom.window;
w.fetch = () => new Promise(() => {}); // no network: status polls and links never answer
w.parent.postMessage = () => {};
w.setInterval = () => 0;
w.eval(fs.readFileSync(scriptPath, "utf8"));

const d = w.document;
const form = d.getElementById("printform");
const f = form.elements;
const shown = (el) => {
  for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
    if (w.getComputedStyle(e).display === "none") return false;
  }
  return true;
};
const labelText = (el) => el.closest("label").firstChild.nodeValue.trim();
const values = (select) => [...select.options].map((o) => o.value);
const sent = () => Object.fromEntries(new w.FormData(form));
const change = (el, value) => {
  el.value = value;
  el.dispatchEvent(new w.Event("change", { bubbles: true }));
};
const select = (...bodies) => w.dispatchEvent(new w.MessageEvent("message", {
  origin: "https://cad.onshape.com",
  data: {
    messageName: "SELECTION",
    selections: bodies.map((b) => ({ selectionType: "BODY", selectionId: b })),
  },
}));

let failed = 0;
function check(what, ok, got) {
  console.log(`${ok ? "PASS" : "FAIL"} ${what}${ok ? "" : `  got: ${JSON.stringify(got)}`}`);
  if (!ok) failed += 1;
}

const FDM_FIELDS = ["walls", "infill", "build_plate_only", "top_layers", "bottom_layers", "brim",
  "plate", "x_chamber_temperature"];
const studioLine = d.getElementById("studio-link").closest("p");

select("A");
// -- the FDM printer first -------------------------------------------------------------
check("FDM: labels Filament and Process",
  labelText(f.filament) === "Filament" && labelText(f.process) === "Process",
  [labelText(f.filament), labelText(f.process)]);
check("FDM: every setting shown and sent",
  FDM_FIELDS.every((n) => shown(f[n])) && "walls" in sent() && "plate" in sent(), sent());
check("FDM: supports off, normal, tree", values(f.supports).join() === "off,normal,tree",
  values(f.supports));
check("FDM: defaults as rendered", f.supports.value === "off" && f.orient.value === "as-modeled",
  [f.supports.value, f.orient.value]);
check("FDM: the desktop hand-off offered", shown(studioLine), studioLine.outerHTML);

// -- switch to the Form 4 ----------------------------------------------------------------
change(f.printer, "formlabs/form4");
check("resin: labels Resin and Layer height",
  labelText(f.filament) === "Resin" && labelText(f.process) === "Layer height",
  [labelText(f.filament), labelText(f.process)]);
check("resin: FDM fields hidden", FDM_FIELDS.every((n) => !shown(f[n])),
  FDM_FIELDS.filter((n) => shown(f[n])));
check("resin: FDM fields not sent", FDM_FIELDS.every((n) => !(n in sent())), sent());
check("resin: More settings hidden", !shown(d.querySelector("details.extras")), "shown");
check("resin: supports none or auto",
  values(f.supports).join() === "off,normal"
  && [...f.supports.options].map((o) => o.textContent).join("|")
    === "None (on the build platform)|Auto supports", values(f.supports));
check("resin: untouched defaults are auto supports and auto orientation",
  f.supports.value === "normal" && f.orient.value === "auto",
  [f.supports.value, f.orient.value]);
check("resin: copies still offered", shown(f.copies) && sent().copies === "1", sent());
check("resin: the loaded resin preselected", f.filament.value === "FLGPGR05", f.filament.value);
check("resin: layer heights offered",
  values(f.process).join() === ",0.05 mm,0.2 mm" && shown(f.process), values(f.process));
check("resin: no desktop hand-off", !shown(studioLine), studioLine.outerHTML);

// -- two parts on the resin printer: one resin, no per-part menus ------------------------
select("A", "B");
check("resin, two parts: no per-part menus",
  d.querySelectorAll("#extras select").length === 0, d.getElementById("extras").innerHTML);
check("resin, two parts: the extra part takes the resin", f.extra.value === "B:FLGPGR05",
  f.extra.value);
change(f.filament, "FLGPCL05");
check("resin, two parts: follows the resin menu", f.extra.value === "B:FLGPCL05", f.extra.value);
check("resin, two parts: printable",
  !form.querySelector('button[type="submit"]').disabled, "disabled");
select("A");

// -- back to FDM: its defaults return ----------------------------------------------------
change(f.printer, "a1");
check("back to FDM: fields shown and labels back",
  FDM_FIELDS.every((n) => shown(f[n])) && labelText(f.filament) === "Filament"
  && labelText(f.process) === "Process", sent());
check("back to FDM: defaults restored",
  values(f.supports).join() === "off,normal,tree" && f.supports.value === "off"
  && f.orient.value === "as-modeled", [f.supports.value, f.orient.value]);
check("back to FDM: the desktop hand-off back", shown(studioLine), studioLine.outerHTML);

// -- a choice the person made is kept where it's offered ---------------------------------
change(f.supports, "tree");
change(f.orient, "z+");
change(f.printer, "formlabs/form4");
check("resin after touching: tree becomes auto, orientation kept",
  f.supports.value === "normal" && f.orient.value === "z+", [f.supports.value, f.orient.value]);
change(f.supports, "off");
change(f.printer, "a1");
check("FDM after touching: off kept", f.supports.value === "off", f.supports.value);

process.exit(failed ? 1 : 0);
