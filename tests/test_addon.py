from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from os2slice import addon, adminauth, auth, config

OPTS = {
    "onshape_access_key": "acc",
    "onshape_secret_key": "sec",
    "bambuddy_api_key": "bb_x",
    "bambuddy_url": "http://172.30.32.1:8000",
    "hosts": ["print.example.duckdns.org:8443"],
    "library_folder": "Onshape",
    "default_printer": "A1 Mini",
    "manual_start": True,
    "walls": 3,
    "infill": 20,
    "supports": "tree",
    "build_plate_only": False,
    "brim": True,
    "copies": 2,
    "presets": [
        {
            "model": "A1 Mini",
            "printer": "Bambu Lab A1 mini 0.4 nozzle",
            "process": "0.20mm Standard @BBL A1M",
            "filament": "Bambu PLA Basic @BBL A1M",
        },
        {"model": 'Odd "model"', "printer": "p\\x", "process": "q", "filament": "r"},
    ],
    "log_level": "info",
}


def test_prepare_renders_a_valid_lan_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in (e for mode in addon.SECRET_OPTIONS.values() for e in mode.values()):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(os, "environ", os.environ.copy())  # prepare() writes to it
    path = addon.prepare(OPTS, data=tmp_path, ssl_dir=Path("/ssl"))
    assert path == tmp_path / "os2slice" / "config.toml"
    cfg = config.load(path, create=False)
    assert cfg.server.identity == "lan" and cfg.server.bind == "0.0.0.0"
    assert cfg.server.hosts == ("print.example.duckdns.org:8443",)
    assert cfg.server.tls_cert == Path("/ssl/fullchain.pem")
    # The redirect port travels by environment, not config.toml: a changed rendering
    # would make _write_config replace the file and drop the edits made on /admin.
    assert os.environ["OS2SLICE_REDIRECT_PORT"] == str(addon.REDIRECT_PORT) == "8080"
    assert cfg.server.redirect_port == 0 and "redirect_port" not in path.read_text()
    # The add-on still writes [bambuddy]; it's read as [targets.bambuddy] (docs/MODULES.md).
    bambuddy = cfg.targets["bambuddy"]
    assert bambuddy.kind == "bambuddy" and bambuddy.values["url"] == "http://172.30.32.1:8000"
    assert bambuddy.models['Odd "model"'].profiles.printer == "p\\x"
    assert (cfg.print_defaults.walls, cfg.print_defaults.supports) == (3, "tree")
    d = cfg.print_defaults
    assert (d.brim, d.copies, d.top_layers, d.bottom_layers) == (True, 2, 5, 3)  # unset: defaults
    assert os.environ["XDG_CONFIG_HOME"] == str(tmp_path)
    assert auth.load_keys().access == "acc"  # from the environment, not a file
    assert "acc" not in path.read_text() and "bb_x" not in path.read_text()


def test_deprecated_manual_start_is_still_rendered() -> None:
    """`manual_start` has no effect any more, but render_config keeps writing it: a
    changed rendering for the same options would replace config.toml and drop the edits
    made on /admin."""
    lines = addon.render_config(OPTS).splitlines()
    assert "manual_start = true" in lines
    assert "manual_start = false" in addon.render_config({**OPTS, "manual_start": False})
    unset = {k: v for k, v in OPTS.items() if k != "manual_start"}
    assert "manual_start = false" in addon.render_config(unset).splitlines()
    assert addon.render_config(OPTS) == addon.render_config(dict(OPTS))
    cfg = config.parse(config.tomllib.loads(addon.render_config(OPTS)), Path("c.toml"))
    assert cfg.targets["bambuddy"].values["manual_start"] is True  # accepted, ignored


@pytest.mark.parametrize(
    "missing", ["onshape_access_key", "onshape_secret_key", "bambuddy_api_key"]
)
def test_missing_secrets_are_named(tmp_path: Path, missing: str) -> None:
    with pytest.raises(addon.OptionsError, match=missing):
        addon.prepare({**OPTS, missing: "  "}, data=tmp_path)


def test_hosts_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    with pytest.raises(addon.OptionsError, match="hosts"):
        addon.prepare({**OPTS, "hosts": []}, data=tmp_path)


def test_addon_manifest_matches_the_entry_point() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = (root / "addon/os2slice/config.yaml").read_text()
    assert "8443/tcp: 8443" in manifest and f"{addon.PORT}/tcp" in manifest
    assert "8080/tcp: 8444" in manifest and f"{addon.REDIRECT_PORT}/tcp" in manifest
    assert 'webui: "http://[HOST]:[PORT:8080]/admin"' in manifest
    assert "host_network" not in manifest  # D-12: own network namespace
    secrets_ = {k for mode in addon.SECRET_OPTIONS.values() for k in mode}
    for key in (*secrets_, "onshape_auth", "onshape_oauth_client_id", "hosts", "presets",
                "bambuddy_url"):  # fmt: skip
        assert f"  {key}:" in manifest
    assert 'CMD ["os2slice-addon"]' in (root / "addon/os2slice/Dockerfile").read_text()


def test_default_plate_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    cfg = config.load(
        addon.prepare({**OPTS, "default_plate": "Cool Plate"}, data=tmp_path), create=False
    )
    assert cfg.default_bed_type == "Cool Plate"
    cfg = config.load(addon.prepare(OPTS, data=tmp_path), create=False)
    assert cfg.default_bed_type is None


def test_web_studio_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    opts = {**OPTS, "web_studio_url": "https://print.example.duckdns.org:3001"}
    cfg = config.load(addon.prepare(opts, data=tmp_path), create=False)
    assert cfg.web_studio is not None
    assert cfg.web_studio.url == "https://print.example.duckdns.org:3001"
    assert cfg.web_studio.inbox == Path("/share/os2slice/inbox")
    assert config.load(addon.prepare(OPTS, data=tmp_path), create=False).web_studio is None


def test_oauth_mode_needs_its_client_and_skips_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    opts = {k: v for k, v in OPTS.items() if not k.startswith("onshape_")}
    opts["onshape_auth"] = "oauth"
    with pytest.raises(addon.OptionsError, match="onshape_oauth_client_id"):
        addon.prepare(opts, data=tmp_path, ssl_dir=Path("/ssl"))
    opts |= {"onshape_oauth_client_id": "ABCDEFGHIJKL", "onshape_oauth_client_secret": "s3cret"}
    path = addon.prepare(opts, data=tmp_path, ssl_dir=Path("/ssl"))
    cfg = config.load(path, create=False)
    assert cfg.onshape_auth == "oauth" and cfg.oauth_client_id == "ABCDEFGHIJKL"
    assert cfg.oauth_redirect_uri == "https://print.example.duckdns.org:8443/auth/callback"
    assert os.environ["ONSHAPE_OAUTH_CLIENT_SECRET"] == "s3cret"
    assert "s3cret" not in path.read_text()  # secrets never land in the config file


# ---- the config page inside the add-on: admin password, persisted edits, secrets ----

PASSWORD = "correct horse battery"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """prepare() writes os.environ; give it a copy without any secret variables."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for var in (*{e for m in addon.SECRET_OPTIONS.values() for e in m.values()},
                "OS2SLICE_SECRET_TARGETS_BAMBUDDY_API_KEY"):  # fmt: skip
        os.environ.pop(var, None)
    monkeypatch.setattr(adminauth, "SCRYPT_N", 2**10)


def run(opts: dict[str, object], data: Path) -> tuple[Path, list[str]]:
    said: list[str] = []
    return addon.prepare(opts, data=data, ssl_dir=Path("/ssl"), say=said.append), said


def admin_file(data: Path) -> Path:
    return addon.state_path(data) / "admin.json"


def test_admin_password_is_hashed_only_when_it_changes(tmp_path: Path, env: None) -> None:
    _, said = run({**OPTS, "admin_password": PASSWORD}, tmp_path)
    store = adminauth.AdminStore(admin_file(tmp_path))
    assert store.verify(PASSWORD) and any("/admin is on" in s for s in said)
    assert stat.S_IMODE(admin_file(tmp_path).stat().st_mode) == 0o600
    first = admin_file(tmp_path).read_text()
    session = store.create_session()

    _, said = run({**OPTS, "admin_password": PASSWORD}, tmp_path)  # a restart
    assert admin_file(tmp_path).read_text() == first  # same salt: not hashed again
    assert store.check_session(session)  # so admin sessions survive the restart
    assert not any("admin password" in s for s in said)

    run({**OPTS, "admin_password": PASSWORD + "!"}, tmp_path)
    assert admin_file(tmp_path).read_text() != first
    assert store.verify(PASSWORD + "!") and not store.verify(PASSWORD)
    assert not store.check_session(session)  # a new password ends the sessions


def test_password_never_leaves_the_hash(
    tmp_path: Path, env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    addon.prepare({**OPTS, "admin_password": PASSWORD}, data=tmp_path, ssl_dir=Path("/ssl"))
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert "admin password set" in out.out
    assert PASSWORD not in "".join(os.environ.values())
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert PASSWORD not in f.read_text(errors="replace")


def test_empty_password_option_leaves_the_stored_one(tmp_path: Path, env: None) -> None:
    _, said = run(OPTS, tmp_path)  # none yet: say /admin is off
    assert not admin_file(tmp_path).exists()
    assert sum("/admin is off until admin_password is set" in s for s in said) == 1

    run({**OPTS, "admin_password": PASSWORD}, tmp_path)
    before = admin_file(tmp_path).read_text()
    _, said = run({**OPTS, "admin_password": ""}, tmp_path)
    assert admin_file(tmp_path).read_text() == before  # a password set earlier keeps working
    assert adminauth.AdminStore(admin_file(tmp_path)).verify(PASSWORD)
    assert not any("/admin is off" in s for s in said)


@pytest.mark.parametrize("short", ["x", "elevenchars"])
def test_short_admin_password_is_an_options_error(tmp_path: Path, env: None, short: str) -> None:
    with pytest.raises(addon.OptionsError, match=r"admin_password: .*at least 12") as e:
        run({**OPTS, "admin_password": short}, tmp_path)
    assert "Configuration tab" in str(e.value) and short not in str(e.value)
    assert not any(tmp_path.iterdir())  # nothing was changed
    assert "OS2SLICE_ADDON" not in os.environ


def config_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_config_is_rendered_only_when_the_options_change(tmp_path: Path, env: None) -> None:
    path, said = run(OPTS, tmp_path)  # first start
    stamp = path.with_name("options.sha256")
    assert stamp.exists() and any("there was none" in s for s in said)
    assert config_text(path).startswith("# Generated by the os2slice add-on")
    assert "/admin persist" in config_text(path)
    assert config.load(path, create=False).print_defaults.walls == 3

    # An edit made on /admin (the page rewrites the whole file) survives a restart.
    edited = config_text(path).replace("walls = 3", "walls = 4")
    path.write_text(edited, encoding="utf-8")
    _, said = run(OPTS, tmp_path)
    assert config_text(path) == edited and any("keeping" in s for s in said)
    assert config.load(path, create=False).print_defaults.walls == 4

    # A changed option renders the file again (the operator's options win).
    _, said = run({**OPTS, "infill": 30}, tmp_path)
    cfg = config.load(path, create=False)
    assert (cfg.print_defaults.walls, cfg.print_defaults.infill) == (3, 30)
    assert any("the options changed" in s for s in said)

    # A missing stamp (an add-on that rendered before 0.2.0) renders again as well.
    path.write_text(edited, encoding="utf-8")
    stamp.unlink()
    run({**OPTS, "infill": 30}, tmp_path)
    assert config.load(path, create=False).print_defaults.walls == 3


def test_reset_config_forces_a_render(tmp_path: Path, env: None) -> None:
    path, _ = run(OPTS, tmp_path)
    edited = config_text(path).replace("walls = 3", "walls = 4")
    path.write_text(edited, encoding="utf-8")
    _, said = run({**OPTS, "reset_config": False}, tmp_path)  # same render: kept
    assert config_text(path) == edited
    _, said = run({**OPTS, "reset_config": True}, tmp_path)
    assert config.load(path, create=False).print_defaults.walls == 3
    assert any("reset_config is on" in s for s in said)
    assert any("turn reset_config off" in s for s in said)
    path.write_text(edited, encoding="utf-8")
    run({**OPTS, "reset_config": True}, tmp_path)  # still on: renders at every start
    assert config_text(path) != edited


def page_secrets(data: Path) -> auth.SecretFile:
    return auth.SecretFile(addon.state_path(data) / auth.SECRETS_FILE)


def test_secret_saved_on_the_page_is_used_while_the_option_is_empty(
    tmp_path: Path, env: None
) -> None:
    page_secrets(tmp_path).set({auth.BAMBUDDY_SECRET: "from-page"})
    run({**OPTS, "bambuddy_api_key": ""}, tmp_path)  # not an error: the page has it
    assert "BAMBUDDY_API_KEY" not in os.environ
    assert auth.file_store_active()
    assert auth.load_bambuddy_key() == ("from-page", "file")
    auth.store_secret("targets.farm.api_key", "farm")  # what /admin does: into the file
    assert page_secrets(tmp_path).get("targets.farm.api_key") == "farm"


def test_filled_in_option_overrides_the_page(tmp_path: Path, env: None) -> None:
    page_secrets(tmp_path).set(
        {
            auth.BAMBUDDY_SECRET: "from-page",
            auth.BAMBUDDY_ENTRY: "older",
            "targets.farm.api_key": "farm",
        }
    )
    _, said = run(OPTS, tmp_path)  # bambuddy_api_key = "bb_x"
    assert auth.load_bambuddy_key() == ("bb_x", "environment")
    assert page_secrets(tmp_path).read() == {"targets.farm.api_key": "farm"}  # others kept
    assert any("bambuddy_api_key is set on the Configuration tab" in s for s in said)
    assert not any("bb_x" in s or "from-page" in s for s in said)
    # Saved on the page while the option is set: used now, replaced at the next start.
    auth.store_bambuddy_key("newer")
    assert auth.load_bambuddy_key() == ("newer", "file")
    run(OPTS, tmp_path)
    assert auth.load_bambuddy_key() == ("bb_x", "environment")


def test_onshape_keys_from_the_page_and_half_pairs(tmp_path: Path, env: None) -> None:
    no_keys = {**OPTS, "onshape_access_key": "", "onshape_secret_key": ""}
    with pytest.raises(addon.OptionsError, match="onshape_access_key, onshape_secret_key"):
        run(no_keys, tmp_path)
    page_secrets(tmp_path).set({auth.ACCESS_ENTRY: "page-a", auth.SECRET_ENTRY: "page-s"})
    run(no_keys, tmp_path)
    assert auth.load_keys() == auth.Keys("page-a", "page-s", "file")
    with pytest.raises(addon.OptionsError, match="Fill in onshape_secret_key"):
        run({**no_keys, "onshape_access_key": "opt-a"}, tmp_path)  # half a pair
    run(OPTS, tmp_path)  # both options filled in: they replace the page's pair
    assert auth.load_keys() == auth.Keys("acc", "sec", "environment")
    assert page_secrets(tmp_path).read() == {}


def test_oauth_client_secret_from_the_page(tmp_path: Path, env: None) -> None:
    opts = {k: v for k, v in OPTS.items() if not k.startswith("onshape_")}
    opts |= {"onshape_auth": "oauth", "onshape_oauth_client_id": "ABCDEFGHIJKL"}
    with pytest.raises(addon.OptionsError, match="onshape_oauth_client_secret"):
        run(opts, tmp_path)
    page_secrets(tmp_path).set({auth.OAUTH_SECRET_ENTRY: "page-o"})
    run(opts, tmp_path)
    assert auth.load_oauth_client_secret() == "page-o"
    run({**opts, "onshape_oauth_client_secret": "opt-o"}, tmp_path)
    assert auth.load_oauth_client_secret() == "opt-o"
    assert auth.OAUTH_SECRET_ENTRY not in page_secrets(tmp_path).read()


def test_manifest_has_the_config_page_options() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = (root / "addon/os2slice/config.yaml").read_text()
    assert "  admin_password: password?" in manifest
    assert "  reset_config: bool" in manifest and "  reset_config: false" in manifest
    assert 'version: "0.3.7"' in manifest
    assert "url: https://github.com/jlbimson/os2slice" in manifest
    docs = (root / "addon/os2slice/DOCS.md").read_text()
    for word in ("admin_password", "reset_config", "/admin", "secrets.json"):
        assert word in docs
