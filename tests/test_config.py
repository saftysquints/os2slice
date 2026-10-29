from __future__ import annotations

from pathlib import Path

import pytest

from os2slice import config
from os2slice.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def test_first_run_writes_the_default(tmp_path: Path) -> None:
    cfg = config.load()
    assert cfg.path == tmp_path / "config" / "os2slice" / "config.toml"
    assert cfg.path.read_text() == config.default_config_text()
    assert cfg.onshape_base_url == "https://cad.onshape.com"
    assert cfg.port == 8765
    assert cfg.export_dir == Path.home() / "OnshapeExports"
    assert set(cfg.slicers) == {"orca", "bambu", "prusa"}


def test_packaged_default_matches_the_example() -> None:
    assert (ROOT / "os2slice.example.toml").read_text() == config.default_config_text()


def test_load_without_create(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No config file"):
        config.load(tmp_path / "missing.toml", create=False)


def test_bad_toml(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("[export\n")
    with pytest.raises(ConfigError, match="Can't read"):
        config.load(p)


def _parse(data: dict) -> config.Config:
    return config.parse(data, Path("/x/config.toml"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"onshape": {"base_url": "https://evil.example"}}, "base_url"),
        ({"onshape": {"base_url": "http://cad.onshape.com"}}, "base_url"),
        ({"onshape": {"base_url": "https://cad.onshape.com.evil.io"}}, "base_url"),
        ({"server": {"port": 80}}, "server.port"),
        ({"server": {"port": "8765"}}, "server.port"),
        ({"server": {"port": True}}, "server.port"),
        ({"export": {"dir": "relative/dir"}}, "absolute"),
        ({"export": {"format": "obj"}}, "export.format"),
        ({"export": {"units": "furlong"}}, "export.units"),
        ({"export": {"keep_days": -1}}, "keep_days"),
        ({"export": {"dirr": "~/x"}}, "unknown key"),
        ({"exports": {}}, "unknown table"),
        ({"slicers": {"Orca": {"argv": ["x"]}}}, "names may only"),
        ({"slicers": {"orca": {"argv": []}}}, "non-empty list"),
        ({"slicers": {"orca": {"argv": "orca-slicer {file}"}}}, "non-empty list"),
        ({"slicers": {"orca": {"argv": ["x", 3]}}}, "non-empty list"),
        ({"slicers": {"orca": "orca-slicer"}}, "must be a table"),
        (
            {
                "bambuddy": {
                    "base_url": "http://bb:8000",
                    "presets": {
                        "X1C": {
                            "printer": "p",
                            "process": "q",
                            "filament": "r",
                            "bed_type": "Glass",
                        }
                    },
                }
            },
            "bed_type",
        ),
    ],
)
def test_invalid_config(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _parse(data)


def test_trailing_slash_and_name_default() -> None:
    cfg = _parse(
        {"onshape": {"base_url": "https://acme.onshape.com/"}, "slicers": {"x": {"argv": ["x"]}}}
    )
    assert cfg.onshape_base_url == "https://acme.onshape.com"
    assert cfg.slicers["x"].name == "x"


# -- modules: [slicers.*] with kind, [targets.*], [printers.*] (docs/MODULES.md) ----------

FARM = {"kind": "bambuddy", "url": "http://bb:8000/"}
A1_PROFILES = {"printer": "p", "process": "q", "filament": "Bambu PLA Basic @BBL A1M"}


def test_targets_and_printers_tables() -> None:
    cfg = _parse(
        {
            "targets": {
                "farm": {
                    **FARM,
                    "folder": "Parts",
                    "models": {
                        "A1 Mini": {
                            "profiles": A1_PROFILES,
                            "bed_type": "Textured PEI Plate",
                            "extra": {"preset_source": "cloud"},
                        }
                    },
                }
            },
            "printers": {"X1C_01": {"profiles": {"filament": "Bambu ASA @BBL X1C"}}},
            "default_printer": "A1 Mini",
        }
    )
    farm = cfg.targets["farm"]
    assert (farm.kind, farm.values) == (
        "bambuddy",
        {"url": "http://bb:8000", "folder": "Parts", "manual_start": False},
    )
    a1 = farm.models["A1 Mini"]
    assert a1.slicer == "" and a1.profiles.filament == "Bambu PLA Basic @BBL A1M"
    assert a1.bed_type == "Textured PEI Plate" and a1.extra == {"preset_source": "cloud"}
    over = cfg.printers["X1C_01"]
    assert over.target is None and over.profiles.filament == "Bambu ASA @BBL X1C"
    assert cfg.default_printer == "A1 Mini" and not hasattr(cfg, "bambuddy")
    assert set(cfg.slicers) == set() and cfg.slicer_modules == {}


def test_deprecated_manual_start_true_still_loads() -> None:
    """`manual_start` has no effect any more (D-33), but old configs keep loading."""
    cfg = _parse({"targets": {"farm": {**FARM, "manual_start": True}}})
    assert cfg.targets["farm"].values["manual_start"] is True
    legacy = _parse({"bambuddy": {"base_url": "http://bb:8000", "manual_start": True}})
    assert legacy.targets["bambuddy"].values["manual_start"] is True


def test_print_default_wait_for_start() -> None:
    assert _parse({}).default_wait_for_start is False
    assert _parse({"print_defaults": {"walls": 3}}).default_wait_for_start is False
    cfg = _parse({"print_defaults": {"wait_for_start": True, "walls": 3}})
    assert cfg.default_wait_for_start is True and cfg.print_defaults.walls == 3
    assert config.load().default_wait_for_start is False  # the shipped default
    for bad in ("yes", 1, "true"):
        with pytest.raises(ConfigError, match="wait_for_start must be true or false"):
            _parse({"print_defaults": {"wait_for_start": bad}})


def test_a_slicer_module_and_a_desktop_slicer_side_by_side() -> None:
    cfg = _parse(
        {
            "slicers": {
                "orca": {"argv": ["orca-slicer", "{file}"]},
                "bb": {"kind": "bambuddy", "url": "http://bb:8000"},
            },
            "targets": {"farm": FARM},
            "printers": {"X1C_01": {"target": "farm", "slicer": "bb"}},
        }
    )
    assert set(cfg.slicers) == {"orca"} and set(cfg.slicer_modules) == {"bb"}
    assert cfg.slicer_modules["bb"].values["url"] == "http://bb:8000"
    assert cfg.printers["X1C_01"].slicer == "bb"


def test_legacy_bambuddy_table_is_read_as_a_target() -> None:
    preset = {**A1_PROFILES, "source": "cloud", "bed_type": "Cool Plate"}
    cfg = _parse(
        {
            "bambuddy": {
                "base_url": "http://bb:8000",
                "public_url": "https://bb.example:8000",
                "default_printer": "A1 Mini",
                "manual_start": False,
                "presets": {"A1 Mini": preset},
            }
        }
    )
    t = cfg.targets["bambuddy"]
    assert t.kind == "bambuddy"
    assert t.values == {
        "url": "http://bb:8000",
        "folder": "Onshape",
        "manual_start": False,
        "public_url": "https://bb.example:8000",
    }
    d = t.models["A1 Mini"]
    assert d.slicer == "" and d.profiles.process == "q" and d.bed_type == "Cool Plate"
    assert d.extra == {"preset_source": "cloud"}
    assert cfg.default_printer == "A1 Mini"


def test_default_printer_may_move_to_the_top_level() -> None:
    cfg = _parse({"bambuddy": {"base_url": "http://bb:8000"}, "default_printer": "X1C_01"})
    assert cfg.default_printer == "X1C_01"


MODELS_X1C = {"X1C": {"bed_type": "Glass"}}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"targets": {"farm": {"url": "http://bb"}}}, "kind must be one of"),
        ({"targets": {"farm": {"kind": "octoprint", "url": "http://x"}}}, "kind must be one of"),
        ({"targets": {"farm": {"kind": "bambuddy"}}}, "url is required"),
        ({"targets": {"farm": {**FARM, "url": "ftp://x"}}}, "must be a URL"),
        ({"targets": {"farm": {**FARM, "manual_start": "yes"}}}, "true or false"),
        ({"targets": {"farm": {**FARM, "api_key": "s3cret"}}}, "is a secret"),
        ({"targets": {"farm": {**FARM, "colour": "red"}}}, "unknown key"),
        ({"targets": {"Farm": FARM}}, "names may only"),
        ({"targets": {"farm": {**FARM, "models": MODELS_X1C}}}, "bed_type"),
        ({"targets": {"farm": {**FARM, "models": {"X1C": {"profile": {}}}}}}, "unknown key"),
        (
            {"targets": {"farm": {**FARM, "models": {"X1C": {"profiles": {"nozzle": "x"}}}}}},
            "unknown key",
        ),
        ({"slicers": {"s": {"kind": "bambuddy", "url": "http://x", "argv": ["x"]}}}, "unknown"),
        (
            {"bambuddy": {"base_url": "http://bb:8000"}, "targets": {"bambuddy": FARM}},
            "can't both be set",
        ),
        (
            {
                "bambuddy": {"base_url": "http://bb:8000", "default_printer": "A"},
                "default_printer": "B",
            },
            "not both",
        ),
        ({"default_printer": 3}, "default_printer"),
        ({"printers": {"X1C_01": {"slicer": "bb"}}}, "needs a target"),
        ({"targets": {"farm": FARM}, "printers": {"a/b": {}}}, "printer names"),
        ({"targets": {"farm": FARM}, "printers": {"p": {"target": "nope"}}}, r"no \[targets.nope"),
        ({"targets": {"farm": FARM}, "printers": {"p": {"bed_mm": [0, 3]}}}, "bed_mm"),
        ({"targets": {"farm": FARM}, "printers": {"p": {"technology": "sls"}}}, "technology"),
        ({"targets": {"farm": FARM}, "printers": {"p": {"materials": "PLA"}}}, "materials"),
        ({"targets": {"farm": FARM}, "printers": {"p": {"colour": 1}}}, "unknown key"),
    ],
)
def test_invalid_module_config(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _parse(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        # A printer may only name a slicer that exists...
        ({"targets": {"farm": FARM}, "printers": {"p": {"slicer": "nope"}}}, r"no \[slicers.nope"),
        # ...that slices on the server, not a desktop hand-off...
        (
            {
                "slicers": {"orca": {"argv": ["orca-slicer"]}},
                "targets": {"farm": FARM},
                "printers": {"p": {"target": "farm", "slicer": "orca"}},
            },
            "desktop slicer",
        ),
        # ...also in a model's defaults.
        (
            {"targets": {"farm": {**FARM, "models": {"X1C": {"slicer": "studio"}}}}},
            r"no \[slicers.studio",
        ),
        # A slicer key may not shadow a target that slices for itself.
        (
            {
                "slicers": {"farm": {"kind": "bambuddy", "url": "http://x"}},
                "targets": {"farm": FARM},
            },
            "clashes",
        ),
    ],
)
def test_pairing_errors(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _parse(data)


def test_pairing_checks_media_technology_and_exclusive_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With stand-in module kinds: what a slicer makes must be what the target takes."""
    from dataclasses import replace

    from os2slice.modules import registry
    from os2slice.modules.base import ModuleSpec

    base = registry.spec_for("bambuddy")

    def kind(spec: ModuleSpec) -> type:
        return type(spec.kind, (), {"spec": spec})

    stand_ins = {
        "gcode-slicer": replace(base, kind="gcode-slicer", role="slicer", makes=("gcode",)),
        "sla-slicer": replace(base, kind="sla-slicer", role="slicer", technology="sla"),
        "preform": replace(base, kind="preform", role="slicer", pairs_only_with=("preform",)),
    }
    for k, spec in stand_ins.items():
        monkeypatch.setitem(registry.SLICERS, k, kind(spec))
    for slicer, message in (
        ("gcode-slicer", "makes gcode, but BamBuddy takes gcode.3mf"),
        ("sla-slicer", "is sla, BamBuddy is fdm"),
        ("preform", "only work with each other"),
    ):
        data = {
            "slicers": {"s": {"kind": slicer, "url": "http://s"}},
            "targets": {"farm": FARM},
            "printers": {"p": {"target": "farm", "slicer": "s"}},
        }
        with pytest.raises(ConfigError, match=message):
            _parse(data)


def test_web_orca_table(tmp_path: Path) -> None:
    cfg = config.parse({"web_orca": {"url": "https://print.lan:3444/"}}, tmp_path / "c")
    assert cfg.web_orca is not None and cfg.web_orca.url == "https://print.lan:3444"
    assert cfg.web_orca.inbox == Path("/share/os2slice/orca-inbox")
    assert cfg.web_orca.status == Path("/share/os2slice/web-orca.json")
    assert cfg.web_studio is None
    with pytest.raises(ConfigError, match=r"web_orca\.url must look like https://host:3444"):
        config.parse({"web_orca": {"url": "http://print.lan:3444"}}, tmp_path / "c")


def test_panel_slicer_links(tmp_path: Path) -> None:
    c = config.parse({}, tmp_path / "c")
    assert c.panel_local_slicer == "bambu-studio" and c.panel_web_slicers == ()
    both = {"web_studio": {"url": "https://h:3001"}, "web_orca": {"url": "https://h:3444"}}
    c = config.parse(both, tmp_path / "c")
    assert c.panel_web_slicers == ("bambu-studio", "orcaslicer")  # every one set up
    c = config.parse({**both, "panel": {"local_slicer": "orcaslicer", "web_slicer": "orcaslicer"}},
                     tmp_path / "c")  # fmt: skip
    assert c.panel_local_slicer == "orcaslicer" and c.panel_web_slicers == ("orcaslicer",)
    c = config.parse(
        {**both, "panel": {"local_slicer": "none", "web_slicer": "none"}}, tmp_path / "c"
    )
    assert c.panel_local_slicer == "" and c.panel_web_slicers == ()
    with pytest.raises(ConfigError, match=r"needs a \[web_orca\] table"):
        config.parse({"panel": {"web_slicer": "orcaslicer"}}, tmp_path / "c")
    with pytest.raises(ConfigError, match=r"panel\.local_slicer must be one of"):
        config.parse({"panel": {"local_slicer": "cura"}}, tmp_path / "c")


def test_override_without_target_needs_one_discovering_target_that_suits_it() -> None:
    """Beside PreForm Server, a BamBuddy override's sidecar slicer still loads."""
    data = {
        "slicers": {"studio": {"kind": "bambu-studio-api", "url": "http://s:3001"}},
        "targets": {
            "farm": FARM,
            "formlabs": {"kind": "preform-server", "url": "http://local-preform-server:44388"},
        },
        "printers": {"X1C_01": {"slicer": "studio"}, "Form4-A": {"slicer": "formlabs"}},
    }
    assert _parse(data).printers["X1C_01"].slicer == "studio"
    data["printers"] = {"X1C_01": {"slicer": "nowhere"}}
    with pytest.raises(ConfigError, match=r"no \[slicers\.nowhere\]"):
        _parse(data)
