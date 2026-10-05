"""Loaded filament (AMS slots, external spools) and matching slicer presets.

Shapes are from BamBuddy's `GET /printers/{id}/status` (docs/BAMBUDDY_API.md).
Global tray ids follow BamBuddy/Bambu: AMS unit n slot s → n*4+s, AMS-HT units
are 128-135 (one slot each), external spools are 254/255.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

EXTERNAL_IDS = (254, 255)
# Physical extruders on dual-nozzle printers: 0 = right (main), 1 = left (deputy).
NOZZLE_NAMES = {0: "right", 1: "left"}
EXTERNAL_EXTRUDER = {254: 1, 255: 0}  # Ext-L feeds the left nozzle, Ext-R the right


# Named colours for labels; nearest match by RGB distance.
_COLOURS = {
    "black": (20, 20, 20),
    "white": (245, 245, 245),
    "grey": (137, 137, 137),
    "silver": (192, 192, 192),
    "red": (200, 30, 40),
    "orange": (240, 120, 30),
    "yellow": (255, 230, 60),
    "green": (40, 160, 70),
    "teal": (0, 150, 150),
    "blue": (30, 90, 200),
    "purple": (120, 60, 170),
    "pink": (240, 130, 180),
    "brown": (120, 80, 40),
    "beige": (220, 200, 160),
}


@dataclass(frozen=True)
class Slot:
    tray_id: int  # global tray id, what ams_mapping takes
    label: str  # "AMS 1 · slot 2", "External"
    material: str  # tray_type, e.g. "PETG"
    color: str  # "#RRGGBB"
    brand: str = ""  # tray_sub_brands, e.g. "Support for ABS"
    extruder: int | None = None  # dual-nozzle printers only: the physical nozzle it feeds
    color_known: bool = True  # False: the tray reported no colour, `color` is a grey stand-in

    @property
    def external(self) -> bool:
        return self.tray_id in EXTERNAL_IDS

    def describe(self) -> str:
        name = self.brand or self.material
        nozzle = f" ({NOZZLE_NAMES[self.extruder]} nozzle)" if self.extruder in NOZZLE_NAMES else ""
        return f"{self.label}: {name} · {color_name(self.color)}{nozzle}"


def slots_from_status(status: dict[str, Any], dual_nozzle: bool = False) -> list[Slot]:
    """Loaded slots, in AMS order then external spools. Empty slots are skipped.

    With `dual_nozzle`, each slot also says which nozzle it feeds (AMS units from
    the printer's `ams_extruder_map`, external spools by side).
    """
    out: list[Slot] = []
    ams_map = status.get("ams_extruder_map") or {}
    for unit in status.get("ams") or []:
        try:
            unit_id = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            material = str(tray.get("tray_type") or "").strip()
            if not material or tray.get("exists") is False:
                continue
            slot = int(tray.get("id", 0))
            if unit_id >= 128:  # AMS-HT: one slot, the unit id is the tray id
                gid, label = unit_id, f"AMS HT {unit_id - 127}"
            else:
                gid, label = unit_id * 4 + slot, f"AMS {unit_id + 1} · slot {slot + 1}"
            extruder = _int(ams_map.get(str(unit_id))) if dual_nozzle else None
            out.append(_slot(gid, label, tray, extruder))
    externals = [t for t in status.get("vt_tray") or [] if str(t.get("tray_type") or "").strip()]
    for n, tray in enumerate(externals, start=1):
        label = "External" if len(externals) == 1 else f"External {n}"
        try:
            gid = int(tray.get("id"))
        except (TypeError, ValueError):
            continue
        if gid in EXTERNAL_IDS:
            if dual_nozzle:
                label = {254: "External left", 255: "External right"}[gid]
            out.append(_slot(gid, label, tray, EXTERNAL_EXTRUDER[gid] if dual_nozzle else None))
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slot(gid: int, label: str, tray: dict[str, Any], extruder: int | None = None) -> Slot:
    raw = str(tray.get("tray_color") or "")
    known = re.fullmatch(r"[0-9A-Fa-f]{6,8}", raw) is not None
    color = f"#{raw[:6].upper()}" if known else "#808080"
    return Slot(
        gid,
        label,
        str(tray.get("tray_type")).strip(),
        color,
        str(tray.get("tray_sub_brands") or "").strip(),
        extruder,
        known,
    )


def sliced_nozzle(threemf: bytes, filament: int = 1) -> int | None:
    """The physical nozzle a sliced .gcode.3mf prints `filament` with, or None.

    Reads it the way BamBuddy's dispatcher does: the filament's group in
    slice_info.config, the group's extruder (from the plate's <nozzle> table, or
    the group id itself), then `physical_extruder_map`. None on single-nozzle files.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(threemf)) as z:
            settings = json.loads(z.read("Metadata/project_settings.config"))
            # BamBuddy's own slicer output; expat resolves no external entities.
            info = ElementTree.fromstring(z.read("Metadata/slice_info.config"))  # noqa: S314
    except (KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return None
    physical = settings.get("physical_extruder_map") or []
    if len(physical) <= 1:
        return None
    table = {}
    for nozzle in info.iter("nozzle"):
        group, extruder = _int(nozzle.get("id")), _int(nozzle.get("extruder_id"))
        if group is not None and extruder is not None:
            table[group] = extruder - 1
    for elem in info.iter("filament"):
        if _int(elem.get("id")) == filament:
            group = _int(elem.get("group_id"))
            if group is None:
                return None
            index = table.get(group, group)
            return _int(physical[index]) if 0 <= index < len(physical) else None
    return None


def sliced_type(threemf: bytes, filament: int = 1) -> str:
    """The filament type ("ASA") a sliced .gcode.3mf names for `filament` in
    slice_info.config, what BamBuddy's dispatcher matches trays against; "" if none."""
    try:
        with zipfile.ZipFile(io.BytesIO(threemf)) as z:
            # BamBuddy's own slicer output; expat resolves no external entities.
            info = ElementTree.fromstring(z.read("Metadata/slice_info.config"))  # noqa: S314
    except (KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""
    for elem in info.iter("filament"):
        if _int(elem.get("id")) == filament:
            return str(elem.get("type") or "").strip()
    return ""


def color_name(hex_color: str) -> str:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", hex_color)
    if not m:
        return "unknown colour"
    rgb = tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    return min(
        _COLOURS, key=lambda n: sum((a - b) ** 2 for a, b in zip(_COLOURS[n], rgb, strict=True))
    )


def preset_suffix(filament_preset: str) -> str:
    """'Bambu PLA Basic @BBL A1M' → '@BBL A1M' (the printer part of Bambu preset names)."""
    return filament_preset[filament_preset.rfind("@") :] if "@" in filament_preset else ""


def compatible_presets(names: list[str], suffix: str, printer_preset: str = "") -> list[str]:
    """The filament presets made for one printer model, by name: Bambu's own end in its
    code ("Generic PETG @BBL A1M" for '@BBL A1M'), presets saved in Bambu Studio usually
    in the printer preset's name ("My PETG @Bambu Lab A1 mini 0.4 nozzle"). Sorted by
    name without that suffix, each name once."""
    ends = tuple(f" {e}" for e in (suffix, f"@{printer_preset}" if printer_preset else "") if e)
    if not ends:
        return []
    found = {n for n in names if n.endswith(ends)}
    return sorted(found, key=lambda n: (preset_label(n).casefold(), n))


def preset_label(name: str) -> str:
    """'Generic PETG @BBL A1M' → 'Generic PETG': a preset's name without its printer part."""
    return name[: name.rfind(" @")].strip() if " @" in name else name


def match_preset(slot: Slot, suffix: str, names: list[str]) -> str | None:
    """The slicer filament preset for a loaded slot, or None if nothing fits.

    Material is a hard filter (like BamBuddy's own slice dialog); among matches,
    prefer Generic, then Bambu <m> Basic, Bambu <m>, PolyLite <m>, then any.
    """
    if not suffix:
        return None
    material = slot.material.upper()
    ours = [n for n in names if n.endswith(" " + suffix)]
    if slot.brand.lower().startswith("support"):
        exact = [n for n in ours if slot.brand.lower() in n.lower()]
        return sorted(exact)[0] if exact else None
    for candidate in (
        f"Generic {slot.material} {suffix}",
        f"Bambu {slot.material} Basic {suffix}",
        f"Bambu {slot.material} {suffix}",
        f"PolyLite {slot.material} {suffix}",
    ):
        if candidate in ours:
            return candidate
    token = re.compile(
        rf"^\S+ {re.escape(material)}(?: [^@]*)? {re.escape(suffix)}$", re.IGNORECASE
    )
    loose = sorted(n for n in ours if token.match(n) and "support" not in n.lower())
    return loose[0] if loose else None
