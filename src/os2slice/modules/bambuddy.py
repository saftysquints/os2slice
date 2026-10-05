"""BamBuddy as a slicer and a target (role "both"), over `os2slice.bambuddy.BambuddyClient`.

Slicing uploads the STL (one plain part) or a Bambu project 3MF (several parts, copies,
or a loaded filament on a dual-nozzle printer) to BamBuddy's library and slices it with
BamBuddy's own slicer; the result stays in the library, so `submit` queues it without a
second upload. Shapes are in docs/BAMBUDDY_API.md; decisions D-14, D-19, D-20, D-25.

As a target it also offers one pool per model, "Any <model>": the queue item names the
model instead of a printer, and BamBuddy's scheduler dispatches it to the first idle
printer of that model with the chosen filaments (type and colour) loaded. On a
dual-nozzle model each pool filament is pinned to the nozzle that feeds it on most of
the model's printers, and a job no printer can take on those nozzles isn't queued (D-35).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar

import httpx

from os2slice import files
from os2slice.bambuddy import BambuddyClient, BambuddyError, PresetChoice, SliceResult
from os2slice.errors import AuthError, Os2sliceError
from os2slice.filaments import (
    EXTERNAL_IDS,
    NOZZLE_NAMES,
    Slot,
    color_name,
    compatible_presets,
    match_preset,
    preset_suffix,
    sliced_nozzle,
    sliced_type,
    slots_from_status,
)
from os2slice.modules.bambu_project import BED_MM, Project, layout, with_tower_retries
from os2slice.modules.base import (
    MEDIA_GCODE_3MF,
    Field,
    Health,
    Material,
    ModelDefaults,
    ModuleAuthError,
    ModuleError,
    ModuleSpec,
    PrinterInfo,
    PrinterStatus,
    ProfileCatalog,
    Profiles,
    Progress,
    SliceInput,
    SliceOutput,
    Submission,
    distinct_materials,
)

log = logging.getLogger(__name__)

READY_STATES = ("IDLE", "FINISH", "FAILED")
OWN_KEYS = ("printer_id", "target_model", "members")  # discovered; config can't override
PRESET_CACHE_S = 600.0

SPEC = ModuleSpec(
    kind="bambuddy",
    label="BamBuddy",
    role="both",
    technology="fdm",
    fields=(
        Field("url", "BamBuddy URL", "url", required=True, help="e.g. http://127.0.0.1:8000"),
        Field(
            "folder",
            "Library folder",
            "str",
            default="Onshape",
            help="one subfolder per Onshape document",
        ),
        Field(
            "manual_start",
            "Wait for Start in BamBuddy (deprecated)",
            "bool",
            default=False,
            help="deprecated, has no effect: the person printing ticks Wait for Start on "
            "the page (the CLI has --wait-for-start); prints start by themselves otherwise",
        ),
        Field(
            "public_url",
            "BamBuddy URL for browsers",
            "url",
            help="for the 'Open BamBuddy' links; default: this host, port 8000",
        ),
        Field(
            "api_key",
            "API key",
            "secret",
            required=True,
            help="read status, library and queue permissions only",
        ),
    ),
    makes=(MEDIA_GCODE_3MF,),
    accepts=(MEDIA_GCODE_3MF,),
    discovers_printers=True,
    help="BamBuddy slices with its Bambu Studio sidecar and queues on Bambu printers.",
)


class BambuddyModule:
    """One BamBuddy instance: slicer and target. Opens a client per call (thread-safe)."""

    spec: ClassVar[ModuleSpec] = SPEC

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        key: str = "bambuddy",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        api_key = values.get("api_key")
        if not api_key:
            raise AuthError(
                f"No BamBuddy API key for [targets.{key}]",
                "Run `os2slice setup-keys --bambuddy`"
                if key == "bambuddy"
                else f"Run `os2slice setup-keys --secret targets.{key}.api_key`",
            )
        self.key = key
        self.url = str(values["url"]).rstrip("/")
        self.folder = str(values.get("folder") or "Onshape")
        self.public_url = str(values.get("public_url") or "").rstrip("/")
        self.models: Mapping[str, ModelDefaults] = values.get("models") or {}
        self._api_key = str(api_key)
        self._transport = transport
        self._names: tuple[float, list[str]] = (0.0, [])
        self._lock = threading.Lock()

    def client(self) -> BambuddyClient:
        return BambuddyClient(self.url, self._api_key, transport=self._transport)

    def ui_url(self, request_host: str = "") -> str:
        """BamBuddy's queue as browsers reach it (the configured url is often internal)."""
        base = self.public_url
        if not base and request_host:
            base = f"http://{request_host.rsplit(':', 1)[0]}:8000"
        return f"{base}/queue" if base else ""

    # -- both ------------------------------------------------------------------

    def check(self) -> Health:
        try:
            with self.client() as bb:
                printers = bb.list_printers()
                auth_on = bb.auth_enabled()
        except Os2sliceError as e:
            return Health(False, e.one_line())
        summary = f"{self.url}: {len(printers)} printers"
        if not auth_on:
            summary += "; auth off by choice (D-16), os2slice still sends its key"
        missing = sorted({p.model for p in printers if p.is_active and p.model not in self.models})
        detail = (
            f"no profiles for model(s) {', '.join(missing)}; those printers can't be used"
            if missing
            else ""
        )
        return Health(True, summary, detail=detail)

    # -- slicer ----------------------------------------------------------------

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        with self.client() as bb:
            names = bb.preset_names()
        return ProfileCatalog(
            tuple(names["printer"]), tuple(names["process"]), tuple(names["filament"])
        )

    def slice(self, job: SliceInput, progress: Progress) -> SliceOutput:
        if job.media not in (None, MEDIA_GCODE_3MF):
            raise ModuleError(f"BamBuddy can't make {job.media} files", "Pick another slicer")
        if job.settings.filament_overrides(job.bed_type):
            raise ModuleError(
                "BamBuddy slices with its filament presets as they are",
                "Leave the panel's filament settings (temperatures, flow...) empty for "
                "this printer",
            )
        printer = job.printer
        dual = printer.nozzle_count > 1
        filaments = distinct_materials(job.parts)
        as_project = (
            bool(job.extra.get("project"))
            or len(job.parts) > 1
            or job.copies > 1
            or (dual and bool(filaments))
        )
        # Lay the project out first: too many copies is refused before anything is uploaded.
        project = layout(job) if as_project else None
        presets = PresetChoice(
            job.profiles.printer,
            job.profiles.process,
            job.profiles.filament,
            source=str(printer.extra.get("preset_source") or "standard"),
        )
        colours = [m.colour for m in filaments if m.colour]
        with self.client() as bb:
            root = bb.ensure_folder(self.folder)
            document = str(job.extra.get("document") or "")
            folder = bb.ensure_folder(files.sanitize(document, "document"), root)
            if project is None:
                progress("Uploading to BamBuddy")
                name = f"{job.job_name}.stl"
                file_id = bb.upload(folder, name, job.parts[0].stl)
                progress("Slicing")
                slice_job = bb.start_slice(
                    file_id,
                    presets,
                    job.settings,
                    auto_orient=job.auto_orient,
                    filament_colours=colours or None,
                    bed_type=job.bed_type,
                    extra_overrides=dict(job.process_overrides) or None,
                )
                sliced = bb.wait_for_slice(slice_job, on_status=lambda s: progress(f"Slicing: {s}"))
            else:
                progress("Uploading to BamBuddy")
                name = f"{job.job_name}.3mf"
                file_id = bb.upload(folder, name, project.threemf)
                sliced = self._slice_project(bb, file_id, job, project, presets, colours, progress)
            log.info("sliced %s → library file %s", name, sliced.library_file_id)
            data = bb.download_file(sliced.library_file_id)
        if dual and filaments and all(m.extruder is not None for m in filaments):
            # Every filament must print on the nozzle its slot feeds. A pool's ("Any H2D")
            # filaments carry the nozzle that feeds them on most of its printers (D-35);
            # skipped when one has none (a bare preset, or no member says): the slicer
            # picks the nozzle then, and submit() checks a member can take it.
            for n, m in enumerate(filaments, start=1):
                got = sliced_nozzle(data, n)
                if got != m.extruder:
                    raise BambuddyError(
                        f"Filament {n} prints on the {NOZZLE_NAMES.get(got, 'unknown')} nozzle, "  # type: ignore[arg-type]
                        f"but {m.raw.get('slot', m.label)} feeds the "
                        f"{NOZZLE_NAMES[m.extruder]} one; not queued",  # type: ignore[index]
                        "BamBuddy's slicer changed how it maps nozzles; see docs/BAMBUDDY_API.md",
                    )
        return SliceOutput(
            data=data,
            filename=f"{job.job_name}.gcode.3mf",
            media=MEDIA_GCODE_3MF,
            print_time_s=sliced.print_time_seconds,
            material_g=sliced.filament_used_g,
            report={
                "module": SPEC.kind,
                "url": self.url,
                "library_file_id": sliced.library_file_id,
                "name": sliced.name,
            },
        )

    def _slice_project(
        self,
        bb: BambuddyClient,
        file_id: int,
        job: SliceInput,
        project: Project,
        presets: PresetChoice,
        colours: list[str],
        progress: Progress,
    ) -> SliceResult:
        """Slice the uploaded 3MF, moving the prime tower on tower errors (D-20)."""

        def attempt(tower: dict[str, str]) -> SliceResult:
            slice_job = bb.start_slice(
                file_id,
                presets,
                job.settings,
                auto_orient=job.auto_orient,
                filament_colours=colours or None,
                bed_type=job.bed_type,
                filament_presets=list(project.filament_profiles),
                extra_overrides={**job.process_overrides, **tower},
                auto_arrange=job.auto_arrange,
            )
            return bb.wait_for_slice(slice_job, on_status=lambda s: progress(f"Slicing: {s}"))

        progress("Slicing")
        return with_tower_retries(project.tower_spots(job.printer), attempt, progress)

    # -- target ----------------------------------------------------------------

    def printers(self, configured: tuple[PrinterInfo, ...] = ()) -> tuple[PrinterInfo, ...]:
        """BamBuddy's printers with the per-model defaults, then per-printer overrides;
        then one pool ("Any <model>") per model that has defaults, after the printers."""
        with self.client() as bb:
            found = bb.list_printers()
        overrides = {c.name: c for c in configured}
        out = []
        by_model: dict[str, list[PrinterInfo]] = {}
        for p in found:
            d = self.models.get(p.model, ModelDefaults())
            extra: dict[str, Any] = {"printer_id": p.id, **d.extra}
            if d.bed_type:
                extra["bed_type"] = d.bed_type
            info = PrinterInfo(
                key=f"{self.key}/{p.id}",
                name=p.name,
                technology="fdm",
                model=p.model,
                target=self.key,
                slicer=d.slicer or self.key,
                profiles=d.profiles,
                bed_mm=BED_MM.get(p.model),
                nozzle_count=p.nozzle_count,
                active=p.is_active,
                ui_url=self.ui_url(),
                extra=extra,
            )
            if p.model in self.models:
                by_model.setdefault(p.model, []).append(info)
            if p.name in overrides:
                info = _override(info, overrides[p.name])
            out.append(info)
        out.extend(self._pools(by_model, overrides))
        return tuple(out)

    def _pools(
        self, by_model: dict[str, list[PrinterInfo]], overrides: Mapping[str, PrinterInfo]
    ) -> list[PrinterInfo]:
        """One "Any <model>" per model with defaults: BamBuddy dispatches a job queued on
        it to the first idle printer of the model with the filament loaded."""
        out = []
        for model, members in by_model.items():
            d = self.models[model]
            extra: dict[str, Any] = {
                **d.extra,
                "target_model": model,
                "members": tuple(m.extra["printer_id"] for m in members if m.active),
            }
            if d.bed_type:
                extra["bed_type"] = d.bed_type
            info = PrinterInfo(
                # "<target>/any:<model>": ':' is in neither BamBuddy's numeric ids nor a
                # [printers.<key>] key (config.PRINTER_KEY_RE), so it can't collide.
                key=f"{self.key}/any:{model}",
                name=f"Any {model}",
                technology="fdm",
                model=model,
                target=self.key,
                slicer=d.slicer or self.key,
                profiles=d.profiles,
                bed_mm=BED_MM.get(model),
                nozzle_count=max(m.nozzle_count for m in members),
                active=any(m.active for m in members),
                ui_url=self.ui_url(),
                extra=extra,
                pool=True,
            )
            if info.name in overrides:
                info = _override(info, overrides[info.name])
            out.append(info)
        return out

    def status(self, printer: PrinterInfo) -> PrinterStatus:
        if printer.pool:
            return self._pool_status(printer)
        dual = printer.nozzle_count > 1
        suffix = preset_suffix(printer.profiles.filament)
        try:
            with self.client() as bb:
                raw = bb.printer_status(int(printer.extra["printer_id"]))
                slots = slots_from_status(raw, dual)
                names = self._filament_names(bb) if slots and suffix else []
        except ModuleAuthError:
            raise
        except ModuleError as e:  # BamBuddy down or refusing: the printer is offline to us
            log.warning("bambuddy %s: no status for %s: %s", self.url, printer.name, e.message)
            return PrinterStatus("offline", False, False, e.message)
        materials = tuple(
            Material(
                id=str(s.tray_id),
                label=s.describe(),
                kind=s.material,
                colour=s.color,
                extruder=s.extruder if s.extruder in NOZZLE_NAMES else None,
                profile=match_preset(s, suffix, names) or "",
                raw={"tray_id": s.tray_id, "slot": s.label, "brand": s.brand},
            )
            for s in slots
        )
        return _status_of(raw, materials)

    def _pool_status(self, printer: PrinterInfo) -> PrinterStatus:
        """A pool's status: ready when any member is; its materials are the distinct
        (type, colour) pairs loaded across the members, what BamBuddy's scheduler can
        match a job against (docs/BAMBUDDY_API.md). On a dual-nozzle model each one
        carries the nozzle that feeds it on most members (`_pool_nozzle`, D-35), so the
        slice is pinned to it like a single printer's (D-20).
        """
        members = [int(i) for i in printer.extra.get("members") or ()]
        suffix = preset_suffix(printer.profiles.filament)
        dual = printer.nozzle_count > 1
        states: list[PrinterStatus] = []
        # (type, colour) → (first slot seen, member per slot, member → nozzles feeding it)
        found: dict[tuple[str, str], tuple[Slot, list[int], dict[int, set[int]]]] = {}
        names: list[str] = []
        try:
            with self.client() as bb:
                for pid in members:
                    try:
                        raw = bb.printer_status(pid)
                    except ModuleAuthError:
                        raise
                    except ModuleError as e:  # one member down: the others may still do
                        log.warning(
                            "bambuddy %s: no status for printer %s: %s", self.url, pid, e.message
                        )
                        states.append(PrinterStatus("offline", False, False, e.message))
                        continue
                    states.append(_status_of(raw, ()))
                    for slot in slots_from_status(raw, dual):
                        colour = slot.color.upper() if slot.color_known else ""
                        k = (slot.material.upper(), colour)
                        _, on, feeds = found.setdefault(k, (slot, [], {}))
                        on.append(pid)
                        if slot.extruder is not None and slot.extruder in NOZZLE_NAMES:
                            feeds.setdefault(pid, set()).add(slot.extruder)
                if found and suffix:
                    names = self._filament_names(bb)
        except ModuleAuthError:
            raise
        except ModuleError as e:
            log.warning("bambuddy %s: no status for %s: %s", self.url, printer.name, e.message)
            return PrinterStatus("offline", False, False, e.message)
        n = len(states)
        # A tray that reports no colour is offered as "any colour": its type only, so
        # the job isn't forced onto a made-up grey that no printer has (pool_overrides).
        materials = []
        for slot, on, feeds in found.values():
            extruder, nozzle_note = _pool_nozzle(feeds) if dual else (None, "")
            materials.append(
                Material(
                    id=pool_material_id(slot.material, slot.color if slot.color_known else ""),
                    label=f"{slot.brand or slot.material} · "
                    + (color_name(slot.color) if slot.color_known else "any colour")
                    + nozzle_note
                    + _loaded_note(len(on), len(set(on)), n),
                    kind=slot.material,
                    colour=slot.color if slot.color_known else None,
                    extruder=extruder,
                    profile=match_preset(slot, suffix, names) or "",
                    raw={"type": slot.material, "brand": slot.brand, "printers": tuple(on)},
                )
            )
        ready = sum(1 for st in states if st.ready)
        if not any(st.connected for st in states):
            detail = "no printer of this model answers" if n else "no active printer"
            return PrinterStatus("offline", False, False, detail, tuple(materials))
        return PrinterStatus(
            state="IDLE" if ready else "BUSY",
            connected=True,
            ready=ready > 0,
            detail=f"{ready} of {n} free",
            materials=tuple(materials),
        )

    def filament_presets(self, printer: PrinterInfo) -> tuple[str, ...]:
        """For a pool: every filament preset made for its model (by name, D-19), sorted,
        from the cached preset list. The panel offers them after the loaded filaments;
        one is sliced with as is and queued without a filament override, so BamBuddy
        matches its type from the file. Empty for a single printer."""
        if not printer.pool:
            return ()
        suffix = preset_suffix(printer.profiles.filament)
        with self.client() as bb:
            names = self._filament_names(bb)
        return tuple(compatible_presets(names, suffix, printer.profiles.printer))

    def submit(
        self,
        printer: PrinterInfo,
        output: SliceOutput,
        *,
        start: bool,
        materials: tuple[Material, ...] = (),
        progress: Progress = lambda s: None,
    ) -> Submission:
        """Queue the sliced file. `start=False` → manual_start: it waits for Start (D-13).
        `start` is the only source; the `manual_start` config value is deprecated and unused.

        On a pool BamBuddy picks the printer: the item names the model, and each chosen
        material becomes a forced type+colour filament override; no tray ids and no AMS
        mapping (BamBuddy maps the trays of the printer it picks, at dispatch). On a
        dual-nozzle model it is refused first unless a member has every filament on the
        nozzle the file prints it with (`_check_pool_nozzles`, D-35).
        """
        if output.media != MEDIA_GCODE_3MF:
            raise ModuleError(f"BamBuddy can't print {output.media} files")
        mapping: list[int] | None = None
        use_ams: bool | None = None
        if not printer.pool:
            try:
                trays = [int(m.id) for m in materials]
            except ValueError as e:
                raise ModuleError("BamBuddy needs AMS tray ids as the materials") from e
            if len(trays) == 1 and trays[0] in EXTERNAL_IDS:
                use_ams = False  # one external spool: bypass the AMS (to verify at a printer)
            elif trays:
                mapping, use_ams = trays, True
        report = output.report
        with self.client() as bb:
            if printer.pool and printer.nozzle_count > 1:
                self._check_pool_nozzles(bb, printer, output.data, materials)
            file_id = report.get("library_file_id")
            if not (report.get("module") == SPEC.kind and report.get("url") == self.url
                    and isinstance(file_id, int)):  # fmt: skip
                # Sliced elsewhere: put the file into our library folder first.
                progress("Uploading to BamBuddy")
                file_id = bb.upload(bb.ensure_folder(self.folder), output.filename, output.data)
            if printer.pool:
                item = bb.queue_print(
                    file_id,
                    None,
                    not start,
                    target_model=str(printer.extra["target_model"]),
                    filament_overrides=pool_overrides(materials) or None,
                )
            else:
                item = bb.queue_print(
                    file_id, int(printer.extra["printer_id"]), not start, mapping, use_ams
                )
        if printer.pool:
            idle = f"an idle {printer.model} with the filament loaded"
            how = f"waits for {idle}" if start else f"waits for Start in BamBuddy, then for {idle}"
            reason = str(item.get("waiting_reason") or "")
            if reason:
                how += f" ({reason[:200]})"
        else:
            how = "starts when the printer is free" if start else "waits for Start in BamBuddy"
        return Submission(
            id=str(item.get("id")),
            state="started" if start else "waiting",
            detail=f"item {item.get('id')}, {how}",
            url=self.ui_url(),
            raw=item,
        )

    def _check_pool_nozzles(
        self,
        bb: BambuddyClient,
        printer: PrinterInfo,
        data: bytes,
        materials: tuple[Material, ...],
    ) -> None:
        """Refuse a dual-nozzle pool job that no member printer can take as sliced.

        BamBuddy's scheduler picks a printer by filament type and colour alone, then at
        dispatch maps each filament only to trays on the nozzle the file prints it with;
        when that finds none the item goes out with `use_ams` and no AMS mapping and the
        printer fails it (0700-7000-0002-0008, docs/BAMBUDDY_API.md). So: some member,
        from its live status, must have every filament (the forced type and colour, or a
        preset's type as the file names it) in a slot feeding that filament's nozzle. A
        filament whose nozzle the file doesn't say isn't checked (BamBuddy doesn't filter
        it either); a member with a filament switch (FTS) feeds either nozzle.
        """
        wants: list[tuple[str, str | None, int]] = []  # (type, colour or None, nozzle)
        chosen: tuple[Material | None, ...] = materials or (None,)
        for n, m in enumerate(chosen, start=1):
            nozzle = sliced_nozzle(data, n)
            if nozzle is None or nozzle not in NOZZLE_NAMES:
                continue
            colour = m.colour if m is not None and not m.raw.get("preset") else None
            if m is not None and colour:
                kind = str(m.raw.get("type") or m.kind)  # forced: pool_overrides
            else:
                colour = None
                kind = sliced_type(data, n) or (m.kind if m is not None else "")
            if kind:
                wants.append((kind, colour, nozzle))
        if not wants:
            return
        members = [int(i) for i in printer.extra.get("members") or ()]
        answered = 0
        for pid in members:
            try:
                raw = bb.printer_status(pid)
            except ModuleAuthError:
                raise
            except ModuleError as e:
                log.warning("bambuddy %s: no status for printer %s: %s", self.url, pid, e.message)
                continue
            answered += 1
            if _can_take(raw, wants):
                return
        if not answered:
            raise ModuleError(
                f"Couldn't check which {printer.model} can print this: no printer answered; "
                "not queued",
                "Check BamBuddy and the printers, then try again",
            )
        need = "; ".join(
            f"{kind} {color_name(colour) if colour else '(any colour)'} on the "
            f"{NOZZLE_NAMES[nozzle]} nozzle"
            for kind, colour, nozzle in wants
        )
        raise ModuleError(
            f"No {printer.model} has {need}, as the file was sliced; not queued",
            "BamBuddy only maps a filament to an AMS feeding the nozzle it was sliced for. "
            "Load it in an AMS on that side of one of the printers, or print on a "
            "specific printer",
        )

    # -- plumbing --------------------------------------------------------------

    def _filament_names(self, bb: BambuddyClient) -> list[str]:
        """BamBuddy's filament preset names, cached for 10 minutes."""
        with self._lock:
            stamp, names = self._names
            if time.monotonic() - stamp < PRESET_CACHE_S and names:
                return names
        names = bb.filament_preset_names()
        with self._lock:
            self._names = (time.monotonic(), names)
        return names


def _override(info: PrinterInfo, o: PrinterInfo) -> PrinterInfo:
    """A configured [printers."<name>"] entry over a discovered printer, field by field."""
    profiles = Profiles(
        o.profiles.printer or info.profiles.printer,
        o.profiles.process or info.profiles.process,
        o.profiles.filament or info.profiles.filament,
    )
    return replace(
        info,
        slicer=o.slicer or info.slicer,
        profiles=profiles,
        bed_mm=o.bed_mm or info.bed_mm,
        extra={**info.extra, **o.extra, **{k: info.extra[k] for k in OWN_KEYS if k in info.extra}},
        materials=o.materials or info.materials,
    )


def _status_of(raw: Mapping[str, Any], materials: tuple[Material, ...]) -> PrinterStatus:
    """One printer's PrinterStatus from BamBuddy's status body."""
    connected = bool(raw.get("connected", True))
    state = str(raw.get("state") or ("unknown" if connected else "offline"))
    waiting = bool(raw.get("awaiting_plate_clear"))
    return PrinterStatus(
        state=state,
        connected=connected,
        ready=connected and state.upper() in READY_STATES and not waiting,
        detail="waiting for the plate to be cleared" if waiting else "",
        materials=materials,
        raw=raw,
    )


def _pool_nozzle(feeds: Mapping[int, set[int]]) -> tuple[int | None, str]:
    """The nozzle a dual-nozzle pool filament is pinned to, and its label note.

    `feeds` is member printer → the nozzles a slot holding the filament feeds there.
    The nozzle that feeds it on the most printers wins (a printer with it on both
    sides counts for both); the right (main) nozzle on a tie. None, and no note,
    when no member says which nozzle (an AMS missing from `ams_extruder_map`).
    """
    counts = {n: sum(1 for nozzles in feeds.values() if n in nozzles) for n in NOZZLE_NAMES}
    if not any(counts.values()):
        return None, ""
    best = max(sorted(counts), key=lambda n: counts[n])  # max keeps the first: 0 on a tie
    note = f" · {NOZZLE_NAMES[best]} nozzle"
    others = [n for n in sorted(counts) if n != best and counts[n]]
    if others:
        note += f" on {counts[best]}, " + ", ".join(
            f"{NOZZLE_NAMES[n]} on {counts[n]}" for n in others
        )
    return best, note


def _can_take(raw: Mapping[str, Any], wants: list[tuple[str, str | None, int]]) -> bool:
    """Whether a dual-nozzle printer (its status body) has each wanted filament (type,
    colour or None for any, nozzle) in a slot feeding that nozzle, as BamBuddy's
    dispatch-time matcher requires (docs/BAMBUDDY_API.md)."""
    switch = raw.get("fila_switch")
    fts = isinstance(switch, Mapping) and bool(switch.get("installed"))
    slots = slots_from_status(dict(raw), True)

    def fits(s: Slot, kind: str, colour: str | None, nozzle: int) -> bool:
        if _canonical_type(s.material) != _canonical_type(kind):
            return False
        if colour is not None and not (
            s.color_known and s.color.upper() == f"#{colour.lstrip('#').upper()[:6]}"
        ):
            return False
        return fts or s.extruder == nozzle

    return all(any(fits(s, *want) for s in slots) for want in wants)


# Types BamBuddy treats as one (v1.2.5.7 utils/filament_types.py FILAMENT_TYPE_GROUPS).
_TYPE_GROUPS = {"PA12-CF": "PA-CF", "PAHT-CF": "PA-CF"}


def _canonical_type(kind: str) -> str:
    upper = kind.strip().upper()
    return _TYPE_GROUPS.get(upper, upper)


def _loaded_note(slots: int, printers: int, members: int) -> str:
    """ " (loaded …)" for a pool material: how many slots hold it, on how many of the
    model's printers. `slots` counts AMS slots and external spools across the members."""
    where = f" in {slots} slots" if slots > printers else ""
    if members > 1:
        return f" (loaded{where} on {printers} of {members} printers)"
    return f" (loaded{where})"


def pool_material_id(kind: str, colour: str) -> str:
    """A pool's material id, "<TYPE>.<RRGGBB>" (the server's MATERIAL_ID shape): what is
    wanted, not where it's loaded. Stable while the type and colour are. No colour (a
    tray that reports none): "<TYPE>.ANY"."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", kind.upper())[:24] or "X"
    return f"{safe}.{colour.lstrip('#').upper()[:6] or 'ANY'}"


def pool_overrides(materials: tuple[Material, ...]) -> list[dict[str, Any]]:
    """BamBuddy's filament_overrides for a pool: filament n (1-based, the print's
    filament order) must be exactly this type and colour (force_color_match), so the
    scheduler only dispatches to a printer that has each one loaded.

    Shape read from BamBuddy 1.0.22 (print_scheduler._get_missing_force_color_slots and
    _apply_filament_overrides, the frontend's PrintQueueItemCreate): slot_id, type,
    color ("#RRGGBB"; compared on its first six hex digits, any case), color_name
    (only for messages), force_color_match. A material without a type or colour adds
    nothing: BamBuddy then matches the 3MF's own filament type.
    """
    out: list[dict[str, Any]] = []
    for n, m in enumerate(materials, start=1):
        kind = str(m.raw.get("type") or m.kind)
        if m.raw.get("preset") or not kind or not m.colour:
            continue  # a preset, not a loaded filament: BamBuddy matches the file's type
        out.append(
            {
                "slot_id": n,
                "type": kind,
                "color": m.colour,
                "color_name": color_name(m.colour),
                "force_color_match": True,
            }
        )
    return out
