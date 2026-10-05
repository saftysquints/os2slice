# Decisions and open questions

Add an entry whenever you make a non-obvious choice. Format: ID, status, decision, why.

## Decided

- **D-1: Local helper (reached through a localhost listener, D-3), not the slicers' own `prusaslicer://` / `orcaslicer://` handlers.** Those only download from whitelisted model sites (Prusa reviews each domain), so a self-hosted export URL would be refused. Launching the slicer with a local file path works the same for all four.
- **D-2: No PreForm on Linux.** (2026-09-30) Josh runs PreForm 3.62 under Wine, but launching it with a file argument didn't open the part. Josh decided to drop PreForm on Linux. It stays in scope for Windows and macOS (Phase 4), where it runs natively. On Linux, `slicer=preform` should fail with a clear notification rather than attempt Wine.
- **D-3: Local HTTP listener on `http://localhost:8765` (option B), not a custom `os2slice://` scheme.** (2026-09-30) Onshape's extension form rejects custom schemes: "must start with https:// or http://localhost. IPv4 URL is not allowed." Josh chose B over (A), an https redirect page on GitHub Pages that bounces to `os2slice://`. B needs no hosting and no scheme registration, avoids the browser's "open xdg-open?" prompt, and shows the result in the tab it opens. Cost: an always-running user service. **Keep A possible:** parsing takes a query-param mapping (not a transport), so an `os2slice://` handler plus redirect page can be added later without touching the core. Spike findings are in ONSHAPE_API.md. Since D-11, this listener only serves the secondary local-slicer mode.
- **D-6: Official Onshape extension menu, not a userscript.** Tampermonkey buttons break when Onshape changes its DOM; extensions are a supported API.
- **D-7: Personal API keys in the keyring, not a hosted OAuth server.** This is a personal/internal tool; OAuth hosting only matters for an App Store listing.
- **D-8: Linux first.** Josh's dev machine is Linux; Windows and macOS come in Phase 4.
- **D-4: Browser sandboxing: moot with D-3.** Snap/Flatpak browsers can open `http://localhost`. Only a revived option A (custom scheme) would need this checked.
- **D-9: Listener hardening.** No `Referer`/`Origin` reaches localhost from Onshape (https → http), so we can't check the caller. Instead: bind `127.0.0.1` only; require `Host` to be `localhost:<port>` or `127.0.0.1:<port>` (blocks DNS rebinding); for `/open` require `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-User: ?1` (a user-clicked top-level navigation, which blocks `<img>`/`fetch`/iframe/auto-redirect drive-bys); GET only; one export at a time; strict param validation. Worst case is unchanged: a hostile page that gets a click opens one of your own parts in your slicer.
- **D-10: Small Phase 1 calls.** (2026-09-30) Files for the default configuration are tagged `default` instead of a hash of the empty string. Slicer stdout/stderr go to `slicers.log` (truncated past 5 MB), not the rotating `os2slice.log`, so a chatty slicer can't push out our own history. After launching, we watch the slicer for 1.5 s: a non-zero exit in that window (bad flatpak ID, crash) is reported as a failure; a zero exit counts as success because single-instance slicers hand the file to the running window and quit. Config errors exit with code 1. `send --part` takes a part ID or an exact part name.
- **D-11 (superseded by D-17 for users): The BamBuddy print service runs on the NUC, reached through Tailscale over HTTPS.** (2026-09-30, Josh) The NUC is `homeassistant` (Home Assistant OS). Josh isn't the tailnet admin (2026-09-30). Tailscale Services need a **tagged** host, which would take `homeassistant` out of Josh's ownership. So the preferred route is **Option A**: the os2slice add-on runs its own `tailscaled` as a new Josh-owned device `os2slice` and uses plain `tailscale serve` for `https://os2slice.<tailnet>.ts.net`. MagicDNS and HTTPS certificates are already enabled on the tailnet. **Option B** is a Service `svc:os2slice` on a tagged `homeassistant`. The request to the admin is written up in the Claude doc "os2slice: Tailscale access request for homeassistant". _Waiting on the admin._ Onshape only accepts `https://` or `http://localhost` Action URLs (D-3). Tailscale Serve gives the NUC a real certificate at `https://<nuc>.<tailnet>.ts.net`, so any of Josh's machines on the tailnet can use the menu item with nothing installed. Serve also adds `Tailscale-User-Login`, which lets us allow only listed users. The tailnet is shared with colleagues and tagged devices, so that check matters. BamBuddy is called on the NUC's own network. The desktop localhost listener (D-3) survives only for the secondary local-slicer mode. _To verify in Phase 2: Onshape accepts the ts.net URL, and the identity header arrives._
- **D-13: A print needs an explicit human confirmation.** (2026-09-30) Starting a print moves real hardware, so the old worst case ("a hostile link opens your own part in your slicer") no longer holds. Rules: an Onshape link (GET) only ever shows a confirmation page, with no upload, slice or print. Printing takes a POST from that page with a CSRF token, `Sec-Fetch-Site: same-origin`, an allowed Tailscale user, and a printer that BamBuddy reports as ready. The CLI asks `[y/N]`. Tests and scripts never print without an explicit flag and Josh at the printer. _Amended by D-33 (2026-10-02): the POST is the confirmation; after it the target may start the job by itself unless the user ticks "Wait for Start"._
- **D-14: Orientation and settings.** (2026-09-30) os2slice rotates the mesh itself before upload (a validated rotation; "this face down" from the plane normal of a face selected in Onshape) and sends `auto_orient: false`, unless the user picks auto-orient. That keeps orientation exact and slicer-independent. Settings go to BamBuddy's `SliceRequest.process_overrides` with Bambu Studio key names (`wall_loops`, `sparse_infill_density` as `"NN%"`, `enable_support`, `support_type` `normal(auto)`/`tree(auto)`, `support_on_build_plate_only`); the spike showed them applied in the sliced file. `auto_arrange: true` centres the part on the plate.
- **D-15: The primary UI is a print panel inside Onshape.** (2026-09-30, Josh wants orientation and settings chosen "in the Onshape interface") Onshape's *Element right panel* extensions embed our page in an iframe and send `SELECTION` messages, so the user can select the part and the face to put on the bed right in the model. The simpler right-click → confirmation page (new tab) is built first (M3) because it shares all server logic and gives a working flow sooner; the panel (M4) reuses it.
- **D-16: BamBuddy authentication stays off.** (2026-09-30, Josh) Printers and the queue answer without a key on the LAN and the tailnet; that's accepted for this shop. os2slice still sends its least-privilege key, so turning auth on later needs no code change. `doctor` reports it as a note, not a warning.
- **D-17: LAN-only access on a DuckDNS name; Tailscale becomes optional.** (2026-09-30, Josh) Not all intended users run Tailscale. They're all in the Davis Mechatronics Onshape company and on the LAN when they print, and open access on the LAN is acceptable. Onshape needs an `https://` hostname (no bare IPs), so: the official Home Assistant **DuckDNS** add-on gives a name like `<name>.duckdns.org` whose A record is set to homeassistant's LAN address (its `ipv4` option), and renews a Let's Encrypt certificate via DNS-01 into `/ssl/`. Nothing is exposed to the internet, and the company DNS isn't touched. Checked 2026-09-30: the LAN router resolves public names to private IPs, so there's no DNS-rebind blocking. os2slice serves HTTPS itself with those files (`server.identity = "lan"`, bound to the LAN, `Host` must be the DuckDNS name). Access control on the LAN is by design none. Other websites still can't trigger prints (same-origin POST + single-use token, D-13), and queued jobs wait for Start in BamBuddy (`manual_start`). Supersedes the Tailscale route in D-11 for users; the admin request is on hold. Alternatives considered: a record in company DNS (needs a Google Cloud service account for renewals), or self-signed certificates (a warning per browser, and they break the panel iframe).
- **D-20: Multi-material = several Onshape parts printed as one object.** (2026-09-30) The text (or any second colour) is modeled as its own part. In the panel you select all the parts; the first uses the main printer + filament menu and each other part gets its own slot menu (the right-click page and single-part prints are unchanged). os2slice exports each part, orients them with one shared rotation and one shared drop onto the bed (`orient_parts`, so the text stays on its base), and packs them into a Bambu-style 3MF with one filament per distinct slot (`threemf.build_3mf`). On dual-nozzle printers each filament is pinned to its slot's nozzle with the 3MF plate's **Manual filament map** (1 = left, 2 = right), which the slicer honours (docs/BAMBUDDY_API.md). This replaces D-19's undocumented `filament_colours` lever, also for single parts with a slot choice. When both nozzles print, the prime tower moves to x=165, y=250 (reachable by both). Every filament's nozzle is checked in the sliced file before queueing, and the queue item gets `ams_mapping` with one tray per filament.
- **D-21: Bambu Studio in the browser: one shared session on homeassistant, with local Bambu Studio as the fallback.** (2026-10-01, Josh) A second local add-on, `bambustudio_web`, wraps the pinned `lscr.io/linuxserver/bambustudio:v02.08.04.57-ls172` image (Bambu Studio 2.8.4 streamed by Selkies; 6.2 GB, about 1 GB RAM). Tested locally under Home Assistant's limits (64 MB `/dev/shm`, default seccomp): it runs, renders in a browser, and accepts files. Its hook copies the Duck DNS certificate into `/config/ssl` (trusted HTTPS on host port 3443; 3001 belongs to the Bambu Studio API add-on), sets Bambu Studio's `single_instance` so later files join the open window, and optionally adds basic auth (user `bambu`; the image's own PASSWORD handling runs too early for an add-on, so the hook writes `.htpasswd` itself). A small service opens `*.3mf` dropped into `/share/os2slice/inbox` and writes `/share/os2slice/web-studio.json` with the viewer count (established connections to the Selkies stream on 127.0.0.1:8082). In the panel, "Open in Bambu Studio: in the browser" hands the part over and opens the tab; when the session is in use it asks first (it may be your own tab); "on this computer" stays available. **No GPU (2026-10-01):** with `video: true` the session rendered GL on the UHD 620 and encoded with VAAPI zero-copy, and Josh got a black screen in Firefox and Chrome. With `video: false` it renders in software (Pixman) and encodes with x264, as in the working local test. **AppArmor off (2026-10-01):** the black screen was Bambu Studio aborting at start (`sig=6` in the kernel audit log, `ANOM_ABEND ... subj=docker-default`). Docker's default AppArmor profile denies silently; the local test host has no AppArmor, so it never showed. With `apparmor: false` the app runs. The inbox service copies the session environment from labwc (display `:0` on `wayland-0`, not the image's `:1`), restarts Bambu Studio if it exits, logs its output to `/share/os2slice/web-studio-app.log`, and reports `app_running`; the panel shows "unavailable" while it isn't running. **Handoff fix (2026-10-01):** Bambu Studio 02.08 on Linux names its single-instance D-Bus object `/com.bambulab/...`; dots are invalid in a D-Bus path, so a second launch aborts in libdbus and the open window never gets the file. The add-on preloads a small shim (`addon/bambustudio_web/shim/dbus_path_fix.c`, `/etc/ld.so.preload`) that rewrites the prefix to `/com/bambulab/` on both sides. Launches also get `DBUS_SESSION_BUS_ADDRESS` from `~/.dbus/session-bus`, since D-Bus X11 autolaunch can't find the bus from the inbox service. Drop the shim when Bambu Studio fixes the path. **Profiles in "Open in Bambu Studio" (2026-10-01):** both Bambu Studio links (browser and local) now slice the selection in BamBuddy first (same path as printing, never queued) and add that slice's `Metadata/project_settings.config` to os2slice's own geometry 3MF, so the printer, filament presets and colours, plate, walls/infill/supports and prime tower come along and the project stays editable (not a G-code preview). The file's `Application` metadata is set to `BambuStudio-<settings version>`: Bambu Studio ignores the settings of a 3MF that isn't labelled as its own. If the slice fails, the part opens geometry-only and the reason is logged. Each open adds a file pair to BamBuddy's library. Four concurrent sessions would need Docker on a 16–32 GB machine, or Kasm (whose free edition isn't licensed for commercial use).

## Open

- **D-12: NUC packaging and secrets.** homeassistant runs HAOS, where BamBuddy 1.0.22 and the Bambu Studio API sidecar are add-ons (host network, BamBuddy on :8000). **Leaning:** os2slice as a local HA add-on (Dockerfile + `config.yaml`, secrets as `password` options), published through the Tailscale add-on's `services` option. **Fallback** (Josh offered): move BamBuddy + sidecar to the Ubuntu NUC and run everything in Docker Compose with `tailscale serve`. Access (2026-09-30): Claude connects as `root@homeassistant` (Terminal & SSH add-on, port 22) with the dedicated key `~/.ssh/<key>` (no passphrase; revoke by removing it from the add-on's Authorized Keys). HAOS 18.3 / Supervisor 2026.09.3. Local add-ons go in **`/local_apps`** (add-ons are now called "apps"; CLI `ha apps …`, `ha store …`). The SSH container has no docker or python3; the Supervisor builds local apps itself. **Isolation requirement (2026-09-30):** the os2slice add-on must *not* use `host_network`. Other add-ons on homeassistant (BamBuddy included) share the host network, so anything there could reach a host-bound `127.0.0.1:8765` and forge `Tailscale-User-Login`. With its own `tailscaled` in its own network namespace (Option A), only `tailscale serve` can reach the app. BamBuddy stays reachable from the add-on at the host's address. Decide at Gate 2 after the hosting spike (open questions: does a Tailscale Service need admin approval or a tagged host on this shared tailnet, and does `Tailscale-User-Login` reach the add-on?). Either way the NUC has no desktop keyring, so the "keys only in the keyring" rule becomes "keys only in the keyring or an OS secret store, never in the repo, a URL or a log".
- **D-18: Whose Onshape access does the service use?** The service exports with one read-only API key (Josh's today), so it can only print parts in documents that account can open; coworkers' unshared documents fail with 403. Options: coworkers share with Josh; a dedicated company service account (recommended; costs a seat); or per-user OAuth through the app (not built). _For the company admin to decide; see the rollout doc._
- **D-19: Printing from a loaded filament slot.** (2026-09-30) The printer menu is "printer + filament": each loaded AMS slot / external spool (from BamBuddy's printer status) is an option, plus the configured preset. The default is a loaded slot of the configured material, else a lone loaded slot, else the preset, so a PLA preset isn't sliced for a printer holding PETG. The slot's material picks the filament preset by name (Generic, then Bambu <m> Basic, Bambu <m>, PolyLite <m>, then any <m> variant; "Support for …" slots by brand), the slice gets the slot's colour, and the queue item gets `ams_mapping: [global tray id]` + `use_ams: true`. **External spool:** `use_ams: false` and no mapping (unverified until tested at a printer). **Dual-nozzle H2D (2026-09-30):** each slot knows its nozzle (AMS per `ams_extruder_map`, Ext-L 254 → left, Ext-R 255 → right). BamBuddy's slice API ignores `filament_map`, but its slicer puts a single-filament part on the left nozzle when no `filament_colours` are sent and on the right when they are. (Superseded by D-20: the nozzle is now pinned in the 3MF.) os2slice sent colours only for right-nozzle slots, then read the sliced file's nozzle the way BamBuddy's dispatcher does (`sliced_nozzle`) and refuses to queue on a mismatch. Verified live on all four H2D_01 slots (AMS 1, AMS 2, Ext-L, Ext-R). Slots whose nozzle can't be determined stay disabled. **Build plate:** a per-model `bed_type` in config plus a per-print choice, because the H2D preset's default plate (Cool Plate) refuses PETG and ASA.
- **D-5: Whole Part Studio export.** One merged STL (easy, but loses per-part identity) or one file per part passed together (keeps parts separate on the plate)? Leaning toward one file per part.

## D-22: Virtual printers for the web Bambu Studio (2026-10-01)

Bambu Studio (web) reaches the printers only through BamBuddy virtual printers, so every
job still lands in BamBuddy. Each VP needs its own IP on homeassistant (BamBuddy uses host
networking), so the host's network interface is static with its main address plus one
extra LAN address per VP. VPs, all Print Queue mode with a target printer (live
AMS/nozzle/camera mirror; access code = the real printer's): one per real printer, each
on its own address, with the model set to match (H2D for the H2Ds). Auto-dispatch and Save AMS mapping
left as Josh had them. The add-on trusts BamBuddy's VP CA automatically (printer.cer).

## D-23: Per-user Onshape sign-in for document access (2026-10-01)

Josh chose per-user sign-in over a shared key or a service account: each coworker
authorizes the os2slice OAuth app once, and the service reads Onshape with that person's
own access (read-only), so no document sharing is needed. `[onshape] auth = "oauth"`
turns it on (add-on option `onshape_auth`); `"keys"` (the shared API key pair) stays the
default and the CLI keeps using keys.

- **Flow:** OAuth 2 authorization code. Onshape's sign-in page can't run inside the panel
  (its CSP `frame-ancestors` allows only Onshape sites), so the panel's **Sign in with
  Onshape** button opens it in a pop-up (`/auth/start` → Onshape → `/auth/callback`). The
  callback exchanges the code (client secret only on the server), asks
  `/api/users/sessioninfo` who signed in, stores the grant and creates a session.
- **Getting the session into the panel:** the pop-up is a top-level page, the panel a
  third-party iframe, so they have separate cookie jars. The callback page posts a one-time
  claim code (2 min) to `window.opener` with our own origin as target; the panel redeems it
  with a same-origin `POST /auth/claim`, which sets its own cookie (`SameSite=None;
  Partitioned`, i.e. CHIPS, so it works where third-party cookies are blocked). Neither
  Onshape nor os2slice sends `Cross-Origin-Opener-Policy`, so `window.opener` survives
  (checked 2026-10-01). The `/print` tab uses the top-level cookie and comes back to its
  page after signing in.
- **Login CSRF:** `state` is single use, 10 min, and bound to a nonce cookie set by
  `/auth/start` in the same browser, so a crafted link can't sign someone into another
  person's account (or hand their session to an attacker's panel).
- **Storage:** `signins.json` in the state dir (0600, atomic writes): per user the access
  and refresh tokens; sessions only as SHA-256 hashes, 30 days. Each refresh stores the new
  refresh token. A refused refresh (revoked) drops the user, who signs in again.
- **Per-user isolation:** previews are cached per user, model download links carry the
  user who made them (Bambu Studio fetches them without cookies), print jobs run as the
  user who confirmed them, and CSRF tokens are bound to the user.
- **Sign out** (panel link) ends the browser's sessions and deletes the user's grant.
- **To verify live (Phase 8):** the token exchange's exact shape, `expires_in`, and that
  `sessioninfo` returns `name` for OAuth tokens (it's null for API keys).


## D-24: Docker Compose as a first-class deployment (2026-10-01)

Home Assistant is optional. `docker-compose.yml` runs the same two images on any Linux
Docker host: `os2slice` (root `Dockerfile`, non-root uid 1000, config from a mounted
`docker/config.toml`, secrets from `.env`) and, under the `web-studio` profile, the
unchanged `addon/bambustudio_web` image. BamBuddy stays outside (its own install, host
networking) and is reached as `host.docker.internal`. A `duckdns` profile replaces Home
Assistant's Duck DNS add-on: lego (DNS-01 via Duck DNS) keeps `./certs` current and sets the
A record to the LAN address. The web container needs `apparmor:unconfined` for the same
reason as on HAOS (D-21). The two containers share a `shared` volume for the inbox and
status file; the inbox belongs to uid 1000 (os2slice and the image's `abc` user). Tested
locally 2026-10-01: doctor all PASS against the real BamBuddy, sign-in redirect, web
session status, and a cross-container handoff; the `duckdns` script dry-run with stubs.

## D-25: Brim, top/bottom layers and copies (2026-10-01)

- **Brim is a toggle, and off means off.** On sends `brim_type: "outer_only"` (the preset's 5 mm width); off sends `"no_brim"`, overriding the Bambu presets' `auto_brim`, so the checkbox means what it says. `[print_defaults] brim` sets the starting state.
- **Top/bottom layers are always sent** (`top_shell_layers`, `bottom_shell_layers`, 0–30), like walls and infill. The defaults (5 / 3) are the Bambu "0.20mm Standard" presets' own values. Note Bambu Studio also has `top_shell_thickness`/`bottom_shell_thickness` minimums that can add layers beyond the count; we don't override those.
- **Copies are laid out by os2slice, not the slicer.** Copies > 1 force the 3MF path: the selection is exported once and the 3MF has one build item per copy, all pointing at the same object (Bambu Studio loads them as instances). The grid is the squarest one that fits the bed (5 mm margin; 6 mm between copies, 16 mm with a brim), centred like a single part, so the prime-tower placement (D-20) sees the whole grid as the footprint. Too many copies for the bed is refused before anything is uploaded. With auto-orient, BamBuddy's auto-arrange is on and re-packs them. 1–25 copies; all copies go on one plate and one queue item.
- **A missing checkbox in our own forms means unchecked** (`_form_settings`), so a config default of `true` can be turned off from the page. Before this, an unticked "build plate only" fell back to the config default.

## D-26: Separate printer and filament menus in the panel (2026-10-01, Josh)

The panel has a **Printer** menu (printer names) and a **Filament** menu (the preset filament, value `""`, then that printer's loaded slots, value = global tray id). The server sends every printer's filament choices as JSON (`data-choices`) and renders the default printer's options itself; `panel.js` refills the menu when the printer changes, keeping the preset choice across printers and otherwise taking that printer's default (D-19's rule). The extra parts' menus (D-20) list the chosen printer's slots from it. The form gains a `filament` field; `printer` may still be the combined `name|tray` value, which the **right-click page keeps**, because that page runs no JavaScript (CSP without `script-src`) and so can't make one menu follow the other. Sending a tray in both fields is refused.

## D-27: Slicer and target modules (2026-10-01, Josh)

os2slice becomes a general CAD → print broker rather than a BamBuddy front end. Two
kinds of pluggable module, bound per printer in config: **slicers** (geometry + settings
→ print file) and **targets** (print file → printer or farm queue). BamBuddy is wrapped
as one of each, so today's path keeps working while slicing moves to the Bambu Studio /
OrcaSlicer sidecars called directly, and Moonraker, PrusaLink and PreFormServer join as
targets. Contract: `src/os2slice/modules/base.py`; design: `docs/MODULES.md`. Rules kept
from before: a target's `submit(start=False)` must leave the job waiting for a person
(D-13); modules call only their configured URL; secrets live in the secret store under
`<section>.<name>.<key>`, never in config.toml. Each module declares its config fields
(`ModuleSpec`), which is what the web config page (D-28) renders. Priority: FDM first,
SLA (issue #2) after. Development on Josh's desktop only; nothing is deployed to
barnassistant until he says so.

_Note (2026-10-01, later):_ the desktop-only restriction ended with the deployment.
Add-on 0.2.0 runs on barnassistant at `https://dm-print.duckdns.org:8443`; Josh set the
admin password, migrated `[bambuddy]` to `[targets.bambuddy]` on `/admin`, and added the
"Bambu Studio API" add-on (port 3001) as a `bambu-studio-api` slicer there. PR #3 merged
it into `main`.

## D-28: Web config page (2026-10-01, Josh)

A config page in the service (`/admin`) for slicer and printer connections, Onshape
and server settings, print defaults, secrets, `doctor`, jobs and the log. It is the
most sensitive page the service has and the service is open to the LAN (D-17), so:
it needs an **admin password** (scrypt hash in the state dir, set with
`os2slice admin-password` or the add-on option; no browser-based bootstrap, so a fresh
install can't be claimed from the LAN), its own session cookie (`Secure; HttpOnly;
SameSite=Strict`, 12 h), the same CSRF and same-origin rules as Print, login
rate-limiting, and secrets that are write-only. Config writes are atomic
(`config.toml.tmp` → rename) through a small TOML emitter for our own schema (no new
dependency), the service reloads its config after a save, and settings that need a
restart (bind, port, TLS) say so on the page.

## D-29: The module seam, as built (Phase 9a, 2026-10-01)

Calls made while moving the BamBuddy path onto the D-27 modules, keeping behaviour:
- A role-"both" module is configured once under `[targets.<key>]`; it is also slicer
  `<key>`, and an empty `slicer` means the target slices for itself.
- Discovered printers are keyed `<target>/<id>`; forms send that key, the CLI and
  `default_printer` take a key or a name. Material ids are strings
  (`[A-Za-z0-9_.-]{1,40}`); an id the printer doesn't have fails the job (before any
  upload) rather than the form, since only the target can tell.
- `BambuddyError` is a `ModuleError`. BamBuddy's slice always downloads the sliced file
  (`SliceOutput.data`); `submit` queues the library file the slice made instead of
  uploading again, and uploads a file sliced elsewhere into the library root folder.
- The preset `source` tier is BamBuddy-specific: it lives in the model's `extra`
  (`preset_source`), not in `Profiles`.
- A desktop hand-off can't be a printer's slicer (config error); the MODULES.md example
  that did so now uses a server-side slicer key.
- `cfg.bambuddy` is gone; `[bambuddy]` is only read as `[targets.bambuddy]`.

## D-30: Joining the modules (Phase 9 integration, 2026-10-01)

Calls made when the sidecar slicers (9b), the Moonraker / PrusaLink targets (9c) and the
seam (9a) were joined:
- **Prime tower handling lives once, core-side**, in `bambu_project.with_tower_retries`,
  shared by every Bambu-style slicer (BamBuddy and the sidecars): with more than one
  distinct material it tries `tower_spots()` in order as `wipe_tower_x`/`wipe_tower_y`
  process keys and moves on when the slicer's error mentions a conflict, the
  (un)printable area, or the wipe / prime tower. The sidecar module builds its 3MF with
  `bambu_project.layout` too, so copies and the footprint are the same as BamBuddy's,
  and too many copies is a `BadRequest` before any upload on every slicer.
- **Sidecar gram figures come from `Metadata/slice_info.config`** (`weight`), then the
  G-code header's per-filament list, then the HTTP headers: the sidecar's
  `X-Filament-Used-g` is only the first filament's weight on multi-filament slices.
- **Offline convention:** `Target.status()` returns `PrinterStatus("offline",
  connected=False, ready=False, detail=<why>)` when the printer or the service in front
  of it can't be reached, so menus and plans still render; it raises only for config
  and auth problems. BamBuddy follows it now (any non-auth BamBuddy error while reading
  status = offline).
- **`ModuleAuthError(ModuleError)`** for a service's 401/403: HTTP 502 as before, CLI
  exit code 3 like other key problems, and a fix that names the secret
  (`targets.<key>.api_key`, `slicers.<key>.api_key`). `BambuddyAuthError` is both it and
  the older `AuthError`. Modules now get their config `key` so messages can name it.
- `SliceInput.process_overrides` is the typed channel for extra slicer keys from the
  core; `extra` stays module-specific. `close()` is optional on modules and
  `Modules.close()` / `with Modules.from_config(...)` closes them.
- `Health.detail` is a warning line, not information: the resolver sidecar's 503
  "unhealthy" for a missing `dataPath` alone (slicing works; BamBuddy's add-on answers
  that way) passes with a warning instead of failing `doctor`.

## D-31: The config page, as built (Phase 9d, 2026-10-01)

Calls made building `/admin` (D-28):
- Server and Onshape settings saved on the page go to the file but not into the running
  service: the socket, the Host check, the sign-in redirect URI and the Onshape client
  were made from them at start, so changing them live could lock the admin out. Every
  page says "restart needed" until then. Everything else (modules, printers, defaults,
  export, web studio) applies at once through `Service.reload()`.
- A save builds the new modules once as a trial before writing, so a missing required
  secret or a module that refuses its values fails the form, not the reload.
- Reload closes the replaced modules unless a print job is queued or running (it may
  hold them); then they are left to the garbage collector.
- CSRF tokens are bound to the admin session as well as the form's action; login has its
  own token without a session. A POST without Sec-Fetch-Site is accepted only with an
  `Origin` equal to the Host (older browsers), never with neither.
- Module and printer names can't be renamed on the page (secrets are stored under the
  name); remove and add instead. The file is rewritten whole, without its comments.

## D-32: The add-on and the config page (add-on 0.2.0, 2026-10-01)

Making `/admin` work inside the Home Assistant add-on (D-12, D-28):
- **`admin_password` option** (`password?`, 12+ characters, else an options error).
  Hashed into `admin.json` only when `verify()` says it changed, so a restart keeps the
  admin signed in. An empty option leaves a stored password alone: clearing a field on
  the Configuration tab is too easy to do by accident to be the way to lock the page.
- **`config.toml` persists.** It is written from the options only when there is none,
  when the sha256 of the rendered text differs from `options.sha256` beside it, or with
  `reset_config` on. Hashing the *rendered* text (not the options) means an add-on
  upgrade that renders differently also rewrites it; the cost is that such an upgrade
  discards page edits (DOCS.md says so). `render_config` still emits the
  legacy `[bambuddy]` table; the page's Migrate button converts it.
- **Secret file.** No keyring in the container, so with `OS2SLICE_ADDON=1` or a null /
  fail keyring backend secrets are saved in `<state dir>/secrets.json` (0600, atomic).
  Lookup: keyring (when usable) → file → environment. A keyring that exists but refuses
  stays an error on a desktop (no silent plain-file fallback).
- **Options win over the page.** The file comes before the environment, so an empty
  option lets the page's value through; a filled-in option is exported to the
  environment *and* its entries are deleted from the file at each start. A page save
  while the option is filled in therefore works until the next start. Secret options
  are no longer required when the file holds that secret (the Onshape key pair counts
  as one: both options or neither).

## D-33: Auto start and printer pools on BamBuddy (2026-10-02, Josh)

Josh wants a print to go to the first idle printer that has the right material loaded and start by itself. BamBuddy's scheduler already does exactly that (read from its source, 1.0.22, `backend/app/services/print_scheduler.py`): a queue item with `target_model` instead of `printer_id` is dispatched to any connected, idle printer of that model whose AMS trays or external spool hold every filament type the 3MF uses (BamBuddy extracts the types itself when the item is model-based); `filament_overrides` with `force_color_match` require an exact type + colour; `manual_start: true` stages the item so it never auto-dispatches.

Decided:

- **The Print POST is the human confirmation (D-13 amended).** Targets submit with `start=True` by default. The panel and the confirmation page get a "Wait for Start" toggle, off by default; the CLI gets `--wait-for-start`. The toggle is always off by default: waiting is only ever a per-print choice (Josh, 2026-10-02, after the first build let the target's `manual_start` config value set the default and Home Assistant's stored `manual_start: true` made the box come up checked). The `manual_start` config key and add-on option stay accepted but have no effect; the add-on keeps rendering the line so `config.toml` isn't rewritten. _Later the same day (Josh):_ the initial state of the toggle is a global print default, `[print_defaults] wait_for_start` (false unless set), edited on `/admin` Print defaults and not an add-on option (the add-on's rendering of its options must not change). It applies to every target: Moonraker and PrusaLink start after upload too unless the box is ticked; their start paths are read from docs and still untested on a printer. Everything else in D-13 stands: a GET never prints, the POST needs the single-use CSRF token and `Sec-Fetch-Site: same-origin`, scripts and tests print only behind an explicit flag.
- **Printer pools.** The BamBuddy target offers "Any <model>" entries next to the real printers. A pool print is queued with `target_model`, no `printer_id` and no `ams_mapping` (BamBuddy remaps trays at dispatch), and the chosen filament's type + colour as a forced `filament_overrides` entry, so "the correct material" means that colour, not just that type. The pool's filament menu lists what is loaded across the model's printers. On dual-nozzle pools the nozzle pinning and checks are as in D-35 (2026-10-05; before that they were skipped, which failed a print).
- **BamBuddy settings (Josh):** turn on `require_plate_clear`, otherwise a printer still showing "awaiting plate clear" counts as idle for an unattended start; every pooled printer needs an SD card.
- Rejected: keeping `manual_start = true` and pressing Start in BamBuddy (what Josh wants to avoid), and matching by type only (any PLA spool would do for a colour the user chose).

## D-34: Colour icons in the filament menus (2026-10-02)

- Every filament option starts with the nearest coloured-square emoji (🟥🟧🟨🟩🟦🟪🟫⬛⬜), from HSL rules in `filament_icons.py` (ported to `panel.js` as `colourEmoji`; `tests/test_filament_icons.py` runs both on the same colours). No colour, or an empty changer gate, gets no icon. `data-color` is unchanged.
- Browsers with customizable selects (`appearance: base-select`, Chrome 135+) show an exact-colour swatch instead: each option carries `<span class="fc-emoji">` and `<span class="fc-swatch" style="background:#RRGGBB">` (colour validated and normalised to six hex digits first), and the menus (`select.fc`) start with `<button><selectedcontent></selectedcontent></button>` so the closed menu shows the chosen swatch too. Older parsers drop the spans and the button and keep the text, so `/print` needs no script.
- Rejected: a colour-only `background` on `<option>` (unreadable labels, ignored by most native pickers) and an all-JS custom dropdown (no JS on `/print`, and native selects keep keyboard and form behaviour).

## D-35: Nozzle pinning on dual-nozzle printer pools (2026-10-05)

- Why: a pool print on "Any H2D" with blue ASA failed on the printer with 0700-7000-0002-0008 "Failed to get AMS mapping table" (BamBuddy queue item 9). os2slice pinned no nozzle for pools, the slicer put the filament on the right nozzle, BamBuddy's scheduler picked the only H2D with blue ASA (by type and colour only), whose ASA sat in an AMS feeding the left nozzle, and BamBuddy's dispatch-time matcher filters trays strictly by nozzle, so it found none and sent the print without an AMS mapping (docs/BAMBUDDY_API.md, 2026-10-05).
- `_pool_status` reads each member's slots with their nozzles (`slots_from_status(raw, dual)`, as `status()` does for one printer). A pool material's `extruder` is the nozzle that feeds that (type, colour) on the **most member printers**, counting printers, not slots (one with it on both sides counts for both). A tie goes to the right (main) nozzle, 0. Slots whose AMS isn't in `ams_extruder_map` don't count; with no member saying, `extruder` stays None. When members disagree the label says so: "PLA · red · left nozzle on 2, right on 1 (loaded on 3 of 3 printers)".
- With every filament's `extruder` set, the single-printer machinery applies unchanged: the project 3MF gets the Manual filament map (D-20) and the slice is checked with `sliced_nozzle` before queueing.
- Before queueing on a dual-nozzle pool, `BambuddyModule.submit` (`_check_pool_nozzles`) reads every member's live status and refuses with a `ModuleError` unless **one** member has every filament, in a slot feeding the nozzle the sliced file prints it with: the forced type + colour, or for a preset (no colour) the type the file names in `slice_info.config`. Filaments whose nozzle the file doesn't say aren't checked (BamBuddy doesn't filter them either); a member with a filament switch (FTS) counts for either nozzle; no member answering is refused too. It runs before the file is uploaded or queued, so it also covers files sliced by another slicer module.
- Not done: **presets are not pinned.** A bare preset carries no type in os2slice (only its name), and guessing the type from the name to pick a nozzle was judged too fragile; the slicer picks its nozzle, and the pre-queue check refuses a preset job that no member can map. A job mixing a loaded filament and a preset isn't pinned either (the Manual map needs every filament), so it relies on the check as well.
- Residual risk: BamBuddy's scheduler still ignores nozzles. If several printers have the colour, one on each side, it may dispatch to the wrong one; only BamBuddy can close that (a nozzle-aware printer choice), or the person picks a specific printer.
- Rejected: dropping `force_color_match` (any same-type spool would do, D-33) and sending an `ams_mapping` for pools (the printer isn't known at queue time; BamBuddy recomputes it at dispatch for model-based items).
