// os2slice print panel: follows Onshape's selection (docs/ONSHAPE_API.md).
// Only reads messages from the configured Onshape origin; writes text via textContent.
// Several selected parts = one multi-material object, each part with its own filament.
"use strict";
(() => {
  const root = document.getElementById("panel");
  const ONSHAPE = root.dataset.onshape;
  const ids = JSON.parse(root.dataset.ids);
  const parts = JSON.parse(root.dataset.parts);
  const form = document.getElementById("printform");
  const orient = form.elements.orient;
  const faceOpt = orient.querySelector('option[value="face"]');
  const button = form.querySelector('button[type="submit"]'); // not a select's own button
  const shown = document.getElementById("selection");
  const extrasBox = document.getElementById("extras");
  const printer = form.elements.printer;
  const filament = form.elements.filament;
  const FILAMENTS = JSON.parse(filament.dataset.choices || "{}"); // printer -> menu entries
  const process = form.elements.process;
  const machine = form.elements.machine;
  const toolSlot = form.elements.filament_tool; // "Load into": a tool for a filament not loaded
  const toolSlotLabel = document.getElementById("tool-slot");
  const MACHINES = JSON.parse(machine.dataset.choices || "{}"); // printer -> menu entries
  const PROCESSES = JSON.parse(process.dataset.choices || "{}"); // printer -> menu entries
  const studio = document.getElementById("studio-link");
  // The shared slicer sessions in the browser (D-21): web Bambu Studio and/or OrcaSlicer,
  // each a link with data-app (its /panel/<app> routes) and data-label.
  const webApps = [...document.querySelectorAll("a.web-app")].map((link) => ({
    link,
    app: link.dataset.app,
    label: link.dataset.label,
    state: document.getElementById(`${link.dataset.app}-state`),
    status: "unknown",
    reason: "", // why it's "down", from the server
    sending: false, // a part is on its way to that session
  }));
  let selectionOk = false;
  const mac = /Mac/.test(navigator.userAgent);

  // A resin printer (the printer option's data-tech "sla") has one resin, a layer height,
  // supports (none or auto) and copies. The FDM-only fields are hidden and disabled (so
  // neither sent nor validated) while one is chosen; untouched supports and orientation
  // take the technology's defaults.
  const supports = form.elements.supports;
  const FDM_ONLY = ["walls", "infill", "build_plate_only", "top_layers", "bottom_layers",
    "brim", "plate"];
  const moreSettings = form.querySelector("details.extras");
  const FDM_SUPPORTS = supports ? [...supports.options].map((o) => [o.value, o.textContent]) : [];
  const SLA_SUPPORTS = [["off", "None (on the build platform)"], ["normal", "Auto supports"]];
  const DEFAULTS = {
    fdm: { supports: supports ? supports.value : "", orient: "as-modeled" },
    sla: { supports: "normal", orient: "auto" },
  };
  const touched = { supports: false, orient: false };
  let tech = "fdm";
  const resin = () => tech === "sla";

  let bodies = [];
  let faces = [];
  const chosen = {}; // extra part id -> tray id, kept across re-renders
  const chosenTool = {}; // extra part id -> the tool its (not loaded) filament goes into
  const slotPicks = {}; // extra part id -> the tool the user chose for it

  const post = (name) => window.parent.postMessage({ ...ids, messageName: name }, ONSHAPE);
  post("applicationInit");
  setInterval(() => post("keepAlive"), 60000);

  // The part a face picked on its own belongs to, by face id: its name once known.
  const faceParts = {};
  function lookUpFacePart(face) {
    faceParts[face] = null; // asked
    const q = new URLSearchParams({
      d: form.elements.d.value, wv: form.elements.wv.value, wvid: form.elements.wvid.value,
      e: form.elements.e.value, c: form.elements.c.value, face,
    });
    fetch(`/panel/face-part?${q}`, { credentials: "same-origin" })
      .then((res) => (res.ok ? res.json() : null))
      .then((info) => {
        if (info && info.name) { faceParts[face] = info.name; sync(); }
      })
      .catch(() => {});
  }

  // -- filament colour icons (port of os2slice/filament_icons.py: keep the two in agreement)
  const BASE_SELECT = Boolean(window.CSS && CSS.supports && CSS.supports("appearance", "base-select"));
  const SQUARES = {
    red: "\u{1F7E5}", orange: "\u{1F7E7}", yellow: "\u{1F7E8}", green: "\u{1F7E9}",
    blue: "\u{1F7E6}", purple: "\u{1F7EA}", brown: "\u{1F7EB}", black: "⬛", white: "⬜",
  };
  // "#RRGGBB" (upper case) from "#RRGGBB" or "#RRGGBBAA"; null for anything else.
  function normaliseColour(colour) {
    if (typeof colour !== "string") return null;
    const m = /^#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$/.exec(colour.trim());
    return m ? `#${m[1].toUpperCase()}` : null;
  }
  // The coloured square nearest to a filament colour, or "" (same HSL rules as Python).
  function colourEmoji(colour) {
    const hex = normaliseColour(colour);
    if (!hex) return "";
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const top = Math.max(r, g, b);
    const bottom = Math.min(r, g, b);
    const chroma = top - bottom;
    const lum = (top + bottom) / 2;
    const sat = chroma === 0 ? 0 : chroma / (lum <= 0.5 ? top + bottom : 2 - top - bottom);
    let hue = 0;
    if (chroma !== 0) {
      if (top === r) hue = ((g - b) / chroma) % 6;
      else if (top === g) hue = (b - r) / chroma + 2;
      else hue = (r - g) / chroma + 4;
      hue = (hue * 60 + 360) % 360;
    }
    if (lum < 0.15) return SQUARES.black;
    if (lum > 0.9) return SQUARES.white;
    if (chroma < 0.1 || sat < 0.12) return lum >= 0.6 ? SQUARES.white : SQUARES.black;
    if (hue < 12 || hue >= 335) return SQUARES.red;
    if (hue < 70 && (top < 0.65 || (hue < 50 && sat < 0.45 && lum < 0.75))) return SQUARES.brown;
    if (hue < 45) return SQUARES.orange;
    if (hue < 70) return SQUARES.yellow;
    if (hue < 165) return SQUARES.green;
    if (hue < 255) return SQUARES.blue;
    return SQUARES.purple;
  }
  // An option's content, like filament_icons.option_content: emoji, swatch, label.
  function fillOption(opt, label, colour) {
    const hex = normaliseColour(colour);
    if (hex) {
      const emoji = document.createElement("span");
      emoji.className = "fc-emoji";
      emoji.textContent = `${colourEmoji(hex)} `;
      const swatch = document.createElement("span");
      swatch.className = "fc-swatch";
      swatch.style.background = hex;
      opt.append(emoji, swatch, document.createTextNode(label));
    } else {
      opt.textContent = label;
    }
  }
  // Empty a filament menu but keep its <button><selectedcontent> (customizable selects).
  function clearOptions(select) {
    for (const o of [...select.querySelectorAll("option, optgroup")]) o.remove();
  }
  // A filament menu made here: with customizable selects, the closed menu shows the swatch.
  function filamentSelect() {
    const select = document.createElement("select");
    select.className = "fc";
    if (BASE_SELECT) {
      const b = document.createElement("button");
      b.append(document.createElement("selectedcontent"));
      select.append(b);
    }
    return select;
  }
  // Mirror the chosen option into <selectedcontent>, in case the browser didn't after a refill.
  function showChosen(select) {
    const shownContent = select.querySelector("selectedcontent");
    if (!shownContent) return;
    const o = select.options[select.selectedIndex];
    shownContent.replaceChildren(...(o ? [...o.childNodes].map((n) => n.cloneNode(true)) : []));
  }
  const entryOf = (value) => (FILAMENTS[printerName()] || []).find((c) => c.value === value);

  const printerName = () => printer.value;
  const nameOf = (id) => parts[id] || id;

  // A label's own text, before its control: "Filament " / "Resin ".
  function setLabelText(control, text) {
    const label = control && control.closest("label");
    if (label && label.firstChild && label.firstChild.nodeType === 3) {
      label.firstChild.nodeValue = text;
    }
  }

  // Switch the settings to the chosen printer's technology (FDM or resin).
  function applyTech() {
    const opt = printer.options[printer.selectedIndex];
    const next = (opt && opt.dataset.tech) || "fdm";
    if (next === tech) return;
    tech = next;
    const sla = resin();
    for (const name of FDM_ONLY) {
      const el = form.elements[name];
      if (!el) continue;
      el.disabled = sla;
      el.closest("label").hidden = sla;
    }
    for (const el of form.querySelectorAll("[name^='x_']")) el.disabled = sla;
    if (moreSettings) moreSettings.hidden = sla;
    setLabelText(filament, sla ? "Resin " : "Filament ");
    setLabelText(process, sla ? "Layer height " : "Process ");
    if (supports) {
      const keep = supports.value;
      supports.replaceChildren(...(sla ? SLA_SUPPORTS : FDM_SUPPORTS).map(([value, label]) => {
        const o = document.createElement("option");
        o.value = value;
        o.textContent = label;
        return o;
      }));
      const offered = [...supports.options].some((o) => o.value === keep);
      supports.value = touched.supports && offered ? keep : DEFAULTS[tech].supports;
    }
    if (!touched.orient && orient.value !== "face") orient.value = DEFAULTS[tech].orient;
    // The desktop and browser slicer hand-offs open Bambu/Orca projects: FDM only.
    for (const link of [studio, ...webApps.map((w) => w.link)]) {
      if (link) link.closest("p").hidden = sla;
    }
  }

  // Refill the filament menu for the chosen printer: keep the choice if that printer
  // has it too (e.g. the preset), else take the printer's default (a loaded slot of the
  // configured material, a lone slot, or the preset).
  // Whether the chosen printer profile uses the filament changer: then its tools are
  // offered (and come first). Without a printer profile menu, everything is offered.
  function changerProfile() {
    const opt = machine.options[machine.selectedIndex];
    return !opt || opt.dataset.mmu !== undefined;
  }

  // Refill the filament menu for the chosen printer and printer profile: keep the choice
  // if it's still offered, else take the printer's default (a loaded slot or tool of the
  // configured material, a lone slot, or the preset), else the same profile without a tool.
  function renderFilaments() {
    const keep = filament.value;
    const all = FILAMENTS[printerName()] || [];
    const entries = changerProfile() ? all : all.filter((c) => !c.tool);
    clearOptions(filament);
    for (const c of entries) {
      const opt = document.createElement("option");
      opt.value = c.value;
      fillOption(opt, c.label, c.empty ? "" : c.color); // an empty gate has no filament
      opt.disabled = Boolean(c.disabled);
      if (c.color) opt.dataset.color = c.color;
      if (c.tool) opt.dataset.tool = "";
      filament.append(opt);
    }
    const usable = entries.filter((c) => !c.disabled);
    const preferred = all.find((c) => c.default);
    const pick = usable.find((c) => c.value === keep && keep !== "")
      || usable.find((c) => c.default)
      || (preferred && usable.find((c) => !c.tool && c.profile && c.profile === preferred.profile))
      || usable[0];
    if (pick) filament.value = pick.value;
    showChosen(filament);
  }

  // Refill the printer profile menu for the chosen printer, like the process menu.
  function renderMachines() {
    const keep = machine.value;
    const entries = MACHINES[printerName()] || [];
    machine.replaceChildren();
    for (const c of entries) {
      const opt = document.createElement("option");
      opt.value = c.value;
      opt.textContent = c.label;
      if (c.mmu) opt.dataset.mmu = "";
      machine.append(opt);
    }
    const pick = entries.find((c) => c.value === keep) || entries.find((c) => c.default);
    if (pick) machine.value = pick.value;
    machine.closest("label").hidden = entries.length === 0;
  }

  // Refill the process menu for the chosen printer (hidden when it has no own profiles),
  // keeping the choice if that printer has it too.
  function renderProcesses() {
    const keep = process.value;
    const entries = PROCESSES[printerName()] || [];
    process.replaceChildren();
    for (const c of entries) {
      const opt = document.createElement("option");
      opt.value = c.value;
      opt.textContent = c.label;
      process.append(opt);
    }
    const pick = entries.find((c) => c.value === keep) || entries.find((c) => c.default);
    if (pick) process.value = pick.value;
    process.closest("label").hidden = entries.length === 0;
  }

  // The changer's tools, for a filament that isn't loaded: "T2, now CR-PETG Transparent".
  function toolChoices() {
    return (FILAMENTS[printerName()] || []).filter((c) => c.tool).map((c) => ({
      value: c.value,
      label: c.label.split(" → ")[0].replace(/^(T\d+): /, "$1, now "),
      color: c.color,
      profile: c.profile,
      empty: Boolean(c.empty),
    }));
  }

  // A filament profile (not a tool) picked with a changer printer profile goes into a tool
  // the user loads before starting: fill a "Load into" menu, keeping `keep` if offered,
  // else a tool that has that filament already, else an empty one, else T0.
  function fillToolMenu(select, keep, profile) {
    const tools = toolChoices();
    clearOptions(select);
    for (const t of tools) {
      const opt = document.createElement("option");
      opt.value = t.value;
      fillOption(opt, t.label, t.empty ? "" : t.color); // an empty gate has no filament
      select.append(opt);
    }
    const pick = tools.find((t) => t.value === keep)
      || tools.find((t) => profile && t.profile === profile && !t.empty)
      || tools.find((t) => t.empty) || tools[0];
    if (pick) select.value = pick.value;
    showChosen(select);
    return tools.length > 0;
  }

  function needsTool(option) {
    return Boolean(option) && option.dataset.tool === undefined && changerProfile();
  }

  let slotPicked = ""; // the tool the user chose in "Load into" (kept until they change it)
  function renderToolSlot() {
    const opt = filament.options[filament.selectedIndex];
    const entry = (FILAMENTS[printerName()] || []).find((c) => opt && c.value === opt.value);
    const show = needsTool(opt) && fillToolMenu(toolSlot, slotPicked, entry && entry.profile);
    toolSlotLabel.hidden = !show;
    toolSlot.disabled = !show; // a disabled field isn't sent
  }

  // The loaded slots of the chosen printer (not the preset), for the extra parts' menus.
  function slotOptions() {
    return [...filament.options].filter((o) => o.value !== "" && !o.disabled);
  }

  function renderExtras() {
    extrasBox.replaceChildren();
    if (resin()) return; // every part prints in the one resin of the main menu
    const extra = bodies.slice(1);
    const options = slotOptions();
    for (const id of extra) {
      const label = document.createElement("label");
      label.textContent = `Filament for ${nameOf(id)} `;
      const select = filamentSelect();
      select.dataset.part = id;
      for (const o of options) {
        const opt = document.createElement("option");
        const entry = entryOf(o.value);
        opt.value = o.value;
        fillOption(opt, entry ? entry.label : o.textContent, o.dataset.color);
        opt.dataset.color = o.dataset.color || "";
        if (o.dataset.tool !== undefined) opt.dataset.tool = "";
        select.append(opt);
      }
      if (chosen[id] && [...select.options].some((o) => o.value === chosen[id])) {
        select.value = chosen[id];
      }
      chosen[id] = select.value;
      showChosen(select);
      label.append(select);
      extrasBox.append(label);
      // Its own "Load into" menu, when it's a filament that isn't loaded.
      const slotLabel = document.createElement("label");
      slotLabel.textContent = "Load into ";
      const slot = filamentSelect();
      slotLabel.append(slot);
      extrasBox.append(slotLabel);
      const renderSlot = () => {
        const opt = select.options[select.selectedIndex];
        const entry = opt && entryOf(opt.value);
        const profile = entry ? entry.profile : "";
        const show = needsTool(opt) && fillToolMenu(slot, slotPicks[id], profile);
        slotLabel.hidden = !show;
        chosenTool[id] = show ? slot.value : "";
      };
      renderSlot();
      select.addEventListener("change", () => { chosen[id] = select.value; renderSlot(); sync(); });
      slot.addEventListener("change", () => {
        chosenTool[id] = slot.value;
        slotPicks[id] = slot.value;
        sync();
      });
    }
  }

  function extraValue() {
    if (resin()) {
      return filament.value ? bodies.slice(1).map((id) => `${id}:${filament.value}`).join(",") : "";
    }
    return bodies.slice(1).filter((id) => chosen[id])
      .map((id) => `${id}:${chosenTool[id] ? `${chosenTool[id]}.${chosen[id]}` : chosen[id]}`)
      .join(",");
  }

  function sync() {
    const part = bodies[0] || "";
    const face = faces.length === 1 ? faces[0] : "";
    const multi = bodies.length > 1;
    form.elements.p.value = part;
    form.elements.face.value = face;
    form.elements.extra.value = multi ? extraValue() : "";
    faceOpt.disabled = !face;
    if (face && orient.value !== "face" && !sync.userPicked) orient.value = "face";
    if (!face && orient.value === "face") orient.value = DEFAULTS[tech].orient;

    let text;
    let ok = Boolean(part || face) && faces.length <= 1;
    if (faces.length > 1) text = "Select at most one face (the one the print stands on).";
    else if (multi) {
      text = `${bodies.length} parts as one print: ${bodies.map(nameOf).join(", ")}`;
      if (face) text += ", that face down";
      const everyPartHasSlot = filament.value !== ""
        && (resin() || bodies.slice(1).every((id) => chosen[id]));
      if (!everyPartHasSlot) {
        text += resin() ? ". Pick a resin from the list (not the configured one)."
          : ". Pick a loaded filament (not the preset) for every part.";
        ok = false;
      }
    } else if (part && face) text = `${nameOf(part)}, that face down`;
    else if (part) text = nameOf(part);
    else if (face) {
      const owner = faceParts[face];
      if (owner === undefined) lookUpFacePart(face);
      text = owner ? `${owner}, that face down` : "The part with the selected face, that face down";
    }
    else text = "Select a part (or several, for multi-material), or the face it stands on.";
    shown.textContent = text;
    button.disabled = !ok;
    document.dispatchEvent(new CustomEvent("os2slice:selection"));
    selectionOk = Boolean(part || face) && faces.length <= 1;
    scheduleStudioLink(selectionOk);
    updateWebApps();
  }

  // The shared Bambu Studio in the browser (one session for everyone, D-21).
  function updateWebApps() {
    for (const w of webApps) {
      const label = {
        free: "(free)", busy: "(in use)", unknown: "(unavailable)", down: "(not starting)",
      }[w.status];
      if (!w.sending) w.state.textContent = label || "";
      // "down": the app keeps exiting; hovering says why and when it's retried.
      const why = w.status === "down" ? w.reason : "";
      w.state.title = why;
      w.link.title = why;
      const usable = w.status !== "unknown" && w.status !== "down";
      w.link.classList.toggle("off", !(selectionOk && usable));
    }
  }

  async function pollWebApp(w) {
    w.reason = "";
    try {
      const res = await fetch(`/panel/${w.app}/status`, { credentials: "same-origin" });
      const body = res.ok ? await res.json() : { state: "unknown" };
      w.status = body.state;
      w.reason = body.reason || "";
    } catch {
      w.status = "unknown";
    }
    updateWebApps();
  }

  for (const w of webApps) {
    w.link.addEventListener("click", (ev) => {
      if (w.link.classList.contains("off")) { ev.preventDefault(); return; }
      const own = studio
        ? "\n\nCancel, then use \"on this computer\" to open it in your own slicer." : "";
      if (w.status === "busy" && !window.confirm(
        `Someone has the web ${w.label} open (maybe you). Send this part there anyway?${own}`)) {
        ev.preventDefault();
        return;
      }
      // The link opens the session in a new tab; this hands it the part. The server
      // slices it first (for the printer and filament settings), so it takes a while.
      w.sending = true;
      w.state.textContent = "(preparing… slicing for the settings)";
      fetch(`/panel/${w.app}`, {
        method: "POST", credentials: "same-origin",
        body: new URLSearchParams(new FormData(form)),
      }).then((res) => {
        w.sending = false;
        w.state.textContent = res.ok ? "(sent: it opens in a few seconds)" : "(failed: see the log)";
      }).catch(() => {
        w.sending = false;
        w.state.textContent = "(failed)";
      });
    });
    pollWebApp(w);
    setInterval(() => pollWebApp(w), 10000);
  }

  // "Open in … on this computer": a short-lived download URL for this selection as a 3MF,
  // handed to the slicer's URL handler: Bambu Studio (Windows/Linux vs macOS forms) or
  // OrcaSlicer (one form). Absent when the panel offers no local slicer.
  let linkTimer = 0;
  let linkFetch = null;
  function scheduleStudioLink(enabled) {
    if (!studio) return;
    clearTimeout(linkTimer);
    studio.classList.add("off");
    studio.href = "#";
    if (!enabled || resin()) return;
    linkTimer = setTimeout(async () => {
      if (linkFetch) linkFetch.abort();
      linkFetch = new AbortController();
      try {
        const res = await fetch("/panel/model-link", {
          method: "POST", signal: linkFetch.signal, credentials: "same-origin",
          body: new URLSearchParams(new FormData(form)),
        });
        if (!res.ok) return;
        const { url } = await res.json();
        const encoded = encodeURIComponent(url);
        if (studio.dataset.scheme === "orcaslicer") studio.href = `orcaslicer://open?file=${encoded}`;
        else studio.href = mac ? `bambustudioopen://${encoded}` : `bambustudio://open?file=${encoded}`;
        studio.classList.remove("off");
      } catch (err) {
        if (err.name !== "AbortError") studio.title = String(err);
      }
    }, 400);
  }

  // The preview takes each part's filament colour.
  const syncColor = () => {
    const opt = filament.options[filament.selectedIndex];
    root.dataset.color = (opt && opt.dataset.color) || "";
    document.dispatchEvent(new CustomEvent("os2slice:color"));
  };
  printer.addEventListener("change", () => {
    applyTech();
    renderMachines(); renderFilaments(); renderToolSlot(); renderProcesses(); renderExtras();
    syncColor(); sync();
  });
  if (supports) supports.addEventListener("change", () => { touched.supports = true; });
  machine.addEventListener("change", () => {
    renderFilaments(); renderToolSlot(); renderExtras(); syncColor(); sync();
  });
  filament.addEventListener("change", () => { renderToolSlot(); syncColor(); sync(); });
  toolSlot.addEventListener("change", () => { slotPicked = toolSlot.value; sync(); });
  orient.addEventListener("change", () => {
    sync.userPicked = true;
    touched.orient = true;
    sync();
  });
  // The Bambu Studio link carries the settings (copies, brim, ...): rebuild it on edits.
  const SETTINGS = ["walls", "infill", "supports", "build_plate_only", "top_layers",
    "bottom_layers", "brim", "copies", "plate", "process", "machine", "filament_tool"];
  form.addEventListener("change", (ev) => {
    const name = ev.target.name || "";
    if (SETTINGS.includes(name) || name.startsWith("x_")) scheduleStudioLink(selectionOk);
  });
  applyTech();
  renderMachines();
  renderFilaments();
  renderToolSlot();
  renderProcesses();
  syncColor();

  window.addEventListener("message", (ev) => {
    if (ev.origin !== ONSHAPE || !ev.data || ev.data.messageName !== "SELECTION") return;
    const sel = Array.isArray(ev.data.selections) ? ev.data.selections : [];
    bodies = sel.filter((s) => s && s.selectionType === "BODY").map((s) => String(s.selectionId));
    faces = sel
      .filter((s) => s && s.selectionType === "ENTITY" && s.entityType === "FACE")
      .map((s) => String(s.selectionId));
    sync.userPicked = false;
    renderExtras();
    sync();
  });
  // For the preview: part id -> colour of the filament it prints with.
  window.os2sliceColours = () => {
    const out = {};
    if (bodies[0]) out[bodies[0]] = root.dataset.color || "";
    for (const sel of extrasBox.querySelectorAll("select[data-part]")) {
      const o = sel.options[sel.selectedIndex];
      out[sel.dataset.part] = (o && o.dataset.color) || "";
    }
    return out;
  };
  renderExtras();
  sync();
})();
