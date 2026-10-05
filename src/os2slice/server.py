"""`os2slice serve`: the confirmation page and print jobs (Phase 4).

Security rules (docs/ARCHITECTURE.md, D-11, D-13):
- Every request: Host must be one of server.hosts; with identity = "tailscale",
  Tailscale-User-Login must be in server.allowed_users.
- GET never uploads, slices or prints. It only reads to build the page.
- POST /print needs a single-use CSRF token from that page, Sec-Fetch-Site:
  same-origin and a matching Origin. Only then does a job start.
"""

from __future__ import annotations

import functools
import hashlib
import html
import http.cookies
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from os2slice import __version__, admin, extra_settings, files, printing
from os2slice.adminauth import AdminStore, admin_path
from os2slice.auth import get_secret
from os2slice.config import DESKTOP_SLICERS, Config, WebStudioConfig
from os2slice.config import load as load_config
from os2slice.errors import AuthError, BadRequest, ConfigError, Os2sliceError
from os2slice.filament_icons import SELECTED_BUTTON, option_content
from os2slice.jobs import CsrfTokens, Job, JobStore
from os2slice.modules import bambu_project
from os2slice.modules.base import Material, PrinterInfo, PrinterStatus
from os2slice.modules.registry import Modules, SecretLookup
from os2slice.oauth import (
    SESSION_DAYS,
    BearerAuth,
    Grant,
    GrantStore,
    OAuthSettings,
    SignIns,
    TokenEndpoint,
    UserTokens,
)
from os2slice.oauth import static_bearer as oauth_static_bearer
from os2slice.onshape import OnshapeClient
from os2slice.orientation import FACE_ID_RE, Orientation
from os2slice.redirect import make_redirect_server
from os2slice.request import PART_ID_RE, ExportRequest, parse_print_query
from os2slice.settings import (
    COPIES_RANGE,
    PLATE_LABELS,
    SHELL_RANGE,
    SUPPORTS,
    PrintSettings,
    check_bed_type,
)

log = logging.getLogger(__name__)

MAX_BODY = 16_384
REQUEST_FIELDS = ("d", "wv", "wvid", "e", "p", "c")
FORM_FIELDS = frozenset(
    {
        *REQUEST_FIELDS,
        "csrf",
        "claim",  # /auth/claim (D-23)
        "printer",
        "filament",  # the panel's own filament menu: a Material.id, "" = preset
        "filament_tool",  # its "Load into" menu: the changer tool for a filament not loaded
        "process",  # the panel's process menu: one of the user's own profiles, "" = preset
        "machine",  # the panel's printer profile menu: the same, for printer profiles
        "orient",
        "walls",
        "infill",
        "supports",
        "build_plate_only",
        "top_layers",
        "bottom_layers",
        "brim",
        "copies",
        "face",
        "plate",
        "extra",
        "manual_start",  # the Wait for Start checkbox: absent = start by itself, "on" = wait
        *(f"x_{key}" for key in extra_settings.BY_KEY),  # extra settings (empty = profile's)
    }
)
CHECKBOXES = ("build_plate_only", "brim")  # an unchecked box isn't sent at all
# Material ids are module-specific (a global tray id on BamBuddy) but always this shape.
MATERIAL_ID = r"[A-Za-z0-9_.-]{1,40}"
EXTRA_RE = re.compile(rf"([A-Za-z0-9_+\-]{{1,32}}):({MATERIAL_ID})")
MAX_PRINTER_LEN = 100
TOOL_ID_RE = re.compile(r"t\d{1,2}")  # a changer tool's Material.id (moonraker.py)
MAX_PROFILE_LEN = 200  # a process profile name from the panel
MAX_PARTS = 16
ORIENT_CHOICES = [
    ("as-modeled", "As modeled (bottom face down)"),
    ("auto", "Let the slicer choose (auto-orient)"),
    ("z+", "Top (+Z) face down"),
    ("x+", "+X side down"),
    ("x-", "-X side down"),
    ("y+", "+Y side down"),
    ("y-", "-Y side down"),
]
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null` on our
    # own form POST, which the Origin check (rightly) refuses.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
STATIC_FILES = {  # the only files /static/ serves
    "panel.js",
    "preview.js",
    "auth.js",
    "vendor/three.module.js",
    "vendor/three.core.js",
    "vendor/STLLoader.js",
    "vendor/OrbitControls.js",
}
MODEL_PATH_RE = re.compile(r"/models/([A-Za-z0-9_-]{22})/os2slice\.3mf")
MODEL_LINK_TTL = 900  # seconds a Bambu Studio download link stays valid
KNOWN_PATHS = frozenset(
    {"/print", "/health", "/panel", "/panel/print", "/panel/preview", "/panel/model-link"}
    | {"/panel/web-studio", "/panel/web-studio/status"}
    | {"/panel/web-orca", "/panel/web-orca/status", "/panel/face-part"}
    | {"/auth/start", "/auth/callback", "/auth/claim", "/auth/sign-out"}
    | {f"/static/{f}" for f in STATIC_FILES}
)
PREVIEW_FIELDS = frozenset({*REQUEST_FIELDS, "face", "orient", "extra", "only"})
# The shared browser sessions a part can be sent to: route name -> (config field, label).
WEB_APPS = {"web-studio": ("web_studio", "Bambu Studio"), "web-orca": ("web_orca", "OrcaSlicer")}
WEB_STATUS_PATHS = {f"/panel/{app}/status": app for app in WEB_APPS}
WEB_ROUTES = {"bambu-studio": "web-studio", "orcaslicer": "web-orca"}  # by slicer name
DEFAULT_BED = (256, 256)  # the preview's bed when a printer doesn't say (PrinterInfo.bed_mm)
JOB_PATH_RE = re.compile(r"/jobs/([A-Za-z0-9_-]{22})")

OnshapeFactory = Callable[[], OnshapeClient]
OnshapeAsFactory = Callable[[httpx.Auth], OnshapeClient]  # a signed-in user's client (D-23)
SESSION_COOKIE = "os2s"  # top-level pages: the /print tab and the sign-in window
PANEL_COOKIE = "os2s_p"  # the panel inside Onshape (partitioned third-party cookie)
STATE_COOKIE = "os2s_state"
SESSION_MAX_AGE = SESSION_DAYS * 86400
NEXT_RE = re.compile(r"/(print|panel)\?[A-Za-z0-9%=&._~+-]{0,1500}")


class Forbidden(Os2sliceError):
    exit_code = 2
    http_status = 403


class NotSignedIn(AuthError):
    http_status = 401


@dataclass
class SignIn:
    """Per-user Onshape sign-in (D-23); None on the Service in API-key mode."""

    store: GrantStore
    tokens: UserTokens
    endpoint: TokenEndpoint
    pending: SignIns
    onshape_as: OnshapeAsFactory


def make_signin(
    cfg: Config,
    client_secret: str,
    store_path: Path,
    onshape_as: OnshapeAsFactory,
    transport: httpx.BaseTransport | None = None,
) -> SignIn:
    """Wire up per-user sign-in for `[onshape] auth = "oauth"` (D-23)."""
    settings = OAuthSettings(
        cfg.oauth_client_id, client_secret, cfg.oauth_redirect_uri, cfg.oauth_base_url
    )
    endpoint = TokenEndpoint(settings, transport)
    store = GrantStore(store_path)
    return SignIn(store, UserTokens(store, endpoint), endpoint, SignIns(), onshape_as)


class Service:
    """Everything a request handler needs; built once per process.

    `cfg` and `modules` are swapped together by `reload()` (the config page, D-28).
    Server and Onshape settings stay as they were at start (`boot_cfg`): the socket,
    the Host check and the Onshape client were made from them, so they need a restart.
    """

    def __init__(
        self,
        cfg: Config,
        onshape: OnshapeFactory,
        modules: Modules,
        signin: SignIn | None = None,
        *,
        admin_store: AdminStore | None = None,
        secrets: SecretLookup | None = None,
        module_transport: httpx.BaseTransport | None = None,
        module_transports: dict[str, httpx.BaseTransport] | None = None,
    ) -> None:
        self.cfg = cfg
        self.boot_cfg = cfg  # what the socket, Host check and Onshape client use
        self.file_cfg = cfg  # config.toml as last loaded (may differ: needs restart)
        self.onshape = onshape
        self.modules = modules  # slicers and targets (docs/MODULES.md)
        self.signin = signin
        self.jobs = JobStore()
        self.tokens = CsrfTokens()
        self.previews = PreviewCache()
        self.model_links: OrderedDict[str, tuple[float, tuple[Any, ...], bytes | None]] = (
            OrderedDict()
        )
        self.model_lock = threading.Lock()
        self.admin = admin_store if admin_store is not None else AdminStore(admin_path())
        self.secrets = secrets  # None: the secret store (auth.get_secret)
        self.module_transport = module_transport  # tests: replace the network
        self.module_transports = dict(module_transports or {})
        self.restart_notes: set[str] = set()  # e.g. new Onshape keys (read once at start)
        self._reload_lock = threading.Lock()

    def secret(self, name: str) -> str | None:
        return (self.secrets or get_secret)(name)

    def transport_for(self, key: str) -> httpx.BaseTransport | None:
        return self.module_transports.get(key, self.module_transport)

    def build_modules(self, cfg: Config, overlay: dict[str, str] | None = None) -> Modules:
        """The config's modules; `overlay` secrets win over the store (not saved yet)."""
        extra = overlay or {}

        def lookup(name: str) -> str | None:
            return extra.get(name) or self.secret(name)

        return Modules.from_config(
            cfg, secrets=lookup, transport=self.module_transport,
            transports=self.module_transports,
        )  # fmt: skip

    def reload(self) -> Config:
        """Re-read config.toml, rebuild the modules and swap both in. Returns the file's
        config. Raises (keeping the old config) if it doesn't load."""
        with self._reload_lock:
            new = load_config(self.cfg.path, create=False)
            modules = self.build_modules(new)
            boot = self.boot_cfg
            running = replace(
                new,
                server=boot.server,
                port=boot.port,
                onshape_base_url=boot.onshape_base_url,
                onshape_auth=boot.onshape_auth,
                oauth_client_id=boot.oauth_client_id,
                oauth_base_url=boot.oauth_base_url,
            )
            old = self.modules
            self.cfg, self.modules, self.file_cfg = running, modules, new
        log.warning("config reloaded from %s", new.path)
        self._retire(old)
        return new

    def _retire(self, old: Modules) -> None:
        """Close the replaced modules, unless a print job may still be using them."""
        if self.jobs.busy():
            log.info("a print job is running; the old modules are left to be collected")
            return
        old.close()

    def onshape_for(self, user_id: str | None) -> OnshapeClient:
        """API-key mode: the shared client. Sign-in mode: that user's own access."""
        if self.signin is None:
            return self.onshape()
        if user_id is None:
            raise NotSignedIn("Sign in with Onshape first", "Use the Sign in button in the panel")
        return self.signin.onshape_as(BearerAuth(self.signin.tokens, user_id))

    def require_printers(self) -> None:
        if not self.modules.targets:
            raise ConfigError("No printers are configured on the server", "Add a [targets.*] table")

    def start_note(self) -> str:
        """Where queued prints wait for Start: every target's name (D-13)."""
        return " or ".join(dict.fromkeys(t.spec.label for t in self.modules.targets.values()))


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        svc = service

    return Handler


def serve(service: Service, redirect_port: int | None = None) -> None:
    """Run the service. `redirect_port` overrides [server] redirect_port (the add-on
    passes its own); like the config value it needs identity = "lan"."""
    httpd = make_server(service)
    s = service.cfg.server
    if redirect_port is None:
        redirect_port = s.redirect_port
    elif s.identity != "lan" and redirect_port:
        log.error('ignoring redirect port %s: it needs identity = "lan"', redirect_port)
        redirect_port = 0
    scheme = "https" if s.tls_cert else "http"
    log.info("serving on %s://%s:%s (hosts %s, identity %s)", scheme, s.bind, s.port, s.hosts,
             s.identity)  # fmt: skip
    print(f"os2slice serving on {scheme}://{s.bind}:{s.port} for {', '.join(s.hosts)}")
    redirect = None
    if redirect_port:
        target = f"https://{s.hosts[0]}/admin"
        redirect = make_redirect_server(s.bind, redirect_port, target)
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        log.info("redirecting http://%s:%s to %s", s.bind, redirect_port, target)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if redirect is not None:
            redirect.shutdown()
            redirect.server_close()
        service.modules.close()  # the current ones; reload() closed those it replaced


def make_server(service: Service, port: int | None = None) -> ThreadingHTTPServer:
    s = service.cfg.server
    httpd = ThreadingHTTPServer((s.bind, s.port if port is None else port), make_handler(service))
    if s.tls_cert and s.tls_key:
        tls = TlsCertificate(s.tls_cert, s.tls_key)
        # Handshake lazily in the handler thread, so a slow client can't stall accept().
        httpd.socket = tls.context.wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False
        )
        threading.Thread(target=tls.watch, daemon=True).start()
    return httpd


class TlsCertificate:
    """Loads the certificate, and reloads it when DuckDNS renews the files (D-17)."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.cert, self.key = cert, key
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._stamp = self._mtimes()
        self.context.load_cert_chain(cert, key)

    def _mtimes(self) -> tuple[float, float]:
        return (self.cert.stat().st_mtime, self.key.stat().st_mtime)

    def reload_if_changed(self) -> bool:
        stamp = self._mtimes()
        if stamp == self._stamp:
            return False
        self.context.load_cert_chain(self.cert, self.key)  # applies to new connections
        self._stamp = stamp
        log.info("reloaded TLS certificate %s", self.cert)
        return True

    def watch(self, every: float = 3600.0) -> None:
        while True:
            time.sleep(every)
            try:
                self.reload_if_changed()
            except (OSError, ssl.SSLError) as e:
                log.error("TLS certificate reload failed (keeping the old one): %s", e)


class _Handler(BaseHTTPRequestHandler):
    svc: Service
    server_version = f"os2slice/{__version__}"
    sys_version = ""

    # -- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_PUT

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        user = "?"
        self._frame_origin = ""  # set by pages Onshape may frame (the panel)
        self._scripts = False
        self._cache = ""  # overrides Cache-Control (static files, previews)
        self._cookies: list[str] = []
        self._who: Grant | None = None  # signed-in Onshape user (D-23)
        try:
            if url.path == "/favicon.ico" and method == "GET":
                self._send(204, b"")
                return
            user = self._check_host_and_user()
            if self.svc.signin is not None:
                self._who = self._signed_in_user()
                if self._who is not None:
                    user = f"onshape:{self._who.user_id}"
            if url.path == "/admin" or url.path.startswith("/admin/"):
                admin.handle(self, method, url)  # the config page (D-28)
            elif url.path == "/health" and method == "GET":
                self._send(200, f'{{"ok": true, "version": "{__version__}"}}\n'.encode(),
                           "application/json")  # fmt: skip
            elif url.path == "/print" and method == "GET":
                self._get_print(url.query, user)
            elif url.path == "/print" and method == "POST":
                self._post_print(user, panel=False)
            elif url.path == "/auth/start" and method == "GET":
                self._get_auth_start(url.query)
            elif url.path == "/auth/callback" and method == "GET":
                self._get_auth_callback(url.query)
            elif url.path == "/auth/claim" and method == "POST":
                self._post_auth_claim()
            elif url.path == "/auth/sign-out" and method == "POST":
                self._post_sign_out()
            elif url.path == "/panel" and method == "GET":
                self._get_panel(url.query, user)
            elif url.path == "/panel/print" and method == "POST":
                self._post_print(user, panel=True)
            elif url.path in ("/panel/web-studio", "/panel/web-orca") and method == "POST":
                self._post_web_studio(url.path.removeprefix("/panel/"))
            elif url.path in WEB_STATUS_PATHS and method == "GET":
                self._get_web_studio_status(WEB_STATUS_PATHS[url.path])
            elif url.path == "/panel/model-link" and method == "POST":
                self._post_model_link()
            elif (m := MODEL_PATH_RE.fullmatch(url.path)) and method == "GET":
                self._get_model(m.group(1))
            elif url.path == "/panel/face-part" and method == "GET":
                self._get_face_part(url.query)
            elif url.path == "/panel/preview" and method == "GET":
                self._get_preview(url.query)
            elif url.path.startswith("/static/") and method == "GET":
                self._get_static(url.path.removeprefix("/static/"))
            elif (m := JOB_PATH_RE.fullmatch(url.path)) and method == "GET":
                self._get_job(m.group(1), user)
            elif url.path in KNOWN_PATHS or JOB_PATH_RE.fullmatch(url.path):
                self._method_not_allowed()
            else:
                self._page(404, "Not found", "<p>Nothing here.</p>")
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError) as e:
            # The browser stopped listening, e.g. the panel cancelled a preview download
            # when the selection changed; nothing more can be sent.
            log.info("%s %s: client went away (%s)", method, url.path, type(e).__name__)
        except Os2sliceError as e:
            log.warning("%s %s by %s: %s", method, url.path, user, e.one_line())
            self._error_page(e)
        except Exception:
            log.exception("%s %s by %s crashed", method, url.path, user)
            self._page(500, "Unexpected error", "<p>Something went wrong; see the server log.</p>")

    # -- checks ------------------------------------------------------------

    def _check_host_and_user(self) -> str:
        cfg = self.svc.cfg.server
        host = self.headers.get("Host", "")
        if host not in cfg.hosts:
            raise Forbidden(f"Wrong host {host[:60]!r}")
        if cfg.identity == "none":
            return "local"
        if cfg.identity == "lan":
            return "lan"  # open to the LAN by design (D-17)
        login = self.headers.get("Tailscale-User-Login", "")
        if login not in cfg.allowed_users:
            raise Forbidden("You're not allowed to use this service", "Ask Josh to add your login")
        return login

    def _uid(self) -> str | None:
        return self._who.user_id if self._who else None

    def _onshape(self) -> OnshapeClient:
        return self.svc.onshape_for(self._uid())

    def _require_navigation(self) -> None:
        dest = self.headers.get("Sec-Fetch-Dest")
        if dest != "document":
            raise Forbidden(
                "Open this page from Onshape's menu", "It can't be loaded inside another page"
            )

    def _require_same_origin_post(self) -> None:
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("Cross-site form submission refused")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin is not None and urlsplit(origin).netloc != host:
            raise Forbidden("Form came from another site")

    # -- per-user Onshape sign-in (D-23) ------------------------------------

    def _cookie(self, name: str) -> str:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _set_cookie(self, name: str, value: str, max_age: int, *, panel: bool = False,
                    path: str = "/") -> None:  # fmt: skip
        # The panel lives in Onshape's iframe: its cookie must be SameSite=None and is
        # Partitioned (CHIPS), so it works with third-party cookies otherwise blocked.
        site = "SameSite=None; Partitioned" if panel else "SameSite=Lax"
        self._cookies.append(
            f"{name}={value}; Path={path}; Max-Age={max_age}; Secure; HttpOnly; {site}"
        )

    def _signed_in_user(self) -> Grant | None:
        store = self.svc.signin.store  # type: ignore[union-attr]
        for name in (PANEL_COOKIE, SESSION_COOKIE):
            sid = self._cookie(name)
            if sid and (grant := store.session_user(sid)) is not None:
                return grant
        return None

    def _signin(self) -> SignIn:
        if self.svc.signin is None:
            raise ConfigError(
                "Sign-in isn't enabled on this server", 'Set [onshape] auth = "oauth"'
            )
        return self.svc.signin

    def _get_auth_start(self, query: str) -> None:
        """Top-level (popup or tab): remember where to go back, send the user to Onshape."""
        signin = self._signin()
        self._require_navigation()
        params = dict(parse_qsl(query, max_num_fields=2))
        next_path = params.get("next", "")
        if not NEXT_RE.fullmatch(next_path):
            next_path = ""
        state, nonce = signin.pending.start(next_path)
        # Binds the answer to this browser (no signing someone else in via a crafted link).
        self._set_cookie(STATE_COOKIE, nonce, 600, path="/auth/")
        settings = signin.endpoint.settings
        self.send_response(303)
        self.send_header("Location", settings.authorize_url(state))
        self._headers(0)

    def _get_auth_callback(self, query: str) -> None:
        signin = self._signin()
        self._require_navigation()
        params = dict(parse_qsl(query, max_num_fields=6))
        if "error" in params:
            raise NotSignedIn("Onshape sign-in was cancelled", "Try Sign in again")
        next_path = signin.pending.finish(params.get("state", ""), self._cookie(STATE_COOKIE))
        if next_path is None:
            raise Forbidden("This sign-in link has expired or isn't yours", "Try Sign in again")
        code = params.get("code", "")
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", code):
            raise BadRequest("Onshape sent back no sign-in code")
        tokens = signin.endpoint.exchange_code(code)
        with signin.onshape_as(oauth_static_bearer(tokens["access_token"])) as onshape:
            uid, name = onshape.whoami()
        signin.tokens.save_new(uid, name, tokens)
        sid = signin.store.new_session(uid)
        self._set_cookie(SESSION_COOKIE, sid, SESSION_MAX_AGE)
        self._set_cookie(STATE_COOKIE, "", 0, path="/auth/")
        claim = signin.pending.offer_claim(sid)
        log.warning("signed in: Onshape user %s", uid)
        self._scripts = True
        self._page(200, "Signed in", _signed_in_page(name, claim, next_path))

    def _post_auth_claim(self) -> None:
        """The panel (iframe) picks up the session its sign-in window just made."""
        signin = self._signin()
        self._require_same_origin_post()
        sid = signin.pending.redeem_claim(self._read_form().get("claim", ""))
        if sid is None or signin.store.session_user(sid) is None:
            raise Forbidden("That sign-in has expired", "Try Sign in again")
        self._set_cookie(PANEL_COOKIE, sid, SESSION_MAX_AGE, panel=True)
        self._send(200, b'{"ok": true}', "application/json")

    def _post_sign_out(self) -> None:
        """Ends this browser's sessions and forgets the user's Onshape grant."""
        signin = self._signin()
        self._require_same_origin_post()
        for name in (PANEL_COOKIE, SESSION_COOKIE):
            if sid := self._cookie(name):
                signin.store.end_session(sid)
        if self._who is not None:
            signin.store.drop_user(self._who.user_id)
        self._set_cookie(PANEL_COOKIE, "", 0, panel=True)
        self._set_cookie(SESSION_COOKIE, "", 0)
        self._send(200, b'{"ok": true}', "application/json")

    # -- /print ------------------------------------------------------------

    def _get_print(self, query: str, user: str) -> None:
        self._require_navigation()
        cfg = self.svc.cfg
        self.svc.require_printers()
        req = parse_print_query(query)
        if req.part_id is None:
            raise BadRequest("No part selected", "Right-click a part in the parts list")
        if self.svc.signin is not None and self._who is None:
            start = "/auth/start?" + urlencode({"next": f"/print?{query}"[:1500]})
            self._page(401, "Sign in", _sign_in_page(start))
            return
        with self._onshape() as onshape:
            doc = onshape.get_document_name(req.document_id)
            part = onshape.get_part_name(req)
        views = self._printer_views()
        token = self.svc.tokens.issue(user, _bind(req))
        body = _print_form(req, doc, part, views, cfg, token, self.svc)
        self._page(200, f"Print {part}", body)

    def _post_print(self, user: str, panel: bool) -> None:
        self._require_same_origin_post()
        form = self._read_form()
        req, face, orientation, printer, material, extra, process, machine = _selection(form, panel)
        fields = {k: form[k] for k in REQUEST_FIELDS if k in form and form[k] != ""}
        settings = _form_settings(form, self.svc.cfg)
        plate = check_bed_type(form.get("plate"))
        wait = _wait_for_start(form)
        # Redeem last, so a typo in a setting doesn't burn the form. The panel's token
        # is bound to the Part Studio (the part comes from the live selection).
        bound = _bind_element(req) if panel else _bind(req)
        if not self.svc.tokens.redeem(form.get("csrf", ""), user, bound):
            raise Forbidden("This form has expired or was already used", "Reload it from Onshape")
        cfg = self.svc.cfg

        uid = self._uid()
        mods = self.svc.modules

        def work(progress: Callable[[str], None]) -> list[str]:
            with self.svc.onshape_for(uid) as onshape:
                job_req = req
                if job_req.part_id is None:
                    job_req = replace(req, part_id=onshape.part_of_face(req, face))
                plan = printing.plan_print(
                    job_req, cfg, onshape, mods, printer, orientation, settings, material, plate,
                    extra, process or None, machine or None, wait,
                )  # fmt: skip
                progress("Checked the part and the printer")
                out = printing.execute_print(
                    plan, cfg, onshape, mods, queue=True, progress=progress
                )
            s = out.slice
            lines = plan.summary_lines()
            minutes = f"{s.print_time_s // 60} min" if s.print_time_s else "?"
            lines.append(f"Sliced:      {minutes}, {s.material_g or 0:.1f} g")
            item = out.submission.id if out.submission else "?"
            lines.append(f"Queued:      item {item} on {plan.printer.name}")
            return lines

        back = "/panel?" + urlencode({k: v for k, v in fields.items() if k != "p"}) if panel else ""
        job = self.svc.jobs.start(user, f"Part on printer {printer}", work, back=back)
        log.warning("print job %s started by %s for %s", job.id, user, req)
        self.send_response(303)
        self.send_header("Location", f"/jobs/{job.id}")
        self._headers(0)

    # -- /panel (Element right panel iframe, D-15) ----------------------------

    def _get_panel(self, query: str, user: str) -> None:
        cfg = self.svc.cfg
        self._frame_origin = cfg.onshape_base_url
        if self.headers.get("Sec-Fetch-Dest") != "iframe":
            raise Forbidden("This page only works inside Onshape's right panel")
        self.svc.require_printers()
        params = dict(parse_qsl(query, keep_blank_values=True))
        if params.get("server", cfg.onshape_base_url) != cfg.onshape_base_url:
            raise Forbidden("This panel only works with the configured Onshape server")
        req = parse_print_query(query)
        if self.svc.signin is not None and self._who is None:
            self._scripts = True
            self._page(200, "Sign in", _sign_in_panel())
            return
        try:
            with self._onshape() as onshape:
                parts = {str(p.get("partId")): str(p.get("name")) for p in onshape.list_parts(req)}
        except AuthError:
            if self.svc.signin is None:
                raise
            # Onshape refused the saved sign-in (revoked, e.g. after the app changed owner);
            # the grant is gone, so offer a fresh sign-in rather than an error page.
            self._scripts = True
            self._page(200, "Sign in", _sign_in_panel())
            return
        views = self._printer_views()
        token = self.svc.tokens.issue(user, _bind_element(req))
        self._scripts = True
        links = self.svc.modules.ui_links(self.headers.get("Host", ""))
        body = _panel_body(req, parts, views, cfg, token, self.svc, links)
        if self._who is not None:
            body = (_who_line(self._who.name) + body
                    + f'<script src="/static/auth.js?v={static_version()}"></script>')  # fmt: skip
        self._page(200, "Print", body)

    def _printer_views(self) -> list[PrinterView]:
        """Active printers of every target, with their state and loaded materials.

        Printers without profiles are listed (to say so) but their status isn't read.
        """
        mods = self.svc.modules
        views = []
        for p in mods.printers():
            if not p.active:
                continue
            if not printing.has_profiles(p):
                views.append(PrinterView(p, "", False, ()))
                continue
            status = mods.target_for(p).status(p)
            own = mods.own_profiles(p)
            materials = printing.materials_of(p, status, own, mods.pool_presets(p))
            views.append(PrinterView(p, _state(status), True, materials, own.process,
                                     own.printer, own.mmu_printers))  # fmt: skip
        return views

    # -- Open in Bambu Studio ------------------------------------------------

    def _post_model_link(self) -> None:
        """A 15-minute download URL for the current selection as a 3MF (read-only)."""
        self._require_same_origin_post()
        form = self._read_form()
        # The link is fetched by Bambu Studio without cookies: it carries the user (D-23).
        selection = (_selection(form, panel=True), *self._studio_settings(form), self._uid())
        token = secrets.token_urlsafe(16)
        with self.svc.model_lock:
            links = self.svc.model_links
            links[token] = (time.monotonic() + MODEL_LINK_TTL, selection, None)
            while len(links) > 200:
                links.popitem(last=False)
        scheme = "https" if self.svc.cfg.server.tls_cert else "http"
        url = f"{scheme}://{self.headers.get('Host')}/models/{token}/os2slice.3mf"
        self._send(200, json.dumps({"url": url}).encode(), "application/json")

    # -- the shared web Bambu Studio session (D-21) ---------------------------

    def _studio_settings(self, form: dict[str, str]) -> tuple[PrintSettings, str | None]:
        return _form_settings(form, self.svc.cfg), check_bed_type(form.get("plate"))

    def _studio_project(
        self, selection: Selection, settings: PrintSettings, plate: str | None, uid: str | None
    ) -> tuple[bytes, printing.PrintPlan]:
        """The selection as a Bambu Studio project, sliced (not queued) for its settings."""
        req, face, orientation, printer, material, extra, process, machine = selection
        cfg, mods = self.svc.cfg, self.svc.modules
        with self.svc.onshape_for(uid) as onshape:
            if req.part_id is None:
                req = replace(req, part_id=onshape.part_of_face(req, face))
            plan = printing.plan_print(
                req, cfg, onshape, mods, printer, orientation, settings, material, plate, extra,
                process or None, machine or None,
            )  # fmt: skip
            return printing.studio_project(plan, cfg, onshape, mods), plan

    def _web_studio(self, app: str) -> WebStudioConfig:
        field, label = WEB_APPS[app]
        ws: WebStudioConfig | None = getattr(self.svc.cfg, field)
        if ws is None:
            raise ConfigError(f"The web {label} isn't set up", f"Add [{field}] to the config")
        return ws

    def _get_web_studio_status(self, app: str) -> None:
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("Only for the print panel")
        ws = self._web_studio(app)
        self._send(200, json.dumps(web_studio_status(ws)).encode(), "application/json")

    def _post_web_studio(self, app: str) -> None:
        """Hand the selection's 3MF to the shared session; the tab opens client-side."""
        self._require_same_origin_post()
        ws = self._web_studio(app)
        form = self._read_form()
        selection = _selection(form, panel=True)
        data, plan = self._studio_project(selection, *self._studio_settings(form), self._uid())
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{files.sanitize(plan.part_name, 'part')}-{stamp}-{secrets.token_hex(2)}.3mf"
        ws.inbox.mkdir(parents=True, exist_ok=True)
        tmp = ws.inbox / f".{name}.part"
        tmp.write_bytes(data)
        os.replace(tmp, ws.inbox / name)  # the watcher only picks up finished *.3mf
        log.info("handed %s to the web %s", name, WEB_APPS[app][1])
        self._send(200, json.dumps({"url": ws.url, "file": name}).encode(), "application/json")

    def _get_model(self, token: str) -> None:
        """Serve the 3MF to Bambu Studio. The token is the credential (no browser headers)."""
        with self.svc.model_lock:
            entry = self.svc.model_links.get(token)
        if entry is None or entry[0] < time.monotonic():
            self._page(404, "Link expired", "<p>Open it again from the Onshape panel.</p>")
            return
        expires, selection, data = entry
        if data is None:
            data, _ = self._studio_project(*selection)
            with self.svc.model_lock:
                self.svc.model_links[token] = (expires, selection, data)
        self.send_response(200)
        self.send_header("Content-Type", "model/3mf")
        self.send_header("Content-Disposition", 'attachment; filename="os2slice.3mf"')
        self._headers(len(data))
        self.wfile.write(data)

    # -- /panel/preview and /static ------------------------------------------

    def _get_face_part(self, query: str) -> None:
        """{"part": id, "name": name}: the part a face picked on its own belongs to, so the
        panel can name it. Read-only; same-origin fetch only."""
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("Only for the print panel")
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError as e:
            raise BadRequest("Malformed query string") from e
        params: dict[str, str] = {}
        for k, v in pairs:
            if (k not in REQUEST_FIELDS and k != "face") or k in params:
                raise BadRequest(f"Unexpected parameter {k[:30]!r}")
            params[k] = v
        face = params.get("face", "")
        if not FACE_ID_RE.fullmatch(face):
            raise BadRequest("Invalid face ID")
        fields = {k: v for k, v in params.items() if k in REQUEST_FIELDS and v != ""}
        req = parse_print_query(urlencode(fields))
        with self._onshape() as onshape:
            part = onshape.part_of_face(req, face)
            names = {str(p.get("partId")): str(p.get("name")) for p in onshape.list_parts(req)}
        body = {"part": part, "name": names.get(part, part)}
        self._cache = "private, max-age=300"
        self._send(200, json.dumps(body).encode(), "application/json")

    def _get_preview(self, query: str) -> None:
        """The part as STL in the chosen orientation. Read-only; same-origin fetch only."""
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("The preview is only for the print panel")
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        except ValueError as e:
            raise BadRequest("Malformed query string") from e
        params: dict[str, str] = {}
        for k, v in pairs:
            if k not in PREVIEW_FIELDS or k in params:
                raise BadRequest(f"Unexpected parameter {k[:30]!r}")
            params[k] = v
        fields = {k: v for k, v in params.items() if k in REQUEST_FIELDS and v != ""}
        req = parse_print_query(urlencode(fields))
        face = params.get("face", "")
        if face and not FACE_ID_RE.fullmatch(face):
            raise BadRequest("Invalid face ID")
        orient = params.get("orient", "as-modeled")
        orientation = (
            Orientation("face", face)
            if orient == "face" and face
            else Orientation.parse("as-modeled" if orient == "face" else orient)
        )
        if req.part_id is None and not face:
            raise BadRequest("Select a part")
        extra = [p for p in params.get("extra", "").split(",") if p]
        if len(extra) >= MAX_PARTS or not all(PART_ID_RE.fullmatch(p) for p in extra):
            raise BadRequest("Invalid part list")
        only = params.get("only", "")
        # Per user: a cached preview must never reach someone who can't open the part.
        key = (f"{self._uid() or ''}|{_bind(req)}|{','.join(extra)}|{face}|"
               f"{orientation.kind}:{orientation.value}")  # fmt: skip
        if not extra:
            stl = self.svc.previews.get(key)
            if stl is None:
                with self._onshape() as onshape:
                    if req.part_id is None:
                        req = replace(req, part_id=onshape.part_of_face(req, face))
                    stl = printing.oriented_stl(orientation, onshape, req)
                self.svc.previews.put(key, stl)
        else:
            ids = [req.part_id or "", *extra]
            if only not in ids:
                raise BadRequest("Unknown part for the preview")
            stl = self.svc.previews.get(f"{key}|{only}")
            if stl is None:
                with self._onshape() as onshape:
                    placed = printing.oriented_parts(orientation, onshape, req, ids)
                for part_id, data in zip(ids, placed, strict=True):
                    self.svc.previews.put(f"{key}|{part_id}", data)
                stl = placed[ids.index(only)]
        self._cache = "private, max-age=300"
        self._send(200, stl, "model/stl")

    def _get_static(self, name: str) -> None:
        if name not in STATIC_FILES:
            self._page(404, "Not found", "<p>Nothing here.</p>")
            return
        body = resources.files("os2slice").joinpath("static", *name.split("/")).read_bytes()
        self._cache = "public, max-age=86400"
        self._send(200, body, "text/javascript; charset=utf-8")

    def _read_form(self) -> dict[str, str]:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype != "application/x-www-form-urlencoded":
            raise BadRequest("Expected a form submission")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as e:
            raise BadRequest("Missing Content-Length") from e
        if not 0 < length <= MAX_BODY:
            raise BadRequest("Form too large")
        raw = self.rfile.read(length).decode("utf-8", errors="strict")
        try:
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=40)
        except ValueError as e:
            raise BadRequest("Malformed form") from e
        form: dict[str, str] = {}
        for k, v in pairs:
            if k not in FORM_FIELDS or k in form:
                raise BadRequest(f"Unexpected form field {k[:30]!r}")
            form[k] = v
        return form

    # -- /jobs ---------------------------------------------------------------

    def _get_job(self, job_id: str, user: str) -> None:
        job = self.svc.jobs.get(job_id, user)
        if job is None:
            self._page(404, "Job not found", "<p>No such job (jobs are kept in memory).</p>")
            return
        if self.headers.get("Sec-Fetch-Dest") == "iframe" and job.back:
            self._frame_origin = self.svc.cfg.onshape_base_url
        links = self.svc.modules.ui_links(self.headers.get("Host", ""))
        self._page(200, "Print job", _job_body(job, links), refresh=not job.finished)

    # -- responses -----------------------------------------------------------

    def _error_page(self, e: Os2sliceError) -> None:
        fix = f"<p>{_e(e.fix)}</p>" if e.fix else ""
        self._page(e.http_status, "Can't do that", f"<p><b>{_e(e.message)}</b></p>{fix}")

    def _page(self, status: int, title: str, body: str, refresh: bool = False) -> None:
        doc = _layout(title, body, refresh).encode()
        self._send(status, doc, "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self._headers(len(body))
        self.wfile.write(body)

    def _headers(self, length: int) -> None:
        frame = getattr(self, "_frame_origin", "") or "'none'"
        script = (
            "; script-src 'self'; connect-src 'self'" if getattr(self, "_scripts", False) else ""
        )
        self.send_header("Content-Security-Policy", f"{CSP}{script}; frame-ancestors {frame}")
        cache = getattr(self, "_cache", "")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, cache if (k == "Cache-Control" and cache) else v)
        for cookie in getattr(self, "_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _method_not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self._headers(0)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)


# -- rendering -------------------------------------------------------------------


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _bind(req: ExportRequest) -> str:
    return "|".join((req.document_id, req.wvm, req.wvm_id, req.element_id, req.part_id or "",
                     req.configuration))  # fmt: skip


class PreviewCache:
    """A few recent oriented STLs, so flipping options doesn't re-export from Onshape."""

    def __init__(self, entries: int = 16, max_bytes: int = 64_000_000) -> None:
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._entries, self._max_bytes = entries, max_bytes

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._entries or (
                sum(map(len, self._items.values())) > self._max_bytes and len(self._items) > 1
            ):
                self._items.popitem(last=False)


def _bind_element(req: ExportRequest) -> str:
    parts = (req.document_id, req.wvm, req.wvm_id, req.element_id, req.configuration)
    return "panel|" + "|".join(parts)


def _state(status: PrinterStatus) -> str:
    if not status.connected:
        return "offline"
    return status.describe().lower()


def _options(choices: Iterable[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{_e(v)}"{" selected" if v == selected else ""}>{_e(label)}</option>'
        for v, label in choices
    )


def _print_form(
    req: ExportRequest,
    doc: str,
    part: str,
    views: list[PrinterView],
    cfg: Config,
    token: str,
    svc: Service,
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_e(v)}">'
        for k, v in (
            ("d", req.document_id),
            ("wv", req.wvm),
            ("wvid", req.wvm_id),
            ("e", req.element_id),
            ("p", req.part_id or ""),
            ("c", req.configuration),
            ("csrf", token),
        )
    )
    config = (
        f"<dt>Configuration</dt><dd><code>{_e(req.configuration)}</code></dd>"
        if req.configuration
        else ""
    )
    return f"""
<dl><dt>Part</dt><dd>{_e(part)}</dd><dt>Document</dt><dd>{_e(doc)}</dd>{config}</dl>
<form method="post" action="/print">{hidden}
{_printer_select(views, svc.modules.default_printer)}
{_plate_select(cfg)}
<label>Orientation <select name="orient">{_options(ORIENT_CHOICES, "as-modeled")}</select></label>
{_settings_fields(cfg)}
{_wait_field(svc)}
<button type="submit">Slice and queue print</button>
</form>"""


def _wait_field(svc: Service) -> str:
    """The Wait for Start checkbox (D-13: the person printing chooses). Its initial state
    is `[print_defaults] wait_for_start` (default unchecked: prints start by themselves,
    D-33); the person's tick wins. A target's `manual_start` config value is deprecated
    and doesn't change it."""
    where = svc.start_note() or "the queue"
    checked = " checked" if svc.cfg.default_wait_for_start else ""
    return (
        f'<label class="check"><input type="checkbox" name="manual_start" value="on"{checked}>'
        f" Wait for Start in {_e(where)} (don't start by itself)</label>"
    )


def _wait_for_start(form: dict[str, str]) -> bool:
    """The Wait for Start checkbox: unchecked isn't sent at all."""
    value = form.get("manual_start")
    if value not in (None, "on"):
        raise BadRequest("Invalid Wait for Start choice")
    return value == "on"


def _plate_select(cfg: Config) -> str:
    label = PLATE_LABELS.get(cfg.default_bed_type or "", cfg.default_bed_type)
    default = f"Default: {label}" if cfg.default_bed_type else "Printer default"
    opts = _options([("", default), *PLATE_LABELS.items()], "")
    return f'<label>Build plate <select name="plate">{opts}</select></label>'


def _form_settings(form: dict[str, str], cfg: Config) -> PrintSettings:
    """Settings from one of our own forms, where a missing checkbox means unchecked, with
    the extra settings the config offers that were filled in."""
    settings = PrintSettings.from_strings({c: "" for c in CHECKBOXES} | form, cfg.print_defaults)
    extras = extra_settings.parse_form(form, cfg.panel_extras)
    return replace(settings, extras=extras) if extras else settings


def _extra_fields(cfg: Config) -> str:
    """The panel's extra settings ([panel] extra_settings), each empty: the profile's own."""
    fields = []
    for key in cfg.panel_extras:
        s = extra_settings.BY_KEY[key]
        unit = f" ({s.unit})" if s.unit else ""
        name = f"x_{key}"
        if s.kind in ("bool", "choice"):
            choices = s.choices if s.kind == "choice" else (("1", "On"), ("0", "Off"))
            options = _options((("", "From the profile"), *choices), "")
            control = f'<select name="{name}">{options}</select>'
        else:
            step = "1" if s.kind == "int" else "any"
            lo, hi = extra_settings.number_text(s.lo), extra_settings.number_text(s.hi)
            control = (
                f'<input type="number" name="{name}" min="{lo}" max="{hi}" step="{step}" '
                'placeholder="From the profile">'
            )
        title = f' title="{_e(s.help)}"' if s.help else ""
        fields.append(f"<label{title}>{_e(s.label)}{_e(unit)} {control}</label>")
    if not fields:
        return ""
    return (
        '<details class="extras"><summary>More settings</summary>' + "".join(fields) + "</details>"
    )


def _settings_fields(cfg: Config) -> str:
    d = cfg.print_defaults

    def checked(on: bool) -> str:
        return " checked" if on else ""

    supports = _options(((s, s.capitalize()) for s in SUPPORTS), d.supports)
    lo, hi = SHELL_RANGE
    return f"""<div class="row">
<label>Walls <input type="number" name="walls" min="1" max="10" value="{d.walls}" required></label>
<label>Infill % <input type="number" name="infill" min="0" max="100" value="{d.infill}" required>
</label><label>Supports <select name="supports">{supports}</select></label>
</div>
<label class="check"><input type="checkbox" name="build_plate_only" value="true"{checked(d.build_plate_only)}>
Supports from the build plate only</label>
<div class="row">
<label>Top layers <input type="number" name="top_layers" min="{lo}" max="{hi}" value="{d.top_layers}" required></label>
<label>Bottom layers <input type="number" name="bottom_layers" min="{lo}" max="{hi}" value="{d.bottom_layers}" required></label>
<label>Copies <input type="number" name="copies" min="{COPIES_RANGE[0]}" max="{COPIES_RANGE[1]}" value="{d.copies}" required></label>
</div>
<label class="check"><input type="checkbox" name="brim" value="true"{checked(d.brim)}> Brim</label>"""  # noqa: E501


@dataclass(frozen=True)
class PrinterView:
    printer: PrinterInfo
    state: str
    configured: bool  # has a slicer and profiles (else it's only listed as unusable)
    materials: tuple[Material, ...]  # loaded (or configured) materials, with their profiles
    processes: tuple[str, ...] = ()  # the user's own process profiles in its slicer
    machines: tuple[str, ...] = ()  # ... and printer profiles
    mmu_machines: tuple[str, ...] = ()  # the printer profiles that use a filament changer

    def matches(self, wanted: str) -> bool:
        return bool(wanted) and wanted in (self.printer.key, self.printer.name)


# part request, face, orientation, printer key or name, material id, extra (part, material),
# process profile and printer profile ("" = the printer's own)
Selection = tuple[ExportRequest, str, Orientation, str, str | None, list[tuple[str, str]], str, str]


def _selection(form: dict[str, str], panel: bool) -> Selection:
    """Validate what the page or panel selected: part(s), face, orientation, printer, slots."""
    fields = {k: form[k] for k in REQUEST_FIELDS if k in form and form[k] != ""}
    req = parse_print_query(urlencode(fields))
    face = form.get("face", "")
    if face and not FACE_ID_RE.fullmatch(face):
        raise BadRequest("Invalid face ID")
    orient = form.get("orient", "as-modeled")
    if orient == "face":
        if not face:
            raise BadRequest("Select the face to put on the bed")
        orientation = Orientation("face", face)
    else:
        orientation = Orientation.parse(orient)
    if not panel and req.part_id is None:
        raise BadRequest("No part selected", "Right-click a part in the parts list")
    if panel and req.part_id is None and not face:
        raise BadRequest("Select a part, or the face it should stand on")
    printer, material = _printer_choice(
        form.get("printer", ""), form.get("filament", ""), form.get("filament_tool", "")
    )
    extra = _parse_extra(form.get("extra", "")) if panel else []
    if extra and req.part_id is None:
        raise BadRequest("Select the parts to print")
    process = form.get("process", "") if panel else ""
    machine = form.get("machine", "") if panel else ""
    for what, value in (("process", process), ("printer profile", machine)):
        if len(value) > MAX_PROFILE_LEN or any(ord(c) < 32 for c in value):
            raise BadRequest(f"Invalid {what} choice")
    return req, face, orientation, printer, material, extra, process, machine


def web_studio_status(ws: WebStudioConfig, now: float | None = None) -> dict[str, Any]:
    """{"state": "free" | "busy" | "unknown", "viewers": n} from the add-on's status file."""
    now = time.time() if now is None else now
    try:
        data = json.loads(ws.status.read_text())
        viewers, updated = int(data["viewers"]), float(data["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown", "viewers": None}
    if now - updated > 15:  # the add-on writes it every 2 s
        return {"state": "unknown", "viewers": None}
    if data.get("app_running") is False:  # desktop up, Bambu Studio not (yet) running
        return {"state": "unknown", "viewers": viewers}
    return {"state": "busy" if viewers > 0 else "free", "viewers": viewers}


def _parse_extra(value: str) -> list[tuple[str, str]]:
    """'JKD:4,JLD:0' → [('JKD', '4'), ('JLD', '0')]: further parts and their material ids."""
    if not value:
        return []
    items = value.split(",")
    if len(items) >= MAX_PARTS:
        raise BadRequest(f"At most {MAX_PARTS} parts per print")
    out = []
    for item in items:
        m = EXTRA_RE.fullmatch(item)
        if not m:
            raise BadRequest("Invalid part/filament list")
        out.append((m.group(1), m.group(2)))
    return out


def _split_printer(value: str) -> tuple[str, str | None]:
    """'farm/2|4' → ('farm/2', '4'): a printer key and a material id; 'farm/2' → preset."""
    m = re.fullmatch(rf"(.+)\|({MATERIAL_ID})", value)
    return (m.group(1), m.group(2)) if m else (value, None)


def _printer_choice(printer: str, filament: str, tool: str = "") -> tuple[str, str | None]:
    """The page's combined 'key|material' menu, or the panel's printer + filament menus.

    `tool` is the panel's "Load into" menu: a changer tool for a filament that isn't
    loaded (printing.SLOT_RE, "t2.f-…", or "t2.preset" for the preset filament)."""
    if len(printer) > MAX_PRINTER_LEN:
        raise BadRequest("Invalid printer choice")
    key, material = _split_printer(printer)
    if filament:
        if material is not None or not re.fullmatch(MATERIAL_ID, filament):
            raise BadRequest("Invalid filament choice")
        material = filament
    if tool and not TOOL_ID_RE.fullmatch(material or ""):
        if not TOOL_ID_RE.fullmatch(tool):
            raise BadRequest("Invalid tool choice")
        material = f"{tool}.{material or 'preset'}"
        if not re.fullmatch(MATERIAL_ID, material):
            raise BadRequest("Invalid filament choice")
    return key, material


def _material_label(m: Material, printer: PrinterInfo) -> tuple[str, bool]:
    """(menu label, disabled) for a loaded material. A pool's materials without a nozzle
    (BamBuddy picks the printer, and so the trays, at dispatch; D-35) stay usable."""
    if printer.nozzle_count > 1 and m.extruder is None and not printer.pool:
        return f"{m.label} (nozzle unknown)", True
    if m.raw.get("empty"):
        return m.label, True  # an empty changer gate: "T3: empty"
    if not m.profile:
        return f"{m.label} (no matching preset)", True
    return m.label, False


def _default_choice(view: PrinterView) -> str:
    """Prefer a loaded material of the configured kind, then a lone one, then the preset.
    A pool's presets (not loaded anywhere) are never the default."""
    key = view.printer.key
    usable = [
        m for m in view.materials if printing.usable(m, view.printer) and not printing.is_preset(m)
    ]
    if not usable or not view.configured:
        return key
    for m in usable:
        if m.profile == view.printer.profiles.filament:
            return f"{key}|{m.id}"
    configured = view.printer.profiles.filament.upper()
    for m in usable:
        if m.kind and re.search(rf"\b{re.escape(m.kind.upper())}\b", configured):
            return f"{key}|{m.id}"
    return f"{key}|{usable[0].id}" if len(usable) == 1 else key


def _printer_select(views: list[PrinterView], default_printer: str) -> str:
    selected = next(
        (_default_choice(v) for v in views if v.matches(default_printer) and v.configured), ""
    )
    groups, skipped = [], []
    for v in views:
        if not v.configured:
            skipped.append(v.printer.name)
            continue
        name, key = v.printer.name, v.printer.key
        preset_label = v.printer.profiles.filament.split(" @")[0]
        opts: list[tuple[str, str, str, bool, str]] = []  # value, label, colour, disabled, icon
        for m in _menu_order(v):
            if m is None:
                opts.append((key, f"{name} · preset filament ({preset_label})", "", False, ""))
                continue
            label, disabled = _material_label(m, v.printer)
            icon = "" if m.raw.get("empty") else m.colour or ""  # an empty gate: no filament
            opts.append((f"{key}|{m.id}", f"{name} · {label}", m.colour or "", disabled, icon))
        html_opts = "".join(
            f'<option value="{_e(val)}"'
            + (f' data-color="{_e(color)}"' if color else "")
            + (" disabled" if disabled else "")
            + (" selected" if val == selected and not disabled else "")
            + f">{option_content(label, icon)}</option>"
            for val, label, color, disabled, icon in opts
        )
        groups.append(
            f'<optgroup label="{_e(f"{name} ({v.printer.model}), {v.state}")}">'
            f"{html_opts}</optgroup>"
        )
    note = (
        f'<p class="muted">No presets configured for: {_e(", ".join(skipped))}</p>'
        if skipped
        else ""
    )
    return (
        '<label>Printer and filament <select name="printer" class="fc" required>'
        f"{SELECTED_BUTTON}{''.join(groups)}"
        f"</select></label>{note}"
    )


def _menu_order(v: PrinterView) -> list[Material | None]:
    """A printer's filament menu in order; None is the configured preset filament.

    A printer: the preset, then each material. A pool: the filaments loaded across its
    printers, then every preset made for the model by name, the configured one (None)
    in its place among them, or first when it isn't one of them.
    """
    if not v.printer.pool:
        return [None, *v.materials]
    loaded = [m for m in v.materials if not printing.is_preset(m)]
    configured = v.printer.profiles.filament
    presets: list[Material | None] = [
        None if m.profile == configured else m for m in v.materials if printing.is_preset(m)
    ]
    if None not in presets:
        presets.insert(0, None)
    return [*loaded, *presets]


def _filament_choices(v: PrinterView) -> list[dict[str, Any]]:
    """The panel's filament menu for one printer (`_menu_order`)."""
    default = _split_printer(_default_choice(v))[1]
    preset_label = v.printer.profiles.filament.split(" @")[0]
    out: list[dict[str, Any]] = []
    for m in _menu_order(v):
        if m is None:
            out.append(
                {
                    "value": "",
                    "label": f"Preset filament ({preset_label})",
                    "default": default is None,
                    "tool": False,
                    "profile": v.printer.profiles.filament,
                }
            )
            continue
        label, disabled = _material_label(m, v.printer)
        tool = printing.is_tool(m)
        if tool and m.profile and not m.raw.get("empty"):
            label += f" → {m.profile}"  # the profile it was matched to
        out.append(
            {
                "value": m.id,
                "label": label,
                "color": m.colour or "",
                "disabled": disabled,
                "default": m.id == default and not disabled,
                "tool": tool,  # panel.js shows tools only with a changer printer profile
                "empty": bool(m.raw.get("empty")),  # an empty gate: where a new one goes
                "profile": m.profile,
            }
        )
    return out


def _machine_choices(v: PrinterView) -> list[dict[str, Any]]:
    """The panel's printer profile menu for one printer, like the process menu; each
    entry says whether that profile uses the filament changer ("mmu")."""
    if not v.machines:
        return []
    preset = v.printer.profiles.printer
    mmu = set(v.mmu_machines)
    out = [{"value": "", "label": preset, "default": True, "mmu": preset in mmu}]
    out += [
        {"value": n, "label": n, "default": False, "mmu": n in mmu}
        for n in v.machines
        if n != preset
    ]
    return out


def _process_choices(v: PrinterView) -> list[dict[str, Any]]:
    """The panel's process menu for one printer: its own process profiles, the configured
    one preselected (value "" means "the configured one"). Empty: the menu stays hidden."""
    if not v.processes:
        return []
    preset = v.printer.profiles.process
    out = [{"value": "", "label": preset, "default": True}]
    out += [{"value": n, "label": n, "default": False} for n in v.processes if n != preset]
    return out


def _panel_printer_selects(views: list[PrinterView], default_printer: str) -> str:
    """Separate printer and filament menus; panel.js refills the filament menu per printer.

    Both are keyed by PrinterInfo.key, which is also the printer menu's value.
    """
    usable = [v for v in views if v.configured]
    skipped = [v.printer.name for v in views if not v.configured]
    first = next((v for v in usable if v.matches(default_printer)), usable[0] if usable else None)
    printers = "".join(
        f'<option value="{_e(v.printer.key)}"{" selected" if v is first else ""}>'
        f"{_e(f'{v.printer.name} ({v.printer.model}), {v.state}')}</option>"
        for v in usable
    )
    choices = {v.printer.key: _filament_choices(v) for v in usable}
    processes = {v.printer.key: _process_choices(v) for v in usable}
    machines = {v.printer.key: _machine_choices(v) for v in usable}
    # Server-rendered options for the first printer, so the form is complete before JS runs.
    filaments = "".join(
        f'<option value="{_e(c["value"])}"'
        + (f' data-color="{_e(c["color"])}"' if c.get("color") else "")
        + (" disabled" if c.get("disabled") else "")
        + (" selected" if c["default"] else "")
        + f">{option_content(c['label'], '' if c.get('empty') else c.get('color'))}</option>"
        for c in (choices[first.printer.key] if first else [])
    )
    note = (
        f'<p class="muted">No presets configured for: {_e(", ".join(skipped))}</p>'
        if skipped
        else ""
    )
    machine_options = "".join(
        f'<option value="{_e(c["value"])}"{" selected" if c["default"] else ""}'
        f"{' data-mmu' if c['mmu'] else ''}>{_e(c['label'])}</option>"
        for c in (machines[first.printer.key] if first else [])
    )
    process_options = "".join(
        f'<option value="{_e(c["value"])}"{" selected" if c["default"] else ""}>'
        f"{_e(c['label'])}</option>"
        for c in (processes[first.printer.key] if first else [])
    )
    return (
        f'<label>Printer <select name="printer" required>{printers}</select></label>'
        f"<label{'' if machine_options else ' hidden'}>Printer profile "
        f'<select name="machine" data-choices="{_e(json.dumps(machines))}">'
        f"{machine_options}</select></label>"
        f'<label>Filament <select name="filament" class="fc" '
        f'data-choices="{_e(json.dumps(choices))}">{SELECTED_BUTTON}{filaments}</select></label>'
        '<label id="tool-slot" hidden>Load into <select name="filament_tool" class="fc">'
        f"{SELECTED_BUTTON}</select></label>"
        f"<label{'' if process_options else ' hidden'}>Process "
        f'<select name="process" data-choices="{_e(json.dumps(processes))}">'
        f"{process_options}</select></label>{note}"
    )


@functools.cache
def static_version() -> str:
    """A hash of the static files, so a deploy busts browsers' day-long cache of them."""
    h = hashlib.sha256()
    for name in sorted(STATIC_FILES):
        h.update(resources.files("os2slice").joinpath("static", *name.split("/")).read_bytes())
    return h.hexdigest()[:12]


def _panel_body(
    req: ExportRequest,
    parts: dict[str, str],
    views: list[PrinterView],
    cfg: Config,
    token: str,
    svc: Service,
    links: list[tuple[str, str]],
) -> str:
    ids = {"documentId": req.document_id, "workspaceId": req.wvm_id, "elementId": req.element_id}
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_e(v)}">'
        for k, v in (
            ("d", req.document_id),
            ("wv", req.wvm),
            ("wvid", req.wvm_id),
            ("e", req.element_id),
            ("c", req.configuration),
            ("p", ""),
            ("face", ""),
            ("extra", ""),
            ("csrf", token),
        )
    )
    orient = _options([("face", "Selected face down"), *ORIENT_CHOICES], "as-modeled")
    beds = {v.printer.key: v.printer.bed_mm or DEFAULT_BED for v in views}
    # The preview lays copies out like bambu_project.copy_offsets, with the same spacing.
    layout = {
        "gap": bambu_project.COPY_GAP,
        "brimGap": bambu_project.BRIM_GAP,
        "margin": bambu_project.BED_MARGIN,
    }
    open_links = "".join(
        f'<p class="links"><a href="{_e(url)}" target="_blank" rel="noopener">\n'
        f"Open {_e(label)}</a></p>"
        for label, url in links
    )
    return f"""<div id="panel" data-onshape="{_e(cfg.onshape_base_url)}"
 data-ids="{_e(json.dumps(ids))}" data-parts="{_e(json.dumps(parts))}"
 data-beds="{_e(json.dumps(beds))}" data-layout="{_e(json.dumps(layout))}">
<p id="selection" class="sel">Loading…</p>
{_studio_links(cfg)}
{open_links}
<div id="preview" class="preview">
<p id="preview-note" class="muted">Select a part to preview it.</p></div>
<form id="printform" method="post" action="/panel/print">{hidden}
{_panel_printer_selects(views, svc.modules.default_printer)}
<div id="extras"></div>
{_plate_select(cfg)}
<label>Orientation <select name="orient">{orient}</select></label>
{_settings_fields(cfg)}
{_extra_fields(cfg)}
{_wait_field(svc)}
<button type="submit" disabled>Slice and queue print</button>
</form></div>
<script src="/static/panel.js?v={static_version()}"></script>
<script type="module" src="/static/preview.js?v={static_version()}"></script>"""


def _sign_in_panel() -> str:
    return f"""<div id="signin">
<p>Sign in with your Onshape account to print from this panel. os2slice then reads parts
with your own access (read-only).</p>
<p><button type="button" id="signin-button">Sign in with Onshape</button></p>
<p id="signin-note" class="muted">A small Onshape window opens; allow pop-ups if asked.</p>
</div>
<script src="/static/auth.js?v={static_version()}"></script>"""


def _sign_in_page(start: str) -> str:
    return (
        "<p>os2slice reads parts with your own Onshape access. Sign in once, then you come "
        f'back here.</p><p><a href="{_e(start)}">Sign in with Onshape</a></p>'
    )


def _signed_in_page(name: str, claim: str, next_path: str) -> str:
    return f"""<div id="signed-in" data-claim="{_e(claim)}" data-next="{_e(next_path)}">
<p>Signed in to os2slice as <b>{_e(name)}</b>.</p>
<p class="muted">This window closes by itself. If it doesn't, close it and go back to Onshape.</p>
</div>
<script src="/static/auth.js?v={static_version()}"></script>"""


def _who_line(name: str) -> str:
    return (
        f'<p class="muted links">Signed in as {_e(name)} · '
        '<a href="#" id="signout-button">Sign out</a></p>'
    )


def _studio_links(cfg: Config) -> str:
    """The "Open in …" lines ([panel] web_slicer and local_slicer), one per slicer."""
    links: dict[str, list[str]] = {}
    for slicer in cfg.panel_web_slicers:
        app = WEB_ROUTES[slicer]
        ws: WebStudioConfig | None = getattr(cfg, WEB_APPS[app][0])
        if ws is None:
            continue
        links.setdefault(slicer, []).append(
            f'<a id="{app}-link" class="off web-app" href="{_e(ws.url)}" target="_blank" '
            f'rel="noopener" data-app="{app}" data-label="{_e(WEB_APPS[app][1])}">'
            f'in the browser</a> <span id="{app}-state" class="muted"></span>'
        )
    if cfg.panel_local_slicer:
        # Opened by the slicer's URL handler on the user's computer (D-21).
        links.setdefault(cfg.panel_local_slicer, []).append(
            f'<a id="studio-link" class="off" href="#" data-scheme="{cfg.panel_local_slicer}">'
            "on this computer</a>"
        )
    return "".join(
        f'<p class="links">Open in {DESKTOP_SLICERS[slicer]}: {" · ".join(parts)}</p>'
        for slicer, parts in links.items()
    )


def _job_body(job: Job, ui_links: Iterable[tuple[str, str]] = ()) -> str:
    steps = "".join(f"<li>{_e(s)}</li>" for s in job.steps) or "<li>Waiting to start…</li>"
    if job.state == "done":
        tail = "<h2>Queued ✓</h2><pre>" + _e("\n".join(job.result)) + "</pre>"
    elif job.state == "failed":
        fix = f"<p>{_e(job.fix)}</p>" if job.fix else ""
        tail = f"<h2>Failed</h2><p><b>{_e(job.error)}</b></p>{fix}"
    else:
        tail = '<p class="muted">Working… this page refreshes itself.</p>'
    links = []
    if job.back and job.finished:
        links.append(f'<a href="{_e(job.back)}">Print another</a>')
    for label, url in ui_links:
        text = f"Open {label}'s queue"
        links.append(f'<a href="{_e(url)}" target="_blank" rel="noopener">{_e(text)}</a>')
    back = f'<p class="links">{" · ".join(links)}</p>' if links else ""
    return f"<ol>{steps}</ol>{tail}{back}"


def _layout(title: str, body: str, refresh: bool) -> str:
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{_e(title)} · os2slice</title>
<style>
:root {{ --bg:#fff; --fg:#1b1d21; --muted:#5f6670; --line:#d5d9df; --accent:#0b6bcb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16181c; --fg:#e6e8eb; --muted:#9aa3ad; --line:#343a42; --accent:#5aa9ff; }}
}}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,sans-serif;
       max-width:34rem; margin:0 auto; padding:16px; }}
h1 {{ font-size:1.25rem; margin:.2rem 0 1rem; }} h2 {{ font-size:1.05rem; }}
dl {{ display:grid; grid-template-columns:auto 1fr; gap:.2rem .8rem; }} dt {{ color:var(--muted); }}
dd {{ margin:0; overflow-wrap:anywhere; }}
label {{ display:block; margin:.7rem 0 .2rem; }} label.check {{ display:flex; gap:.4rem; }}
[hidden] {{ display:none !important; }}
select,input[type=number] {{ display:block; width:100%; box-sizing:border-box;
  margin-top:.2rem; padding:.4rem; background:var(--bg); color:var(--fg);
  border:1px solid var(--line); border-radius:6px; }}
.row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:.6rem; }}
button {{ margin-top:1rem; width:100%; padding:.65rem; font-size:1rem; border:0; border-radius:6px;
  background:var(--accent); color:#fff; cursor:pointer; }}
.muted {{ color:var(--muted); font-size:.9rem; }} pre {{ white-space:pre-wrap; }}
.sel {{ padding:.5rem; border:1px solid var(--line); border-radius:6px; }}
.links {{ margin:.4rem 0; font-size:.9rem; }} a.off {{ opacity:.45; pointer-events:none; }}
.preview {{ position:relative; height:230px; margin-top:.6rem; border:1px solid var(--line);
  border-radius:6px; overflow:hidden; }}
.preview canvas {{ display:block; width:100%; height:100%; touch-action:none; }}
#preview-note {{ position:absolute; left:.5rem; bottom:.3rem; margin:0; pointer-events:none; }}
button:disabled {{ opacity:.45; cursor:default; }} a {{ color:var(--accent); }}
/* Filament menus (select.fc, filament_icons.py): the option text starts with a coloured
   emoji; with customizable selects, an exact-colour swatch replaces it. */
@supports (appearance: base-select) {{
  select.fc, select.fc::picker(select) {{ appearance:base-select; }}
  select.fc {{ display:flex; align-items:center; gap:.3rem; }}
  select.fc > button {{ all:unset; flex:1; min-width:0; }}
  select.fc selectedcontent {{ display:block; overflow:hidden; white-space:nowrap;
    text-overflow:ellipsis; }}
  select.fc::picker(select) {{ background:var(--bg); color:var(--fg);
    border:1px solid var(--line); border-radius:6px; }}
  select.fc .fc-emoji {{ display:none; }}
  select.fc .fc-swatch {{ display:inline-block; width:.85em; height:.85em; margin-right:.45em;
    vertical-align:-.08em; border-radius:3px; box-sizing:border-box;
    border:1px solid color-mix(in srgb, var(--fg) 40%, transparent); }}
}}
</style></head><body><h1>{_e(title)}</h1>{body}</body></html>"""
