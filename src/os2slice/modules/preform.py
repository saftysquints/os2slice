"""PreForm Server (Formlabs) as a slicer and a target (role "both"), for the Form 4.

PreForm Server is PreForm without its window behind a local HTTP API, shipped by Formlabs
as the `formlabs/preform-server` Linux container. Shapes are in docs/PREFORM_API.md (✅
rows verified live against the server; nothing yet from os2slice). Slicing makes one
scene per job from an STL in the folder both containers mount (`share_dir`): orient,
supports, copies, layout, checks and print time, then saves a .form file. Submitting
reopens that file and uploads it to the printer's queue with `print_now` always sent:
true only when the print should start and `remote_start` is on (D-36). The module never
logs in, never uploads firmware and never touches a scene it didn't make.

The server has no auth: anything that reaches its port can print. Never publish it on
the LAN; os2slice reaches it on the containers' internal network.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from os2slice import __version__
from os2slice.errors import ConfigError
from os2slice.modules.base import (
    MEDIA_FORM,
    Field,
    Health,
    Material,
    ModelDefaults,
    ModuleError,
    ModuleSpec,
    PartGeometry,
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

RAFT_TYPES = ("FULL_RAFT", "MINI_RAFT", "MINI_RAFTS_ON_BP")

SPEC = ModuleSpec(
    kind="preform-server",
    label="PreForm Server (Formlabs)",
    role="both",
    technology="sla",
    fields=(
        Field(
            "url",
            "PreForm Server URL",
            "url",
            required=True,
            help="Home Assistant add-on: http://local-preform-server:44388; Docker Compose: "
            "http://preform-server:44388. The server has no auth: never publish its port "
            "on the LAN.",
        ),
        Field(
            "share_dir",
            "Shared folder",
            "path",
            default="/share/os2slice/preform",
            help="a folder os2slice and PreForm Server both mount; one subfolder per job, "
            "removed afterwards",
        ),
        Field(
            "server_share_dir",
            "Shared folder as the server sees it",
            "path",
            help="the same folder inside PreForm Server's container; default: the same path",
        ),
        Field(
            "printers",
            "Printer addresses",
            "list",
            help="each printer's IP address (or serial name, e.g. Form4-ClairvoyantAnt), "
            "looked up directly because mDNS doesn't reach the container",
        ),
        Field(
            "version",
            "Pinned PreForm Server version",
            "str",
            help="e.g. 3.64.1.664, the image tag's; the health check warns when another runs",
        ),
        Field(
            "remote_start",
            "Start prints remotely",
            "bool",
            default=False,
            help="off: every job waits in the printer's queue until someone starts it on "
            "the touchscreen (D-36)",
        ),
        Field(
            "raft_type",
            "Raft",
            "choice",
            choices=RAFT_TYPES,
            help="the raft under supported parts; default: PreForm's",
        ),
    ),
    makes=(MEDIA_FORM,),
    accepts=(MEDIA_FORM,),
    discovers_printers=True,
    pairs_only_with=("preform-server",),
    help="Formlabs resin printers (Form 4) through PreForm Server: supports, layout and "
    "the .form job; uploads to the printer's queue.",
)

TIMEOUT = httpx.Timeout(30.0)
OPERATION_TIMEOUT = 1800.0  # s one async operation may take (a full platform's upload)
POLL = 1.0  # s between operation polls
PROGRESS_EVERY = 30.0  # s between "still working" lines: an operation's progress is coarse
CATALOG_TTL = 600.0  # the catalog only changes with the server's version
DISCOVER_TIMEOUT = 3  # s PreForm looks for a printer while a page renders
SUBMIT_DISCOVER_TIMEOUT = 15  # s it looks before an upload
DISCOVER_BACKOFF = 60.0  # s before looking for a printer again that wasn't found
PRINT = "/v1/scene/print/"
DEFAULT_LAYER = 0.1
DEFAULT_LABEL = "(Default settings)"  # how the catalog marks a material's default setting
ADDRESS_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
LAYER_RE = re.compile(r"(\d{1,2}(?:\.\d{1,4})?) mm")
IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
OWN_KEYS = ("ip", "machine_type", "build_height_mm")  # discovered; config can't override
# What to do about an error, by its stable code (the spec: never match on the message).
FIXES = {
    "INPUT_ERROR": "If PreForm Server restarted or dropped the scene, print again",
    "OPERATION_IN_PROGRESS": "PreForm Server is busy with that scene: try again",
    "BUILD_VOLUME_LIMIT_EXCEEDED": "PreForm Server holds too many scenes: restart its container",
    "INTERNAL_ERROR": "Look at the PreForm Server container's log",
    "UNKNOWN_FAILURE": "Look at the PreForm Server container's log",
}


class PreformError(ModuleError):
    """PreForm Server's error envelope {"error": {"code", "message"}}; `status` None when
    the server didn't answer at all."""

    def __init__(
        self, message: str, fix: str = "", code: str = "", status: int | None = None
    ) -> None:
        super().__init__(message, fix)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Setting:
    """One row of PreForm's print-setting catalog: what a scene is made with."""

    machine_type: str
    material: str  # the catalog's label, "Grey V5"
    code: str  # "FLGPGR05"
    layer: float  # mm
    print_setting: str  # "DEFAULT", "SUPPORTSV1"…
    default: bool  # labelled "(Default settings)"
    scene_settings: Mapping[str, Any]

    def describe(self) -> str:
        return f"{self.material}, {layer_label(self.layer)} layers"


def layer_label(layer: float) -> str:
    """A layer thickness as the process profile names it: 0.1 -> "0.1 mm"."""
    return f"{layer:g} mm"


def parse_layer(label: str) -> float:
    """A process profile ("0.1 mm") back to its layer thickness in mm."""
    m = LAYER_RE.fullmatch(label.strip())
    if not m or float(m.group(1)) <= 0:
        raise ModuleError(
            f"{label!r} isn't a layer height", 'Pick one like "0.1 mm" for a resin printer'
        )
    return float(m.group(1))


def merge_stl(parts: tuple[PartGeometry, ...]) -> bytes:
    """Every part's triangles in one binary STL (they share one rotation and drop)."""
    bodies = []
    total = 0
    for p in parts:
        data = p.stl
        count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
        if count < 0 or len(data) < 84 + 50 * count:
            raise ModuleError(f"{p.name} isn't a binary STL")
        bodies.append(data[84 : 84 + 50 * count])
        total += count
    # A header starting with "solid" reads as ASCII STL to some parsers.
    header = b"os2slice binary STL".ljust(80, b"\0")
    return header + struct.pack("<I", total) + b"".join(bodies)


def catalog_settings(catalog: list[dict[str, Any]]) -> list[Setting]:
    """Every (machine type, material, layer, print setting) in catalog order, once each
    (the Form 4 and 4B share FORM-4-0)."""
    out: list[Setting] = []
    seen: set[tuple[str, str, float, str]] = set()
    for ptype in catalog:
        for mat in ptype.get("materials") or []:
            if not isinstance(mat, dict):
                continue
            for ms in mat.get("material_settings") or []:
                s = ms.get("scene_settings") if isinstance(ms, dict) else None
                if not isinstance(s, dict):
                    continue
                layer = s.get("layer_thickness_mm")
                if isinstance(layer, bool) or not isinstance(layer, (int, float)):
                    continue  # "ADAPTIVE" (older printers only)
                key = (
                    str(s.get("machine_type") or ""),
                    str(s.get("material_code") or ""),
                    float(layer),
                    str(s.get("print_setting") or ""),
                )
                if key in seen or not key[0] or not key[1]:
                    continue
                seen.add(key)
                label = str(mat.get("label") or key[1])
                default = DEFAULT_LABEL in str(ms.get("label") or "")
                out.append(Setting(key[0], label, key[1], key[2], key[3], default, dict(s)))
    return out


def printer_type(catalog: list[dict[str, Any]], product_name: str) -> dict[str, Any] | None:
    """The catalog's printer type for a device's product name ("Form 4")."""
    return next((t for t in catalog if str(t.get("label") or "") == product_name), None)


def type_machine(ptype: Mapping[str, Any]) -> str:
    """A printer type's machine type, from its first material setting ("FORM-4-0")."""
    for mat in ptype.get("materials") or []:
        for ms in (mat.get("material_settings") if isinstance(mat, dict) else None) or []:
            s = ms.get("scene_settings") if isinstance(ms, dict) else None
            if isinstance(s, dict) and s.get("machine_type"):
                return str(s["machine_type"])
    return ""


def type_volume(ptype: Mapping[str, Any]) -> tuple[float, float, float] | None:
    dims = ptype.get("build_volume_dimensions_mm")
    if (
        isinstance(dims, list)
        and len(dims) == 3
        and all(isinstance(d, (int, float)) and not isinstance(d, bool) for d in dims)
    ):
        return float(dims[0]), float(dims[1]), float(dims[2])
    return None


def layer_labels(settings: list[Setting]) -> tuple[str, ...]:
    return tuple(layer_label(t) for t in sorted({s.layer for s in settings}))


def find_setting(
    settings: list[Setting], machine_type: str, code: str, layer: float, model: str = ""
) -> Setting:
    """The catalog row for a material at a layer thickness: its DEFAULT print setting,
    else the one labelled "(Default settings)" (Tough 2000 V2's is SUPPORTSV1), else the
    first. A ModuleError says what the catalog has instead."""
    model = model or machine_type or "printer"
    on_machine = [s for s in settings if s.machine_type == machine_type]
    if not on_machine:
        raise ModuleError(
            f"PreForm Server has no print settings for {machine_type or 'this printer'}",
            "Set the printer profile to a machine type PreForm knows (Form 4: FORM-4-0)",
        )
    of_material = [s for s in on_machine if s.code == code]
    if not of_material:
        raise ModuleError(
            f"{code} isn't a {model} material in PreForm Server's catalog",
            "Pick one of the printer's materials",
        )
    at_layer = [s for s in of_material if abs(s.layer - layer) < 1e-6]
    if not at_layer:
        raise ModuleError(
            f"{of_material[0].material} doesn't print at {layer_label(layer)} layers "
            f"on the {model}",
            f"Pick one of: {', '.join(layer_labels(of_material))}",
        )
    return (
        next((s for s in at_layer if s.print_setting == "DEFAULT"), None)
        or next((s for s in at_layer if s.default), None)
        or at_layer[0]
    )


def _matches(device: Mapping[str, Any], address: str) -> bool:
    return bool(address) and address in (device.get("id"), device.get("ip_address"))


def _per_model(result: Any) -> list[dict[str, Any]]:
    per = result.get("per_model_results") if isinstance(result, dict) else None
    return [r for r in (per or {}).values() if isinstance(r, dict)]


def _envelope(body: Any) -> tuple[str, str]:
    """(code, message) of an {"error": {"code", "message"}} body, or ("", "")."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("code") or ""), str(err.get("message") or "")[:300]


def _duration(ms: float) -> str:
    minutes = max(1, round(ms / 60000))
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("PreForm Server url must be an http:// URL", f"Got {url!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("PreForm Server url must not carry credentials, a query or a fragment")
    return url.strip().rstrip("/")


class PreformServer:
    """One PreForm Server: slicer and target of the Formlabs printers it can reach."""

    spec: ClassVar[ModuleSpec] = SPEC

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        key: str = "preform",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        where = f"[targets.{key}]"
        self.key = key
        self.url = _check_url(str(values.get("url") or ""))
        share = str(values.get("share_dir") or "/share/os2slice/preform")
        if not Path(share).is_absolute():
            raise ConfigError(f"{where} share_dir must be an absolute path", f"Got {share!r}")
        self.share_dir = Path(share)
        server_share = str(values.get("server_share_dir") or share)
        if not PurePosixPath(server_share).is_absolute():
            raise ConfigError(f"{where} server_share_dir must be an absolute path")
        self.server_share_dir = PurePosixPath(server_share)
        self.addresses = tuple(str(a).strip() for a in values.get("printers") or ())
        for a in self.addresses:
            if not ADDRESS_RE.fullmatch(a):
                raise ConfigError(
                    f"{where} printers: {a!r} isn't an IP address or a printer's serial name"
                )
        self.version = str(values.get("version") or "").strip()
        self.remote_start = bool(values.get("remote_start", False))
        self.raft_type = str(values.get("raft_type") or "")
        if self.raft_type and self.raft_type not in RAFT_TYPES:
            raise ConfigError(f"{where} raft_type must be one of {', '.join(RAFT_TYPES)}")
        self.models: Mapping[str, ModelDefaults] = values.get("models") or {}
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._catalog: tuple[float, list[dict[str, Any]]] | None = None
        self._not_found: dict[str, float] = {}  # address -> when to look for it again
        self._client = httpx.Client(
            base_url=self.url,
            headers={"User-Agent": f"os2slice/{__version__}", "Accept": "application/json"},
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, Any],
        *,
        key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> PreformServer:
        return cls(values, key=key, transport=transport)

    def close(self) -> None:
        self._client.close()

    # -- both ------------------------------------------------------------------

    def check(self) -> Health:
        try:
            version = self.server_version()
            devices = self._device_list()
        except ModuleError as e:
            return Health(False, e.one_line())
        warnings = []
        if self.version and version != self.version:
            warnings.append(
                f"PreForm Server {version or '(no version)'} is running, but "
                f"[targets.{self.key}] pins {self.version}"
            )
        try:
            self.share_dir.mkdir(parents=True, exist_ok=True)
            writable = os.access(self.share_dir, os.W_OK)
        except OSError:
            writable = False
        if not writable:
            warnings.append(f"share_dir {self.share_dir} isn't writable")
        missing = [a for a in self.addresses if not any(_matches(d, a) for d in devices)]
        if missing:
            warnings.append(f"PreForm Server hasn't found printer(s) {', '.join(missing)} yet")
        summary = f"PreForm Server {version}, {len(devices)} printer(s)"
        return Health(True, summary, version, "; ".join(warnings))

    def server_version(self) -> str:
        body = self._json(self._request("GET", "/v1/"))
        return str(body.get("version") or "") if isinstance(body, dict) else ""

    # -- slicer ----------------------------------------------------------------

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        """Machine types, layer heights and material codes, narrowed to a printer model
        ("Form 4", or a machine type) when given."""
        catalog = self._catalog_types()
        settings = catalog_settings(catalog)
        if printer_model:
            ptype = printer_type(catalog, printer_model)
            machine = type_machine(ptype) if ptype else printer_model
            settings = [s for s in settings if s.machine_type == machine]
        return ProfileCatalog(
            printer=tuple(sorted({s.machine_type for s in settings})),
            process=layer_labels(settings),
            filament=tuple(sorted({s.code for s in settings})),
        )

    def own_profiles(self) -> ProfileCatalog:
        """Every layer height the catalog has, for the panel's process menu. Printer and
        filament stay empty: the materials come from status()."""
        return ProfileCatalog(process=layer_labels(self._settings()))

    def slice(self, job: SliceInput, progress: Progress) -> SliceOutput:
        materials = distinct_materials(job.parts)
        if len(materials) > 1:
            raise ModuleError(
                "A resin print is one material",
                f"Pick the same resin for every part ({', '.join(m.label for m in materials)} "
                "were chosen), or print them separately",
            )
        code = (materials[0].profile if materials else "") or job.profiles.filament
        if not code:
            raise ModuleError(f"No resin chosen for {job.printer.name}", "Pick a material")
        machine = job.profiles.printer or str(job.printer.extra.get("machine_type") or "")
        layer = parse_layer(job.profiles.process)
        model = job.printer.model or machine
        version = self.server_version()
        setting = find_setting(self._settings(), machine, code, layer, model)
        folder, server_folder = self._job_folder()
        scene = ""
        try:
            (folder / "model.stl").write_bytes(merge_stl(job.parts))
            progress(f"Making a PreForm scene: {setting.describe()}")
            scene = self._create_scene(setting)
            progress("Importing into PreForm")
            model_ids = self._import(scene, server_folder / "model.stl", job.job_name, progress)
            if job.auto_orient:
                progress("Orienting in PreForm")
                self._call("/v1/scene/model/auto-orient/", {"scene_id": scene}, progress)
            else:
                # Identity: keeps the core's rotation and seats the part on the platform
                # (min z 0); auto-layout places it in x/y afterwards.
                self._call(
                    "/v1/scene/model/orient/",
                    {"scene_id": scene, "orientation": {"linear": IDENTITY}},
                    run_async=False,
                )
            supported = job.settings.supports != "off"
            if supported:
                progress("Generating supports")
                body: dict[str, Any] = {"scene_id": scene}
                if self.raft_type:
                    body["raft_type"] = self.raft_type
                self._call("/v1/scene/model/auto-support/", body, progress)
            if job.copies > 1:
                progress(f"Making {job.copies} copies")
                self._call(
                    "/v1/scene/model/duplicate/",
                    {"scene_id": scene, "model_ids": model_ids, "count": job.copies - 1},
                    run_async=False,
                )
            progress("Laying out the build platform")
            self._layout(scene, job, model)
            progress("Checking printability")
            warnings = self._checks(scene, model, supported, progress)
            progress("Calculating the print time")
            times = self._call("/v1/scene/calculate-print-time/", {"scene_id": scene}, progress)
            total = (times or {}).get("total_print_time_s") if isinstance(times, dict) else None
            info = self._json(self._request("POST", "/v1/scene/get/", json={"scene_id": scene}))
            info = info if isinstance(info, dict) else {}
            layers = info.get("layer_count")
            volume = (info.get("material_usage") or {}).get("volume_ml")
            progress("Saving the .form file")
            filename = f"{job.job_name}.form"
            self._call(
                "/v1/scene/save/",
                {"scene_ids": [scene], "file": str(server_folder / filename)},
                progress,
            )
            data = self._read(folder / filename)
        finally:
            if scene:
                self._delete_scene(scene)
            shutil.rmtree(folder, ignore_errors=True)
        report = {
            "machine_type": setting.machine_type,
            "material_code": setting.code,
            "material": setting.material,
            "layer_thickness_mm": setting.layer,
            "print_setting": setting.print_setting,
            "volume_ml": volume,
            "server_version": version,
        }
        return SliceOutput(
            data=data,
            filename=filename,
            media=MEDIA_FORM,
            print_time_s=int(total) if isinstance(total, (int, float)) else None,
            material_g=None,
            layers=layers if isinstance(layers, int) and not isinstance(layers, bool) else None,
            report=report,
            warnings=tuple(warnings),
        )

    def _import(self, scene: str, path: PurePosixPath, name: str, progress: Progress) -> list[str]:
        body = {
            "scene_id": scene,
            "file": str(path),
            "units": "MILLIMETERS",
            "repair_behavior": "REPAIR",
            "name": name,
        }
        result = self._call(
            "/v1/scene/model/import/",
            body,
            progress,
            fix=f"Mount share_dir into PreForm Server's container as {self.server_share_dir}",
        )
        models = result.get("new_models") if isinstance(result, dict) else None
        ids = [str(m["model_id"]) for m in models or [] if isinstance(m, dict) and "model_id" in m]
        if not ids:
            raise ModuleError("PreForm Server imported no model from the STL")
        return ids

    def _layout(self, scene: str, job: SliceInput, model: str) -> None:
        try:
            self._call("/v1/scene/model/auto-layout/", {"scene_id": scene})
        except PreformError as e:
            if e.code != "OPERATION_FAILED":
                raise
            what = f"{job.copies} copies don't fit" if job.copies > 1 else "The part doesn't fit"
            raise ModuleError(
                f"{what} on the {model}'s platform",
                "Make fewer copies" if job.copies > 1 else "Check its size and orientation",
            ) from e

    def _checks(self, scene: str, model: str, supported: bool, progress: Progress) -> list[str]:
        """PreForm's printability checks, as plain sentences shown before Print."""
        out = []
        body = {"scene_id": scene}
        bounds = self._call("/v1/scene/model/validate/in-bounds/", body, run_async=False)
        if (isinstance(bounds, dict) and bounds.get("all_in_bounds") is False) or any(
            r.get("in_bounds") is False for r in _per_model(bounds)
        ):
            out.append(f"Part of the print is outside the {model}'s build volume.")
        manifold = self._call("/v1/scene/model/validate/manifoldness/", body, progress)
        if any(r.get("is_manifold") is False for r in _per_model(manifold)):
            out.append("PreForm couldn't repair the mesh into a closed solid.")
        if not supported:
            return out
        cups = max(
            (int(r.get("cup_count") or 0) for r in _per_model(
                self._call("/v1/scene/model/validate/cups/", body, progress))),
            default=0,
        )  # fmt: skip
        minima = max(
            (int(r.get("minima_count") or 0) for r in _per_model(
                self._call("/v1/scene/model/validate/minima/", body, progress))),
            default=0,
        )  # fmt: skip
        unsupported = max(
            (float(r.get("percent_unsupported") or 0.0) for r in _per_model(
                self._call("/v1/scene/model/validate/supportedness/", body, progress))),
            default=0.0,
        )  # fmt: skip
        if cups:
            out.append(f"{cups} cup(s) that can trap resin; consider drain holes.")
        if minima:
            out.append(f"{minima} unsupported minima: those spots may not print.")
        if unsupported > 0:
            out.append(f"{unsupported:.1f} % of the surface is unsupported.")
        return out

    # -- target ----------------------------------------------------------------

    def printers(self, configured: tuple[PrinterInfo, ...] = ()) -> tuple[PrinterInfo, ...]:
        """The real printers PreForm Server sees, with the per-model defaults, then
        per-printer overrides; then each configured address it hasn't found, inactive."""
        devices = self._devices()
        catalog = self._catalog_types()
        settings = catalog_settings(catalog)
        overrides = {c.name: c for c in configured}
        out = []
        for d in devices:
            info = self._info(d, catalog, settings)
            if info.name in overrides:
                info = _override(info, overrides[info.name])
            out.append(info)
        for a in self.addresses:
            if any(_matches(d, a) for d in devices):
                continue
            info = PrinterInfo(
                key=f"{self.key}/{a}",
                name=a,
                technology="sla",
                model="",
                target=self.key,
                slicer=self.key,
                active=False,
                extra={"ip": a},
            )
            out.append(_override(info, overrides[a]) if a in overrides else info)
        return tuple(out)

    def _info(
        self, d: Mapping[str, Any], catalog: list[dict[str, Any]], settings: list[Setting]
    ) -> PrinterInfo:
        model = str(d.get("product_name") or "")
        ptype = printer_type(catalog, model)
        machine = type_machine(ptype) if ptype else ""
        volume = type_volume(ptype) if ptype else None
        defaults = self.models.get(model, ModelDefaults())
        profiles = default_profiles(
            defaults.profiles, machine, str(d.get("tank_material_code") or ""), settings
        )
        return PrinterInfo(
            key=f"{self.key}/{d.get('id')}",
            name=str(d.get("id")),
            technology="sla",
            model=model,
            target=self.key,
            slicer=defaults.slicer or self.key,
            profiles=profiles,
            bed_mm=volume[:2] if volume else None,
            extra={
                **defaults.extra,
                "ip": str(d.get("ip_address") or ""),
                "machine_type": machine,
                "build_height_mm": volume[2] if volume else None,
            },
            ui_url="",
        )

    def status(self, printer: PrinterInfo) -> PrinterStatus:
        if not printer.active:
            return PrinterStatus(
                "offline", False, False, f"PreForm Server hasn't found {printer.name}"
            )
        try:
            device = self._device(printer)
            settings = self._settings()
        except ModuleError as e:
            log.warning("preform %s: no status for %s: %s", self.url, printer.name, e.message)
            return PrinterStatus("offline", False, False, e.message)
        if device is None:
            return PrinterStatus(
                "offline", False, False, f"PreForm Server doesn't see {printer.name}"
            )
        machine = str(printer.extra.get("machine_type") or "") or printer.profiles.printer
        names = {s.code: s.material for s in settings}
        tank = str(device.get("tank_material_code") or "")
        label = f"{names.get(tank, tank)} (loaded)"
        loaded = (Material(tank, label, tank, profile=tank, raw={"loaded": True}),) if tank else ()
        others = {s.code: s.material for s in settings if s.machine_type == machine}
        others.pop(tank, None)
        materials = loaded + tuple(
            Material(code, label, code, profile=code)
            for code, label in sorted(others.items(), key=lambda kv: kv[1])
        )
        bits = [f"{names.get(tank, tank)} in the tank" if tank else "no resin tank reported"]
        left = device.get("estimated_print_time_remaining_ms")
        if isinstance(left, (int, float)) and not isinstance(left, bool) and left > 0:
            bits.insert(0, f"{_duration(left)} left")
        connected = bool(device.get("is_connected"))
        return PrinterStatus(
            state=str(device.get("status") or "unknown"),
            connected=connected,
            ready=connected,
            detail=", ".join(bits),
            materials=materials,
            raw=device,
        )

    def submit(
        self,
        printer: PrinterInfo,
        output: SliceOutput,
        *,
        start: bool,
        materials: tuple[Material, ...] = (),
        progress: Progress = lambda s: None,
    ) -> Submission:
        if output.media != MEDIA_FORM:
            raise ModuleError(
                f"PreForm Server takes .form jobs, not {output.media}",
                "Pair this printer with its PreForm Server as the slicer",
            )
        print_now = bool(start and self.remote_start)
        job_name = output.filename.removesuffix(".form") or "os2slice"
        folder, server_folder = self._job_folder()
        scenes: list[str] = []
        try:
            try:
                (folder / "job.form").write_bytes(output.data)
            except OSError as e:
                raise ModuleError(f"Can't write {folder / 'job.form'}: {e.strerror}") from e
            progress("Opening the job in PreForm Server")
            opened = self._call("/v1/scene/open/", {"file": str(server_folder / "job.form")})
            found = opened.get("scenes") if isinstance(opened, dict) else None
            wanted = ""
            for s in found or []:
                if isinstance(s, dict) and s.get("scene_id"):
                    scenes.append(str(s["scene_id"]))
                    wanted = str((s.get("scene_settings") or {}).get("material_code") or "")
            if len(scenes) != 1:
                raise ModuleError(f"The .form file holds {len(scenes)} jobs, not one")
            progress(f"Looking for {printer.name}")
            device = self._find_for_upload(printer)
            # By IP when known: the one way an upload was verified live (docs/PREFORM_API.md).
            address = str(device.get("ip_address") or device.get("id") or self._address(printer))
            # print_now always goes explicitly: left out, PreForm defers to the printer's
            # ready_to_print_now and a ready printer starts at once.
            body = {
                "scene_id": scenes[0],
                "printer": address,
                "job_name": job_name,
                "print_now": print_now,
            }
            progress(f"Uploading {job_name} to {printer.name}")
            log.warning("preform %s: uploading %s to %s (print_now=%s)",
                        self.url, job_name, address, print_now)  # fmt: skip
            result = self._call(
                PRINT,
                body,
                progress,
                fix=f"Check {printer.name} is on and reachable from PreForm Server's container",
            )
        finally:
            for s in scenes:
                self._delete_scene(s)
            shutil.rmtree(folder, ignore_errors=True)
        job_id = str(result.get("job_id") or "") if isinstance(result, dict) else ""
        if not job_id:
            raise ModuleError("PreForm Server uploaded the job but gave no job id")
        which = printer.model or printer.name
        if print_now:
            detail = f"sent to {printer.name}; it starts printing"
        else:
            detail = f"waits in the {which}'s queue; start it on the printer's touchscreen"
            if start:
                detail += (
                    f" (resin prints aren't started remotely unless [targets.{self.key}] "
                    "remote_start is on)"
                )
        tank = str(device.get("tank_material_code") or "")
        if tank and wanted and tank != wanted:
            detail += f"; its tank holds {tank}, this job needs {wanted}"
        return Submission(
            id=job_id,
            state="started" if print_now else "waiting",
            detail=detail,
            url="",
            raw={"job_id": job_id, "print_now": print_now, "printer": address},
        )

    # -- printers --------------------------------------------------------------

    def _address(self, printer: PrinterInfo) -> str:
        return str(printer.extra.get("ip") or "") or printer.name

    def _device_list(self) -> list[dict[str, Any]]:
        """The real printers PreForm Server sees (a fresh server also lists 14 virtual)."""
        body = self._json(self._request("GET", "/v1/device/list/"))
        devices = body.get("devices") if isinstance(body, dict) else None
        return [
            d
            for d in devices or []
            if isinstance(d, dict) and d.get("id") and d.get("connection_type") != "VIRTUAL"
        ]

    def _discover(self, address: str, timeout: int) -> None:
        self._call(
            "/v1/device/discover/",
            {"timeout_seconds": timeout, "printer": address},
            run_async=False,
            timeout=httpx.Timeout(timeout + 10.0),
        )

    def _look_up(self, address: str) -> bool:
        """Discover one address unless it failed recently; whether it was looked up. A
        printer PreForm can't find waits DISCOVER_BACKOFF before the next try, so an
        offline printer doesn't slow every page."""
        now = self._clock()
        with self._lock:
            if self._not_found.get(address, 0.0) > now:
                return False
        try:
            self._discover(address, DISCOVER_TIMEOUT)
        except PreformError as e:
            if e.status is None:
                raise
            log.warning("preform %s: can't find %s: %s", self.url, address, e.message)
            with self._lock:
                self._not_found[address] = now + DISCOVER_BACKOFF
            return False
        with self._lock:
            self._not_found.pop(address, None)
        return True

    def _devices(self) -> list[dict[str, Any]]:
        """The device list, after looking up each configured address it lacks (by IP:
        mDNS doesn't reach the container)."""
        devices = self._device_list()
        missing = [a for a in self.addresses if not any(_matches(d, a) for d in devices)]
        looked = [self._look_up(a) for a in missing]
        return self._device_list() if any(looked) else devices

    def _device(self, printer: PrinterInfo) -> dict[str, Any] | None:
        address = self._address(printer)
        devices = self._device_list()
        for d in devices:
            if d.get("id") == printer.name or _matches(d, address):
                return d
        if self._look_up(address):
            for d in self._device_list():
                if d.get("id") == printer.name or _matches(d, address):
                    return d
        return None

    def _find_for_upload(self, printer: PrinterInfo) -> dict[str, Any]:
        address = self._address(printer)
        for d in self._device_list():
            if d.get("id") == printer.name or _matches(d, address):
                return d
        try:
            self._discover(address, SUBMIT_DISCOVER_TIMEOUT)
        except PreformError as e:
            if e.status is None:
                raise
            raise ModuleError(
                f"PreForm Server can't find {printer.name} at {address}",
                "Check the printer is on and reachable from PreForm Server's container",
            ) from e
        for d in self._device_list():
            if d.get("id") == printer.name or _matches(d, address):
                return d
        return {}

    # -- catalog ---------------------------------------------------------------

    def _catalog_types(self) -> list[dict[str, Any]]:
        """Every printer type with its materials, layers and print settings (kept
        CATALOG_TTL)."""
        now = self._clock()
        with self._lock:
            if self._catalog and now - self._catalog[0] < CATALOG_TTL:
                return self._catalog[1]
        body = self._json(self._request("POST", "/v1/print-setting/list/", json={}))
        types = body.get("printer_types") if isinstance(body, dict) else None
        if not isinstance(types, list):
            raise ModuleError("PreForm Server's print-setting list has no printer_types")
        catalog = [t for t in types if isinstance(t, dict)]
        with self._lock:
            self._catalog = (now, catalog)
        return catalog

    def _settings(self) -> list[Setting]:
        return catalog_settings(self._catalog_types())

    # -- scenes and files ------------------------------------------------------

    def _job_folder(self) -> tuple[Path, PurePosixPath]:
        """A new folder for one job's files, as os2slice and as the server see it."""
        name = uuid.uuid4().hex
        folder = self.share_dir / name
        try:
            self.share_dir.mkdir(parents=True, exist_ok=True)
            folder.mkdir()
        except OSError as e:
            raise ModuleError(
                f"Can't make a job folder in {self.share_dir}: {e.strerror}",
                f"Check [targets.{self.key}] share_dir is mounted and writable",
            ) from e
        return folder, self.server_share_dir / name

    def _read(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as e:
            raise ModuleError(
                f"PreForm Server wrote no .form file at {path}",
                f"Mount share_dir into its container as {self.server_share_dir}, writable",
            ) from e

    def _create_scene(self, setting: Setting) -> str:
        s = setting.scene_settings
        body = {k: s[k] for k in ("machine_type", "material_code", "layer_thickness_mm")}
        if s.get("print_setting"):
            body["print_setting"] = s["print_setting"]
        made = self._call("/v1/scene/create/", body, run_async=False)
        scene = made.get("scene_id") if isinstance(made, dict) else None
        if not isinstance(scene, str) or not scene:
            raise ModuleError("PreForm Server made a scene without an id")
        return scene

    def _delete_scene(self, scene: str) -> None:
        try:
            self._request("DELETE", "/v1/scene/delete/", json={"scene_id": scene})
        except ModuleError as e:  # cleanup is best effort; never mask the real outcome
            log.warning("preform %s: scene %s not deleted: %s", self.url, scene, e.one_line())

    # -- plumbing --------------------------------------------------------------

    def _call(
        self,
        path: str,
        body: dict[str, Any],
        progress: Progress | None = None,
        *,
        run_async: bool = True,
        fix: str = "",
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        """POST `body` to `path`; with `run_async` as ?async=true, then poll the operation
        until it's done. The call's result (None when it has none)."""
        if path == PRINT and not isinstance(body.get("print_now"), bool):
            raise ModuleError("Refused to upload a job without an explicit print_now")
        kw: dict[str, Any] = {"json": body}
        if run_async:
            kw["params"] = {"async": "true"}
        if timeout is not None:
            kw["timeout"] = timeout
        r = self._request("POST", path, fix=fix, **kw)
        if r.status_code != 202:
            return self._json(r) if r.content else None
        op = self._json(r)
        op_id = op.get("operation_id") if isinstance(op, dict) else None
        if not isinstance(op_id, str) or not op_id:
            raise ModuleError(f"PreForm Server accepted {path} without an operation id")
        return self._wait(op_id, path, progress, fix)

    def _wait(self, op_id: str, path: str, progress: Progress | None, fix: str) -> Any:
        """Poll an operation until it's done (`mode=oneshot`: the server drops the record
        once it's read finished); a "still working" line every PROGRESS_EVERY s."""
        start = self._clock()
        told = 0.0
        while True:
            op = self._json(
                self._request(
                    "GET",
                    "/v1/operation/get/",
                    params={"operation_id": op_id, "mode": "oneshot"},
                    fix="PreForm Server forgets operations when it restarts: print again",
                )
            )
            op = op if isinstance(op, dict) else {}
            status = op.get("status")
            if status == "SUCCEEDED":
                return op.get("result")
            if status == "FAILED":
                code, message = _envelope(op.get("result"))
                raise PreformError(
                    f"PreForm Server: {message or f'{path} failed'}",
                    fix or FIXES.get(code, ""),
                    code,
                    200,
                )
            ran = self._clock() - start
            if ran > OPERATION_TIMEOUT:
                raise ModuleError(
                    f"PreForm Server took over {OPERATION_TIMEOUT:.0f} s on {path}",
                    "It can't cancel an operation: if it's stuck, restart its container",
                )
            if progress is not None and ran - told >= PROGRESS_EVERY:
                told = ran - ran % PROGRESS_EVERY
                progress(f"PreForm is still working ({ran:.0f} s)")
            self._sleep(POLL)

    def _request(self, method: str, path: str, *, fix: str = "", **kw: Any) -> httpx.Response:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise PreformError(
                f"PreForm Server at {self.url} didn't answer in time ({path})",
                "Look at its container",
            ) from e
        except httpx.TransportError as e:
            raise PreformError(
                f"Can't reach PreForm Server at {self.url} ({type(e).__name__})",
                f"Start its container, or check [targets.{self.key}] url",
            ) from e
        log.debug("preform %s %s -> %s", method, path, r.status_code)
        if r.is_redirect:
            raise PreformError(f"PreForm Server redirected {path} (not followed)", "", "", 0)
        if r.is_success:
            return r
        try:
            code, message = _envelope(r.json())
        except ValueError:
            code, message = "", ""
        if r.status_code == 404 and not code:
            raise PreformError(
                f"PreForm Server has no {path} (HTTP 404)",
                f"Its API changed: compare its version with [targets.{self.key}] version",
                "",
                404,
            )
        raise PreformError(
            f"PreForm Server: {message or f'HTTP {r.status_code} on {path}'}",
            fix or FIXES.get(code, ""),
            code,
            r.status_code,
        )

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise ModuleError("PreForm Server returned invalid JSON") from e


def default_profiles(given: Profiles, machine: str, tank: str, settings: list[Setting]) -> Profiles:
    """A discovered printer's profiles: the model's configured ones, each empty field
    filled from the printer: its machine type, the resin in its tank, and 0.1 mm when
    that resin (or the type's first material) prints at it, else its first layer."""
    printer = given.printer or machine
    filament = given.filament or tank
    on_machine = [s for s in settings if s.machine_type == printer]
    chosen = [s for s in on_machine if s.code == filament]
    if not chosen and on_machine:
        chosen = [s for s in on_machine if s.code == on_machine[0].code]
    layers = sorted({s.layer for s in chosen})
    process = given.process
    if not process and layers:
        best = DEFAULT_LAYER if any(abs(t - DEFAULT_LAYER) < 1e-6 for t in layers) else layers[0]
        process = layer_label(best)
    return Profiles(printer, process, filament)


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
