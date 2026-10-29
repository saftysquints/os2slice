"""Load and validate ~/.config/os2slice/config.toml."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

from os2slice import extra_settings
from os2slice.bambuddy import PresetChoice
from os2slice.errors import BadRequest, ConfigError
from os2slice.modules.base import ModelDefaults, ModuleSpec, Profiles
from os2slice.request import FORMATS, SLICER_KEY_RE, Fmt
from os2slice.settings import PrintSettings, check_bed_type

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ONSHAPE_BASE_RE = re.compile(r"https://[a-z0-9-]+\.onshape\.com")
HTTP_BASE_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")
PRESET_SOURCES = ("standard", "cloud", "local", "orca_cloud")
IP_RE = re.compile(r"(\d{1,3}\.){3}\d{1,3}")
HOST_RE = re.compile(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?|\[::1\](:[0-9]{1,5})?")
UNITS = ("millimeter", "centimeter", "meter", "inch", "foot", "yard")
FIX = "Edit {path}"
# [printers.<key>]: BamBuddy printer names ("A1 Mini", "X1C_01") are keys too, so spaces
# are allowed; "/" (discovered keys, "<target>/<id>") and "|" (form values) are not.
PRINTER_KEY_RE = re.compile(r"[A-Za-z0-9_.()+-][A-Za-z0-9 _.()+-]{0,63}")
# The panel's "Open in …" links: a slicer on the user's own computer (its URL handler), and
# the shared browser session of each, configured by its table.
DESKTOP_SLICERS = {"bambu-studio": "Bambu Studio", "orcaslicer": "OrcaSlicer"}
WEB_SLICER_TABLES = {"bambu-studio": "web_studio", "orcaslicer": "web_orca"}
TOP_LEVEL = {
    "onshape", "server", "export", "slicers", "targets", "printers", "default_printer",
    "bambuddy", "print_defaults", "web_studio", "web_orca", "panel",
}  # fmt: skip


@dataclass(frozen=True)
class SlicerConfig:
    key: str
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ModuleConfig:
    """A server-side module: [slicers.<key>] with `kind`, or [targets.<key>]."""

    key: str
    kind: str
    values: Mapping[str, Any]  # validated non-secret fields, defaults filled in
    models: Mapping[str, ModelDefaults] = field(default_factory=dict)  # targets only


@dataclass(frozen=True)
class PrinterConfig:
    """[printers.<key>]: a hand-configured printer, or overrides for a discovered one.

    `target` None = an override, matched by name against every discovering target.
    """

    key: str
    target: str | None = None
    slicer: str = ""
    name: str = ""
    model: str = ""
    technology: str = ""
    bed_mm: tuple[float, float] | None = None
    nozzle_count: int = 1
    profiles: Profiles = field(default_factory=Profiles)
    materials: tuple[str, ...] = ()
    bed_type: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    path: Path
    onshape_base_url: str
    port: int
    export_dir: Path
    export_format: Fmt
    units: str
    keep_days: int
    slicers: dict[str, SlicerConfig]  # desktop hand-offs ([slicers.*] with argv)
    slicer_modules: dict[str, ModuleConfig] = field(default_factory=dict)
    targets: dict[str, ModuleConfig] = field(default_factory=dict)
    printers: dict[str, PrinterConfig] = field(default_factory=dict)
    default_printer: str = ""  # a printer key or name
    print_defaults: PrintSettings = field(default_factory=PrintSettings)
    default_bed_type: str | None = None  # [print_defaults] bed_type
    # [print_defaults] wait_for_start: the Wait for Start box's initial state (D-33)
    default_wait_for_start: bool = False
    server: ServerConfig = field(default_factory=lambda: ServerConfig())
    web_studio: WebStudioConfig | None = None
    web_orca: WebStudioConfig | None = None  # the same for the web OrcaSlicer (orca-web)
    panel_extras: tuple[str, ...] = ()  # [panel] extra_settings: extra_settings.CATALOG keys
    # [panel] local_slicer: the "on this computer" link's app ("" = no link), and
    # web_slicer: the browser sessions the panel offers (DESKTOP_SLICERS names).
    panel_local_slicer: str = "bambu-studio"
    panel_web_slicers: tuple[str, ...] = ()
    # [onshape] auth: "keys" = one shared API key pair; "oauth" = each user signs in (D-23)
    onshape_auth: str = "keys"
    oauth_client_id: str = ""
    oauth_base_url: str = "https://oauth.onshape.com"

    @property
    def oauth_redirect_uri(self) -> str:
        """Where Onshape sends users back; register it on the OAuth app."""
        return f"https://{self.server.hosts[0]}/auth/callback"


@dataclass(frozen=True)
class WebStudioConfig:
    """A shared slicer session in the browser: Bambu Studio (bambustudio_web add-on, D-21)
    or OrcaSlicer (orcaslicer_web)."""

    url: str  # what browsers open, e.g. https://print.example.duckdns.org:3001
    inbox: Path  # where os2slice drops 3MF files for it to open
    status: Path  # its {"viewers": n, "updated": t} file


@dataclass(frozen=True)
class ServerConfig:
    """How `os2slice serve` decides who may use it (D-11, D-13)."""

    bind: str = "127.0.0.1"
    port: int = 8765
    hosts: tuple[str, ...] = ("localhost:8765", "127.0.0.1:8765")  # accepted Host headers
    identity: str = "none"  # "none" (localhost only) | "lan" (D-17) | "tailscale" (D-11)
    allowed_users: tuple[str, ...] = ()  # Tailscale logins, when identity = "tailscale"
    tls_cert: Path | None = None  # PEM chain; required for "lan"
    tls_key: Path | None = None
    redirect_port: int = 0  # plain-HTTP listener that only 303s to https://hosts[0]/admin; 0 = off


@dataclass(frozen=True)
class BambuddyConfig:
    """The legacy [bambuddy] table, before it becomes [targets.bambuddy]."""

    base_url: str
    folder: str
    default_printer: str
    manual_start: bool  # deprecated, no effect (D-33); still accepted
    presets: dict[str, PresetChoice]  # keyed by BamBuddy printer model, e.g. "A1 Mini"
    public_url: str = ""  # BamBuddy's UI as browsers reach it (default: this host, port 8000)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "os2slice"


def config_path() -> Path:
    return config_dir() / "config.toml"


def default_config_text() -> str:
    return resources.files("os2slice").joinpath("default_config.toml").read_text("utf-8")


def load(path: Path | None = None, create: bool = True) -> Config:
    """Load the config, writing the default first if it doesn't exist yet."""
    path = path or config_path()
    if not path.exists():
        if not create:
            raise ConfigError(f"No config file at {path}", "Run any os2slice command to create it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_config_text(), encoding="utf-8")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Can't read {path}: {e}", FIX.format(path=path)) from e
    return parse(data, path)


def parse(data: dict[str, Any], path: Path) -> Config:
    fix = FIX.format(path=path)

    def fail(msg: str) -> ConfigError:
        return ConfigError(f"config.toml: {msg}", fix)

    def table(name: str) -> dict[str, Any]:
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise fail(f"[{name}] must be a table")
        return value

    def check_keys(name: str, tbl: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(tbl) - allowed)
        if unknown:
            raise fail(f"unknown key(s) in [{name}]: {', '.join(unknown)}")

    unknown_tables = sorted(set(data) - TOP_LEVEL)
    if unknown_tables:
        raise fail(f"unknown table(s): {', '.join(unknown_tables)}")

    onshape = table("onshape")
    check_keys("onshape", onshape, {"base_url", "auth", "oauth_client_id", "oauth_url"})
    base_url = str(onshape.get("base_url", "https://cad.onshape.com")).rstrip("/")
    if not ONSHAPE_BASE_RE.fullmatch(base_url):
        raise fail("onshape.base_url must look like https://<name>.onshape.com")
    onshape_auth = onshape.get("auth", "keys")
    if onshape_auth not in ("keys", "oauth"):
        raise fail('onshape.auth must be "keys" or "oauth"')
    oauth_client_id = str(onshape.get("oauth_client_id", ""))
    oauth_base_url = str(onshape.get("oauth_url", "https://oauth.onshape.com")).rstrip("/")
    if not ONSHAPE_BASE_RE.fullmatch(oauth_base_url):
        raise fail("onshape.oauth_url must look like https://<name>.onshape.com")
    if onshape_auth == "oauth" and not re.fullmatch(r"[A-Za-z0-9=+/._-]{8,200}", oauth_client_id):
        raise fail('onshape.oauth_client_id is required when auth = "oauth"')

    server = table("server")
    check_keys(
        "server",
        server,
        {"port", "bind", "hosts", "identity", "allowed_users", "tls_cert", "tls_key",
         "redirect_port"},
    )  # fmt: skip
    port = server.get("port", 8765)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise fail("server.port must be an integer from 1024 to 65535")
    bind = server.get("bind", "127.0.0.1")
    if not isinstance(bind, str) or not IP_RE.fullmatch(bind):
        raise fail("server.bind must be an IPv4 address such as 127.0.0.1 or 0.0.0.0")
    hosts = server.get("hosts", [f"localhost:{port}", f"127.0.0.1:{port}"])
    if not (isinstance(hosts, list) and hosts and all(HOST_RE.fullmatch(str(h)) for h in hosts)):
        raise fail("server.hosts must be a list of host[:port] names")
    identity = server.get("identity", "none")
    if identity not in ("none", "lan", "tailscale"):
        raise fail('server.identity must be "none", "lan" or "tailscale"')
    users = server.get("allowed_users", [])
    if not (isinstance(users, list) and all(isinstance(u, str) and "@" in u for u in users)):
        raise fail("server.allowed_users must be a list of Tailscale logins")
    if identity == "tailscale" and not users:
        raise fail('server.allowed_users can\'t be empty when identity = "tailscale"')
    if identity == "none" and any(not _is_local_host(str(h)) for h in hosts):
        raise fail('identity = "none" is only allowed when every server.hosts entry is localhost')
    tls = [server.get(k) for k in ("tls_cert", "tls_key")]
    if any(v is not None and (not isinstance(v, str) or not v) for v in tls) or (
        (tls[0] is None) != (tls[1] is None)
    ):
        raise fail("server.tls_cert and server.tls_key must both be file paths, or both unset")
    if onshape_auth == "oauth" and (identity != "lan" or tls[0] is None):
        # Sign-in cookies are Secure and Onshape needs an https redirect URI.
        raise fail('onshape.auth = "oauth" needs server.identity = "lan" with tls_cert/tls_key')
    tls_cert, tls_key = (Path(v).expanduser() if v else None for v in tls)
    if bind not in ("127.0.0.1",) and identity != "lan":
        raise fail('Only identity = "lan" may bind beyond 127.0.0.1')
    if identity == "lan":
        if tls_cert is None:
            raise fail('identity = "lan" needs server.tls_cert and server.tls_key (HTTPS)')
        if "hosts" not in server:
            raise fail('identity = "lan" needs server.hosts, e.g. ["<name>.duckdns.org:8443"]')
    redirect_port = server.get("redirect_port", 0)
    if (
        not isinstance(redirect_port, int)
        or isinstance(redirect_port, bool)
        or not 0 <= redirect_port <= 65535
    ):
        raise fail("server.redirect_port must be an integer from 1 to 65535, or 0 for off")
    if redirect_port and redirect_port == port:
        raise fail("server.redirect_port must differ from server.port (or be 0 for off)")
    if redirect_port and identity != "lan":
        raise fail('server.redirect_port needs identity = "lan" (or set it to 0 for off)')
    server_cfg = ServerConfig(
        bind, port, tuple(map(str, hosts)), identity, tuple(users), tls_cert, tls_key,
        redirect_port,
    )  # fmt: skip

    export = table("export")
    check_keys("export", export, {"dir", "format", "units", "keep_days"})
    export_dir = export.get("dir", "~/OnshapeExports")
    if not isinstance(export_dir, str) or not export_dir:
        raise fail("export.dir must be a path")
    export_path = Path(export_dir).expanduser()
    if not export_path.is_absolute():
        raise fail("export.dir must be an absolute path (or start with ~)")
    fmt = export.get("format", "stl")
    if fmt not in FORMATS:
        raise fail(f"export.format must be one of {', '.join(FORMATS)}")
    units = export.get("units", "millimeter")
    if units not in UNITS:
        raise fail(f"export.units must be one of {', '.join(UNITS)}")
    keep_days = export.get("keep_days", 30)
    if not isinstance(keep_days, int) or isinstance(keep_days, bool) or keep_days < 0:
        raise fail("export.keep_days must be a whole number ≥ 0")

    slicers: dict[str, SlicerConfig] = {}
    slicer_modules: dict[str, ModuleConfig] = {}
    for key, entry in table("slicers").items():
        where = f"slicers.{key}"
        if not SLICER_KEY_RE.fullmatch(key):
            raise fail(f"[{where}]: names may only use a-z, 0-9, _ and -")
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        if entry.get("kind", "desktop") != "desktop":
            # A server-side slicer module; one with argv and no kind is the desktop hand-off.
            spec = _spec(entry["kind"], where, fail)
            if spec.role not in ("slicer", "both"):
                raise fail(f"[{where}]: {spec.kind} isn't a slicer")
            slicer_modules[key] = ModuleConfig(key, spec.kind, _values(where, spec, entry, fail))
            continue
        check_keys(where, {k: v for k, v in entry.items() if k != "kind"}, {"name", "argv"})
        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a for a in argv)
        ):
            raise fail(f"{where}.argv must be a non-empty list of strings")
        name = entry.get("name", key)
        if not isinstance(name, str) or not name:
            raise fail(f"{where}.name must be a string")
        slicers[key] = SlicerConfig(key=key, name=name, argv=tuple(argv))

    bambuddy = _parse_bambuddy(data, fail) if "bambuddy" in data else None
    targets = _parse_targets(table("targets"), fail)
    default_printer = data.get("default_printer", "")
    if not isinstance(default_printer, str):
        raise fail("default_printer must be a printer name or key")
    if bambuddy is not None:
        # Compatibility: [bambuddy] is read as [targets.bambuddy], slicing with itself.
        if "bambuddy" in targets:
            raise fail("[bambuddy] and [targets.bambuddy] can't both be set; keep one")
        if bambuddy.default_printer and default_printer:
            raise fail("set default_printer at the top level or in [bambuddy], not both")
        default_printer = default_printer or bambuddy.default_printer
        targets = {"bambuddy": _bambuddy_target(bambuddy), **targets}
    printers = _parse_printers(table("printers"), fail)
    _check_pairs(slicers, slicer_modules, targets, printers, fail)

    pd = table("print_defaults")
    check_keys(
        "print_defaults",
        pd,
        {
            "walls",
            "infill",
            "supports",
            "build_plate_only",
            "top_layers",
            "bottom_layers",
            "brim",
            "copies",
            "bed_type",
            "wait_for_start",
        },
    )
    default_wait_for_start = pd.get("wait_for_start", False)
    if not isinstance(default_wait_for_start, bool):
        raise fail("print_defaults.wait_for_start must be true or false")
    try:
        not_settings = ("bed_type", "wait_for_start")
        print_defaults = PrintSettings(**{k: pd[k] for k in pd if k not in not_settings})
        default_bed_type = check_bed_type(pd.get("bed_type"))
    except BadRequest as e:
        raise fail(f"[print_defaults]: {e.message}") from e

    def web_app(name: str, port: int, inbox: str, status: str) -> WebStudioConfig | None:
        ws = table(name)
        if not ws:
            return None
        check_keys(name, ws, {"url", "inbox", "status"})
        url = str(ws.get("url", "")).rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", url):
            raise fail(f"{name}.url must look like https://host:{port}")
        inbox_path = Path(str(ws.get("inbox", inbox))).expanduser()
        status_path = Path(str(ws.get("status", status))).expanduser()
        if not (inbox_path.is_absolute() and status_path.is_absolute()):
            raise fail(f"{name}.inbox and .status must be absolute paths")
        return WebStudioConfig(url, inbox_path, status_path)

    web_studio = web_app(
        "web_studio", 3001, "/share/os2slice/inbox", "/share/os2slice/web-studio.json"
    )
    web_orca = web_app(
        "web_orca", 3444, "/share/os2slice/orca-inbox", "/share/os2slice/web-orca.json"
    )

    panel = table("panel")
    check_keys("panel", panel, {"extra_settings", "local_slicer", "web_slicer"})
    wanted = panel.get("extra_settings", [])
    if not (isinstance(wanted, list) and all(isinstance(k, str) for k in wanted)):
        raise fail("panel.extra_settings must be a list of setting names")
    try:
        panel_extras = extra_settings.check_keys(wanted)
    except BadRequest as e:
        raise fail(f"panel.extra_settings: {e.message}. {e.fix}") from e
    names = ", ".join(f'"{n}"' for n in (*DESKTOP_SLICERS, "none"))
    local = panel.get("local_slicer", "bambu-studio")
    if local not in (*DESKTOP_SLICERS, "none"):
        raise fail(f"panel.local_slicer must be one of {names}")
    configured_web = {"bambu-studio": web_studio, "orcaslicer": web_orca}
    web = panel.get("web_slicer")
    if web is None:  # every browser session that is set up
        web_slicers = tuple(n for n, ws in configured_web.items() if ws is not None)
    elif web == "none":
        web_slicers = ()
    elif web in DESKTOP_SLICERS:
        if configured_web[web] is None:
            raise fail(f'panel.web_slicer = "{web}" needs a [{WEB_SLICER_TABLES[web]}] table')
        web_slicers = (web,)
    else:
        raise fail(f"panel.web_slicer must be one of {names}")

    return Config(
        path=path,
        onshape_base_url=base_url,
        port=port,
        export_dir=export_path,
        export_format=cast(Fmt, fmt),
        units=units,
        keep_days=keep_days,
        slicers=slicers,
        slicer_modules=slicer_modules,
        targets=targets,
        printers=printers,
        default_printer=default_printer,
        print_defaults=print_defaults,
        default_bed_type=default_bed_type,
        default_wait_for_start=default_wait_for_start,
        server=server_cfg,
        web_studio=web_studio,
        web_orca=web_orca,
        panel_extras=panel_extras,
        panel_local_slicer="" if local == "none" else local,
        panel_web_slicers=web_slicers,
        onshape_auth=onshape_auth,
        oauth_client_id=oauth_client_id,
        oauth_base_url=oauth_base_url,
    )


def _is_local_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")


def _parse_bambuddy(data: dict[str, Any], fail: Any) -> BambuddyConfig:
    bb = data["bambuddy"]
    if not isinstance(bb, dict):
        raise fail("[bambuddy] must be a table")
    unknown = sorted(
        set(bb) - {"base_url", "public_url", "folder", "default_printer", "manual_start", "presets"}
    )
    if unknown:
        raise fail(f"unknown key(s) in [bambuddy]: {', '.join(unknown)}")
    base_url = str(bb.get("base_url", "")).rstrip("/")
    if not HTTP_BASE_RE.fullmatch(base_url):
        raise fail("bambuddy.base_url must look like http://host:8000")
    public_url = str(bb.get("public_url", "")).rstrip("/")
    if public_url and not HTTP_BASE_RE.fullmatch(public_url):
        raise fail("bambuddy.public_url must look like http://host:8000")
    folder = bb.get("folder", "Onshape")
    if not isinstance(folder, str) or not folder or "/" in folder or len(folder) > 100:
        raise fail("bambuddy.folder must be a short folder name")
    default_printer = bb.get("default_printer", "")
    if not isinstance(default_printer, str):
        raise fail("bambuddy.default_printer must be a printer name")
    manual_start = bb.get("manual_start", False)
    if not isinstance(manual_start, bool):
        raise fail("bambuddy.manual_start must be true or false")
    presets: dict[str, PresetChoice] = {}
    raw = bb.get("presets", {})
    if not isinstance(raw, dict):
        raise fail("[bambuddy.presets] must be a table")
    for model, entry in raw.items():
        where = f"bambuddy.presets.{model}"
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        extra = sorted(set(entry) - {"printer", "process", "filament", "source", "bed_type"})
        if extra:
            raise fail(f"unknown key(s) in [{where}]: {', '.join(extra)}")
        vals = {k: entry.get(k) for k in ("printer", "process", "filament")}
        if not all(isinstance(v, str) and v for v in vals.values()):
            raise fail(f"[{where}] needs printer, process and filament preset names")
        source = entry.get("source", "standard")
        if source not in PRESET_SOURCES:
            raise fail(f"{where}.source must be one of {', '.join(PRESET_SOURCES)}")
        try:
            bed = check_bed_type(entry.get("bed_type"))
        except BadRequest as e:
            raise fail(f"{where}.bed_type: {e.message}") from e
        presets[model] = PresetChoice(source=source, bed_type=bed, **vals)  # type: ignore[arg-type]
    return BambuddyConfig(base_url, folder, default_printer, manual_start, presets, public_url)


Fail = Callable[[str], ConfigError]


def _spec(kind: object, where: str, fail: Fail) -> ModuleSpec:
    from os2slice.modules.registry import kinds

    known = kinds()
    if not isinstance(kind, str) or kind not in known or kind == "desktop":
        names = ", ".join(k for k in known if k != "desktop")
        raise fail(f"{where}.kind must be one of {names}")
    return known[kind]


def _values(
    where: str,
    spec: ModuleSpec,
    entry: dict[str, Any],
    fail: Fail,
    extra: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A module's config values, checked against its spec. Secrets may not appear here."""
    fields = {f.key: f for f in spec.fields}
    for k in entry:
        if k in fields and fields[k].type == "secret":
            raise fail(
                f"{where}.{k} is a secret: keep it out of config.toml and store it with "
                f"`os2slice setup-keys --secret {where}.{k}`"
            )
    unknown = sorted(set(entry) - {"kind"} - set(fields) - set(extra))
    if unknown:
        raise fail(f"unknown key(s) in [{where}]: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for f in spec.fields:
        if f.type == "secret":
            continue
        if f.key not in entry:
            if f.required:
                raise fail(f"{where}.{f.key} is required ({f.label})")
            if f.default is not None:
                out[f.key] = f.default
            continue
        v = entry[f.key]
        ok = {
            "str": isinstance(v, str) and v != "",
            "path": isinstance(v, str) and v != "",
            "int": isinstance(v, int) and not isinstance(v, bool),
            "bool": isinstance(v, bool),
            "url": isinstance(v, str) and bool(HTTP_BASE_RE.fullmatch(v.rstrip("/"))),
            "list": isinstance(v, list) and all(isinstance(x, str) for x in v),
            "choice": v in f.choices,
        }[f.type]
        if not ok:
            want = {
                "url": "a URL like http://host:8000",
                "choice": f"one of {', '.join(f.choices)}",
                "list": "a list of strings",
                "int": "a whole number",
                "bool": "true or false",
            }.get(f.type, "a non-empty string")
            raise fail(f"{where}.{f.key} must be {want}")
        out[f.key] = v.rstrip("/") if f.type == "url" else v
    return out


def _profiles(where: str, value: object, fail: Fail) -> Profiles:
    if not isinstance(value, dict):
        raise fail(f"{where}.profiles must be a table of printer, process and filament names")
    unknown = sorted(set(value) - {"printer", "process", "filament"})
    if unknown:
        raise fail(f"unknown key(s) in {where}.profiles: {', '.join(unknown)}")
    if not all(isinstance(v, str) for v in value.values()):
        raise fail(f"{where}.profiles values must be profile names")
    return Profiles(**value)


def _bed_type(where: str, value: object, fail: Fail) -> str | None:
    if value is not None and not isinstance(value, str):
        raise fail(f"{where}.bed_type must be a build plate name")
    try:
        return check_bed_type(value)
    except BadRequest as e:
        raise fail(f"{where}.bed_type: {e.message}") from e


def _extra(where: str, value: object, fail: Fail) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail(f"{where}.extra must be a table")
    return dict(value)


def _parse_targets(raw: dict[str, Any], fail: Fail) -> dict[str, ModuleConfig]:
    targets: dict[str, ModuleConfig] = {}
    for key, entry in raw.items():
        where = f"targets.{key}"
        if not SLICER_KEY_RE.fullmatch(key):
            raise fail(f"[{where}]: names may only use a-z, 0-9, _ and -")
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        spec = _spec(entry.get("kind"), where, fail)
        if spec.role not in ("target", "both"):
            raise fail(f"[{where}]: {spec.kind} isn't a target")
        values = _values(where, spec, entry, fail, extra=frozenset({"models"}))
        models: dict[str, ModelDefaults] = {}
        raw_models = entry.get("models", {})
        if not isinstance(raw_models, dict):
            raise fail(f"[{where}.models] must be a table")
        for model, m in raw_models.items():
            mw = f'{where}.models."{model}"'
            if not isinstance(m, dict):
                raise fail(f"[{mw}] must be a table")
            unknown = sorted(set(m) - {"slicer", "profiles", "bed_type", "extra"})
            if unknown:
                raise fail(f"unknown key(s) in [{mw}]: {', '.join(unknown)}")
            slicer = m.get("slicer", "")
            if not isinstance(slicer, str) or (slicer and not SLICER_KEY_RE.fullmatch(slicer)):
                raise fail(f"{mw}.slicer must be a slicer key")
            models[model] = ModelDefaults(
                slicer=slicer,
                profiles=_profiles(mw, m.get("profiles", {}), fail),
                bed_type=_bed_type(mw, m.get("bed_type"), fail),
                extra=_extra(mw, m.get("extra", {}), fail),
            )
        targets[key] = ModuleConfig(key, spec.kind, values, models)
    return targets


PRINTER_FIELDS = {
    "target", "slicer", "name", "model", "technology", "bed_mm", "nozzle_count",
    "profiles", "materials", "bed_type", "extra",
}  # fmt: skip


def _parse_printers(raw: dict[str, Any], fail: Fail) -> dict[str, PrinterConfig]:
    printers: dict[str, PrinterConfig] = {}
    for key, entry in raw.items():
        where = f'printers."{key}"'
        if not PRINTER_KEY_RE.fullmatch(key):
            raise fail(f"[{where}]: printer names may use letters, digits, spaces and _.()+-")
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        unknown = sorted(set(entry) - PRINTER_FIELDS)
        if unknown:
            raise fail(f"unknown key(s) in [{where}]: {', '.join(unknown)}")
        target = entry.get("target")
        if target is not None and not (isinstance(target, str) and SLICER_KEY_RE.fullmatch(target)):
            raise fail(f"{where}.target must be a [targets.*] key")
        strs = {k: entry.get(k, "") for k in ("slicer", "name", "model", "technology")}
        if not all(isinstance(v, str) for v in strs.values()):
            raise fail(f"{where}: slicer, name, model and technology must be strings")
        if strs["technology"] not in ("", "fdm", "sla"):
            raise fail(f'{where}.technology must be "fdm" or "sla"')
        bed = entry.get("bed_mm")
        if bed is not None and not (
            isinstance(bed, list)
            and len(bed) == 2
            and all(isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in bed)
        ):
            raise fail(f"{where}.bed_mm must be [width, depth] in mm")
        nozzles = entry.get("nozzle_count", 1)
        if not isinstance(nozzles, int) or isinstance(nozzles, bool) or not 1 <= nozzles <= 16:
            raise fail(f"{where}.nozzle_count must be a whole number from 1 to 16")
        materials = entry.get("materials", [])
        if not (isinstance(materials, list) and all(isinstance(m, str) and m for m in materials)):
            raise fail(f"{where}.materials must be a list of names")
        printers[key] = PrinterConfig(
            key=key,
            target=target,
            slicer=strs["slicer"],
            name=strs["name"],
            model=strs["model"],
            technology=strs["technology"],
            bed_mm=(float(bed[0]), float(bed[1])) if bed else None,
            nozzle_count=nozzles,
            profiles=_profiles(where, entry.get("profiles", {}), fail),
            materials=tuple(materials),
            bed_type=_bed_type(where, entry.get("bed_type"), fail),
            extra=_extra(where, entry.get("extra", {}), fail),
        )
    return printers


def _bambuddy_target(bb: BambuddyConfig) -> ModuleConfig:
    """The legacy [bambuddy] table as [targets.bambuddy] with per-model defaults."""
    values: dict[str, Any] = {
        "url": bb.base_url,
        "folder": bb.folder,
        "manual_start": bb.manual_start,
    }
    if bb.public_url:
        values["public_url"] = bb.public_url
    models = {
        model: ModelDefaults(
            slicer="",  # "" = BamBuddy slices for itself
            profiles=Profiles(p.printer, p.process, p.filament),
            bed_type=p.bed_type,
            extra={"preset_source": p.source},
        )
        for model, p in bb.presets.items()
    }
    return ModuleConfig("bambuddy", "bambuddy", values, models)


def _check_pairs(
    desktop: dict[str, SlicerConfig],
    slicers: dict[str, ModuleConfig],
    targets: dict[str, ModuleConfig],
    printers: dict[str, PrinterConfig],
    fail: Fail,
) -> None:
    """Every slicer named for a printer exists and can feed the printer's target."""
    from os2slice.modules.registry import spec_for

    for key in slicers:
        if key in targets and spec_for(targets[key].kind).role == "both":
            raise fail(f"[slicers.{key}] clashes with [targets.{key}], which slices too")

    def pair(where: str, slicer: str, target_key: str) -> None:
        tspec = spec_for(targets[target_key].kind)
        if not slicer:
            if tspec.role != "both":
                raise fail(f"{where} needs a slicer ({tspec.label} doesn't slice)")
            return
        if slicer in slicers:
            sspec = spec_for(slicers[slicer].kind)
        elif slicer in targets and spec_for(targets[slicer].kind).role == "both":
            sspec = spec_for(targets[slicer].kind)
        elif slicer in desktop:
            raise fail(f"{where}.slicer: {slicer!r} opens a desktop slicer and can't slice here")
        else:
            raise fail(f"{where}.slicer: no [slicers.{slicer}] is configured")
        if not set(sspec.makes) & set(tspec.accepts):
            raise fail(
                f"{where}: {sspec.label} makes {', '.join(sspec.makes) or 'nothing'}, but "
                f"{tspec.label} takes {', '.join(tspec.accepts) or 'nothing'}"
            )
        if sspec.technology != tspec.technology:
            raise fail(
                f"{where}: {sspec.label} is {sspec.technology}, {tspec.label} is {tspec.technology}"
            )
        if (sspec.pairs_only_with and tspec.kind not in sspec.pairs_only_with) or (
            tspec.pairs_only_with and sspec.kind not in tspec.pairs_only_with
        ):
            raise fail(f"{where}: {sspec.label} and {tspec.label} only work with each other")

    for key, t in targets.items():
        for model, d in t.models.items():
            pair(f'targets.{key}.models."{model}"', d.slicer, key)
    discovering = [k for k, t in targets.items() if spec_for(t.kind).discovers_printers]
    for key, p in printers.items():
        where = f'printers."{key}"'
        if p.target is None:
            if not discovering:
                raise fail(f"{where} needs a target (no configured target discovers printers)")
            if p.slicer:
                # An override applies to whichever target finds a printer of that name, so
                # its slicer needs to suit one of them, not all (BamBuddy beside PreForm).
                errors: list[ConfigError] = []
                for t in discovering:
                    try:
                        pair(where, p.slicer, t)
                    except ConfigError as e:
                        errors.append(e)
                if len(errors) == len(discovering):
                    raise errors[0]
            continue
        if p.target not in targets:
            raise fail(f"{where}.target: no [targets.{p.target}] is configured")
        if p.slicer or not spec_for(targets[p.target].kind).discovers_printers:
            pair(where, p.slicer, p.target)
