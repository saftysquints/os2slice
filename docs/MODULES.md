# Modules: slicers, targets, printers

Decided 2026-10-01 (D-27), deployed the same day (add-on 0.2.0 on barnassistant).
os2slice is a general CAD → print broker: the part comes from Onshape, a **slicer
module** turns it into a print file, and a **target module** hands that file to a
printer or a print-farm service. BamBuddy, which did both, is one slicer module and one
target module among several, so slicing can move out of BamBuddy without changing
anything else.

```
Onshape ──export──► orient (orientation.py) ──► SliceInput ──► Slicer.slice() ──► SliceOutput
                                                                                      │
                     panel / page / CLI  ◄── Submission ◄── Target.submit() ◄─────────┘
```

The contract is `src/os2slice/modules/base.py`. Read it before touching a module.

## Module kinds

| kind | role | technology | makes / accepts | status |
|---|---|---|---|---|
| `bambuddy` | slicer + target | fdm | makes `gcode.3mf`; accepts `gcode.3mf` | built; in daily use on barnassistant (`[targets.bambuddy]`, migrated from `[bambuddy]` on `/admin`) |
| `bambu-studio-api` | slicer | fdm | `gcode.3mf`, `gcode` | built (`modules/slicerapi.py`); verified live in local Docker, and in use on barnassistant since 2026-10-01 (the "Bambu Studio API" add-on, port 3001, added on `/admin`); `docs/SLICERAPI_API.md` |
| `orca-slicer-api` | slicer | fdm | `gcode.3mf`, `gcode` | built, same module; verified live in local Docker (port 3003); not deployed |
| `prusaslicer-cli` | slicer | fdm | `gcode`, `bgcode` | subprocess, later |
| `moonraker` | target | fdm | `gcode` | built (`modules/moonraker.py`); read from docs, not yet tested on a printer; `docs/PRINTER_APIS.md` |
| `prusalink` | target | fdm | `gcode`, `bgcode` | built (`modules/prusalink.py`); read from docs, not yet tested on a printer |
| `octoprint` | target | fdm | `gcode` | later |
| `preform-server` | slicer + target (pair only with itself) | sla | `form` | Formlabs Form 4, issue #2 |
| `desktop` | hand-off | any | n/a | the local mode (`[slicers.*]` with `argv`), unchanged |

Pairings that load: either sidecar → `bambuddy` (`gcode.3mf`, BamBuddy uploads it to its
library and queues it), → `moonraker` (`gcode`), → `prusalink` (`gcode`). `bambuddy` as a
slicer pairs only with itself (it makes `gcode.3mf` only), so `bambuddy` → `moonraker`
is refused at load.

A slicer and a target pair when the slicer makes a medium the target accepts and the
technologies match; `pairs_only_with` restricts further (PreForm). Config validation
refuses a printer (or a model default) whose pair can't work, a slicer key that doesn't
exist, and a desktop hand-off named as a printer's slicer (it can't slice on the server).
The print gets the first medium in the target's `accepts` that the slicer `makes`.

**Role "both".** A module whose spec has `role = "both"` (BamBuddy, later PreForm) is
configured once, under `[targets.<key>]`. It is one instance that is both that target
and a slicer of the same key: a printer or a model default may name `<key>` as its
`slicer`, and an empty `slicer` means "the target slices for itself". A
`[slicers.<key>]` may not reuse such a key.

## Config

As implemented in 9a–9c (`config.py`; every kind above marked built is registered):

```toml
default_printer = "X1C_01"       # a printer key or name (was [bambuddy] default_printer)

[slicers.studio]                 # server-side module: has `kind`
kind = "bambu-studio-api"
url = "http://172.30.32.1:3001"

[slicers.orca]                   # desktop hand-off: has `argv`, no `kind` (kind = "desktop")
argv = ["flatpak", "run", "--file-forwarding", "com.orcaslicer.OrcaSlicer", "@@", "{file}", "@@"]

[slicers.orca-api]               # OrcaSlicer as a server-side slicer (9b)
kind = "orca-slicer-api"
url = "http://172.30.32.1:3003"
profile_dir = "/orca-profiles"   # optional: GUI user profiles (machine/, process/, filament/)

[targets.farm]
kind = "bambuddy"                # role "both": slices too, as slicer "farm"
url = "http://172.30.32.1:8000"
folder = "Onshape"               # default "Onshape"
manual_start = false             # deprecated, no effect (still accepted): Wait for Start is per print
public_url = "https://print.example.duckdns.org:8000"
# api key: secret store entry "targets.farm.api_key" (never in this file)

[targets.farm.models."X1C"]      # defaults for discovered printers, by model
slicer = "studio"                # omit: the target slices for itself (role "both")
profiles = { printer = "Bambu Lab X1 Carbon 0.4 nozzle", process = "0.20mm Standard @BBL X1C", filament = "Generic PLA @BBL X1C" }
bed_type = "Textured PEI Plate"
extra = { preset_source = "standard" }   # module-specific (BamBuddy: the presets' tier)

[targets.voron]
kind = "moonraker"
url = "http://voron.lan:7125"
# api key: "targets.voron.api_key" (optional; Moonraker trusts LAN ranges by default)

[printers.voron]                 # a hand-configured printer (non-discovering target)
target = "voron"
slicer = "orca-api"              # required here; must be a server-side slicer
model = "Voron 2.4 350"
bed_mm = [350, 350]
nozzle_count = 1
profiles = { printer = "Voron 2.4 350 0.4 nozzle", process = "0.20mm Standard @Voron", filament = "Generic PLA @System" }
materials = ["PLA black", "PETG grey"]   # when the target can't report what's loaded

[printers."X1C_01"]              # overrides for a discovered printer, matched by name
slicer = "orca-api"              # any of: slicer, profiles (per field), bed_mm, bed_type,
                                 # materials, extra; no `target` = every discovering target
```

- `[printers.<key>]` keys: `target`, `slicer`, `name`, `model`, `technology` (`fdm`/`sla`,
  default: the target's), `bed_mm`, `nozzle_count`, `profiles`, `materials`, `bed_type`,
  `extra`. Keys may contain spaces (BamBuddy names like `"A1 Mini"`), not `/` or `|`.
- Discovered printers get the key `<target>/<id>` (BamBuddy: `farm/3`). The web forms
  send that key; the CLI and `default_printer` accept a key or a name.
- BamBuddy printer pools: after its printers, a BamBuddy target lists one
  `<target>/any:<model>` named `Any <model>` (`PrinterInfo.pool = True`) for each model
  that has `[targets.<key>.models."<model>"]` defaults and at least one printer. It has
  the model's slicer, profiles, bed and nozzle count, and `extra.target_model`; it is
  active when any printer of the model is. Its status is ready when any of them is, and
  its materials are the distinct loaded (type, colour) pairs across them, with ids
  `<TYPE>.<RRGGBB>` (no tray ids; `<TYPE>.ANY` for a tray that reports no colour).
  Submitting queues with `target_model` and `filament_overrides` (`force_color_match`
  per chosen filament with a type and colour, `slot_id` = filament n) and no
  `printer_id` or `ams_mapping`: BamBuddy's scheduler dispatches it to the first idle
  printer of that model with every filament loaded and maps its trays itself. On
  dual-nozzle models (H2D) each pool material's `extruder` is the nozzle that feeds it
  on the most member printers (right on a tie; label "PLA · red · left nozzle (loaded)",
  "· left nozzle on 2, right on 1" when they disagree), so the project is pinned and
  checked after slicing as for one printer (D-20); a material no member can place (an
  AMS missing from `ams_extruder_map`) or a preset has none and stays selectable, and
  the slicer chooses then. Before queueing on a dual-nozzle pool, `submit` refuses
  unless one member has every filament (forced type + colour, or a preset's type as the
  sliced file names it) in a slot feeding the nozzle the file prints it with (D-35). A
  `[printers."Any <model>"]` entry overrides a pool like any discovered printer.
- A pool's filament menu (panel and page): first the loaded (type, colour) pairs,
  labelled "PETG · black (loaded)" ("(loaded in 3 slots on 2 of 2 printers)" with several printers), each
  matched to its preset by name (D-19); then every filament preset made for the model,
  sorted and labelled without the suffix: BamBuddy's cached preset names ending in the
  configured filament's `@BBL <code>` or in `@<printer preset>` (Bambu Studio's name
  for a saved user preset), from the target's optional `filament_presets(printer)`
  (`Modules.pool_presets`, empty when unreadable). The configured preset is the
  "Preset filament (…)" entry (value `""`) in its place among them; it is the default
  unless a filament is loaded (then `_default_choice` picks as for a printer). A preset
  choice has the id `f-<sha256[:12]>` (`printing.preset_material`, `Material.raw
  ["preset"]`): the slice uses that preset and no colour, and the queue item gets no
  `filament_overrides` entry for it, so BamBuddy dispatches on the filament type in the
  sliced file. A loaded choice forces its type and colour as above.
- Module field values are validated against the kind's `ModuleSpec.fields` (type,
  required, default); unknown keys and secrets in the file are refused.

Sidecar profiles: by default a profile name is a system preset, sent as a stub that
inherits it. With `profile_dir` (an OrcaSlicer config folder or one `user/<id>/` folder,
`modules/orca_profiles.py`), a name found among the user's profiles (`<kind>/*.json`, and
`_local/<bundle>/<kind>/*.json` from imported bundles) is uploaded whole instead,
resolved against its system parents within their own vendor (`system/<Vendor>.json`),
with legacy keys renamed first as the GUI does; the panel's settings still go on top.
When the printer profile is a user one, the process and filaments carry
`compatible_printers = [<printer>]`, because their system parents list only system
printers and the slicer refuses the mismatch. `own_profiles()` lists them; the core
(`Modules.own_profiles`) offers them in the panel: the filaments as materials (only for
a printer whose target reports none and with no configured `materials`), the processes
as a Process menu (`plan_print(process=...)`, which accepts only those and the configured
one).

Secrets: every `type = "secret"` field is looked up as `<section>.<name>.<key>` in the
secret store (`auth.get_secret`): the keyring entry of that name, then
`OS2SLICE_SECRET_<SECTION>_<NAME>_<KEY>` (dots and dashes as underscores) in the
environment (dev, the add-on and Docker). A required secret that's missing stops the
service at start. `os2slice setup-keys --secret targets.farm.api_key` stores one; the
config page writes secrets to the store and never shows them.

Compatibility: the old `[bambuddy]` table (with `[bambuddy.presets.<model>]`) is read
as `[targets.bambuddy]` (kind `bambuddy`, slicing for itself) with the presets as
per-model defaults (`source` becomes `extra.preset_source`), and its `default_printer`
as the top-level one. `[bambuddy]` and `[targets.bambuddy]` can't both be set, nor two
`default_printer`s. The old keyring entry `bambuddy_api_key` and `$BAMBUDDY_API_KEY`
are read as `targets.bambuddy.api_key`, so existing configs, keys and the add-on keep
working. `[print_defaults]`, `[onshape]`, `[server]`, `[export]`, `[web_studio]` are
unchanged.

## Configuring from the browser

The config page `/admin` (D-28, `admin.py`) edits the same config.toml. It is off
(503) until an admin password is set on the server: `os2slice admin-password` on a
desktop or in a container, the `admin_password` option in the add-on. Then, after
signing in at `/admin/login`:

- **Slicers / Targets** (`/admin/slicers`, `/admin/targets`; `…/new?kind=`,
  `…/edit?key=`, POST `…/save`, `…/remove`) list the configured modules with their kind, key fields
  (secrets only as "set"/"not set") and health from `check()`. **Add** offers every
  kind in `registry.kinds()` for that role (desktop hand-offs included, edited as
  `name` + `argv`) and renders the form from `ModuleSpec.fields`: `str`/`url`/`path`
  as text, `int` as a number, `bool` as a checkbox, `list` one per line, `choice` as a
  select, `secret` as a write-only password input. A discovering target
  (`discovers_printers`) also gets its per-model defaults (`models."<model>"`: slicer,
  profiles, bed_type; `extra` is kept as it was). **Test connection** builds that one
  module from the form (secrets from the form, else the store) and shows `check()`
  without saving. A module needs no page code: a new kind appears once registered.
- **Printers** (`/admin/printers`; POST `…/save`, `…/remove`, `…/default`): `[printers.*]` entries, plus the printers targets found (read-only,
  with **Override** to create `[printers."<name>"]`) and `default_printer`. Profile
  fields suggest the names the chosen slicer's `profiles()` returns.
- **Onshape** (`/admin/onshape`), **Server** (`/admin/server`), **Print defaults**
  (`/admin/defaults`, including **Wait for Start by default**, `wait_for_start`),
  **Onshape panel** (`/admin/panel`: the "Open in …" slicers and
  extra settings, `[panel]`), **Secrets** (`/admin/secrets`: every `<section>.<key>.<field>`
  the config implies, write-only), **Jobs** (`/admin/jobs`), **Log** (`/admin/log`),
  **Password** (`/admin/password`: change it, given the current one); **Sign out** is
  POST `/admin/logout`. The overview `/admin` runs `doctor`'s config, Onshape, secret
  and module-health checks.
- The legacy `[bambuddy]` table shows as "bambuddy (legacy table)" with **Migrate**
  (POST `/admin/targets/migrate`),
  which rewrites it as `[targets.bambuddy]` + `models` (presets' `source` →
  `extra.preset_source`, its `default_printer` → the top level); the result parses to
  the same `Config`.

Saves are validated by `config.parse` before anything is written, the file is
rewritten whole (comments are not kept), and the service reloads its modules at once;
server and Onshape settings wait for a restart. In the Home Assistant add-on the file
is written from the add-on options only on the first start, when those options change,
or with `reset_config` on, so page edits persist across restarts until then; secrets
saved on the page go to `secrets.json` in the state dir (no keyring there), and a
filled-in secret option replaces its page value at each start (D-32).

## What the core does with a printer

1. `printers()` of every configured target (`registry.Modules.printers`) → one list for
   the printer menu, each with `technology`, which selects the settings schema (FDM
   today; SLA later), the bed for the preview, and `ui_url` for the "watch it" link.
   A printer without a slicer or profiles is listed as unusable.
2. `status()` for the chosen printer → state line and loaded `materials` for the filament
   menu. A target that can't tell returns none, and the menu falls back to the printer's
   configured `materials` (`PrinterInfo.materials`). Menu values are `Material.id`
   (`[A-Za-z0-9_.-]{1,40}`); the right-click page's single menu sends `<key>|<id>`. A
   material is usable once it has a `profile` and, on a dual-nozzle printer that isn't a
   pool, an `extruder` (a pool's material may have one, D-35, but needn't).
3. On Print: export + orient as today → `SliceInput` → the printer's slicer →
   `SliceOutput` → the printer's target `submit(start=not wait)`, where `wait` is the
   person's **Wait for Start** checkbox (form field `manual_start`, absent or `on`; the
   CLI's `--wait-for-start` / `--no-wait-for-start`). Its initial state is the global
   print default `[print_defaults] wait_for_start` (bool, default `false`, set on
   `/admin/defaults`; it applies to every target, Moonraker and PrusaLink included), so
   by default prints start by themselves unless the person ticks it (D-33); the
   person's choice always wins, and `plan_print` takes it as an explicit bool. A
   target's `manual_start` setting is deprecated, still accepted, and has no effect. The Print button's same-origin POST
   with its CSRF token, or the CLI's `[y/N]`, is the human confirmation (D-13). The job
   page shows the `Submission`.
4. Multi-material (D-20) and copies (D-25): the core exports the parts and orients them
   with one rotation and one drop (`orient_parts`); each `PartGeometry` carries its
   material. Filament n is the n-th distinct material (`base.distinct_materials`), and
   the target's `submit(materials=...)` gets the same list. A slicer that wants a Bambu
   / Orca project builds it with `modules/bambu_project.py` (`build_project(job)`,
   `layout(job)` for the footprint and filament profiles, `tower_spots`, `copy_offsets`;
   bed from `PrinterInfo.bed_mm`, else a table by model); one that wants STL per part
   gets `parts` as is. With more than one filament a Bambu-style slicer asks for a prime
   tower beside the footprint: `bambu_project.with_tower_retries(project.tower_spots(
   printer), attempt, progress)` calls `attempt(tower)` with each spot (process keys
   `wipe_tower_x`/`wipe_tower_y`, on top of `SliceInput.process_overrides`) until one
   isn't refused as a tower problem (D-20, D-30).
5. "Open in Bambu Studio" (D-21) slices with the printer's slicer when it makes
   `gcode.3mf` and copies that file's `Metadata/project_settings.config` onto the
   project built from the same `SliceInput`.

Optional on a target: `ui_url(request_host="") -> str`, its own queue page, for links
that belong to no single printer (the panel, job pages). BamBuddy derives
`http://<host>:8000/queue` from the request when `public_url` isn't set.

## Module checklist

- One file per module in `src/os2slice/modules/`, registered in `registry.py`
  (`SLICERS` and/or `TARGETS` by `spec.kind`). The class is built as
  `Cls.from_values(values, *, key, transport=None)` when it has that classmethod, else
  `Cls(values, *, key, transport=None)`; `key` is passed only when the signature takes
  it. `values` are the validated fields with secrets resolved (plus `models` for
  targets), `key` its config name (use it in error fixes: "[targets.<key>]").
- `spec` with every config field, so the config page and `doctor` need no module code.
- Only the configured URL is called; nothing from a request. Timeouts on every call.
- `ModuleError` with the service's own reason text; never swallow it. A 401/403 from
  the service is `ModuleAuthError` (still HTTP 502; CLI exit code 3) whose fix names
  the secret to check (`targets.<key>.api_key`).
- `check()` never raises: it returns `Health(ok=False, summary=<why>)`. `Health.detail`
  is a warning line (doctor prints it as WARN), so leave it empty when all is well.
- Target `status()` follows the offline convention: an unreachable printer or service
  is `PrinterStatus("offline", connected=False, ready=False, detail=<why>)`, not an
  exception; only config and auth problems raise.
- Slicers apply `settings.process_overrides()`, then `SliceInput.process_overrides`
  (extra keys from the core), then their own tower spot; `SliceInput.extra` is for
  module-specific facts only.
- Optional `close()` when the module holds pooled connections; `Modules.close()` (or
  `with Modules.from_config(...) as modules:`) calls it.
- A fake transport in `tests/fakes_<module>.py` built from recorded shapes, and a
  `docs/<MODULE>_API.md` with what was verified live (✅) versus read from docs.
- Live tests behind `OS2SLICE_LIVE_<MODULE>_URL`; printing behind an explicit flag only.
