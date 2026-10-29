from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from os2slice import config
from os2slice.errors import ConfigError
from os2slice.modules import registry
from os2slice.modules.base import (
    Material,
    ModelDefaults,
    ModuleError,
    PartGeometry,
    PrinterInfo,
    Profiles,
    SliceInput,
    SliceOutput,
    Slicer,
    Target,
)
from os2slice.modules.preform import (
    PROGRESS_EVERY,
    SPEC,
    PreformServer,
    layer_label,
    parse_layer,
)
from os2slice.settings import PrintSettings
from tests.conftest import make_stl
from tests.fakes_preform import FORM4, FORM4_IP, FORM_MAGIC, PREFORM_URL, FakePreform

PRINTER = PrinterInfo(
    key=f"form/{FORM4}",
    name=FORM4,
    technology="sla",
    model="Form 4",
    target="form",
    slicer="form",
    profiles=Profiles("FORM-4-0", "0.1 mm", "FLGPGR05"),
    bed_mm=(200.0, 125.0),
    extra={"ip": FORM4_IP, "machine_type": "FORM-4-0", "build_height_mm": 210.0},
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def make(fake: FakePreform, tmp_path: Path, clock: Clock | None = None, **values: Any):
    clock = clock or Clock()
    values = {
        "url": PREFORM_URL,
        "share_dir": str(tmp_path / "share"),
        "printers": [FORM4_IP],
        **values,
    }
    return PreformServer(
        values, key="form", transport=fake.transport(), sleep=clock.sleep, clock=clock
    )


def job(**kw: Any) -> SliceInput:
    base: dict[str, Any] = {
        "job_name": "Bracket",
        "printer": PRINTER,
        "parts": (PartGeometry("Bracket", make_stl(12)),),
        "settings": PrintSettings(),
        "profiles": PRINTER.profiles,
    }
    return SliceInput(**{**base, **kw})


def leftovers(tmp_path: Path) -> list[Path]:
    share = tmp_path / "share"
    return list(share.iterdir()) if share.exists() else []


# -- spec, registry, config -----------------------------------------------------------


def test_spec_and_registry() -> None:
    assert SPEC.kind == "preform-server" and SPEC.role == "both" and SPEC.technology == "sla"
    assert SPEC.makes == ("form",) and SPEC.accepts == ("form",)
    assert SPEC.discovers_printers and SPEC.pairs_only_with == ("preform-server",)
    assert {f.key for f in SPEC.fields} == {
        "url", "share_dir", "server_share_dir", "printers", "version", "remote_start",
        "raft_type",
    }  # fmt: skip
    assert registry.SLICERS["preform-server"] is registry.TARGETS["preform-server"]
    t = registry.build_target("preform-server", {"url": PREFORM_URL}, key="form")
    assert isinstance(t, Target) and isinstance(t, Slicer) and t.key == "form"
    t.close()


def test_config_parses_and_pairs_with_itself(tmp_path: Path) -> None:
    form = {
        "kind": "preform-server",
        "url": "http://local-preform-server:44388",
        "printers": [FORM4_IP],
        "models": {"Form 4": {"profiles": {"filament": "FLGPGR05", "process": "0.05 mm"}}},
    }
    cfg = config.parse(
        {"targets": {"form": form}, "printers": {"p": {"target": "form", "model": "Form 4"}}},
        tmp_path / "c.toml",
    )
    t = cfg.targets["form"]
    assert t.values["share_dir"] == "/share/os2slice/preform"
    assert t.values["remote_start"] is False
    assert t.models["Form 4"].profiles == Profiles("", "0.05 mm", "FLGPGR05")
    mods = registry.Modules.from_config(cfg, secrets=lambda name: None)
    assert mods.slicers["form"] is mods.targets["form"]
    mods.close()
    bambuddy = {"kind": "bambuddy", "url": "http://bb:8000"}
    with pytest.raises(ConfigError, match="PreForm Server"):
        config.parse(
            {
                "targets": {"form": form, "farm": bambuddy},
                "printers": {"p": {"target": "form", "slicer": "farm"}},
            },
            tmp_path / "c.toml",
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"url": "ftp://preform"}, "http"),
        ({"url": "http://user:pw@preform:44388"}, "credentials"),
        ({"url": PREFORM_URL, "share_dir": "relative/dir"}, "absolute"),
        ({"url": PREFORM_URL, "server_share_dir": "srv"}, "absolute"),
        ({"url": PREFORM_URL, "printers": ["192.168.1.1; rm"]}, "serial name"),
        ({"url": PREFORM_URL, "raft_type": "BIG_RAFT"}, "raft_type"),
    ],
)
def test_bad_values_refused(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        PreformServer(values)


def test_layer_labels() -> None:
    assert layer_label(0.1) == "0.1 mm" and layer_label(0.025) == "0.025 mm"
    assert parse_layer("0.1 mm") == 0.1 and parse_layer("0.05 mm") == 0.05
    for bad in ("0.1", "0.1mm", "0.1 mm; x", "abc", "0 mm", ""):
        with pytest.raises(ModuleError):
            parse_layer(bad)


# -- printers ---------------------------------------------------------------------------


def test_printers_discovers_by_ip_and_fills_profiles(tmp_path: Path) -> None:
    fake = FakePreform()
    pf = make(fake, tmp_path)
    (p,) = pf.printers(())
    assert p.key == f"form/{FORM4}" and p.name == FORM4 and p.active
    assert (p.technology, p.model, p.target, p.slicer) == ("sla", "Form 4", "form", "form")
    assert p.bed_mm == (200.0, 125.0) and p.ui_url == ""
    assert p.extra == {"ip": FORM4_IP, "machine_type": "FORM-4-0", "build_height_mm": 210.0}
    # the tank's resin (device/list after discover has the full record), 0.1 mm
    assert p.profiles == Profiles("FORM-4-0", "0.1 mm", "FLRGWH01")
    assert fake.bodies["/v1/device/discover/"] == [{"timeout_seconds": 3, "printer": FORM4_IP}]
    # Listed now: no second lookup, and the catalog is cached.
    pf.printers(())
    assert fake.paths().count("POST /v1/device/discover/") == 1
    assert fake.paths().count("POST /v1/print-setting/list/") == 1


def test_printers_back_off_after_a_failed_lookup(tmp_path: Path) -> None:
    fake = FakePreform(printer=None)
    clock = Clock()
    pf = make(fake, tmp_path, clock)
    (p,) = pf.printers(())
    assert (p.key, p.name, p.active, p.technology) == (f"form/{FORM4_IP}", FORM4_IP, False, "sla")
    pf.printers(())
    assert fake.paths().count("POST /v1/device/discover/") == 1
    clock.now += 61
    pf.printers(())
    assert fake.paths().count("POST /v1/device/discover/") == 2


def test_printers_without_addresses_never_discover(tmp_path: Path) -> None:
    fake = FakePreform(discovered=True)
    pf = make(fake, tmp_path, printers=[])
    assert [p.name for p in pf.printers(())] == [FORM4]
    assert "POST /v1/device/discover/" not in fake.paths()


@pytest.mark.parametrize(
    ("defaults", "tank", "expected"),
    [
        (Profiles(filament="FLGPGR05"), "FLRGWH01", Profiles("FORM-4-0", "0.1 mm", "FLGPGR05")),
        (Profiles(filament="FLRG1011"), "", Profiles("FORM-4-0", "0.05 mm", "FLRG1011")),
        (Profiles(process="0.16 mm"), "FLGPBK05", Profiles("FORM-4-0", "0.16 mm", "FLGPBK05")),
        # no resin reported: no filament, the type's first material's layer
        (Profiles(), "", Profiles("FORM-4-0", "0.1 mm", "")),
    ],
)
def test_printers_model_defaults(
    tmp_path: Path, defaults: Profiles, tank: str, expected: Profiles
) -> None:
    from tests.fakes_preform import form4_record

    fake = FakePreform(printer=form4_record(tank_material_code=tank), discovered=True)
    models = {"Form 4": ModelDefaults(profiles=defaults, extra={"note": "x"})}
    (p,) = make(fake, tmp_path, models=models).printers(())
    assert p.profiles == expected
    assert p.extra["note"] == "x"


def test_printers_overrides_by_name(tmp_path: Path) -> None:
    fake = FakePreform(discovered=True)
    o = PrinterInfo(
        key=FORM4, name=FORM4, technology="sla", model="", target="", slicer="",
        profiles=Profiles(process="0.05 mm"), extra={"ip": "10.0.0.9", "colour": "grey"},
    )  # fmt: skip
    (p,) = make(fake, tmp_path).printers((o,))
    assert p.profiles == Profiles("FORM-4-0", "0.05 mm", "FLRGWH01")
    assert p.extra["ip"] == FORM4_IP and p.extra["colour"] == "grey"


def test_printers_raise_when_the_server_is_down(tmp_path: Path) -> None:
    with pytest.raises(ModuleError, match="Can't reach PreForm Server"):
        make(FakePreform(down=True), tmp_path).printers(())


# -- status -----------------------------------------------------------------------------


def test_status_loaded_resin_first(tmp_path: Path) -> None:
    fake = FakePreform(discovered=True)
    s = make(fake, tmp_path).status(PRINTER)
    assert (s.state, s.connected, s.ready) == ("Not ready", True, True)
    assert s.detail == "Rigid 4000 V1 in the tank"
    assert [(m.id, m.label) for m in s.materials] == [
        ("FLRGWH01", "Rigid 4000 V1 (loaded)"),
        ("FLGPBK05", "Black V5"),
        ("FLGPGR05", "Grey V5"),
        ("FLRG1011", "Rigid 10K V1.1"),
        ("FLTO2002", "Tough 2000 V2"),
    ]
    assert s.materials[0].raw == {"loaded": True}
    assert all(m.profile == m.id == m.kind for m in s.materials)


def test_status_time_left(tmp_path: Path) -> None:
    from tests.fakes_preform import form4_record

    fake = FakePreform(
        printer=form4_record(status="Printing", estimated_print_time_remaining_ms=4_320_000),
        discovered=True,
    )
    s = make(fake, tmp_path).status(PRINTER)
    assert s.state == "Printing" and s.detail == "1 h 12 min left, Rigid 4000 V1 in the tank"


def test_status_rediscovers_after_a_server_restart(tmp_path: Path) -> None:
    fake = FakePreform()  # not discovered: the server forgot it
    s = make(fake, tmp_path).status(PRINTER)
    assert s.connected and fake.bodies["/v1/device/discover/"][0]["printer"] == FORM4_IP


@pytest.mark.parametrize(
    ("fake", "why"),
    [
        (FakePreform(down=True), "Can't reach PreForm Server"),
        (FakePreform(printer=None), f"doesn't see {FORM4}"),
    ],
)
def test_status_offline_convention(tmp_path: Path, fake: FakePreform, why: str) -> None:
    s = make(fake, tmp_path).status(PRINTER)
    assert (s.state, s.connected, s.ready) == ("offline", False, False)
    assert why in s.detail


def test_status_of_a_printer_never_found(tmp_path: Path) -> None:
    from dataclasses import replace

    fake = FakePreform()
    s = make(fake, tmp_path).status(replace(PRINTER, active=False))
    assert s.state == "offline" and fake.calls == []


# -- profiles ---------------------------------------------------------------------------


def test_own_profiles_only_layers(tmp_path: Path) -> None:
    own = make(FakePreform(), tmp_path).own_profiles()
    assert own.process == ("0.025 mm", "0.05 mm", "0.1 mm", "0.16 mm", "0.2 mm")
    assert own.printer == () and own.filament == ()
    with pytest.raises(ModuleError):
        make(FakePreform(down=True), tmp_path).own_profiles()


def test_profiles_narrowed_to_a_model(tmp_path: Path) -> None:
    pf = make(FakePreform(), tmp_path)
    every = pf.profiles()
    assert every.printer == ("FORM-4-0", "FRML-4-0")
    form4 = pf.profiles("Form 4")
    assert form4.printer == ("FORM-4-0",)
    assert form4.process == ("0.025 mm", "0.05 mm", "0.1 mm", "0.16 mm")
    assert form4.filament == ("FLGPBK05", "FLGPGR05", "FLRG1011", "FLRGWH01", "FLTO2002")
    assert pf.profiles("FRML-4-0").filament == ("FLGPGR05",)


# -- slice ------------------------------------------------------------------------------


def test_slice_plain_part(tmp_path: Path) -> None:
    fake = FakePreform()
    lines: list[str] = []
    out = make(fake, tmp_path).slice(job(), lines.append)
    assert fake.paths() == [
        "GET /v1/",
        "POST /v1/print-setting/list/",
        "POST /v1/scene/create/",
        "POST /v1/scene/model/import/?async=true",
        "POST /v1/scene/model/orient/",
        "POST /v1/scene/model/auto-layout/?async=true",
        "POST /v1/scene/model/validate/in-bounds/",
        "POST /v1/scene/model/validate/manifoldness/?async=true",
        "POST /v1/scene/calculate-print-time/?async=true",
        "POST /v1/scene/get/",
        "POST /v1/scene/save/?async=true",
        "DELETE /v1/scene/delete/",
    ]
    assert fake.bodies["/v1/scene/create/"] == [
        {"machine_type": "FORM-4-0", "material_code": "FLGPGR05", "layer_thickness_mm": 0.1,
         "print_setting": "DEFAULT"}
    ]  # fmt: skip
    imp = fake.bodies["/v1/scene/model/import/"][0]
    assert imp["units"] == "MILLIMETERS" and imp["repair_behavior"] == "REPAIR"
    assert imp["name"] == "Bracket" and imp["file"].startswith(str(tmp_path / "share"))
    assert imp["file"].endswith("/model.stl")
    orient = fake.bodies["/v1/scene/model/orient/"][0]
    assert orient["orientation"] == {"linear": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
    save = fake.bodies["/v1/scene/save/"][0]
    assert save["file"].endswith("/Bracket.form") and len(save["scene_ids"]) == 1
    assert out.data.startswith(FORM_MAGIC)
    assert (out.filename, out.media, out.print_time_s, out.layers) == (
        "Bracket.form", "form", 2991, 363,
    )  # fmt: skip
    assert out.material_g is None and out.warnings == ()
    assert out.report == {
        "machine_type": "FORM-4-0", "material_code": "FLGPGR05", "material": "Grey V5",
        "layer_thickness_mm": 0.1, "print_setting": "DEFAULT", "volume_ml": 7.37,
        "server_version": "3.64.1.664",
    }  # fmt: skip
    assert fake.scenes == {} and len(fake.deleted) == 1
    assert leftovers(tmp_path) == []
    assert "Importing into PreForm" in lines and "Saving the .form file" in lines
    assert "Generating supports" not in lines


def test_slice_merges_parts_into_one_stl(tmp_path: Path) -> None:
    fake = FakePreform()
    parts = (PartGeometry("a", make_stl(12)), PartGeometry("b", make_stl(4)))
    make(fake, tmp_path).slice(job(parts=parts), lambda s: None)
    (stl,) = fake.imported
    assert struct.unpack_from("<I", stl, 80)[0] == 16 and len(stl) == 84 + 50 * 16
    assert not stl.startswith(b"solid")


def test_slice_refuses_a_broken_stl(tmp_path: Path) -> None:
    fake = FakePreform()
    bad = make_stl(12)[:-10]
    with pytest.raises(ModuleError, match="isn't a binary STL"):
        make(fake, tmp_path).slice(job(parts=(PartGeometry("a", bad),)), lambda s: None)
    assert leftovers(tmp_path) == []


def test_slice_auto_orient_supports_raft_and_copies(tmp_path: Path) -> None:
    fake = FakePreform(cups=0)
    pf = make(fake, tmp_path, raft_type="MINI_RAFT")
    out = pf.slice(
        job(auto_orient=True, copies=3, settings=PrintSettings(supports="tree")), lambda s: None
    )
    paths = fake.paths()
    assert "POST /v1/scene/model/orient/" not in paths
    order = [
        "POST /v1/scene/model/auto-orient/?async=true",
        "POST /v1/scene/model/auto-support/?async=true",
        "POST /v1/scene/model/duplicate/",
        "POST /v1/scene/model/auto-layout/?async=true",
        "POST /v1/scene/model/validate/in-bounds/",
        "POST /v1/scene/model/validate/manifoldness/?async=true",
        "POST /v1/scene/model/validate/cups/?async=true",
        "POST /v1/scene/model/validate/minima/?async=true",
        "POST /v1/scene/model/validate/supportedness/?async=true",
    ]
    assert [p for p in paths if p in order] == order
    assert fake.bodies["/v1/scene/model/auto-support/"][0]["raft_type"] == "MINI_RAFT"
    dup = fake.bodies["/v1/scene/model/duplicate/"][0]
    assert dup["count"] == 2 and len(dup["model_ids"]) == 1
    assert out.print_time_s == 300 + 3 * 2691


def test_slice_supports_without_raft_type(tmp_path: Path) -> None:
    fake = FakePreform()
    make(fake, tmp_path).slice(job(settings=PrintSettings(supports="normal")), lambda s: None)
    assert "raft_type" not in fake.bodies["/v1/scene/model/auto-support/"][0]


def test_slice_copies_that_dont_fit(tmp_path: Path) -> None:
    fake = FakePreform(fits=2)
    with pytest.raises(ModuleError, match="3 copies don't fit on the Form 4's platform"):
        make(fake, tmp_path).slice(job(copies=3), lambda s: None)
    assert fake.scenes == {} and leftovers(tmp_path) == []


def test_slice_unsupported_layer_lists_the_layers(tmp_path: Path) -> None:
    fake = FakePreform()
    with pytest.raises(ModuleError) as e:
        make(fake, tmp_path).slice(
            job(profiles=Profiles("FORM-4-0", "0.2 mm", "FLGPGR05")), lambda s: None
        )
    assert e.value.message == "Grey V5 doesn't print at 0.2 mm layers on the Form 4"
    assert e.value.fix == "Pick one of: 0.025 mm, 0.05 mm, 0.1 mm, 0.16 mm"
    assert "POST /v1/scene/create/" not in fake.paths()


def test_slice_unknown_material(tmp_path: Path) -> None:
    with pytest.raises(ModuleError, match="FLXXXX01 isn't a Form 4 material"):
        make(FakePreform(), tmp_path).slice(
            job(profiles=Profiles("FORM-4-0", "0.1 mm", "FLXXXX01")), lambda s: None
        )


def test_slice_uses_the_labelled_default_setting(tmp_path: Path) -> None:
    fake = FakePreform()
    out = make(fake, tmp_path).slice(
        job(profiles=Profiles("FORM-4-0", "0.1 mm", "FLTO2002")), lambda s: None
    )
    assert fake.bodies["/v1/scene/create/"][0]["print_setting"] == "SUPPORTSV1"
    assert out.report["print_setting"] == "SUPPORTSV1"


def test_slice_takes_the_parts_material_and_machine_from_the_printer(tmp_path: Path) -> None:
    fake = FakePreform()
    black = Material("FLGPBK05", "Black V5", "FLGPBK05", profile="FLGPBK05")
    make(fake, tmp_path).slice(
        job(
            parts=(PartGeometry("a", make_stl(), black), PartGeometry("b", make_stl(), black)),
            profiles=Profiles("", "0.1 mm", "FLGPGR05"),
        ),
        lambda s: None,
    )
    body = fake.bodies["/v1/scene/create/"][0]
    assert (body["machine_type"], body["material_code"]) == ("FORM-4-0", "FLGPBK05")


def test_slice_refuses_several_resins(tmp_path: Path) -> None:
    fake = FakePreform()
    grey = Material("FLGPGR05", "Grey V5", "FLGPGR05", profile="FLGPGR05")
    black = Material("FLGPBK05", "Black V5", "FLGPBK05", profile="FLGPBK05")
    parts = (PartGeometry("a", make_stl(), grey), PartGeometry("b", make_stl(), black))
    with pytest.raises(ModuleError, match="A resin print is one material"):
        make(fake, tmp_path).slice(job(parts=parts), lambda s: None)
    assert fake.calls == []


def test_slice_warnings_from_checks(tmp_path: Path) -> None:
    fake = FakePreform(in_bounds=False, manifold=False, cups=2, minima=1, unsupported=3.5)
    out = make(fake, tmp_path).slice(job(settings=PrintSettings(supports="tree")), lambda s: None)
    assert out.warnings == (
        "Part of the print is outside the Form 4's build volume.",
        "PreForm couldn't repair the mesh into a closed solid.",
        "2 cup(s) that can trap resin; consider drain holes.",
        "1 unsupported minima: those spots may not print.",
        "3.5 % of the surface is unsupported.",
    )


def test_slice_cleans_up_when_the_server_cant_see_the_file(tmp_path: Path) -> None:
    fake = FakePreform()
    pf = make(fake, tmp_path, server_share_dir="/srv/preform")
    with pytest.raises(ModuleError, match="Model file not found") as e:
        pf.slice(job(), lambda s: None)
    assert "/srv/preform" in e.value.fix
    assert fake.bodies["/v1/scene/model/import/"][0]["file"].startswith("/srv/preform/")
    assert fake.scenes == {} and len(fake.deleted) == 1
    assert leftovers(tmp_path) == []


def test_slice_says_it_is_still_working(tmp_path: Path) -> None:
    fake = FakePreform(polls=int(PROGRESS_EVERY) + 5)
    lines: list[str] = []
    make(fake, tmp_path).slice(job(), lines.append)
    assert any("still working" in line for line in lines)


def test_error_code_picks_the_fix(tmp_path: Path) -> None:
    fake = FakePreform(
        fail={"/v1/scene/create/": (409, "BUILD_VOLUME_LIMIT_EXCEEDED", "The limit")}
    )
    with pytest.raises(ModuleError) as e:
        make(fake, tmp_path).slice(job(), lambda s: None)
    assert e.value.message == "PreForm Server: The limit"
    assert "restart its container" in e.value.fix


def test_redirects_are_not_followed(tmp_path: Path) -> None:
    def redirect(req: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "http://elsewhere.test/v1/"})

    pf = PreformServer({"url": PREFORM_URL}, transport=httpx.MockTransport(redirect))
    with pytest.raises(ModuleError, match="redirected"):
        pf.server_version()


# -- submit -----------------------------------------------------------------------------


def sliced(fake: FakePreform, tmp_path: Path, **values: Any) -> tuple[PreformServer, SliceOutput]:
    pf = make(fake, tmp_path, **values)
    return pf, pf.slice(job(), lambda s: None)


@pytest.mark.parametrize(
    ("start", "remote_start", "print_now"),
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
)
def test_submit_print_now(tmp_path: Path, start: bool, remote_start: bool, print_now: bool) -> None:
    fake = FakePreform()
    pf, out = sliced(fake, tmp_path, remote_start=remote_start)
    sub = pf.submit(PRINTER, out, start=start)
    (upload,) = fake.uploads
    assert upload["print_now"] is print_now
    assert set(fake.bodies["/v1/scene/print/"][0]) == {
        "scene_id", "printer", "job_name", "print_now",
    }  # fmt: skip
    assert upload["printer"] == FORM4_IP and upload["job_name"] == "Bracket"
    assert sub.id == upload["job_id"] and sub.url == ""
    assert sub.state == ("started" if print_now else "waiting")
    if not print_now:
        assert "waits in the Form 4's queue; start it on the printer's touchscreen" in sub.detail
    assert ("remote_start is on" in sub.detail) is (start and not remote_start)


def test_submit_order_and_cleanup(tmp_path: Path) -> None:
    fake = FakePreform()
    pf, out = sliced(fake, tmp_path)
    fake.calls.clear()
    pf.submit(PRINTER, out, start=False)
    assert fake.paths() == [
        "POST /v1/scene/open/?async=true",
        "GET /v1/device/list/",
        "POST /v1/device/discover/",
        "GET /v1/device/list/",
        "POST /v1/scene/print/?async=true",
        "DELETE /v1/scene/delete/",
    ]
    assert fake.bodies["/v1/scene/open/"][0]["file"].endswith("/job.form")
    assert fake.scenes == {} and leftovers(tmp_path) == []


def test_submit_notes_another_resin_in_the_tank(tmp_path: Path) -> None:
    pf, out = sliced(FakePreform(), tmp_path)  # Grey V5; the tank holds Rigid 4000
    sub = pf.submit(PRINTER, out, start=False)
    assert "its tank holds FLRGWH01, this job needs FLGPGR05" in sub.detail


def test_submit_refuses_other_media(tmp_path: Path) -> None:
    fake = FakePreform()
    gcode = SliceOutput(b"G28\n", "a.gcode", "gcode")
    with pytest.raises(ModuleError, match=r"takes \.form jobs"):
        make(fake, tmp_path).submit(PRINTER, gcode, start=False)
    assert fake.calls == []


def test_submit_printer_not_found(tmp_path: Path) -> None:
    fake = FakePreform()
    pf, out = sliced(fake, tmp_path)
    fake.printer = None
    with pytest.raises(ModuleError, match=f"can't find {FORM4} at {FORM4_IP}"):
        pf.submit(PRINTER, out, start=True)
    assert fake.uploads == [] and fake.scenes == {} and leftovers(tmp_path) == []


def test_print_without_print_now_is_refused(tmp_path: Path) -> None:
    pf = make(FakePreform(), tmp_path)
    with pytest.raises(ModuleError, match="explicit print_now"):
        pf._call("/v1/scene/print/", {"scene_id": "s", "printer": FORM4, "job_name": "j"})


# -- check ------------------------------------------------------------------------------


def test_check_ok(tmp_path: Path) -> None:
    fake = FakePreform(discovered=True)
    h = make(fake, tmp_path, version="3.64.1.664").check()
    assert h.ok and h.version == "3.64.1.664" and h.detail == ""
    assert h.summary == "PreForm Server 3.64.1.664, 1 printer(s)"
    assert (tmp_path / "share").is_dir()


def test_check_warnings(tmp_path: Path) -> None:
    h = make(FakePreform(), tmp_path, version="3.63.0.650").check()
    assert h.ok and h.summary == "PreForm Server 3.64.1.664, 0 printer(s)"
    assert "[targets.form] pins 3.63.0.650" in h.detail
    assert f"hasn't found printer(s) {FORM4_IP}" in h.detail


def test_check_unreachable(tmp_path: Path) -> None:
    h = make(FakePreform(down=True), tmp_path).check()
    assert not h.ok and "Can't reach PreForm Server at http://preform.test:44388" in h.summary
