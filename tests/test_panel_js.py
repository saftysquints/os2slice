"""The print panel's script (static/panel.js) run in jsdom against the server's own markup.

Needs Node and jsdom: `cd tests/js && npm ci`. Without them the test is skipped.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from os2slice import server
from os2slice.config import Config
from os2slice.modules.base import Material, PrinterInfo, Profiles
from os2slice.printing import profile_material
from os2slice.request import ExportRequest

JS = Path(__file__).parent / "js"


def panel_page() -> str:
    """The panel's printer and filament menus for an MMU printer, with the real CSS."""
    printer = PrinterInfo(
        "joshprint", "JoshPrint", "fdm", "RatRig V-Core 3 300", "joshprint", "orca",
        Profiles("JoshPrint 0.5 MMU", "0.2 Strong", "PM ASA"),
    )  # fmt: skip
    tools = (
        Material("t0", "T0: Black (ASA)", "ASA", "#000000", profile="PM ASA", raw={"tool": 0}),
        Material("t1", "T1: PolyLite™ ASA Blue (PETG)", "PETG", "#000000",
                 profile="3DO PETG", raw={"tool": 1}),
        Material("t2", "T2: CR-PETG Transparent", "PETG", "#00FFFF",
                 profile="Creality PETG", raw={"tool": 2}),
        Material("t3", "T3: empty", "ASA", "#FFFFFF", raw={"tool": 3, "empty": True}),
    )  # fmt: skip
    profiles = (
        profile_material("PM ASA", "#F2754E"),
        profile_material("Sirayatech PET-CF"),
        profile_material("Creality PETG"),
    )
    view = server.PrinterView(
        printer, "ready", True, tools + profiles, ("0.2 Strong",),
        ("JoshPrint 0.5", "JoshPrint 0.5 MMU"), ("JoshPrint 0.5 MMU",),
    )  # fmt: skip
    layout = server._layout("Print", "", False)
    css = layout[layout.index("<style>") : layout.index("</style>") + len("</style>")]
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{v}">'
        for k, v in (("d", "D"), ("wv", "w"), ("wvid", "W"), ("e", "E"), ("c", ""),
                     ("p", ""), ("face", ""), ("extra", ""), ("csrf", "x"))
    )  # fmt: skip
    ids = json.dumps({"documentId": "D"})
    parts = json.dumps({"A": "Part A", "B": "Part B"})
    return f"""<!doctype html><html><head>{css}</head><body>
<div id="panel" data-onshape="https://cad.onshape.com" data-ids='{ids}' data-parts='{parts}'
 data-beds="{{}}" data-layout="{{}}">
<p id="selection"></p>
<p class="links"><a id="studio-link" class="off" href="#" data-scheme="orcaslicer">x</a></p>
<form id="printform">{hidden}{server._panel_printer_selects([view], "joshprint")}
<div id="extras"></div>
<select name="orient"><option value="face">f</option><option value="as-modeled" selected>a</option>
</select><button type="submit" disabled>Slice</button></form></div></body></html>"""


def test_panel_script_in_jsdom(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None or not (JS / "node_modules" / "jsdom").is_dir():
        pytest.skip("needs Node and jsdom: cd tests/js && npm ci")
    page = tmp_path / "panel.html"
    page.write_text(panel_page(), encoding="utf-8")
    script = resources.files("os2slice").joinpath("static", "panel.js")
    with resources.as_file(script) as script_path:
        run = subprocess.run(
            [node, str(JS / "panel.test.js"), str(page), str(script_path)],
            cwd=JS, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PASS" in run.stdout and "FAIL" not in run.stdout


def resin_panel_page(cfg: Config) -> str:
    """The whole panel (server._panel_body) with an FDM printer first, then a Form 4."""
    fdm = PrinterInfo(
        "a1", "A1 Mini", "fdm", "A1 Mini", "bambuddy", "bambuddy",
        Profiles("Bambu Lab A1 mini 0.4 nozzle", "0.20mm Standard", "Bambu PLA Basic"),
    )  # fmt: skip
    form4 = PrinterInfo(
        "formlabs/form4", "Form 4", "sla", "Form 4", "formlabs", "formlabs",
        Profiles("FORM-4-0", "0.1 mm", "FLGPGR05"), bed_mm=(200, 125),
    )  # fmt: skip
    resins = (
        Material("FLGPGR05", "Grey V5 (loaded)", "FLGPGR05", profile="FLGPGR05"),
        Material("FLGPCL05", "Clear V5", "FLGPCL05", profile="FLGPCL05"),
    )
    views = [
        server.PrinterView(fdm, "idle", True, (profile_material("Bambu PLA Basic", "#000000"),)),
        server.PrinterView(form4, "idle", True, resins, ("0.05 mm", "0.1 mm", "0.2 mm")),
    ]
    cfg = dataclasses.replace(cfg, panel_extras=("chamber_temperature",))
    svc = SimpleNamespace(
        cfg=cfg, modules=SimpleNamespace(default_printer="a1"), start_note=lambda: "the queue"
    )
    req = ExportRequest("", "D" * 24, "w", "W" * 24, "E" * 24, None)
    parts = {"A": "Part A", "B": "Part B"}
    body = server._panel_body(req, parts, views, cfg, "x", svc, [])  # type: ignore[arg-type]
    layout = server._layout("Print", "", False)
    css = layout[layout.index("<style>") : layout.index("</style>") + len("</style>")]
    return f"<!doctype html><html><head>{css}</head><body>{body}</body></html>"


def test_panel_switches_to_a_resin_printer_in_jsdom(tmp_path: Path, cfg: Config) -> None:
    node = shutil.which("node")
    if node is None or not (JS / "node_modules" / "jsdom").is_dir():
        pytest.skip("needs Node and jsdom: cd tests/js && npm ci")
    page = tmp_path / "panel.html"
    page.write_text(resin_panel_page(cfg), encoding="utf-8")
    script = resources.files("os2slice").joinpath("static", "panel.js")
    with resources.as_file(script) as script_path:
        run = subprocess.run(
            [node, str(JS / "panel_resin.test.js"), str(page), str(script_path)],
            cwd=JS, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PASS" in run.stdout and "FAIL" not in run.stdout
