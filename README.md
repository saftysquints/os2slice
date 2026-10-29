# os2slice

Print Onshape parts from inside Onshape. os2slice sits between Onshape and your printers: it exports the part, slices it with the printer's **slicer module**, and hands the result to the printer's **target module**.

- A **Print** panel in every Part Studio: select a part (or several, for multi-colour), click the face it stands on, pick a printer and a loaded filament, set walls, infill and supports, and press **Slice and queue print**. Nothing prints until someone starts the job.
- Printers through **BamBuddy** (Bambu Lab), **Klipper/Moonraker** or **Prusa/PrusaLink**.
- Slicing in BamBuddy, or in a **Bambu Studio** or **OrcaSlicer** API sidecar.
- A web config page, `/admin`, for printers, slicers, secrets and settings.
- A live 3D preview of the part as it will sit on the plate.
- **Open in Bambu Studio** or **OrcaSlicer**: in a shared slicer served in the browser (or Bambu Studio on your own computer), with the printer and filament profiles already set.
- Your own OrcaSlicer process and filament profiles in the panel, and extra settings (chamber temperature, layer height, …) chosen on `/admin`.
- Each user signs in with their own Onshape account (read-only), so no documents need sharing.

It runs on a machine on the printers' LAN, either with **Docker Compose** or as a **Home Assistant** add-on, and serves that LAN only.

```
Onshape (browser) ──panel / right-click──▶ os2slice ──▶ Onshape API (export, read-only)
                                              │
                                              ├──▶ slicer module: BamBuddy │ Bambu Studio / OrcaSlicer sidecar
                                              ├──▶ target module: BamBuddy queue │ Moonraker │ PrusaLink ──▶ printers
                                              └──▶ Bambu Studio (web) ──virtual printers──▶ BamBuddy
```

| Module | Kind | Status |
|---|---|---|
| BamBuddy (slices and queues) | `bambuddy` | in daily use |
| Bambu Studio API sidecar | `bambu-studio-api` | verified live (local Docker, and the Home Assistant add-on) |
| OrcaSlicer API sidecar | `orca-slicer-api` | verified live in local Docker |
| Klipper through Moonraker | `moonraker` | built from the API docs, not yet tested on hardware |
| Prusa through PrusaLink | `prusalink` | built from the API docs, not yet tested on hardware |
| Formlabs Form 4 through PreForm Server (slices and queues) | `preform-server` | built from API calls verified live against the server, not yet run from os2slice |

[`docs/MODULES.md`](docs/MODULES.md) has the details and the config format.

## Requirements

- **A Linux machine on the printers' LAN** with either Docker Compose or Home Assistant OS. For Bambu printers, [BamBuddy](https://github.com/maziggy/bambuddy) with your printers added: leave its auth off, or create an API key with *read status*, *library* and *queue*. Klipper and Prusa printers need their Moonraker or PrusaLink URL (and API key).
- **A DNS name and certificate** for that machine's **LAN** address. Onshape only opens `https://` pages, but nothing needs to be reachable from the internet. Duck DNS works well; both install routes can manage it for you.
- **Onshape:** a developer account to create the app. Assigning it to coworkers needs a Professional or Enterprise plan.
- **Users** on the same LAN, in Chrome, Edge, Firefox or Safari, with pop-ups allowed for the os2slice address.

## Install

### Option A: Docker Compose

Any Linux machine with Docker. In short:

```bash
git clone https://github.com/jlbimson/os2slice.git && cd os2slice
cp docker/config.example.toml docker/config.toml   # edit: host name, presets, Onshape client ID
cp docker/env.example .env && chmod 600 .env       # edit: secrets, Duck DNS
docker compose --profile duckdns up -d duckdns     # DNS name + certificate
docker compose --profile web-studio up -d --build  # os2slice + Bambu Studio in the browser
```

[`docs/DOCKER.md`](docs/DOCKER.md) has the full steps.

### Option B: Home Assistant OS add-ons

You'll need the BamBuddy and Duck DNS add-ons (the Duck DNS name pointing at the machine's LAN address), and an SSH add-on with root login. From a clone of this repo:

```bash
OS2SLICE_HA_HOST=root@<ha-host> OS2SLICE_HA_KEY=~/.ssh/<key> scripts/deploy_addon.sh deploy os2slice
```

You can also put those two settings in an untracked `.deploy.env` at the repo root.

This copies the add-on to `/local_apps/os2slice` and installs, updates or rebuilds it. To do it by hand:
1. Run `scripts/deploy_addon.sh stage <dir> os2slice`.
2. Copy `<dir>` to `/local_apps/os2slice`.
3. Install **os2slice** from *Local add-ons* in the add-on store.

Configure it under Settings → Add-ons → os2slice → Configuration. [`addon/os2slice/DOCS.md`](addon/os2slice/DOCS.md) explains each option. The essentials:

| Option | Value |
|---|---|
| `hosts` | `<name>.duckdns.org:8443` |
| `bambuddy_url` | `http://172.30.32.1:8000` (the Home Assistant host, seen from add-ons) |
| `bambuddy_api_key` | the BamBuddy key (any non-empty value if BamBuddy auth is off) |
| `onshape_auth` | `oauth` (per-user sign-in), once the Onshape app exists; until then `keys` with a read-only Onshape API key pair |
| `presets` | slicer presets per printer model (examples included) |
| `admin_password` | turns on the config page `/admin` (12+ characters) |

Start it. The log opens with a pass/fail table, and every line should say PASS.

For Bambu Studio in the browser (optional):
1. Run `scripts/deploy_addon.sh deploy bambustudio_web`.
2. Open `https://<name>.duckdns.org:3443` and finish Bambu Studio's setup wizard.
3. Restart the add-on once.

See [`addon/bambustudio_web/DOCS.md`](addon/bambustudio_web/DOCS.md).

For a Formlabs Form 4 (optional):
1. Run `scripts/deploy_addon.sh deploy preform_server` (built on the machine from Formlabs' `formlabs/preform-server` image; it publishes no port).
2. On `/admin`, add a target of kind `preform-server` with URL `http://local-preform-server:44388` and the Form 4's IP address under `printers`.

See [`addon/preform_server/DOCS.md`](addon/preform_server/DOCS.md) and [`docs/PREFORM_API.md`](docs/PREFORM_API.md).

### Virtual printers (for sending from the web Bambu Studio)

To send prints from Bambu Studio to BamBuddy, create one BamBuddy **virtual printer** per real printer:

1. Give the machine one extra fixed LAN address per virtual printer:
   - **Home Assistant:** `ha network update <iface> --ipv4-method static --ipv4-address <main>/24 --ipv4-address <extra>/24 … --ipv4-gateway <router> --ipv4-nameserver <dns>`.
   - **Docker host:** see [BamBuddy's guide](https://github.com/maziggy/bambuddy-wiki/blob/main/docs/features/virtual-printer.md#dedicated-bind-ip).
2. In BamBuddy, open Settings → Virtual Printer and add one per printer: Print Queue mode, the matching model, its own address, and the real printer as the target. Each card's **Diagnose** should pass.
3. In the web Bambu Studio, open Device → Add printer → by IP. Use each virtual printer's address and the real printer's LAN access code. In Bambu Studio, use **Send**, not Print.

The web Bambu Studio trusts BamBuddy's virtual-printer certificate automatically.

### The Onshape app

Follow [`docs/ONSHAPE_SETUP.md`](docs/ONSHAPE_SETUP.md):
1. Create an OAuth app with two extensions.
2. Register its redirect URL.
3. Put its client ID and secret in os2slice's settings.
4. Assign it to users (company plan), or subscribe through a private store entry.

## Configure

The install options above set up one BamBuddy that slices and queues. Everything else (more targets, sidecar slicers, which printer uses which slicer, per-model profiles, print defaults, secrets) is set on the config page at `https://<name>.duckdns.org:8443/admin`. It also runs the `doctor` checks and shows jobs and the log.

The page is off until an admin password is set on the server, never from a browser:

- Home Assistant: the `admin_password` option, then restart the add-on.
- Docker: `docker compose exec os2slice os2slice admin-password`.
- A desktop install: `os2slice admin-password`.

The same settings live in `config.toml`; [`os2slice.example.toml`](os2slice.example.toml) has commented examples of every table, and `os2slice setup-keys --secret <section>.<key>.<field>` stores a module secret from a terminal.

## Development

```bash
uv tool install -e .          # or: pipx install -e .
uv run pytest -q              # unit tests (Onshape and BamBuddy are faked)
(cd tests/js && npm ci)       # optional: Node + jsdom, for the panel script's test
uv run ruff check . && uv run ruff format --check .
os2slice doctor
```

The same package also has a terminal mode: `os2slice print --url <part URL> --part <id>` slices and sends after a `[y/N]`, and `os2slice send` opens a part in a desktop slicer. Keys come from `os2slice setup-keys` (`--secret <name>` for a module's secret), stored in the system keyring.

Design notes:
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): the pieces and the security rules.
- [`docs/MODULES.md`](docs/MODULES.md): slicer and target modules, and how to add one.
- [`docs/DECISIONS.md`](docs/DECISIONS.md): why things are the way they are.
- [`docs/ONSHAPE_API.md`](docs/ONSHAPE_API.md), [`docs/BAMBUDDY_API.md`](docs/BAMBUDDY_API.md), [`docs/SLICERAPI_API.md`](docs/SLICERAPI_API.md) and [`docs/PRINTER_APIS.md`](docs/PRINTER_APIS.md): the API shapes, marked verified against the real services or read from docs.

## Security

- os2slice only **reads** from Onshape. On slicers and targets it only uploads its own files, slices and queues; it never deletes or edits anything else there. Modules call only the URLs in the config.
- A print needs a button press on os2slice's own page (same-origin POST, single-use token), or a `[y/N]` in the CLI; the job starts by itself once the printer is free, unless the person printing ticks **Wait for Start** (CLI: `--wait-for-start`); then it waits until someone presses Start. This holds for every target (BamBuddy, Moonraker, PrusaLink). Whether the box starts ticked is a print default on `/admin` (`[print_defaults] wait_for_start`, off unless set; the CLI follows it, and `--no-wait-for-start` overrides it).
- The config page `/admin` needs an admin password set on the server (scrypt hash; login rate-limited), and every change is a same-origin POST with a single-use token. Secrets are write-only there: pages show "set" or "not set", never the value, and they never go into `config.toml`.
- Sign-in tokens and secrets stay on the server, in files only the service can read (or the system keyring on a desktop).
- It serves HTTPS on the LAN only, and accepts requests only for the configured host names.
- Don't expose ports 8443 or 3443 to the internet.

## License

[MIT](LICENSE). Bundles [three.js](https://threejs.org) (MIT, `src/os2slice/static/vendor/`).
