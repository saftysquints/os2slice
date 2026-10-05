"""BamBuddy printer pools ("Any <model>") and the per-print Wait for Start choice."""

from __future__ import annotations

import dataclasses
import functools
import re
from typing import Any

import httpx
import pytest

from os2slice import cli, config, printing
from os2slice.auth import Keys
from os2slice.bambuddy import BambuddyClient, BambuddyError
from os2slice.modules import registry
from os2slice.modules.bambuddy import BambuddyModule, pool_material_id
from os2slice.modules.base import Material, ModelDefaults, ModuleError, PartGeometry, SliceOutput
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.settings import PrintSettings
from tests.conftest import DOC, ELEM, WS, make_stl
from tests.fakes import (
    FILAMENT_PRESETS,
    PRINTERS,
    FakeBambuddy,
    dual_nozzle_3mf,
    fake_onshape,
    uploaded_zip,
)
from tests.test_modules import H2D, job_for, module
from tests.test_printing import (  # noqa: F401  (onshape is a fixture)
    mods,
    onshape,
    req,
    with_models,
)
from tests.test_server import Running, choices, panel_form, wait_job

URL = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}"


def two_a1_minis(fake: FakeBambuddy, down: int | None = None) -> httpx.MockTransport:
    """The fake with printer 2 turned into a second A1 Mini (`down`: a printer whose
    status fails)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/printers/":
            rows = [dict(p) for p in PRINTERS]
            rows[1].update(name="A1 Mini 2", model="A1 Mini")
            return httpx.Response(200, json=rows)
        if down is not None and request.url.path == f"/api/v1/printers/{down}/status":
            return httpx.Response(500, json={"detail": "boom"})
        return fake(request)

    return httpx.MockTransport(handler)


def sliced(m: BambuddyModule) -> SliceOutput:
    """A file BamBuddy sliced itself: already in its library as file 31."""
    report = {"module": "bambuddy", "url": m.url, "library_file_id": 31}
    return SliceOutput(b"PK", "x.gcode.3mf", "gcode.3mf", report=report)


def pool(m: BambuddyModule, model: str) -> Any:
    return next(p for p in m.printers(()) if p.name == f"Any {model}")


# -- pools in the printer list ----------------------------------------------------


def test_pools_follow_the_printers_one_per_model_with_defaults() -> None:
    printers = module(FakeBambuddy()).printers(())
    names = [p.name for p in printers]
    assert names == ["A1 Mini", "X1C_01", "Old", "H2D_01", "Any A1 Mini", "Any H2D"]
    a1 = printers[4]
    assert (a1.key, a1.model, a1.target, a1.slicer, a1.bed_mm) == (
        "farm/any:A1 Mini", "A1 Mini", "farm", "farm", (180, 180),
    )  # fmt: skip
    assert a1.pool and a1.active and a1.profiles == printers[0].profiles
    assert a1.extra["target_model"] == "A1 Mini" and "printer_id" not in a1.extra
    assert a1.extra["bed_type"] == "Textured PEI Plate" and a1.extra["members"] == (1,)
    h2d = printers[5]
    assert h2d.key == "farm/any:H2D" and h2d.nozzle_count == 2
    # Keys never collide with a configured [printers.<key>] key.
    assert not config.PRINTER_KEY_RE.fullmatch(a1.key)


def test_registry_finds_a_pool_by_key_or_name(cfg: config.Config) -> None:
    m = mods(cfg, FakeBambuddy())
    assert m.find("Any A1 Mini").key == "bambuddy/any:A1 Mini"
    assert m.find("bambuddy/any:A1 Mini").pool


def test_pool_status_merges_the_models_printers() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = two_a1_minis(fake)
    st = m.status(pool(m, "A1 Mini"))
    assert (st.state, st.ready, st.detail) == ("IDLE", True, "2 of 2 free")
    by_id = {x.id: x for x in st.materials}
    assert set(by_id) == {"PETG.000000", "ASA.FFF144", "PLA.FFFFFF", "PEEK.161616", "PETG.161616"}
    petg = by_id["PETG.000000"]
    assert petg.kind == "PETG" and petg.colour == "#000000" and petg.extruder is None
    assert petg.profile == "Generic PETG @BBL A1M"
    assert petg.label == "PETG · black (loaded on 1 of 2 printers)"
    assert all(r.method == "GET" for r in fake.requests)


def test_pool_status_with_a_member_down_and_all_down() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = two_a1_minis(fake, down=2)
    st = m.status(pool(m, "A1 Mini"))
    assert st.ready and st.detail == "1 of 2 free"
    assert {x.id for x in st.materials} == {"PETG.000000"}
    fake.status = "RUNNING"
    st = m.status(pool(m, "A1 Mini"))
    assert (st.state, st.ready) == ("BUSY", False)
    m._transport = httpx.MockTransport(
        lambda r: fake(r) if r.url.path == "/api/v1/printers/" else httpx.Response(500)
    )
    st = m.status(pool(m, "A1 Mini"))
    assert (st.state, st.connected, st.ready) == ("offline", False, False)


def test_dual_nozzle_pool_materials_carry_the_nozzle_that_feeds_them() -> None:
    m = module(FakeBambuddy())
    h2d = pool(m, "H2D")
    by_id = {x.id: x for x in m.status(h2d).materials}
    red, white = by_id["PLA.FF0000"], by_id["PLA.FFFFFF"]  # AMS 2 → right, AMS 1 → left
    assert (red.extruder, white.extruder) == (0, 1)
    assert red.label == "PLA · red · right nozzle (loaded)" and printing.usable(red, h2d)
    assert by_id["PLA.000000"].extruder == 1 and by_id["PLA.0000FF"].extruder == 0  # Ext-L/R


def test_pool_material_id_fits_the_forms() -> None:
    assert pool_material_id("PETG HF", "#00ff00") == "PETG_HF.00FF00"
    assert re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", pool_material_id("X" * 60, "#FFFFFF"))


# -- queueing on a pool -------------------------------------------------------------


def test_pool_submit_names_the_model_and_forces_the_colours() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    h2d = pool(m, "H2D")
    out = sliced(m)
    red = Material("PLA.FF0000", "PLA · red", "PLA", "#FF0000", raw={"type": "PLA"})
    white = Material("PLA.FFFFFF", "PLA · white", "PLA", "#FFFFFF", raw={"type": "PLA"})
    sub = m.submit(h2d, out, start=True, materials=(red, white))
    assert fake.queued[0] == {
        "library_file_id": 31,
        "target_model": "H2D",
        "manual_start": False,
        "filament_overrides": [
            {"slot_id": 1, "type": "PLA", "color": "#FF0000", "color_name": "red",
             "force_color_match": True},
            {"slot_id": 2, "type": "PLA", "color": "#FFFFFF", "color_name": "white",
             "force_color_match": True},
        ],
    }  # fmt: skip
    assert sub.state == "started" and "waits for an idle H2D with the filament loaded" in sub.detail
    sub = m.submit(h2d, out, start=False)  # the preset filament: no overrides
    assert fake.queued[1] == {"library_file_id": 31, "target_model": "H2D", "manual_start": True}
    assert sub.state == "waiting" and "waits for Start in BamBuddy" in sub.detail


def test_pool_submit_shows_bambuddys_waiting_reason() -> None:
    fake = FakeBambuddy()

    def handler(request: httpx.Request) -> httpx.Response:
        r = fake(request)
        if request.url.path == "/api/v1/queue/":
            return httpx.Response(200, json={**r.json(), "waiting_reason": "Waiting on PLA"})
        return r

    m = module(fake)
    m._transport = httpx.MockTransport(handler)
    out = SliceOutput(b"PK", "x.gcode.3mf", "gcode.3mf")
    sub = m.submit(pool(m, "A1 Mini"), out, start=True)
    assert sub.detail.endswith("(Waiting on PLA)")


def test_single_printer_submit_is_unchanged() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    a1 = m.printers(())[0]
    out = sliced(m)
    m.submit(a1, out, start=False, materials=(Material("254", "ext"),))
    assert fake.queued == [
        {"library_file_id": 31, "printer_id": 1, "manual_start": True, "use_ams": False}
    ]


def test_queue_print_needs_exactly_one_destination() -> None:
    fake = FakeBambuddy()
    with BambuddyClient("http://bb.test:8000", "k", transport=httpx.MockTransport(fake)) as bb:
        with pytest.raises(BambuddyError):
            bb.queue_print(31, None, True)
        with pytest.raises(BambuddyError):
            bb.queue_print(31, 1, True, target_model="X1C")
        with pytest.raises(BambuddyError, match="maps the AMS itself"):
            bb.queue_print(31, None, True, [0], target_model="X1C")
    assert fake.queued == []


def test_dual_nozzle_pool_filament_without_a_nozzle_slices_unpinned() -> None:
    # No nozzle known (a bare preset, an unmapped AMS): no Manual map and no nozzle check;
    # the sliced file prints on the left nozzle and that's accepted.
    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf())
    m = module(fake)
    red = Material("PLA.FF0000", "PLA · red", "PLA", "#FF0000", profile="Generic PLA @BBL H2D")
    job = job_for(m, "Any H2D", [PartGeometry("Part 1", make_stl(), red)])
    m.slice(job, lambda s: None)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert "filament_maps" not in ms and "Manual" not in ms


# -- through the print path -----------------------------------------------------------


def test_plan_and_print_on_a_pool(cfg: config.Config, onshape: OnshapeClient) -> None:  # noqa: F811
    fake = FakeBambuddy(job_states=["completed"])
    m = mods(cfg, fake)
    p = printing.plan_print(req(), cfg, onshape, m, "Any A1 Mini", Orientation.parse("z+"),
                            PrintSettings(), "PETG.000000")  # fmt: skip
    assert p.profiles.filament == "Generic PETG @BBL A1M" and not p.manual_start
    assert "starts on the first free A1 Mini with the filament loaded" in "\n".join(
        p.summary_lines()
    )
    printing.execute_print(p, cfg, onshape, m, queue=True)
    (item,) = fake.queued
    assert item["target_model"] == "A1 Mini" and "printer_id" not in item
    assert item["filament_overrides"][0]["type"] == "PETG"
    assert "ams_mapping" not in item and "use_ams" not in item


def test_plan_takes_the_wait_choice_and_defaults_to_start(
    cfg: config.Config,
    onshape: OnshapeClient,  # noqa: F811
) -> None:
    def plan(c: config.Config, **wait: bool) -> printing.PrintPlan:
        m = mods(c, FakeBambuddy())
        z = Orientation.parse("z+")
        return printing.plan_print(req(), c, onshape, m, None, z, PrintSettings(), **wait)

    assert not plan(cfg).manual_start  # default: start by itself
    assert plan(cfg, manual_start=True).manual_start
    assert not plan(cfg, manual_start=False).manual_start
    # A target still configured `manual_start = true` (deprecated) loads and is ignored.
    cfg2 = waiting(cfg)
    assert not plan(cfg2).manual_start and plan(cfg2, manual_start=True).manual_start


def waiting(cfg: config.Config) -> config.Config:
    """`cfg` with its BamBuddy target set to `manual_start = true` (deprecated)."""
    t = cfg.targets["bambuddy"]
    t = dataclasses.replace(t, values={**t.values, "manual_start": True})
    return dataclasses.replace(cfg, targets={**cfg.targets, "bambuddy": t})


# -- the Wait for Start checkbox --------------------------------------------------


@pytest.fixture
def running(cfg: config.Config) -> Any:
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    yield run
    run.httpd.shutdown()


def _checkbox(html: str) -> str:
    m = re.search(r'<input type="checkbox" name="manual_start"[^>]*>', html)
    assert m, html
    return m.group(0)


def test_wait_checkbox_unchecked_by_default(running: Running) -> None:
    r, _ = running.get_form()
    assert "checked" not in _checkbox(r.text)
    assert "Wait for Start in BamBuddy (don't start by itself)" in r.text
    assert "Any A1 Mini" in r.text  # the pool is in the page's printer menu too
    r, _ = panel_form(running)
    assert "checked" not in _checkbox(r.text)


def test_wait_checkbox_unchecked_even_when_the_target_says_wait(cfg: config.Config) -> None:
    """`manual_start = true` on a target is deprecated and no longer ticks the box."""
    run = Running(waiting(cfg), FakeBambuddy(job_states=["completed"]))
    try:
        r, form = run.get_form()
        assert "checked" not in _checkbox(r.text)
        r2, _ = panel_form(run)
        assert "checked" not in _checkbox(r2.text)
        r = run.post({**form, **choices()})  # unticked: the print starts by itself
        assert r.status_code == 303
        wait_job(run, r.headers["Location"])
        assert run.fake.queued[0]["manual_start"] is False
    finally:
        run.httpd.shutdown()


def test_wait_checkbox_follows_the_print_default(cfg: config.Config) -> None:
    """`[print_defaults] wait_for_start = true` ticks the box on both pages; the
    person's choice still wins (unticked = not sent = start by itself)."""
    run = Running(
        dataclasses.replace(cfg, default_wait_for_start=True),
        FakeBambuddy(job_states=["completed"]),
    )
    try:
        r, form = run.get_form()
        assert " checked" in _checkbox(r.text)
        r2, _ = panel_form(run)
        assert " checked" in _checkbox(r2.text)
        r = run.post({**form, **choices()})  # the person unticked it
        assert r.status_code == 303
        wait_job(run, r.headers["Location"])
        assert run.fake.queued[0]["manual_start"] is False
    finally:
        run.httpd.shutdown()


def test_wait_choice_refusal_keeps_the_token(running: Running) -> None:
    _, form = running.get_form()
    r = running.post({**form, **choices(manual_start="yes")})
    assert r.status_code == 400 and "Invalid Wait for Start choice" in r.text
    r = running.post({**form, **choices()})  # same token: a bad value didn't burn it
    assert r.status_code == 303
    wait_job(running, r.headers["Location"])
    assert running.fake.queued[0]["manual_start"] is False


# -- the CLI ----------------------------------------------------------------------------


def test_cli_wait_for_start_flag() -> None:
    p = cli.build_parser()
    base = ["print", "--url", URL]
    assert p.parse_args(base).wait_for_start is None  # unset: [print_defaults] decides
    assert p.parse_args([*base, "--wait-for-start"]).wait_for_start is True
    assert p.parse_args([*base, "--no-wait-for-start"]).wait_for_start is False


@pytest.mark.parametrize(
    ("flag", "target_says_wait", "default_wait", "waits"),
    [
        ([], False, False, False),
        (["--wait-for-start"], False, False, True),
        (["--no-wait-for-start"], False, False, False),
        ([], True, False, False),
        ([], False, True, True),  # [print_defaults] wait_for_start = true
        (["--no-wait-for-start"], False, True, False),
        (["--wait-for-start"], False, True, True),
    ],
)
def test_cli_print_passes_the_choice(
    cfg: config.Config,
    monkeypatch: pytest.MonkeyPatch,
    flag: list[str],
    target_says_wait: bool,
    default_wait: bool,
    waits: bool,
) -> None:
    if target_says_wait:  # deprecated `manual_start = true`: the CLI still starts
        cfg = waiting(cfg)
    cfg = dataclasses.replace(cfg, default_wait_for_start=default_wait)
    fake = FakeBambuddy(job_states=["completed"])
    real = registry.Modules.from_config.__func__  # type: ignore[attr-defined]
    monkeypatch.setattr(
        registry.Modules,
        "from_config",
        classmethod(
            functools.partial(real, secrets=lambda n: "k", transport=httpx.MockTransport(fake))
        ),
    )
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.auth, "load_keys", lambda: Keys("a", "b"))
    monkeypatch.setattr(
        cli,
        "OnshapeClient",
        functools.partial(OnshapeClient, transport=httpx.MockTransport(fake_onshape)),
    )
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    assert cli.main(["print", "--url", URL, "--part", "JHD", *flag]) == 0
    assert fake.queued[0]["manual_start"] is waits


def test_loaded_note_counts_slots_and_printers_separately() -> None:
    """ "loaded on 3 of 2" read as nonsense: slots and printers are now named."""
    from os2slice.modules.bambuddy import _loaded_note

    assert _loaded_note(1, 1, 1) == " (loaded)"
    assert _loaded_note(2, 1, 1) == " (loaded in 2 slots)"
    assert _loaded_note(1, 1, 2) == " (loaded on 1 of 2 printers)"
    assert _loaded_note(3, 2, 2) == " (loaded in 3 slots on 2 of 2 printers)"
    assert _loaded_note(2, 2, 3) == " (loaded on 2 of 3 printers)"


# -- dual-nozzle pools: the nozzle each filament is pinned to (D-35) --------------------

BLUE_ASA = {"id": 0, "tray_type": "ASA", "tray_color": "2140B4FF", "exists": True, "remain": 0}


def h2d_farm(fake: FakeBambuddy, statuses: dict[int, dict[str, Any]]) -> Any:
    """A handler: the fake with more H2Ds ("H2D_<id>"), each with its own status filament."""
    extra = [
        {"id": pid, "name": f"H2D_{pid:02d}", "model": "H2D", "is_active": True,
         "nozzle_count": 2}
        for pid in statuses
    ]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/printers/":
            fake.requests.append(request)
            return httpx.Response(200, json=[*PRINTERS, *extra])
        for pid, body in statuses.items():
            if path == f"/api/v1/printers/{pid}/status":
                fake.requests.append(request)
                return httpx.Response(
                    200, json={"id": pid, "connected": True, "state": fake.status, **body}
                )
        return fake(request)

    return handler


def ams0(tray: dict[str, Any], side: int | None) -> dict[str, Any]:
    """One AMS (unit 0) holding `tray`, feeding nozzle `side` (None: not in the map)."""
    return {
        "ams_extruder_map": {} if side is None else {"0": side},
        "ams": [{"id": 0, "tray": [tray]}],
        "vt_tray": [],
    }


def red(side: int | None) -> dict[str, Any]:
    tray = {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "exists": True, "remain": 0}
    return ams0(tray, side)


def test_pool_colour_only_on_a_left_fed_ams_is_pinned_left_through_the_print(
    cfg: config.Config,
    onshape: OnshapeClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed print (queue item 9): blue ASA only on H2D_04's AMS 1, which feeds
    the left nozzle. The pool material says left, the 3MF pins it there, and it's queued."""
    from tests import fakes

    monkeypatch.setattr(fakes, "FILAMENT_PRESETS", [*FILAMENT_PRESETS, "Generic ASA @BBL H2D"])
    c = with_models(cfg, H2D=ModelDefaults(profiles=H2D))
    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf(kind="ASA"))
    statuses = {4: {**ams0(BLUE_ASA, 1), "ams_extruder_map": {"0": 1, "1": 0}}}
    m = mods(c, h2d_farm(fake, statuses))
    p = printing.plan_print(req(), c, onshape, m, "Any H2D", Orientation.parse("z+"),
                            PrintSettings(), "ASA.2140B4")  # fmt: skip
    assert p.material is not None and p.material.extruder == 1
    assert p.material.label == "ASA · blue · left nozzle (loaded on 1 of 2 printers)"
    printing.execute_print(p, c, onshape, m, queue=True)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert 'filament_maps" value="1"' in ms and "Manual" in ms  # pinned to the left nozzle
    (item,) = fake.queued
    assert item["target_model"] == "H2D" and item["filament_overrides"][0]["color"] == "#2140B4"
    # Sliced onto the right nozzle after all: refused after slicing, like one printer.
    fake.sliced_3mf = dual_nozzle_3mf(extruder_id=2, kind="ASA")
    with pytest.raises(BambuddyError, match="prints on the right nozzle"):
        printing.execute_print(p, c, onshape, m, queue=True)
    assert len(fake.queued) == 1


def test_pool_nozzle_is_the_one_on_most_printers() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    # H2D_01 (the fake) has red on the right; two more have it on the left.
    m._transport = httpx.MockTransport(h2d_farm(fake, {4: red(1), 6: red(1)}))
    st = m.status(pool(m, "H2D"))
    r = next(x for x in st.materials if x.id == "PLA.FF0000")
    assert r.extruder == 1
    assert r.label == "PLA · red · left nozzle on 2, right on 1 (loaded on 3 of 3 printers)"
    # A tie (one each side) goes to the right (main) nozzle.
    m._transport = httpx.MockTransport(h2d_farm(fake, {4: red(1)}))
    r = next(x for x in m.status(pool(m, "H2D")).materials if x.id == "PLA.FF0000")
    assert r.extruder == 0 and "right nozzle on 1, left on 1" in r.label


def test_pool_nozzle_unknown_stays_none() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = httpx.MockTransport(
        h2d_farm(fake, {4: ams0(BLUE_ASA, None)})
    )  # AMS 0 not in the map
    h2d = pool(m, "H2D")
    asa = next(x for x in m.status(h2d).materials if x.id == "ASA.2140B4")
    assert asa.extruder is None and "nozzle" not in asa.label
    # Another member that knows decides; the unknown one doesn't count.
    m._transport = httpx.MockTransport(
        h2d_farm(fake, {4: ams0(BLUE_ASA, None), 6: ams0(BLUE_ASA, 1)})
    )
    asa = next(x for x in m.status(pool(m, "H2D")).materials if x.id == "ASA.2140B4")
    assert asa.extruder == 1 and asa.label.endswith("· left nozzle (loaded on 2 of 3 printers)")


def test_single_nozzle_pool_materials_have_no_nozzle() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = two_a1_minis(fake)
    st = m.status(pool(m, "A1 Mini"))
    assert all(x.extruder is None and "nozzle" not in x.label for x in st.materials)


# -- the pre-queue check on dual-nozzle pools ------------------------------------------

RED = Material("PLA.FF0000", "PLA · red", "PLA", "#FF0000", extruder=0, raw={"type": "PLA"})


def sliced_on(m: BambuddyModule, data: bytes) -> SliceOutput:
    return dataclasses.replace(sliced(m), data=data)


def status_calls(fake: FakeBambuddy) -> int:
    return sum(1 for r in fake.requests if r.url.path.endswith("/status"))


def test_pool_refuses_a_colour_no_member_has_on_the_sliced_nozzle() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    h2d = pool(m, "H2D")
    left = sliced_on(m, dual_nozzle_3mf())  # H2D_01 has red only on the right
    with pytest.raises(ModuleError, match="No H2D has PLA red on the left nozzle") as e:
        m.submit(h2d, left, start=True, materials=(RED,))
    assert "AMS feeding the nozzle" in e.value.fix
    assert fake.queued == [] and fake.uploads == []
    m.submit(h2d, sliced_on(m, dual_nozzle_3mf(extruder_id=2)), start=True, materials=(RED,))
    assert fake.queued[0]["filament_overrides"][0]["color"] == "#FF0000"


def test_pool_check_needs_one_member_with_it_on_that_side() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = httpx.MockTransport(
        h2d_farm(fake, {4: red(1)})
    )  # H2D_04 has red on the left: it can take it
    m.submit(pool(m, "H2D"), sliced_on(m, dual_nozzle_3mf()), start=True, materials=(RED,))
    assert len(fake.queued) == 1


def test_pool_check_for_a_preset_uses_the_files_type() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    h2d = pool(m, "H2D")
    preset = printing.preset_material("Generic ASA @BBL H2D")
    with pytest.raises(ModuleError, match=r"ASA \(any colour\) on the left nozzle"):
        m.submit(h2d, sliced_on(m, dual_nozzle_3mf(kind="ASA")), start=True, materials=(preset,))
    # PLA on the left: H2D_01 has white PLA in AMS 1, which feeds the left nozzle.
    m.submit(h2d, sliced_on(m, dual_nozzle_3mf()), start=True, materials=(preset,))
    m.submit(h2d, sliced_on(m, dual_nozzle_3mf()), start=True)  # the printer's preset
    assert len(fake.queued) == 2 and "filament_overrides" not in fake.queued[0]


def test_pool_check_with_a_filament_switch_or_no_printer_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import fakes

    fake = FakeBambuddy()
    m = module(fake)
    h2d = pool(m, "H2D")
    left = sliced_on(m, dual_nozzle_3mf())
    monkeypatch.setitem(fakes.STATUS_FILAMENT[3], "fila_switch", {"installed": True})
    m.submit(h2d, left, start=True, materials=(RED,))  # an FTS feeds either nozzle
    assert len(fake.queued) == 1
    m._transport = httpx.MockTransport(
        lambda r: httpx.Response(500) if r.url.path.endswith("/status") else fake(r)
    )
    with pytest.raises(ModuleError, match="no printer answered"):
        m.submit(h2d, left, start=True, materials=(RED,))
    assert len(fake.queued) == 1


def test_pool_check_skipped_without_a_nozzle_in_the_file_and_on_single_nozzle_pools() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m.submit(pool(m, "H2D"), sliced(m), start=True, materials=(RED,))  # b"PK": no nozzle info
    a1 = Material("PETG.000000", "PETG · black", "PETG", "#000000", raw={"type": "PETG"})
    m.submit(pool(m, "A1 Mini"), sliced_on(m, dual_nozzle_3mf()), start=True, materials=(a1,))
    assert len(fake.queued) == 2 and status_calls(fake) == 0
