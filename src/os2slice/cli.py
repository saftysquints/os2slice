"""Command-line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from os2slice import __version__, auth, config, notify, pipeline, printing
from os2slice.bambuddy import BambuddyClient
from os2slice.errors import BadRequest, Os2sliceError
from os2slice.logsetup import log_path, setup_logging
from os2slice.modules import registry
from os2slice.modules.base import MEDIA_FORM
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.request import (
    PART_ID_RE,
    ExportRequest,
    parse_listener_url,
    parse_onshape_url,
)
from os2slice.settings import SLA_DEFAULT_SUPPORTS, SUPPORTS, PrintSettings
from os2slice.slicers import flatpak_app_id, resolve_executable

if TYPE_CHECKING:
    from os2slice import server

log = logging.getLogger("os2slice")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose, console=args.command != "handle")
    log.debug("os2slice %s: %s", __version__, args.command)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except Os2sliceError as e:
        log.debug("command failed", exc_info=True)
        print(f"error: {e.one_line()}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="os2slice", description="Send Onshape parts to a slicer.")
    p.add_argument("--version", action="version", version=f"os2slice {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug output on the terminal")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("send", help="export a part and open it in a slicer")
    s.add_argument("--url", required=True, help="Onshape Part Studio URL (from the address bar)")
    s.add_argument("--slicer", help="slicer key from config.toml (default: the first one)")
    s.add_argument("--part", help="part ID or exact part name (default: the only part)")
    s.add_argument("-c", "--configuration", default="", help="Onshape configuration string")
    s.set_defaults(func=cmd_send)

    pr = sub.add_parser("print", help="slice a part and queue it on a printer")
    pr.add_argument("--url", required=True, help="Onshape Part Studio URL (from the address bar)")
    pr.add_argument(
        "--part",
        action="append",
        help="part ID or exact part name (default: the only part); repeat with --slot for "
        "multi-material, e.g. --part Base --slot 0 --part Text --slot 4",
    )
    pr.add_argument("-c", "--configuration", default="", help="Onshape configuration string")
    pr.add_argument(
        "--printer",
        help="printer name or key, e.g. 'X1C_01', 'bambuddy/2', or 'Any X1C' (the first free "
        "X1C with the filament loaded) (default: config)",
    )
    pr.add_argument(
        "--orient",
        help="as-modeled | x+ x- y+ y- z+ z- (that side down) | face:<face id> | auto "
        "(default: as-modeled; auto on a resin printer)",
    )
    pr.add_argument(
        "--slot",
        action="append",
        help="print from this loaded filament or resin (its id; on BamBuddy the global tray "
        "id: AMS n slot s = n*4+s, external 254/255); one per --part",
    )
    pr.add_argument("--plate", help="build plate, e.g. 'Textured PEI Plate' (default: config)")
    pr.add_argument("--walls", help="wall loops (1-10)")
    pr.add_argument("--infill", help="sparse infill percent (0-100)")
    pr.add_argument(
        "--supports",
        choices=SUPPORTS,
        help="default: config; on a resin printer off = none, else auto supports (default)",
    )
    pr.add_argument("--build-plate-only", action="store_true", help="supports from the plate only")
    pr.add_argument("--top-layers", help="solid top layers (0-30)")
    pr.add_argument("--bottom-layers", help="solid bottom layers (0-30)")
    pr.add_argument(
        "--brim", action=argparse.BooleanOptionalAction, help="outer brim (default: config)"
    )
    pr.add_argument("--copies", help="copies of the part(s) on one plate (1-25)")
    pr.add_argument(
        "--slice-only", action="store_true", help="upload and slice, but don't queue a print"
    )
    pr.add_argument(
        "--wait-for-start",
        action=argparse.BooleanOptionalAction,
        help="leave the queued print waiting until someone presses Start; "
        "--no-wait-for-start: it starts by itself once the printer is free "
        "(default: [print_defaults] wait_for_start, normally off)",
    )
    pr.set_defaults(func=cmd_print)

    sv = sub.add_parser("serve", help="run the web service (confirmation page + print jobs)")
    sv.set_defaults(func=cmd_serve)

    h = sub.add_parser("handle", help="run one listener URL (http://localhost:…/open?…)")
    h.add_argument("url")
    h.set_defaults(func=cmd_handle)

    k = sub.add_parser("setup-keys", help="store Onshape API keys in the system keyring")
    k.add_argument(
        "--from-env",
        action="store_true",
        help="take the key(s) from the environment instead of prompting",
    )
    k.add_argument(
        "--bambuddy", action="store_true", help=f"store the BamBuddy key (${auth.ENV_BAMBUDDY})"
    )
    k.add_argument(
        "--secret",
        metavar="NAME",
        help="store a module secret, e.g. targets.farm.api_key (docs/MODULES.md)",
    )
    k.set_defaults(func=cmd_setup_keys)

    d = sub.add_parser("doctor", help="check keys, config and slicers")
    d.add_argument("--offline", action="store_true", help="don't call the Onshape API")
    d.set_defaults(func=cmd_doctor)

    a = sub.add_parser("admin-password", help="set the password of the config page (/admin)")
    a.set_defaults(func=cmd_admin_password)
    return p


# -- send / handle ----------------------------------------------------------


def cmd_send(args: argparse.Namespace) -> int:
    cfg = config.load()
    slicer = args.slicer or next(iter(cfg.slicers), "")
    if not slicer:
        raise BadRequest("No slicers configured", f"Add a [slicers.<name>] table to {cfg.path}")
    client = OnshapeClient(cfg.onshape_base_url, auth.load_keys())

    def make_request() -> ExportRequest:
        part_given = args.part if args.part and PART_ID_RE.fullmatch(args.part) else None
        req = parse_onshape_url(
            args.url, slicer, cfg.slicers, part_given, args.configuration, cfg.export_format
        )
        return dataclasses.replace(req, part_id=_resolve_part(client, req, args.part))

    with client:
        result = pipeline.run_and_report(make_request, cfg, client=client)
    _print_result(result)
    return result.exit_code


def _resolve_part(client: OnshapeClient, req: ExportRequest, wanted: str | None) -> str:
    parts = client.list_parts(dataclasses.replace(req, part_id=None))
    ids = [str(p.get("partId")) for p in parts]
    listing = ", ".join(f"{p.get('partId')} ({p.get('name')})" for p in parts) or "none"
    if wanted is None:
        if len(parts) == 1:
            return ids[0]
        raise BadRequest("This Part Studio has several parts", f"Pick one with --part: {listing}")
    if wanted in ids:
        return wanted
    by_name = [str(p.get("partId")) for p in parts if p.get("name") == wanted]
    if len(by_name) == 1:
        return by_name[0]
    raise BadRequest(f"No single part matches {wanted!r}", f"Parts: {listing}")


def cmd_print(args: argparse.Namespace) -> int:
    cfg = config.load()
    if not cfg.targets:
        raise config.ConfigError("No printers are configured", f"Add [targets.*] to {cfg.path}")
    orientation = Orientation.parse(args.orient or "as-modeled")
    settings = PrintSettings.from_strings(
        {
            k: v
            for k, v in {
                "walls": args.walls,
                "infill": args.infill,
                "supports": args.supports,
                "build_plate_only": "true" if args.build_plate_only else None,
                "top_layers": args.top_layers,
                "bottom_layers": args.bottom_layers,
                "brim": None if args.brim is None else str(args.brim).lower(),
                "copies": args.copies,
            }.items()
            if v is not None
        },
        cfg.print_defaults,
    )
    with (
        registry.Modules.from_config(cfg) as modules,
        OnshapeClient(cfg.onshape_base_url, auth.load_keys()) as onshape,
    ):
        wanted = args.part or [None]
        slots = args.slot or []
        if len(wanted) > 1 and len(slots) != len(wanted):
            raise BadRequest("Give one --slot per --part for a multi-material print")
        req = parse_onshape_url(args.url, "bambuddy", ["bambuddy"], None, args.configuration)
        ids = [_resolve_part(onshape, req, w) for w in wanted]
        req = dataclasses.replace(req, part_id=ids[0])
        extra = list(zip(ids[1:], slots[1:], strict=True))
        plan = printing.plan_print(
            req, cfg, onshape, modules, args.printer, orientation, settings,
            slots[0] if slots else None, args.plate, extra,
            manual_start=(
                cfg.default_wait_for_start if args.wait_for_start is None else args.wait_for_start
            ),
        )  # fmt: skip
        if plan.sla:
            plan = _resin_defaults(plan, args)
        print("\n".join(plan.summary_lines()))
        if plan.sla and (ignored := _fdm_flags(args)):
            print(f"Note:        {', '.join(ignored)} don't apply to a resin printer; ignored")
        queue = not args.slice_only
        if queue and not _confirm(f"Print on {plan.printer.name}? [y/N] "):
            print("Cancelled; nothing was uploaded or printed.")
            return 0
        outcome = printing.execute_print(
            plan, cfg, onshape, modules, queue=queue, progress=lambda s: print(f"  … {s}")
        )
    s = outcome.slice
    if s.media == MEDIA_FORM:
        print(f"Sliced {s.filename}: {printing.resin_facts(s)}")
    else:
        minutes = f"{s.print_time_s // 60} min" if s.print_time_s else "? min"
        grams = f"{s.material_g:.1f} g" if s.material_g is not None else "? g"
        where = ", ".join(f"{k} {v}" for k, v in s.report.items() if k not in ("module", "url"))
        print(f"Sliced {s.filename}: {minutes}, {grams}" + (f" ({where})" if where else ""))
    for warning in s.warnings:
        print(f"Warning: {warning}")
    sub = outcome.submission
    if sub is not None:
        how = (
            f"press Start in {plan.target_label}'s queue"
            if plan.manual_start
            else "it starts when free"
        )
        if plan.sla:
            print(f"Queued on {plan.printer.name} (job {sub.id}); {sub.detail or how}.")
        else:
            print(f"Queued on {plan.printer.name} (queue item {sub.id}); {how}.")
    return 0


def _resin_defaults(plan: printing.PrintPlan, args: argparse.Namespace) -> printing.PrintPlan:
    """A resin printer's own defaults where no flag was given: auto supports, auto-orient."""
    if args.supports is None:
        settings = dataclasses.replace(plan.settings, supports=SLA_DEFAULT_SUPPORTS)
        plan = dataclasses.replace(plan, settings=settings)
    if args.orient is None:
        plan = dataclasses.replace(plan, orientation=Orientation.parse("auto"))
    return plan


def _fdm_flags(args: argparse.Namespace) -> list[str]:
    """The FDM-only flags given, which a resin printer ignores."""
    given = {
        "--walls": args.walls,
        "--infill": args.infill,
        "--build-plate-only": args.build_plate_only or None,
        "--top-layers": args.top_layers,
        "--bottom-layers": args.bottom_layers,
        "--brim": args.brim,
        "--plate": args.plate,
    }
    return [flag for flag, value in given.items() if value is not None]


def cmd_serve(args: argparse.Namespace) -> int:
    from os2slice import server

    cfg = config.load()
    if not cfg.targets:
        raise config.ConfigError("No printers are configured", f"Add [targets.*] to {cfg.path}")
    modules = registry.Modules.from_config(cfg)
    try:
        service = _make_service(cfg, modules)
    except BaseException:
        modules.close()
        raise
    override = os.environ.get("OS2SLICE_REDIRECT_PORT", "")  # the add-on (addon.py)
    redirect_port = int(override) if override.isdigit() else None
    server.serve(service, redirect_port=redirect_port)  # closes the modules when it stops
    return 0


def _make_service(cfg: config.Config, modules: registry.Modules) -> server.Service:
    from os2slice import server

    signin = None
    if cfg.onshape_auth == "oauth":
        # Each user signs in (D-23); the shared API keys aren't used by the service.
        from os2slice.logsetup import state_dir

        signin = server.make_signin(
            cfg,
            auth.load_oauth_client_secret(),
            state_dir() / "signins.json",
            onshape_as=lambda a: OnshapeClient(cfg.onshape_base_url, a),
        )

        def no_shared_keys() -> OnshapeClient:
            raise auth.AuthError("This server reads Onshape only with each user's sign-in")

        onshape = no_shared_keys
    else:
        keys = auth.load_keys()

        def onshape() -> OnshapeClient:
            return OnshapeClient(cfg.onshape_base_url, keys)

    return server.Service(cfg, onshape=onshape, modules=modules, signin=signin)


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise BadRequest("Printing needs an interactive yes", "Run it from a terminal")
    return input(prompt).strip().lower() in ("y", "yes")


def cmd_handle(args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except Os2sliceError as e:
        log.error("%s", e.one_line())
        notify.notify("os2slice: " + e.message, e.fix, error=True)
        return e.exit_code
    result = pipeline.run_and_report(
        lambda: parse_listener_url(args.url, cfg.slicers, cfg.export_format), cfg
    )
    return result.exit_code


def _print_result(result: pipeline.Result) -> None:
    stream = sys.stdout if result.ok else sys.stderr
    print(result.title, file=stream)
    if result.detail:
        print(f"  {result.detail}", file=stream)


# -- setup-keys ---------------------------------------------------------------


def cmd_setup_keys(args: argparse.Namespace) -> int:
    if args.bambuddy:
        return _setup_bambuddy_key(args.from_env)
    if args.secret:
        return _setup_secret(args.secret, args.from_env)
    if args.from_env:
        access = os.environ.get(auth.ENV_ACCESS, "").strip()
        secret = os.environ.get(auth.ENV_SECRET, "").strip()
    else:
        print("Create keys at Onshape → My account → Developer → API keys (read scope).")
        access = input("Access key: ").strip()
        secret = getpass.getpass("Secret key (hidden): ").strip()
    if not access or not secret or any(c.isspace() for c in access + secret):
        raise BadRequest("Both keys are needed, without spaces")

    cfg = config.load()
    keys = auth.Keys(access, secret, "new")
    with OnshapeClient(cfg.onshape_base_url, keys) as client:
        email = client.check_keys()
    auth.store_keys(access, secret)
    account = f" (account {email})" if email else ""
    print(f"Keys work{account} and are saved in {auth.secret_store_name()}.")
    return 0


def _setup_bambuddy_key(from_env: bool) -> int:
    if from_env:
        key = os.environ.get(auth.ENV_BAMBUDDY, "").strip()
    else:
        print("Create a key in BamBuddy → Settings → API Keys (read status, library, queue).")
        key = getpass.getpass("BamBuddy API key (hidden): ").strip()
    if not key or any(c.isspace() for c in key):
        raise BadRequest("The BamBuddy key is empty or has spaces")
    cfg = config.load()
    target = cfg.targets.get("bambuddy")
    if target is None or target.kind != "bambuddy":
        raise config.ConfigError(
            "BamBuddy isn't configured", f"Add a [bambuddy] table to {cfg.path}"
        )
    with BambuddyClient(target.values["url"], key) as bb:
        printers = bb.list_printers()
        auth_on = bb.auth_enabled()
    auth.store_bambuddy_key(key)
    note = "" if auth_on else " (BamBuddy auth is off, so the key itself couldn't be checked)"
    print(
        f"BamBuddy answered with {len(printers)} printers; key saved in "
        f"{auth.secret_store_name()}{note}."
    )
    return 0


def _setup_secret(name: str, from_env: bool) -> int:
    """Store a module secret (<section>.<key>.<field>) in the secret store; never echoed."""
    if from_env:
        value = os.environ.get(auth.secret_env(name), "").strip()
    else:
        value = getpass.getpass(f"{name} (hidden): ").strip()
    if not value or any(c.isspace() for c in value):
        raise BadRequest(f"{name} is empty or has spaces")
    auth.store_secret(name, value)
    print(f"{name} saved in {auth.secret_store_name()}.")
    return 0


# -- admin-password -----------------------------------------------------------


def cmd_admin_password(args: argparse.Namespace) -> int:
    """Set the config page's password (D-28). Only here, never from a browser."""
    from os2slice.adminauth import AdminStore, admin_path, set_password_interactive

    return 0 if set_password_interactive(AdminStore(admin_path())) else 1


# -- doctor -------------------------------------------------------------------

PASS, WARN, FAIL, NOTE = "PASS", "WARN", "FAIL", "NOTE"  # NOTE: informational, never fails
Add = Callable[[str, str, str], None]
ADDON = (
    os.environ.get("OS2SLICE_ADDON") == "1"
)  # set by the Home Assistant add-on and the Docker image


def cmd_doctor(args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, check: str, detail: str) -> None:
        rows.append((status, check, detail))

    cfg = None
    try:
        cfg = config.load()
        add(PASS, "config", str(cfg.path))
    except Os2sliceError as e:
        add(FAIL, "config", e.one_line())

    check_onshape(cfg, args.offline, add)

    if cfg and not ADDON:
        _check_export_dir(cfg, add)
        if not cfg.slicers and not cfg.targets:
            add(FAIL, "slicers", f"no slicers and no printers in {cfg.path}")
        for s in cfg.slicers.values():
            _check_slicer(s, add)

    if cfg:
        _check_modules(cfg, add, offline=args.offline)
    check_admin_password(add)

    if ADDON:
        pass  # no desktop inside the add-on; the page and the log report errors
    elif shutil.which("notify-send"):
        add(PASS, "notify-send", "desktop notifications available")
    else:
        add(WARN, "notify-send", "not found; errors will only be in the log")
    add(PASS, "log", str(log_path()))

    width = max(len(c) for _, c, _ in rows)
    for status, check, detail in rows:
        print(f"{status}  {check:<{width}}  {detail}")
    return 1 if any(s == FAIL for s, _, _ in rows) else 0


def check_onshape(cfg: config.Config | None, offline: bool, add: Add) -> None:
    """Onshape credentials: the OAuth client secret (D-23), or the shared API keys."""
    if cfg and cfg.onshape_auth == "oauth":
        # Per-user sign-in (D-23): no shared keys; check the OAuth app's client instead.
        try:
            auth.load_oauth_client_secret()
            add(PASS, "Onshape sign-in", f"per user; redirect URI {cfg.oauth_redirect_uri}")
        except Os2sliceError as e:
            add(FAIL, "Onshape sign-in", e.one_line())
    else:
        _check_keys(cfg, offline, add)


def check_admin_password(add: Add) -> None:
    """Whether the config page has a password (a note: /admin is optional, D-28)."""
    from os2slice.adminauth import AdminStore, admin_path

    if AdminStore(admin_path()).has_password():
        add(PASS, "admin password", "set; the config page /admin is on")
    else:
        add(NOTE, "admin password", "not set, so /admin is off; `os2slice admin-password` sets it")


def _check_keys(cfg: config.Config | None, offline: bool, add: Add) -> None:
    keys = None
    try:
        keys = auth.load_keys()
        if keys.source in ("keyring", "file") or ADDON:
            add(PASS, "API keys", f"from the {_secret_source(keys.source)}")
        else:
            add(WARN, "API keys", f"from ${auth.ENV_ACCESS}; run `os2slice setup-keys`")
    except Os2sliceError as e:
        add(FAIL, "API keys", e.one_line())

    if cfg and keys and not offline:
        try:
            with OnshapeClient(cfg.onshape_base_url, keys) as client:
                email = client.check_keys()
                add(PASS, "Onshape login", f"{email or 'keys accepted'} on {cfg.onshape_base_url}")
        except Os2sliceError as e:
            add(FAIL, "Onshape login", e.one_line())


def _check_modules(cfg: config.Config, add: Add, offline: bool) -> None:
    """A row per secret and per configured slicer/target module (its own `check()`)."""
    missing = check_module_secrets(cfg, add)
    if offline or missing:
        return
    try:
        modules = registry.Modules.from_config(cfg)
    except Os2sliceError as e:
        add(FAIL, "modules", e.one_line())
        return
    with modules:
        check_module_health(modules, add)


def _secret_source(source: str) -> str:
    """Where a secret came from, for doctor rows (never its value)."""
    if source == "file":
        return f"secret file {auth.secrets_path()}"
    if source == "environment" and ADDON:
        return "add-on/container settings"
    return "system keyring" if source == "keyring" else source


def check_module_secrets(cfg: config.Config, add: Add) -> bool:
    """A row per module secret (never its value). True when a required one is missing."""
    sections = [("targets", t) for t in cfg.targets.values()]
    sections += [("slicers", s) for s in cfg.slicer_modules.values()]
    missing = False
    for section, m in sections:
        spec = registry.spec_for(m.kind)
        for f in spec.fields:
            if f.type != "secret":
                continue
            name = registry.secret_name(section, m.key, f.key)
            value, source = auth.find_secret(name)
            if value:
                ok = source in ("keyring", "file") or ADDON
                add(PASS if ok else WARN, name, f"from the {_secret_source(source)}")
            elif f.required:
                missing = True
                add(FAIL, name, "not in the secret store or the environment; run `os2slice "
                    + ("setup-keys --bambuddy`" if name == auth.BAMBUDDY_SECRET
                       else f"setup-keys --secret {name}`"))  # fmt: skip
    return missing


def check_module_health(modules: registry.Modules, add: Add) -> None:
    """A row per built slicer/target module, from its own `check()`."""
    seen: set[int] = set()
    for section, key, module in [
        *(("target", k, t) for k, t in modules.targets.items()),
        *(("slicer", k, s) for k, s in modules.slicers.items()),
    ]:
        if id(module) in seen:
            continue  # a slicer + target module is checked once
        seen.add(id(module))
        role = "slicer + target" if module.spec.role == "both" else section
        label = f"{role} {key}"
        try:
            health = module.check()
        except Os2sliceError as e:
            add(FAIL, label, f"{module.spec.label}: {e.one_line()}")
            continue
        add(PASS if health.ok else FAIL, label, f"{module.spec.label}: {health.summary}")
        if health.detail:
            add(WARN, label, health.detail)


def _check_export_dir(cfg: config.Config, add: Add) -> None:
    try:
        cfg.export_dir.mkdir(parents=True, exist_ok=True)
        if os.access(cfg.export_dir, os.W_OK):
            add(PASS, "export dir", str(cfg.export_dir))
        else:
            add(FAIL, "export dir", f"{cfg.export_dir} isn't writable")
    except OSError as e:
        add(FAIL, "export dir", f"{cfg.export_dir}: {e.strerror}")


def _check_slicer(s: config.SlicerConfig, add: Add) -> None:
    label = f"slicer {s.key}"
    exe = resolve_executable(s.argv[0])
    if exe is None:
        add(FAIL, label, f"{s.name}: {s.argv[0]} not found")
        return
    app_id = flatpak_app_id(s.argv)
    if app_id is None:
        add(PASS, label, f"{s.name}: {exe}")
        return
    r = subprocess.run(
        [exe, "info", "--show-ref", app_id], capture_output=True, text=True, check=False
    )
    if r.returncode == 0:
        add(PASS, label, f"{s.name}: flatpak {r.stdout.strip()}")
    else:
        add(FAIL, label, f"{s.name}: flatpak app {app_id} isn't installed")


if __name__ == "__main__":
    sys.exit(main())
