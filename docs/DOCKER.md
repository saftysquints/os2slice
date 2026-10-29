# Docker Compose deployment

Run os2slice on any Linux machine with Docker, no Home Assistant needed. The pieces, all on the same machine on the printers' LAN:

| Service | What it does | Needed? |
|---|---|---|
| BamBuddy | talks to Bambu printers, slices, queues | for Bambu printers (the example config uses it as its one target); installed with BamBuddy's own instructions |
| `os2slice` | the Onshape panel and print pages, HTTPS on port 8443 | yes |
| `duckdns` (profile) | points `<name>.duckdns.org` at this machine's LAN address and keeps its HTTPS certificate in `./certs` | unless you bring your own certificate |
| `bambustudio-web` (profile) | Bambu Studio in a browser tab on port 3443, one shared session | optional |
| `orca-slicer-api` (profile `orca`) | OrcaSlicer as a REST service, for slicing outside BamBuddy | optional (see "Other printers and slicers") |
| `orca-web` (profile `orca-web`) | OrcaSlicer in a browser tab on port 3444, where you keep the profiles os2slice slices with | optional |

Onshape only talks to `https://` pages with a real certificate, so os2slice needs a DNS name and a certificate. The `duckdns` service handles both: the name resolves to the machine's private LAN address, and the certificate comes from Let's Encrypt through a DNS challenge. Nothing is opened to the internet.

## 1. BamBuddy

Install BamBuddy with [its Docker instructions](https://github.com/maziggy/bambuddy). It must use **host networking** for printer discovery and virtual printers. Add your printers, and note:

- **API key:** leave BamBuddy auth off, or create an API key with *read status*, *library* and *queue*.
- **Port:** its port is 8000 on the host. The os2slice containers reach it as `http://host.docker.internal:8000`. If the host runs a firewall, allow the Docker bridge to reach port 8000.

## 2. Settings

```bash
git clone https://github.com/jlbimson/os2slice.git && cd os2slice
cp docker/config.example.toml docker/config.toml
cp docker/env.example .env && chmod 600 .env
```

Edit **`docker/config.toml`**:

- `hosts`: your name and port, e.g. `["print.example.duckdns.org:8443"]`.
- `oauth_client_id`: from the Onshape app (step 5). Until you have it, set `auth = "keys"` and put a read-only Onshape API key pair in `.env`.
- `default_printer` and the `[bambuddy.presets."<model>"]` tables: your BamBuddy printer name and the slicer presets per printer model. `[bambuddy]` is the legacy form of one BamBuddy target; the module form (`[targets.<key>]`, [`MODULES.md`](MODULES.md)) works too, and is how other printers and slicers are added.
- `[web_studio]`: its URL, or delete the table if you won't run the web Bambu Studio.

Edit **`.env`**. It holds the secrets and is never committed:

- `ONSHAPE_OAUTH_CLIENT_SECRET`, `BAMBUDDY_API_KEY`.
- For `duckdns`: `DUCKDNS_DOMAIN`, `DUCKDNS_TOKEN` (from duckdns.org), `LAN_IP` (this machine's fixed LAN address), and `ACME_EMAIL`.
- `WEB_STUDIO_PASSWORD`: optional, a password for the web Bambu Studio.
- Secrets of other modules, as `OS2SLICE_SECRET_<SECTION>_<KEY>_<FIELD>`, e.g. `OS2SLICE_SECRET_TARGETS_VORON_API_KEY` for `targets.voron.api_key` (dots and dashes become underscores).

Give the machine a fixed LAN address, e.g. a DHCP reservation in the router, so the name keeps pointing at it.

## 3. Start

```bash
docker compose --profile duckdns up -d duckdns     # first, so ./certs exists
docker compose logs duckdns                        # wait for "certificate: … updated"
docker compose --profile web-studio up -d --build  # os2slice + web Bambu Studio
docker compose logs os2slice                       # every line of the table should say PASS
```

If you bring your own certificate instead of `duckdns`, put `fullchain.pem` and `privkey.pem` in `./certs`. The private key must be readable by user 1000.

Leave out `--profile web-studio` to run os2slice alone.

For the **web Bambu Studio**:

1. Open `https://<name>.duckdns.org:3443` and finish Bambu Studio's setup wizard.
2. Run `docker compose restart bambustudio-web` once.
3. To send prints from it to BamBuddy, set up one BamBuddy **virtual printer** per printer, as described in the README ("Bambu Studio in the browser"). Each needs its own extra LAN address on this machine; [BamBuddy's guide](https://github.com/maziggy/bambuddy-wiki/blob/main/docs/features/virtual-printer.md#dedicated-bind-ip) shows how to add them on Linux. Add them in the web Bambu Studio by IP.

The container trusts BamBuddy's virtual-printer certificate automatically.

## 4. The config page `/admin` (optional)

`https://<name>.duckdns.org:8443/admin` shows the configured slicers, targets and printers with their health, the Onshape and server settings, print defaults, secrets, jobs and the log. It answers 503 until an admin password is set, and the password can't be set from a browser. Set it in the running container (it asks twice, at least 12 characters):

```bash
docker compose exec os2slice os2slice admin-password
```

The running service picks it up without a restart. What the page keeps, all in the `os2slice-data` volume (`/data/state/os2slice/` in the container):

- `admin.json`: the password's scrypt hash (0600). Admin sessions live in memory and end on a restart. Run the command again to change the password; delete the file to turn the page off.
- `secrets.json`: secrets saved on the page (0600). The image has no keyring (`OS2SLICE_ADDON=1`, null keyring backend), so this file is the secret store. It is read before the environment, so a secret saved on the page wins over the same one in `.env`; remove it from the file to go back to `.env`. Pages show secrets only as "set" or "not set".

**Saving config from the page** writes `docker/config.toml` (mounted read-write). Docker mounts it as a single file, which can't be replaced by a rename, so the page rewrites it in place. Edits made on the host with an editor that replaces the file (most do) are only seen after `docker compose restart os2slice`.

**The Onshape panel page** (`/admin/panel`, stored as `[panel]`) sets what the Print panel offers:

- **Open in a slicer, on this computer** (`local_slicer`): `bambu-studio` (default), `orcaslicer` or `none`. The link hands a short-lived download of the selection to that slicer's URL handler (`bambustudio://`, or `orcaslicer://open?file=`).
- **Open in a slicer, in the browser** (`web_slicer`): one of the sessions set up by `[web_studio]` or `[web_orca]`, `none`, or (left out) every one that is set up.
- **More settings** (`extra_settings`): chamber temperature, nozzle and bed temperatures, layer height, seam, ironing, … (`src/os2slice/extra_settings.py` has the list). Each one ticked appears in the panel, empty, so the profile's value applies until someone fills it in. Filament settings go into every filament of the print; BamBuddy printers refuse them.

```toml
[panel]
local_slicer = "orcaslicer"
web_slicer = "orcaslicer"
extra_settings = ["chamber_temperature", "layer_height"]
```

## 5. Onshape

Follow [`ONSHAPE_SETUP.md`](ONSHAPE_SETUP.md), with `<host>` = your `hosts` entry. Then:

1. Put the client ID in `docker/config.toml` and the secret in `.env`.
2. Run `docker compose up -d` to apply them.

## Other printers and slicers

os2slice can slice with a Bambu Studio or OrcaSlicer API sidecar instead of BamBuddy, and send to Klipper (Moonraker) or Prusa (PrusaLink) printers ([`MODULES.md`](MODULES.md)).

The `orca` profile runs an OrcaSlicer sidecar (`ghcr.io/maziggy/orca-slicer-api`, the fork that takes several filaments per slice). It publishes no port; os2slice reaches it on the compose network:

```toml
[slicers.orca]
kind = "orca-slicer-api"
url = "http://orca-slicer-api:3000"
profile_dir = "/orca-profiles"             # optional: your own OrcaSlicer profiles
```

**Your own profiles.** Point `profile_dir` at an OrcaSlicer config folder (the one with `user/` and `system/`), or at one user folder (`user/<id>/`). os2slice then:

- offers your own process and filament profiles in the Print panel (filaments from imported preset bundles in `_local/` included), when the printer's target can't report what's loaded and the printer lists no `materials`;
- offers your own printer profiles too ("Printer profile" in the panel). A printer profile whose start G-code calls Happy Hare's `MMU_*` macros uses the filament changer: with it, the panel lists the changer's tools (T0, T1, …, from Moonraker's `mmu` object: each gate's material, colour, name and Spoolman spool), each matched to one of your filament profiles of the same type, preferring the spool's vendor in the profile's name. A print from tools loads every tool's filament in tool order, so the G-code's T<n> is the changer's tool n, as in the desktop OrcaSlicer. A filament profile that isn't loaded gets a "Load into" menu: the print is sliced as if it were in that tool, and the job page says what to load before pressing Start ("T1: load 3DO ASA (now …)");
- uploads each profile resolved against the system presets it inherits, within that preset's own vendor, as the GUI does. A RatRig filament and an Orca Filament Library one get their own base presets, which the sidecar alone can't do (it looks in one folder).

Profiles saved later are used from the next print, with no restart.

The `orca-web` profile runs OrcaSlicer itself in a browser tab, `https://<host>:3444` (user `orca`, password `WEB_ORCA_PASSWORD` in `.env`; the certificate is self-signed). Sign in to your Orca account there, and edit profiles there; os2slice reads its config folder read-only:

```toml
profile_dir = "/orca-web/.config/OrcaSlicer"
```

**Open in OrcaSlicer.** With a `[web_orca]` table, the Print panel gets an "Open in OrcaSlicer: in the browser" link beside the Bambu Studio ones. It slices the selection with the printer's slicer (for its settings), drops the project in the shared inbox, and the web OrcaSlicer's own inbox service opens it in the running window (built from `addon/orcaslicer_web`, which reuses the web Bambu Studio's inbox service):

```toml
[web_orca]
url = "https://print.example.lan:3444"   # what browsers open
# inbox = "/share/os2slice/orca-inbox"   # defaults, shared with the orca-web service
# status = "/share/os2slice/web-orca.json"
```

To start it with the profiles you already have, copy your desktop's `~/.config/OrcaSlicer/` (`OrcaSlicer.conf`, `system/`, `user/<id>/`; not the token files) into the `orca-web-config` volume under `.config/OrcaSlicer/`, owned by 1000, before its first start. Without a web OrcaSlicer, copy a user folder to `./orca-profiles` and use `profile_dir = "/orca-profiles"` (then `ORCA_VENDOR` must name the vendor its parents come from).

For a Bambu Studio sidecar, run `ghcr.io/maziggy/bambu-studio-api` yourself, or use the one BamBuddy's own compose file starts (versions and ports in [`SLICERAPI_API.md`](SLICERAPI_API.md)), and point a `kind = "bambu-studio-api"` slicer at its URL.

Then name the slicer key as the `slicer` of a printer (`[printers.<key>]`) or of a model (`[targets.<key>.models."<model>"]`). A Klipper printer through Moonraker:

```toml
[targets.voron]
kind = "moonraker"
url = "http://host.docker.internal:7125"   # Moonraker on this host; it must trust Docker's 172.16.0.0/12
ui_url = "http://voron.lan"                # Mainsail, as browsers reach it

[printers.voron]
target = "voron"
slicer = "orca"
model = "Voron 2.4 350"
bed_mm = [350, 350]
profiles = { printer = "My Voron 0.4", process = "0.20mm Mine", filament = "My PLA" }
bed_type = "High Temp Plate"
```

PrusaLink targets are built from their API docs and not yet tested on a printer.

**Formlabs Form 4.** The `preform` profile runs Formlabs' own PreForm Server for Linux (`formlabs/preform-server`, pinned by `PREFORM_TAG` in `.env`). os2slice's `preform-server` module both slices and sends: it imports the part, orients it (your face down, or PreForm's auto-orient), adds supports, lays out copies, runs PreForm's checks, saves a `.form` job and puts it in the printer's queue. Files go through the shared volume (`/share/os2slice/preform`); the server publishes no port, since its API has no authentication.

```toml
[targets.formlabs]
kind = "preform-server"
url = "http://preform-server:44388"
printers = ["192.168.1.142"]   # the Form 4's address (give it a DHCP reservation): mDNS doesn't reach containers
version = "3.64.1.664"         # what PREFORM_TAG runs; doctor warns when they differ
# share_dir = "/share/os2slice/preform"   # the default
# remote_start = false         # default: jobs wait on the printer's touchscreen (D-36)

[targets.formlabs.models."Form 4"]   # optional defaults; without them: the loaded resin at 0.1 mm
profiles = { printer = "FORM-4-0", process = "0.1 mm", filament = "FLGPGR05" }
```

The Form 4 appears in the printer menu as `formlabs/<its serial name>`, with the loaded resin first in the Resin menu and the layer heights PreForm offers. API notes in [`PREFORM_API.md`](PREFORM_API.md).

## Day to day

| Task | Command |
|---|---|
| Update | `git pull && docker compose --profile web-studio --profile duckdns up -d --build` (add `--profile orca --profile orca-web` if you use them) |
| Logs | `docker compose logs -f os2slice` (also `bambustudio-web`, `duckdns`) |
| Check setup | `docker compose exec os2slice os2slice doctor` |
| Back up | the volumes `os2slice-data` (sign-ins, admin password, page-saved secrets, log), `bambustudio-config` (Bambu Studio settings) and `orca-web-config` (your OrcaSlicer profiles), plus `docker/config.toml`, `.env` and `./certs` |

**Certificate renewal:**
- `duckdns` renews about 30 days before expiry.
- os2slice picks up the new files by itself, within an hour.
- The web Bambu Studio only copies them when it starts, so restart it after a renewal.

**Security:**
- Keep ports 8443 and 3443 on the LAN only: don't forward them from the router.
- The web Bambu Studio runs without AppArmor and seccomp confinement (`security_opt` in `docker-compose.yml`), because Docker's default AppArmor profile makes Bambu Studio abort. Give it a password if the LAN isn't trusted.
