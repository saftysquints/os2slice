# Architecture

os2slice is a CAD → print broker. The part comes from Onshape; a **slicer module** turns
it into a print file and a **target module** hands that file to a printer or a queue.
Both are bound per printer in config and can be edited on the config page `/admin`.
The module contract, the kinds, the config tables and the module checklist are in
[`MODULES.md`](MODULES.md); this page covers the service around them.

## The print service (deployed)

```
Onshape (browser, any machine on the printers' LAN)
  ├─ right-click part → new tab GET /print?d=…&wv=…&wvid=…&e=…&p=…&c=…   (confirmation page)
  └─ Element right panel → iframe GET /panel  ⇄ postMessage (applicationInit, SELECTION: parts + bed face)
       └─ https://<name>.duckdns.org:8443   (D-17: LAN address, HTTPS served by os2slice itself)
            └─ os2slice serve   (HA add-on on barnassistant, or Docker Compose; server.py)
                 ├─ check Host, identity, params, Sec-Fetch-*            (a GET changes nothing)
                 ├─ Onshape (read-only; per-user OAuth or one key pair): part + document names, preview
                 ├─ Modules (registry.py): every target's printers() + status(), materials
                 └─ page / panel: printer, filament, orientation, walls, infill, supports, copies
                         ──[POST + single-use CSRF token, same-origin]──► job (jobs.py, printing.py)
                                ├─ Onshape: export STL per part
                                ├─ orient: one rotation (face-down normal → −Z), drop to Z = 0
                                ├─ the printer's slicer module: SliceInput → slice() → SliceOutput
                                │     bambuddy (slices in BamBuddy) | bambu-studio-api / orca-slicer-api (sidecars)
                                │     | preform-server (PreForm Server container, .form for the Form 4)
                                ├─ the printer's target module: submit(start=False) → Submission
                                │     bambuddy (queue) | moonraker | prusalink | preform-server (Form 4 queue)
                                └─ /jobs/<id> shows progress and the result
  /admin   the config page (admin.py): modules, printers, defaults, secrets, doctor, jobs, log
```

- Deployed 2026-10-01: add-on 0.2.0 on barnassistant (Home Assistant OS) at
  `https://dm-print.duckdns.org:8443`, beside the BamBuddy and "Bambu Studio API" add-ons.
  Its config uses `[targets.bambuddy]` (migrated from the legacy `[bambuddy]` table on
  `/admin`) and the Bambu Studio API add-on as a `bambu-studio-api` slicer.
- A printer = a target + a slicer + profiles. Discovering targets (BamBuddy) list their
  printers and take per-model defaults (`[targets.<key>.models."<model>"]`); others are
  configured by hand (`[printers.<key>]`). Pairing rules and the config schema:
  [`MODULES.md`](MODULES.md).
- Modules call only their configured URL. Endpoint notes: [`BAMBUDDY_API.md`](BAMBUDDY_API.md),
  [`SLICERAPI_API.md`](SLICERAPI_API.md), [`PRINTER_APIS.md`](PRINTER_APIS.md),
  [`PREFORM_API.md`](PREFORM_API.md).
- PreForm Server (D-36) has no authentication and open CORS, so its port is never
  published: os2slice reaches it on the Supervisor's internal network
  (`local-preform-server`) or the Compose network (`preform-server`). Files pass through
  `/share/os2slice/preform`, one folder per job, removed when the job is done.
- `Service.reload()` swaps config and modules after an `/admin` save; server and Onshape
  settings apply after a restart (D-31).
- "Open in Bambu Studio" (D-21) slices with the printer's slicer and hands a project 3MF to
  the shared web Bambu Studio (`/share/os2slice/inbox`) or offers it as a download link.
- Safety model: D-13. Access: D-17. Sign-in: D-23. Packaging: D-12 (add-on), D-24 (Docker).
  Modules: D-27, D-29, D-30, D-36 (resin). Config page: D-28, D-31, D-32.

## Secondary: local-slicer mode (Phases 0–1)

```
os2slice send --slicer orca --url <Part Studio URL> [--part …]
os2slice handle 'http://localhost:8765/open?slicer=orca&d=…&wv=w&wvid=…&e=…&p=…&c=…'
  └─ pipeline.py
       ├─ validate params → ExportRequest   (request.py)
       ├─ keyring → Onshape API keys
       ├─ Onshape REST API → part name + STL bytes
       ├─ write ~/OnshapeExports/<doc>/<part>_<cfg>_<ts>.stl
       ├─ Popen([slicer argv…, file])   (detached; argv from [slicers.<key>], slicers.py)
       └─ notify-send "Sent <part> to OrcaSlicer" (or the error + fix)
```

The only outbound traffic is the Onshape API. Nothing is hosted. D-3 chose a localhost
listener (`http://localhost:8765/open?…`, opened by an Onshape extension) over a custom
`os2slice://` scheme. **In this tree the listener isn't wired up:** `os2slice serve` is
the print service and has no `/open` route, and there is no `install-service` subcommand
or `platform/` package. `handle` parses such a URL and runs the same pipeline, so the
listener only needs a route and a user service. Its HTTP-layer rules (D-9) are kept below
for when it is.

## Module layout (current)

```
src/os2slice/
  __init__.py
  __main__.py     python -m os2slice
  cli.py          argparse entry point: send, print, serve, handle, setup-keys, doctor, admin-password
  addon.py        Home Assistant add-on entry point (os2slice-addon): options → config.toml, secrets, admin password
  request.py      query params / Onshape browser URL → ExportRequest (validation lives here)
  pipeline.py     local mode: ExportRequest → export → save → launch → notify; returns a Result (send, handle)
  server.py       stdlib ThreadingHTTPServer: routing, Host/identity/Sec-Fetch checks, /print, /panel, /jobs, sign-in, TLS reload
  redirect.py     optional plain-HTTP listener ([server] redirect_port) that only 303s to https://<hosts[0]>/admin
  admin.py        the config page /admin: forms that edit config.toml, secrets, doctor, jobs, log (D-28, D-31)
  adminauth.py    admin password (scrypt hash in admin.json), login rate limit, admin sessions
  tomlwrite.py    small TOML writer for our own schema + atomic file writes (used by admin.py)
  config.py       TOML config load/default/validate, including [slicers.*], [targets.*], [printers.*]
  default_config.toml  the config written on first run (same text as os2slice.example.toml)
  auth.py         secret store: keyring, else secrets.json (add-on, Docker), then the environment
  oauth.py        per-user Onshape sign-in (D-23): authorization code flow, signins.json
  onshape.py      httpx client: part name, document name, STL export
  orientation.py  rotate a binary STL and drop it onto Z = 0 (D-14)
  settings.py     validated print settings and their Bambu Studio process overrides
  printing.py     the print path: plan (read-only) → export, orient → slicer → target
  jobs.py         background print jobs and single-use CSRF tokens
  bambuddy.py     BamBuddy REST client (used by modules/bambuddy.py)
  filaments.py    AMS slots / external spools, preset matching, sliced-nozzle check
  threemf.py      Bambu-style 3MF writer and project-settings transplant
  files.py        naming, sanitizing, pruning
  slicers.py      argv building + detached launch (the desktop hand-off)
  notify.py       desktop notifications
  logsetup.py     rotating file log, state dir
  errors.py       exceptions with a message, a fix, an exit code and an HTTP status
  static/         panel.js, preview.js, auth.js, vendored three.js
  modules/        slicer and target modules (docs/MODULES.md, D-27)
    base.py           the contract: ModuleSpec, PrinterInfo, Material, SliceInput, protocols
    registry.py       kind → class, spec_for, Modules (the configured modules of a process)
    bambuddy.py       BamBuddy: slicer + target (role "both")
    slicerapi.py      Bambu Studio / OrcaSlicer API sidecars (bambu-studio-api, orca-slicer-api)
    moonraker.py      Klipper through Moonraker (target)
    prusalink.py      Prusa printers through PrusaLink (target)
    preform.py        Formlabs Form 4 through PreForm Server: slicer + target (role "both")
    bambu_project.py  Bambu/Orca project layout (centring, copies, filaments, prime tower)
addon/            Home Assistant add-ons: os2slice, bambustudio_web, preform_server
docker/           Docker Compose settings examples and the Duck DNS script
tests/            unit tests; fakes*.py are recorded HTTP shapes
scripts/          deploy_addon.sh, spikes, one-offs
```

`request.py` takes a mapping of query params, not a URL of a particular transport. `os2slice handle <url>` (accepts `http://localhost:…/open?…` and, for a future option A, `os2slice://open?…`) and `send` feed the same parser, so a listener or a custom-scheme handler touches only `cli.py`/`server.py` and a small OS-specific install step.


## ExportRequest

```python
@dataclass(frozen=True)
class ExportRequest:
    slicer: str  # local mode: a [slicers.*] key; print path: always "bambuddy", unused
    document_id: str
    wvm: Literal["w", "v", "m"]
    wvm_id: str
    element_id: str
    part_id: str | None  # None = whole Part Studio (Phase 4)
    configuration: str  # "" if default; decoded once, httpx encodes once when sending
    fmt: Literal["stl", "3mf", "step"] = "stl"
```

## Validation rules (the security boundary)

The param rules apply to both modes. The print service (`server.py`, with `admin.py` for `/admin`) enforces:

| Check | Rule |
|---|---|
| bind | `127.0.0.1`, except `identity = "lan"` (D-17), which serves HTTPS itself with `server.tls_cert`/`tls_key` (DuckDNS certificate, reloaded when renewed) and may bind the LAN |
| `Host` | must be in `server.hosts`, else 403 |
| identity | `"lan"`: anyone on the LAN (D-17). `"tailscale"`: `Tailscale-User-Login` must be in `server.allowed_users`. `"none"`: refused by config unless every host is localhost |
| `GET /print` | `Sec-Fetch-Dest: document`; validates the Onshape params (`parse_print_query`: no `slicer`, Onshape's own params allowed). Reads only: part/document names, printers + status. Issues a CSRF token bound to user + part + configuration |
| `POST /print` | `application/x-www-form-urlencoded` ≤ 16 KB, only known fields, `Sec-Fetch-Site: same-origin`, matching `Origin`. Settings and orientation are validated first, then the single-use token (30 min) is redeemed, then a job starts |
| `GET /panel` | `Sec-Fetch-Dest: iframe` only; a `server` param other than the configured Onshape host is refused; same Onshape param rules as `/print`. `POST /panel/print` follows the `POST /print` rules. `POST /panel/model-link` and `/panel/web-studio` slice but never queue: same-origin form POSTs, no token; a model link (`/models/<token>/os2slice.3mf`) is a random token that expires after 15 min |
| `/jobs/<id>` | random 128-bit id, visible only to the user who started it; refreshes itself while running |
| sign-in (D-23) | Only with `[onshape] auth = "oauth"` (needs `identity = "lan"` + TLS). `GET /auth/start` and `/auth/callback`: `Sec-Fetch-Dest: document` only; `state` single use, 10 min, bound to a nonce cookie (`Path=/auth/`) from the same browser; the `next` page must be `/print?…` or `/panel?…`. `POST /auth/claim` and `/auth/sign-out`: same-origin form POSTs; claim codes single use, 2 min. Session cookies: random 256-bit ids, `Secure; HttpOnly`, `SameSite=Lax` (top-level) or `SameSite=None; Partitioned` (panel), 30 days; stored server-side as hashes. Tokens: `signins.json`, 0600, never logged or sent to browsers. Onshape is read with the signed-in user's token; not signed in → the panel/page shows Sign in, other routes 401 |
| `/admin` (D-28) | The config page (`admin.py`). Host/identity checked first as above. No admin password set (`os2slice admin-password`, never from a browser) → 503 on every `/admin` path. Unknown path → 404, wrong method → 405 |
| `/admin` POSTs | `Sec-Fetch-Site: same-origin`, or no Sec-Fetch-Site but an `Origin` equal to the Host; an `Origin`, when sent, must equal the Host; else 403. Then the session (else 303 → `/admin/login`), then the body (urlencoded ≤ 64 KB, ≤ 400 fields, only the fields that form has, else 400), then a single-use CSRF token (30 min) bound to the session and the form's action (else 403), before anything is read or written |
| `/admin/login` | `GET`: the form (signed in → 303 `/admin`). `POST`: CSRF token, then the per-peer rate limit (5 failures → 30 s lockout, doubling to 15 min; 20 from anyone → global; 429 + `Retry-After`), then a constant-time scrypt check (401 when wrong). Success: cookie `os2slice_admin` = random 256-bit token, `Path=/admin; Max-Age=43200; HttpOnly; SameSite=Strict`, plus `Secure` with TLS or `identity = "lan"`; kept in memory as a hash, 12 h, ends on restart and on a password change |
| `/admin/*` | Every other route needs the session, else 303 → `/admin/login`. GETs only read (they may call modules' `check()`, `profiles()`, `printers()`); query params are whitelisted per route. `POST /admin/logout` revokes the session; `POST /admin/password` needs the current password (rate-limited like login) and only rotates it |
| `/admin` saves | Raw TOML → the edit → `config.parse` (same rules as at start; error → the form again, 400, nothing written) → a trial module build with the new secrets (a missing required secret fails here) → secrets to the secret store (never config.toml, never shown again: pages say "set"/"not set") → atomic write (`tomlwrite`, file mode kept) → `Service.reload()`. Reload swaps config and modules under a lock and closes the old modules unless a job is running; `[server]` and `[onshape]` settings and new Onshape keys apply after a restart, and every page says so. "Test connection" builds one module from the form and runs `check()` without saving |
| responses | CSP `default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'` plus `frame-ancestors 'none'` (the panel and the job pages it opens: the configured Onshape host) and, on pages with scripts, `script-src 'self'; connect-src 'self'`; `nosniff`, `Referrer-Policy: same-origin` (with `no-referrer` browsers send `Origin: null` on our own POSTs), `no-store`. Every interpolated value is HTML-escaped |

Any web page can navigate a browser to the service, so every request is hostile until proven otherwise.

**Redirect listener (`redirect.py`, `[server] redirect_port`, off by default).** Home Assistant's "Open Web UI" button only accepts `http(s)://[HOST]:[PORT:n]…`, where `[HOST]` is whatever name the browser used for Home Assistant, so it can't point at the HTTPS service (certificate for the DuckDNS name, every other `Host` refused). With `redirect_port` set (only allowed with `identity = "lan"`, and different from `port`), `serve` also binds `server.bind:redirect_port` over plain HTTP, and that listener answers every `GET`/`HEAD`, on any path and with any `Host`, with `303 Location: https://<server.hosts[0]>/admin` and `Cache-Control: no-store`; every other method gets 405. It never serves content, never reads a request body, and never echoes the path, `Host` or anything else from the request into the response or the log. The add-on sets it to 8080 (published on host port 8444) through `OS2SLICE_REDIRECT_PORT`, which `serve` takes as an override, rather than in `config.toml`: the add-on only rewrites that file when its rendering of the options changes, and a new line would have counted as a change and dropped the edits made on `/admin`.

### HTTP layer of the local listener (D-3, D-9; not wired up in this tree)

| Check | Rule |
|---|---|
| bind | `127.0.0.1` only, port from config (default 8765) |
| method | `GET` only; anything else → 405 |
| `Host` | exactly `localhost:<port>` or `127.0.0.1:<port>`, else 421 (DNS rebinding) |
| path | `/open` (export), `/health` (for `doctor`: returns version, no work done), `/favicon.ico` → 204. Anything else → 404. |
| `/open` headers | `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-User: ?1`. Missing or different → 403 with a page explaining that the link must be clicked from Onshape. |
| concurrency | one export at a time; a second `/open` while busy → 429 page |
| response | `Content-Type: text/html; charset=utf-8`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. All interpolated text is HTML-escaped. |

### Params (`request.py`)

| Param | Rule |
|---|---|
| `slicer` | must be a key that exists in the user's config |
| `d`, `wvid`, `e` | `^[0-9a-f]{24}$` |
| `wv` | one of `w`, `v`, `m` |
| `p` | optional; `^[A-Za-z0-9_+\-]{1,32}$` (Onshape part IDs look like `JHD`) |
| `c` | optional; ≤ 2048 chars after decoding. The literal `{$configuration}` (sent when the Part Studio has no configurations) means default → `""`. Passed to Onshape only as a query param, never used in paths, filenames (hash it) or commands. |
| `fmt` | optional; `stl` / `3mf` / `step` |
| Onshape-added | `companyId`, `sessionCompanyId`, `server`, `userId`, `clientId`, `locale`, `theme`: allowed and ignored. **`server` is never used as the API host.** |
| anything else | reject unknown params, so typos and injection attempts fail loudly |
| repeated params | reject (e.g. two `d=`) |

Also:

- Any other unresolved placeholder like `{$partId}` means the extension context was wrong. Reject it with a clear message.
- The Onshape host comes only from config (default `https://cad.onshape.com`, must match `https://*.onshape.com`).
- **Redirects:** the STL export answers 307 to another Onshape host (e.g. `cad-usw2.onshape.com`). Follow it by hand and re-attach auth only if the target is `https://<sub>.onshape.com`; refuse anything else.
- Slicer argv comes only from config. The only thing interpolated is the file path we created.
- Worst case for a malicious link: it opens one of your own parts in your slicer. Keep it that way.

## Service (Linux, local mode; planned)

The planned `os2slice install-service` (not a subcommand yet) writes `~/.config/systemd/user/os2slice.service`:

```ini
[Unit]
Description=os2slice: Onshape → slicer listener
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=<abs path to os2slice> serve
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

then runs `systemctl --user daemon-reload` and `systemctl --user enable --now os2slice.service`. Tying it to `graphical-session.target` gives it `WAYLAND_DISPLAY`/`DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`, which the slicers and `notify-send` need (verified present on Josh's KDE Wayland session).

## Failure handling

Neither the service nor a launched `handle` has a terminal, so every failure must reach the person who asked:

1. Write it to the log (tracebacks for the unexpected ones).
2. Local mode (`send`, `handle`, `pipeline.run_and_report`): send a notification with a one-line cause and the fix, e.g. "No API keys; run `os2slice setup-keys`" or "OrcaSlicer not found at /opt/…; edit config.toml".
3. Service: show the same message and fix on the page, the panel or the `/jobs/<id>` page, with a non-200 status for a refused request. A module failure carries the service's own reason (`ModuleError`).

Every error is an `Os2sliceError` with a message, a fix, a CLI exit code and an HTTP status (`errors.py`, `modules/base.py`). CLI exit codes (`handle`, `send`, `print`): 0 ok, 1 config/unexpected, 2 bad request, 3 auth (Onshape keys, or a module's 401/403: `ModuleAuthError`), 4 Onshape API error, 5 slicer launch error, 6 module error (any slicer or target module, BamBuddy included). HTTP: bad request 400, no access 403, config/auth/launch 500, Onshape API and module errors 502.
