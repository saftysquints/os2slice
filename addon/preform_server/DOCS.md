# PreForm Server

Formlabs' PreForm Server: PreForm without its window, behind a local HTTP API, from
Formlabs' official Linux image (`formlabs/preform-server`). os2slice's `preform-server`
module uses it to import a part, orient and support it, lay out copies, check it, save a
`.form` job and send it to a Form 4's queue (docs/PREFORM_API.md).

- **No port is published.** The API has no authentication, so only other add-ons reach
  it, at `http://local-preform-server:44388`. Don't add a port mapping.
- **Files** go through `/share/os2slice/preform`, one folder per job, removed when the job
  is done. os2slice and this add-on see it at the same path.
- **Printers** are found by IP address (mDNS doesn't reach add-ons): list the Form 4's
  address in the os2slice target's `printers` field, and give it a DHCP reservation.
- **Jobs wait at the printer** unless the os2slice target's `remote_start` is on: someone
  starts them on the Form 4's touchscreen.
- `/tmp` is a tmpfs: with a persistent `/tmp`, a restarted server finds Xvfb's stale lock
  and never answers again. Telemetry is off (`DISABLE_PREFORMSERVER_TELEMETRY=1`).
- Memory: about 150 MB idle and up to 1.4 GB while preparing a full platform; it keeps
  what it grew to until it restarts.
- The version is pinned in the Dockerfile. PreForm Server and the printer's firmware may
  need to stay in step: upgrade the tag, this add-on's version and the target's `version`
  together.
- If the server exits at start on Home Assistant OS with an AppArmor denial in the host
  log, set `apparmor: false` in `config.yaml` (the Bambu Studio add-on needed that; PreForm
  Server ran under Docker's default profile on Ubuntu).
