"""The module seam: registry, the BamBuddy slicer+target module, Bambu project layout."""

from __future__ import annotations

import httpx
import pytest

from os2slice.bambuddy import BambuddyError
from os2slice.errors import AuthError, BadRequest, ConfigError
from os2slice.modules import bambu_project, registry
from os2slice.modules.bambuddy import BambuddyModule
from os2slice.modules.base import (
    MEDIA_GCODE_3MF,
    Material,
    ModelDefaults,
    ModuleAuthError,
    ModuleError,
    PartGeometry,
    PrinterInfo,
    Profiles,
    SliceInput,
    SliceOutput,
    Slicer,
    Target,
    distinct_materials,
)
from os2slice.orientation import orient_parts
from os2slice.settings import PrintSettings
from tests.conftest import make_stl
from tests.fakes import FakeBambuddy, dual_nozzle_3mf, uploaded_zip

A1 = Profiles(
    "Bambu Lab A1 mini 0.4 nozzle", "0.20mm Standard @BBL A1M", "Bambu PLA Basic @BBL A1M"
)
H2D = Profiles("Bambu Lab H2D 0.4 nozzle", "0.20mm Standard @BBL H2D", "Bambu PLA Basic @BBL H2D")


def module(fake: FakeBambuddy, **values: object) -> BambuddyModule:
    models = {
        "A1 Mini": ModelDefaults(profiles=A1, bed_type="Textured PEI Plate"),
        "H2D": ModelDefaults(profiles=H2D),
    }
    return BambuddyModule(
        {"url": "http://bb.test:8000", "api_key": "k", "models": models, **values},
        key="farm",
        transport=httpx.MockTransport(fake),
    )


def cube(size: float = 10.0) -> bytes:
    """A real (non-degenerate) STL box so layouts have a footprint."""
    import struct

    s = size
    v = [(x, y, z) for x in (0, s) for y in (0, s) for z in (0, s)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]  # fmt: skip
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0) for a, b, c in faces)
    return b"\0" * 80 + struct.pack("<I", len(faces)) + body


# -- registry ------------------------------------------------------------------


def test_registry_and_specs() -> None:
    spec = registry.spec_for("bambuddy")
    assert spec.role == "both" and spec.discovers_printers
    assert spec.makes == spec.accepts == (MEDIA_GCODE_3MF,)
    assert {f.key: f.type for f in spec.fields} == {
        "url": "url", "folder": "str", "manual_start": "bool", "public_url": "url",
        "api_key": "secret",
    }  # fmt: skip
    assert registry.SLICERS["bambuddy"] is registry.TARGETS["bambuddy"] is BambuddyModule
    assert registry.spec_for("desktop").kind == "desktop"
    with pytest.raises(ConfigError, match="Unknown module kind"):
        registry.spec_for("octoprint")
    with pytest.raises(ConfigError, match="isn't a slicer"):
        registry.build_slicer("desktop", {}, key="x")


def test_builders_pass_key_only_to_modules_that_take_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class Plain:  # like Moonraker / PrusaLink: (values, *, transport)
        spec = registry.spec_for("bambuddy")
        ui_url = "http://mainsail.lan"

        def __init__(self, values, *, transport=None):  # type: ignore[no-untyped-def]
            self.values = values

    monkeypatch.setitem(registry.TARGETS, "plain", Plain)
    t = registry.build_target("plain", {"url": "http://x"}, key="voron")
    assert isinstance(t, Plain) and t.values == {"url": "http://x"}
    mods = registry.Modules(targets={"voron": t})
    assert mods.ui_links("host") == [("BamBuddy", "http://mainsail.lan")]  # a plain str works


def test_secrets_are_resolved_by_name_and_required() -> None:
    spec = registry.spec_for("bambuddy")
    asked: list[str] = []

    def lookup(name: str) -> str | None:
        asked.append(name)
        return "sekrit" if name == "targets.farm.api_key" else None

    values = registry.resolve_secrets("targets", "farm", spec, {"url": "http://x"}, lookup)
    assert values == {"url": "http://x", "api_key": "sekrit"} and asked == ["targets.farm.api_key"]
    with pytest.raises(AuthError, match=r"targets\.other\.api_key"):
        registry.resolve_secrets("targets", "other", spec, {"url": "http://x"}, lookup)


def test_module_satisfies_both_protocols() -> None:
    m = module(FakeBambuddy())
    assert isinstance(m, Slicer) and isinstance(m, Target)
    with pytest.raises(AuthError, match="No BamBuddy API key"):
        BambuddyModule({"url": "http://bb.test"}, key="bambuddy")


def test_modules_from_a_legacy_config(cfg) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBambuddy()
    asked: list[str] = []
    mods = registry.Modules.from_config(
        cfg, secrets=lambda n: asked.append(n) or "k", transport=httpx.MockTransport(fake)
    )
    assert asked == ["targets.bambuddy.api_key"]
    assert mods.slicers["bambuddy"] is mods.targets["bambuddy"]  # role "both": one instance
    printers = mods.printers()
    assert [p.key for p in printers] == [
        "bambuddy/1", "bambuddy/2", "bambuddy/5", "bambuddy/3", "bambuddy/any:A1 Mini",
    ]  # fmt: skip
    assert mods.find(None, printers).name == "A1 Mini"  # default_printer
    assert mods.find("bambuddy/2", printers).name == mods.find("X1C_01", printers).name
    with pytest.raises(BadRequest, match="No active printer"):
        mods.find("Old", printers)
    a1 = mods.find("A1 Mini", printers)
    assert mods.slicer_for(a1) is mods.target_for(a1)
    assert mods.ui_links("localhost:8765") == [("BamBuddy", "http://localhost:8000/queue")]
    with pytest.raises(AuthError):
        registry.Modules.from_config(cfg, secrets=lambda n: None)


# -- BamBuddy as a target ---------------------------------------------------------


def test_printers_merge_model_defaults_and_overrides() -> None:
    fake = FakeBambuddy()
    override = PrinterInfo(
        key="X1C_01", name="X1C_01", technology="fdm", model="", target="farm",
        slicer="studio", profiles=Profiles(filament="Bambu ASA @BBL X1C"), extra={"x": 1},
    )  # fmt: skip
    got = {p.name: p for p in module(fake, public_url="https://bb.example").printers((override,))}
    a1 = got["A1 Mini"]
    assert (a1.key, a1.target, a1.slicer, a1.bed_mm) == ("farm/1", "farm", "farm", (180, 180))
    assert a1.profiles == A1 and a1.extra["bed_type"] == "Textured PEI Plate"
    assert a1.ui_url == "https://bb.example/queue"
    x1c = got["X1C_01"]
    assert x1c.slicer == "studio" and x1c.profiles.filament == "Bambu ASA @BBL X1C"
    assert x1c.extra == {"printer_id": 2, "x": 1}
    assert got["H2D_01"].nozzle_count == 2 and not got["Old"].active
    assert all(r.method == "GET" for r in fake.requests)


def test_status_lists_loaded_materials_with_their_profiles() -> None:
    m = module(FakeBambuddy())
    h2d = next(p for p in m.printers(()) if p.name == "H2D_01")
    st = m.status(h2d)
    assert (st.state, st.connected, st.ready) == ("IDLE", True, True)
    by_id = {x.id: x for x in st.materials}
    assert set(by_id) == {"0", "4", "254", "255"}
    assert by_id["0"].extruder == 1 and by_id["4"].extruder == 0  # left / right nozzle
    assert by_id["4"].profile == "Generic PLA @BBL H2D" and by_id["4"].colour == "#FF0000"
    assert by_id["254"].label == "External left: PLA · black (left nozzle)"


def test_status_detail_when_the_plate_is_not_cleared() -> None:
    fake = FakeBambuddy(status="RUNNING")
    m = module(fake)
    a1 = m.printers(())[0]
    real = fake.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        r = real(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={**r.json(), "awaiting_plate_clear": True})
        return r

    m._transport = httpx.MockTransport(handler)
    st = m.status(a1)
    assert st.describe() == "RUNNING, waiting for the plate to be cleared" and not st.ready


def test_status_offline_when_bambuddy_is_unreachable_but_auth_raises() -> None:
    m = module(FakeBambuddy())
    a1 = m.printers(())[0]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    m._transport = httpx.MockTransport(down)
    st = m.status(a1)
    assert (st.state, st.connected, st.ready) == ("offline", False, False)
    assert "Can't reach BamBuddy" in st.detail
    m._transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "no"}))
    with pytest.raises(ModuleAuthError, match="refused the API key") as e:
        m.status(a1)
    assert isinstance(e.value, AuthError)  # older callers still catch it as an AuthError


def job_for(m: BambuddyModule, printer: str, parts: list[PartGeometry], **kw: object) -> SliceInput:
    info = next(p for p in m.printers(()) if p.name == printer)
    return SliceInput(
        job_name="Part 1_default_20261001-120000",
        printer=info,
        parts=tuple(parts),
        settings=kw.pop("settings", PrintSettings()),  # type: ignore[arg-type]
        profiles=info.profiles,
        extra={"document": "doc"},
        **kw,  # type: ignore[arg-type]
    )


def test_slice_one_stl_then_submit_without_a_second_upload() -> None:
    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=b"sliced")
    m = module(fake)
    job = job_for(m, "A1 Mini", [PartGeometry("Part 1", make_stl())], bed_type="Cool Plate")
    steps: list[str] = []
    out = m.slice(job, steps.append)
    assert (out.data, out.media, out.print_time_s, out.material_g) == (
        b"sliced", "gcode.3mf", 709, 2.92,
    )  # fmt: skip
    assert out.filename == "Part 1_default_20261001-120000.gcode.3mf"
    assert out.report["library_file_id"] == 31
    assert [f["name"] for f in fake.folders] == ["Onshape", "doc"]
    body = fake.slice_bodies[0]
    assert body["printer_preset"] == {"source": "standard", "id": A1.printer}
    assert body["bed_type"] == "Cool Plate" and body["auto_arrange"] is True
    assert steps[:2] == ["Uploading to BamBuddy", "Slicing"]
    sub = m.submit(job.printer, out, start=False)
    assert sub.state == "waiting" and sub.id == "99"
    assert fake.queued == [{"library_file_id": 31, "printer_id": 1, "manual_start": True}]
    assert len(fake.uploads) == 1  # queued from the library, not uploaded again


def test_submit_uploads_a_file_sliced_elsewhere_and_maps_trays() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    x1c = next(p for p in m.printers(()) if p.name == "X1C_01")
    out = SliceOutput(b"PK..", "x.gcode.3mf", "gcode.3mf", report={"module": "slicerapi"})
    trays = (Material("1", "white"), Material("0", "yellow"))
    sub = m.submit(x1c, out, start=True, materials=trays)
    assert sub.state == "started" and len(fake.uploads) == 1
    assert fake.queued[0] == {
        "library_file_id": 30, "printer_id": 2, "manual_start": False,
        "ams_mapping": [1, 0], "use_ams": True,
    }  # fmt: skip
    m.submit(x1c, out, start=False, materials=(Material("254", "ext"),))
    assert fake.queued[1]["use_ams"] is False and "ams_mapping" not in fake.queued[1]


def test_dual_nozzle_slot_slices_a_project_and_checks_the_nozzle() -> None:
    right = Material("4", "AMS 2", "PLA", "#FF0000", extruder=0, profile="Generic PLA @BBL H2D")
    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf(extruder_id=2))
    m = module(fake)
    job = job_for(m, "H2D_01", [PartGeometry("Part 1", make_stl(), right)])
    m.slice(job, lambda s: None)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert 'filament_maps" value="2"' in ms
    assert fake.slice_bodies[0]["filament_presets"] == [
        {"source": "standard", "id": "Generic PLA @BBL H2D"}
    ]
    fake.sliced_3mf = dual_nozzle_3mf()  # now it lands on the left nozzle
    with pytest.raises(BambuddyError, match="prints on the left nozzle"):
        m.slice(job, lambda s: None)


def test_slice_refuses_media_it_cannot_make() -> None:
    m = module(FakeBambuddy())
    job = job_for(m, "A1 Mini", [PartGeometry("Part 1", make_stl())], media="bgcode")
    with pytest.raises(ModuleError, match="make bgcode"):
        m.slice(job, lambda s: None)


def test_check_reports_printers_and_unconfigured_models() -> None:
    h = module(FakeBambuddy()).check()
    assert h.ok and "4 printers" in h.summary and "auth off" in h.summary
    assert "X1C" in h.detail and "P1S" not in h.detail  # inactive printers don't count
    down = BambuddyModule(
        {"url": "http://bb.test", "api_key": "k"},
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert not down.check().ok


# -- Bambu project layout -----------------------------------------------------------


def test_project_layout_centres_and_numbers_filaments() -> None:
    m = module(FakeBambuddy())
    white = Material("1", "white", "PLA", "#FFFFFF", profile="Bambu PLA Basic @BBL X1C")
    base, text = orient_parts([cube(), cube(4)], ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    job = job_for(
        m, "A1 Mini", [PartGeometry("Base", base, white), PartGeometry("Text", text, white)]
    )
    p = bambu_project.layout(job)
    assert distinct_materials(job.parts) == (white,) and p.filaments == (white,)
    assert p.filament_profiles == ("Bambu PLA Basic @BBL X1C",)
    assert p.footprint == (85.0, 85.0, 95.0, 95.0)  # the 10 mm cube centred on 180 x 180
    assert p.tower_spots(job.printer) == [{}]  # one filament: no tower
    with pytest.raises(ValueError, match="isn't clear"):
        bambu_project.build_project(job, tower=(80.0, 80.0))
    assert bambu_project.build_project(job, tower=(10.0, 10.0))[:2] == b"PK"


def test_bed_from_printer_info_wins() -> None:
    info = PrinterInfo("v", "Voron", "fdm", "A1 Mini", "t", "s", bed_mm=(350, 350))
    assert bambu_project.bed_of(info) == (350, 350)
    assert bambu_project.bed_of(PrinterInfo("v", "V", "fdm", "Unknown", "t", "s")) == (256, 256)


def test_copies_that_do_not_fit_are_refused() -> None:
    with pytest.raises(BadRequest, match="don't fit"):
        bambu_project.copy_offsets((100, 100), 2, (180, 180))


# -- the registered kinds ------------------------------------------------------------


def test_every_module_kind_is_registered() -> None:
    from os2slice.modules.moonraker import Moonraker
    from os2slice.modules.prusalink import PrusaLink
    from os2slice.modules.slicerapi import BambuStudioApi, OrcaSlicerApi

    assert set(registry.kinds()) == {
        "bambuddy", "bambu-studio-api", "orca-slicer-api", "moonraker", "prusalink",
        "preform-server", "desktop",
    }  # fmt: skip
    assert registry.SLICERS["bambu-studio-api"] is BambuStudioApi
    assert registry.SLICERS["orca-slicer-api"] is OrcaSlicerApi
    assert registry.TARGETS["moonraker"] is Moonraker and registry.TARGETS["prusalink"] is PrusaLink
    for kind, cls in {**registry.SLICERS, **registry.TARGETS}.items():
        assert cls.spec.kind == kind


def test_registry_builds_through_from_values() -> None:
    from os2slice.modules.slicerapi import BambuStudioApi

    s = registry.build_slicer(
        "bambu-studio-api", {"url": "http://studio.test:3001", "timeout_s": 60, "api_key": None},
        key="studio",
    )  # fmt: skip
    assert isinstance(s, BambuStudioApi) and isinstance(s, Slicer)
    assert (s.timeout_s, s.key) == (60, "studio")
    t = registry.build_target("moonraker", {"url": "http://voron.test:7125"}, key="voron")
    assert isinstance(t, Target) and t.key == "voron"
    with registry.Modules(slicers={"studio": s}, targets={"voron": t}):
        pass
    assert s._client.is_closed and t._client.is_closed  # Modules.close() closed both


class _StubTarget:
    spec = registry.spec_for("moonraker")

    def __init__(self, printers: tuple[PrinterInfo, ...] | ModuleError) -> None:
        self._printers = printers

    def printers(self, configured: tuple[PrinterInfo, ...]) -> tuple[PrinterInfo, ...]:
        if isinstance(self._printers, ModuleError):
            raise self._printers
        return self._printers


def test_printers_leave_out_a_target_that_cannot_list_them() -> None:
    ok = PrinterInfo(key="v", name="v", technology="fdm", model="m", target="a", slicer="s")
    down = ModuleError("PreForm Server at http://preform-server:44388 is unreachable")
    mods = registry.Modules(targets={"a": _StubTarget((ok,)), "b": _StubTarget(down)})
    assert mods.printers() == [ok]
    with pytest.raises(ModuleError, match="unreachable"):
        registry.Modules(targets={"b": _StubTarget(down)}).printers()
