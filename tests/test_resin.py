"""Resin (SLA) printers in the core: settings, plan, page, panel, job page and CLI.

A stand-in for a PreForm-style module (role "both", technology "sla", makes and takes
"form") sits beside the BamBuddy fake, shaped like the module contract in docs/MODULES.md.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from os2slice import cli, printing
from os2slice.auth import Keys
from os2slice.config import Config
from os2slice.errors import BadRequest
from os2slice.modules import registry
from os2slice.modules.base import (
    Health,
    Material,
    ModuleSpec,
    PrinterInfo,
    PrinterStatus,
    ProfileCatalog,
    Profiles,
    SliceInput,
    SliceOutput,
    Submission,
)
from os2slice.modules.registry import Modules
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.request import parse_onshape_url
from os2slice.settings import PrintSettings
from tests.conftest import DOC, ELEM, WS
from tests.fakes import FakeBambuddy, fake_modules, fake_onshape
from tests.test_server import (
    FRAME,
    HOST,
    NAV,
    PANEL_QUERY,
    PRINT_QUERY,
    Running,
    post_panel,
    wait_job,
)

URL = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}"
FORM4 = PrinterInfo(
    "formlabs/form4", "Form 4", "sla", "Form 4", "formlabs", "formlabs",
    Profiles("FORM-4-0", "0.1 mm", "FLGPGR05"), bed_mm=(200, 125),
)  # fmt: skip
RESINS = (
    Material("FLGPGR05", "Grey V5 (loaded)", "FLGPGR05", profile="FLGPGR05"),
    Material("FLGPCL05", "Clear V5", "FLGPCL05", profile="FLGPCL05"),
)
LAYERS = ("0.025 mm", "0.05 mm", "0.1 mm", "0.16 mm", "0.2 mm")
WAITING = "waits on the printer until someone starts it on the touchscreen"


class FakeResin:
    """A resin printer's slicer and target in one, like PreForm Server."""

    spec = ModuleSpec(
        kind="test-resin", label="PreForm", role="both", technology="sla", fields=(),
        makes=("form",), accepts=("form",), discovers_printers=True,
    )  # fmt: skip

    def __init__(self) -> None:
        self.jobs: list[SliceInput] = []
        self.submitted: list[tuple[str, bool]] = []

    def check(self) -> Health:
        return Health(True, "ok")

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        return ProfileCatalog(process=LAYERS)

    def own_profiles(self) -> ProfileCatalog:
        return ProfileCatalog(process=LAYERS)

    def printers(self, configured: tuple[PrinterInfo, ...]) -> tuple[PrinterInfo, ...]:
        return (FORM4,)

    def status(self, printer: PrinterInfo) -> PrinterStatus:
        return PrinterStatus("IDLE", True, True, materials=RESINS)

    def slice(self, job: SliceInput, progress: Any) -> SliceOutput:
        self.jobs.append(job)
        progress("Slicing in PreForm")
        return SliceOutput(
            b"form", f"{job.job_name}.form", "form", print_time_s=22_320, layers=1234,
            report={"volume_ml": 7.43}, warnings=("2 unsupported minima",),
        )  # fmt: skip

    def submit(
        self, printer: PrinterInfo, output: SliceOutput, *, start: bool, **kw: Any
    ) -> Submission:
        self.submitted.append((output.filename, start))
        return Submission("job-7", "waiting", WAITING)


def with_resin(mods: Modules, resin: FakeResin, default: str | None = None) -> Modules:
    mods.targets["formlabs"] = mods.slicers["formlabs"] = resin
    if default is not None:
        mods.default_printer = default
    return mods


@pytest.fixture
def onshape() -> OnshapeClient:
    return OnshapeClient(
        "https://cad.onshape.com", Keys("a", "b"), transport=httpx.MockTransport(fake_onshape)
    )


@pytest.fixture
def resin() -> FakeResin:
    return FakeResin()


def mods(cfg: Config, resin: FakeResin, default: str | None = None) -> Modules:
    return with_resin(fake_modules(cfg, FakeBambuddy(job_states=["completed"])), resin, default)


def req() -> Any:
    return parse_onshape_url(URL, "bambuddy", ["bambuddy"], "JHD")


# -- settings ---------------------------------------------------------------------------


def test_resin_settings_describe_supports_and_copies_only() -> None:
    s = PrintSettings(4, 40, "tree", True, brim=True, copies=3)
    assert s.describe("sla") == "auto supports, 3 copies"
    assert PrintSettings(supports="off").describe("sla") == "no supports (on the build platform)"
    assert s.describe() == s.describe("fdm")  # FDM as before
    assert s.for_sla() == PrintSettings(supports="normal", copies=3)
    assert PrintSettings(walls=5, supports="off").for_sla() == PrintSettings(supports="off")


# -- the plan ---------------------------------------------------------------------------


def test_resin_plan_drops_fdm_inputs_and_shows_layer_height(
    cfg: Config, onshape: OnshapeClient, resin: FakeResin
) -> None:
    settings = PrintSettings(
        3, 25, "tree", True, brim=True, extras=(("chamber_temperature", "50"),)
    )
    p = printing.plan_print(
        req(), cfg, onshape, mods(cfg, resin), "Form 4", Orientation.parse("auto"), settings,
        "FLGPGR05", "Textured PEI Plate", process="0.05 mm", machine="Not a printer profile",
    )  # fmt: skip
    assert p.sla and p.settings == PrintSettings(supports="normal")
    assert p.bed_type is None and p.tools == ()
    assert p.profiles == Profiles("FORM-4-0", "0.05 mm", "FLGPGR05")
    assert p.summary_lines() == [
        "Part:        Part 1  (test)",
        "Resin:       Grey V5 (loaded)",
        "Printer:     Form 4 (Form 4), now IDLE",
        "Presets:     FORM-4-0 / FLGPGR05",
        "Layer height: 0.05 mm",
        "Orientation: slicer auto-orient",
        "Settings:    auto supports",
        "Start:       goes to the printer's queue; start it on the printer if it doesn't "
        "start by itself",
    ]
    waits = dataclasses.replace(p, manual_start=True, material=None)
    lines = waits.summary_lines()
    assert "Resin:       the configured resin (FLGPGR05)" in lines
    assert "Start:       waits in PreForm's queue until you press Start" in lines
    with pytest.raises(BadRequest, match=r"No process profile '0\.3 mm'"):
        printing.plan_print(
            req(), cfg, onshape, mods(cfg, resin), "Form 4", Orientation.parse("auto"),
            settings, process="0.3 mm",
        )  # fmt: skip


def test_resin_print_is_one_material(cfg: Config, onshape: OnshapeClient, resin: FakeResin) -> None:
    def plan(first: str | None, extra: str) -> printing.PrintPlan:
        return printing.plan_print(
            req(), cfg, onshape, mods(cfg, resin), "Form 4", Orientation.parse("auto"),
            PrintSettings(), first, extra_parts=[("JKD", extra)],
        )  # fmt: skip

    with pytest.raises(BadRequest, match="A resin print is one material"):
        plan("FLGPGR05", "FLGPCL05")
    with pytest.raises(BadRequest, match="Pick the resin for a print of several parts"):
        plan(None, "FLGPGR05")
    p = plan("FLGPGR05", "FLGPGR05")
    assert p.summary_lines()[:3] == [
        "Parts:       2 parts, one print  (test)",
        "  · Part 1",
        "  · Text",
    ]


def test_resin_print_slices_and_reports_volume_and_warnings(
    cfg: Config, onshape: OnshapeClient, resin: FakeResin, caplog: pytest.LogCaptureFixture
) -> None:
    m = mods(cfg, resin)
    p = printing.plan_print(
        req(), cfg, onshape, m, "Form 4", Orientation.parse("auto"), PrintSettings(supports="off"),
        "FLGPCL05",
    )  # fmt: skip
    with caplog.at_level(logging.WARNING):
        out = printing.execute_print(p, cfg, onshape, m, queue=True)
    (job,) = resin.jobs
    assert job.media == "form" and job.bed_type is None and job.auto_orient
    assert job.settings == PrintSettings(supports="off") and job.profiles.filament == "FLGPCL05"
    assert resin.submitted == [(out.slice.filename, True)]
    assert printing.outcome_lines(p, out) == [
        "Sliced:      6 h 12 min, 1234 layers, ≈ 7.4 mL resin",
        "Warning:     2 unsupported minima",
        f"Queued:      job job-7 on Form 4; {WAITING}",
    ]
    assert "slicer warning: 2 unsupported minima" in caplog.text
    with pytest.raises(BadRequest, match="resin printer"):
        printing.studio_project(p, cfg, onshape, m)


def test_fdm_outcome_lines_are_unchanged(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    m = fake_modules(cfg, fake)
    p = printing.plan_print(
        req(), cfg, onshape, m, None, Orientation.parse("z+"), PrintSettings(), "254"
    )
    out = printing.PrintOutcome(
        printing.slice_input(p, cfg, onshape),
        SliceOutput(b"", "x.gcode.3mf", "gcode.3mf", 3_720, 12.34),
        Submission("42", "started"),
    )
    assert printing.outcome_lines(p, out) == [
        "Sliced:      62 min, 12.3 g",
        "Queued:      item 42 on A1 Mini",
    ]


def test_duration() -> None:
    assert [printing.duration(s) for s in (None, 0, 59, 2_520, 22_320)] == [
        "? min", "? min", "0 min", "42 min", "6 h 12 min",
    ]  # fmt: skip


# -- the right-click page ---------------------------------------------------------------


@pytest.fixture
def srv(cfg: Config, resin: FakeResin) -> Iterator[Running]:
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    with_resin(run.svc.modules, resin)
    yield run
    run.httpd.shutdown()


def forms(html: str) -> list[str]:
    return re.findall(r'<form method="post" action="/print">.*?</form>', html, re.S)


def hidden(form: str) -> dict[str, str]:
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form))
    return {k: v.replace("&amp;", "&") for k, v in fields.items()}


def test_page_has_a_resin_form_beside_the_fdm_one(srv: Running) -> None:
    r = srv.http.get(f"/print?{PRINT_QUERY}", headers=NAV)
    assert r.status_code == 200, r.text
    fdm, sla = forms(r.text)
    assert r.text.index("<h2>Filament printers</h2>") < r.text.index("<h2>Resin printers</h2>")
    assert hidden(fdm)["csrf"] != hidden(sla)["csrf"]  # one token per form
    # The FDM form as before...
    assert "Printer and filament" in fdm and 'name="walls"' in fdm and 'name="plate"' in fdm
    assert "Form 4" not in fdm
    # ...and the resin one: no FDM fields, resin wording, its own defaults.
    assert "Printer and resin" in sla and "A1 Mini" not in sla
    for name in ("walls", "infill", "build_plate_only", "top_layers", "bottom_layers", "brim"):
        assert f'name="{name}"' not in sla
    assert 'name="plate"' not in sla
    assert '<option value="formlabs/form4|FLGPGR05" selected>Form 4 · Grey V5 (loaded)' in sla
    assert "Form 4 · configured resin (FLGPGR05)" in sla
    assert (
        '<select name="supports"><option value="off">None (on the build platform)</option>'
        '<option value="normal" selected>Auto supports</option></select>' in sla
    )
    assert '<option value="auto" selected>' in sla and 'name="copies"' in sla


def test_page_for_a_resin_default_printer_alone(cfg: Config, resin: FakeResin) -> None:
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    run.svc.modules = with_resin(Modules(), resin, "Form 4")
    try:
        r = run.http.get(f"/print?{PRINT_QUERY}", headers=NAV)
        assert r.status_code == 200, r.text
        assert len(forms(r.text)) == 1 and "<h2>" not in r.text
        assert "Printer and resin" in r.text and 'name="walls"' not in r.text
    finally:
        run.httpd.shutdown()


def test_resin_print_from_the_page_shows_volume_and_warnings(
    srv: Running, resin: FakeResin
) -> None:
    r = srv.http.get(f"/print?{PRINT_QUERY}", headers=NAV)
    form = hidden(forms(r.text)[1])
    # A stale form may still post FDM fields: they're ignored, not refused.
    r = srv.post({
        **form, "printer": "formlabs/form4|FLGPGR05", "orient": "auto", "supports": "normal",
        "copies": "2", "walls": "3", "infill": "25", "brim": "true", "plate": "Cool Plate",
    })  # fmt: skip
    assert r.status_code == 303, r.text
    page = wait_job(srv, r.headers["Location"])
    assert "Queued ✓" in page.text, page.text
    for line in (
        "Resin:       Grey V5 (loaded)",
        "Layer height: 0.1 mm",
        "Settings:    auto supports, 2 copies",
        "Sliced:      6 h 12 min, 1234 layers, ≈ 7.4 mL resin",
        "Warning:     2 unsupported minima",
        f"Queued:      job job-7 on Form 4; {WAITING}",
    ):
        assert line in page.text
    assert "Plate:" not in page.text and "walls" not in page.text
    (job,) = resin.jobs
    assert job.settings == PrintSettings(supports="normal", copies=2) and job.bed_type is None
    assert srv.fake.queued == []  # nothing went to BamBuddy


# -- the panel --------------------------------------------------------------------------


def test_panel_marks_resin_printers_and_labels_for_one(srv: Running) -> None:
    r = srv.http.get(f"/panel?{PANEL_QUERY}", headers=FRAME)
    assert r.status_code == 200, r.text
    assert '<option value="formlabs/form4" data-tech="sla">Form 4 (Form 4), idle</option>' in r.text
    assert '<option value="bambuddy/1" selected>' in r.text  # FDM: no data-tech
    assert "<label>Filament <select" in r.text and ">Process <select" in r.text
    choices = re.search(r'name="filament" class="fc" data-choices="([^"]*)"', r.text)
    assert choices and "Configured resin (FLGPGR05)" in choices.group(1)
    srv.svc.modules.default_printer = "Form 4"
    r = srv.http.get(f"/panel?{PANEL_QUERY}", headers=FRAME)
    assert '<option value="formlabs/form4" data-tech="sla" selected>' in r.text
    assert "<label>Resin <select" in r.text and "<label>Layer height <select" in r.text
    assert '<option value="FLGPGR05" selected>Grey V5 (loaded)</option>' in r.text
    assert '<option value="0.025 mm">0.025 mm</option>' in r.text  # layer heights
    beds = re.search(r'data-beds="([^"]*)"', r.text)
    assert beds and "&quot;formlabs/form4&quot;: [200, 125]" in beds.group(1)


def panel_post(srv: Running, **fields: str) -> httpx.Response:
    r = srv.http.get(f"/panel?{PANEL_QUERY}", headers=FRAME)
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', r.text))
    base = {"p": "JHD", "printer": "formlabs/form4", "orient": "auto", "supports": "normal"}
    return post_panel(srv, {**form, **base, **fields})


def test_panel_resin_print_of_two_parts_in_one_resin(srv: Running, resin: FakeResin) -> None:
    r = panel_post(srv, filament="FLGPGR05", extra="JKD:FLGPGR05", process="0.05 mm")
    assert r.status_code == 303, r.text
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "2 parts, one print" in page.text and "Layer height: 0.05 mm" in page.text
    (job,) = resin.jobs
    assert [p.name for p in job.parts] == ["Part 1", "Text"]


def test_panel_resin_print_refuses_two_resins(srv: Running, resin: FakeResin) -> None:
    r = panel_post(srv, filament="FLGPGR05", extra="JKD:FLGPCL05")
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "Failed" in page.text and "A resin print is one material" in page.text
    assert resin.jobs == []


def test_no_bambu_project_for_a_resin_printer(srv: Running) -> None:
    r = srv.http.get(f"/panel?{PANEL_QUERY}", headers=FRAME)
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', r.text))
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
    fields = {**form, "p": "JHD", "printer": "formlabs/form4", "orient": "auto"}
    link = srv.http.post("/panel/model-link", data=fields, headers=h)
    assert link.status_code == 200, link.text
    path = re.sub(r"^https?://[^/]+", "", link.json()["url"])
    got = srv.http.get(path, headers={"Host": HOST})
    assert got.status_code == 400 and "resin printer" in got.text


# -- the CLI ----------------------------------------------------------------------------


@pytest.fixture
def cli_env(cfg: Config, resin: FakeResin, monkeypatch: pytest.MonkeyPatch) -> FakeResin:
    def from_config(cls: type[Modules], cfg: Config, **kw: Any) -> Modules:
        return with_resin(Modules(), resin, "Form 4")

    monkeypatch.setattr(registry.Modules, "from_config", classmethod(from_config))
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.auth, "load_keys", lambda: Keys("a", "b"))
    monkeypatch.setattr(
        cli,
        "OnshapeClient",
        functools.partial(OnshapeClient, transport=httpx.MockTransport(fake_onshape)),
    )
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    return resin


def test_cli_resin_print(cli_env: FakeResin, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["print", "--url", URL, "--part", "JHD", "--walls", "4", "--plate", "Cool Plate"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    for line in (
        "Resin:       the configured resin (FLGPGR05)",
        "Layer height: 0.1 mm",
        "Orientation: slicer auto-orient",
        "Settings:    auto supports",
        "Note:        --walls, --plate don't apply to a resin printer; ignored",
        "Warning: 2 unsupported minima",
        f"Queued on Form 4 (job job-7); {WAITING}.",
    ):
        assert line in out, out
    assert re.search(r"Sliced Part 1_\S+\.form: 6 h 12 min, 1234 layers, ≈ 7\.4 mL resin", out)
    assert "Plate:" not in out and "4 walls" not in out
    (job,) = cli_env.jobs
    assert job.settings == PrintSettings(supports="normal") and job.auto_orient


def test_cli_resin_print_keeps_given_supports_and_orientation(
    cli_env: FakeResin, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["print", "--url", URL, "--part", "JHD", "--supports", "off", "--orient", "z+"]
    assert cli.main([*args, "--slot", "FLGPCL05"]) == 0
    out = capsys.readouterr().out
    assert "Settings:    no supports (on the build platform)" in out
    assert "Orientation: +z side down" in out and "Resin:       Clear V5" in out
    assert "Note:" not in out
    (job,) = cli_env.jobs
    assert job.settings.supports == "off" and not job.auto_orient


def test_resin_print_through_the_preform_module(
    cfg: Config, onshape: OnshapeClient, tmp_path: Any
) -> None:
    """The real module behind the core: plan, slice, queue, and the job waits at the printer."""
    from tests.fakes_preform import FORM4 as FORM4_ID
    from tests.fakes_preform import FakePreform
    from tests.test_preform import make

    fake = FakePreform()
    module = make(fake, tmp_path)
    m = fake_modules(cfg, FakeBambuddy(job_states=["completed"]))
    m.targets["form"] = m.slicers["form"] = module
    p = printing.plan_print(
        req(),
        cfg,
        onshape,
        m,
        FORM4_ID,
        Orientation.parse("auto"),
        PrintSettings(supports="normal"),
    )
    assert p.sla and p.printer.key == f"form/{FORM4_ID}"
    out = printing.execute_print(p, cfg, onshape, m, queue=True)
    assert out.slice.media == "form" and out.slice.filename.endswith(".form")
    assert out.submission is not None and out.submission.state == "waiting"
    (upload,) = fake.uploads
    assert upload["print_now"] is False
    assert any(
        line.startswith("Sliced:") and "mL resin" in line for line in printing.outcome_lines(p, out)
    )
    assert "POST /v1/scene/model/auto-orient/?async=true" in fake.paths()
    assert "POST /v1/scene/model/auto-support/?async=true" in fake.paths()
