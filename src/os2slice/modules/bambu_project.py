"""Bambu-style 3MF projects built from a SliceInput (pure; no network).

Shared by the slicer modules that take a Bambu Studio / OrcaSlicer project: BamBuddy
and the sidecar APIs. The parts arrive oriented and dropped onto Z = 0 together
(`orientation.orient_parts`); this module centres them on the bed, lays out copies
(D-25), gives each distinct material its own filament (D-20), pins filaments to
nozzles on dual-nozzle printers, finds prime-tower spots beside the footprint, and
retries a slice with the next spot when the slicer rejects the tower
(`with_tower_retries`), so that logic lives once for every Bambu-style slicer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from os2slice.errors import BadRequest
from os2slice.modules.base import (
    Material,
    ModuleError,
    PrinterInfo,
    Progress,
    SliceInput,
    distinct_materials,
)
from os2slice.orientation import bounding_box, translate_xy
from os2slice.threemf import Part, build_3mf

# Approximate build plates (mm), by Bambu model name; PrinterInfo.bed_mm wins when set.
BED_MM: dict[str, tuple[float, float]] = {
    "A1 Mini": (180, 180),
    "A1": (256, 256),
    "X1C": (256, 256),
    "X1": (256, 256),
    "X1E": (256, 256),
    "P1S": (256, 256),
    "P1P": (256, 256),
    "P2S": (256, 256),
    "H2D": (350, 320),
    "H2D Pro": (350, 320),
    "H2S": (340, 320),
    "H2C": (350, 320),
}
DEFAULT_BED = (256.0, 256.0)
# Where both nozzles of a dual-nozzle printer reach (H2D: left x 0-325, right x 25-350;
# docs/BAMBUDDY_API.md). The prime tower must stand there when both nozzles print.
SHARED_X = {"H2D": (25, 325), "H2D Pro": (25, 325), "H2C": (25, 325)}
TOWER_W, TOWER_D, GAP = 40.0, 60.0, 12.0  # generous tower footprint and clearance, mm
# Copies: gap between neighbours (more with a brim, Bambu's default brim is 5 mm wide)
# and a margin kept clear around the bed's edge, mm.
COPY_GAP, BRIM_GAP, BED_MARGIN = 6.0, 10.0, 5.0

Footprint = tuple[float, float, float, float]  # x0, y0, x1, y1 on the bed, mm

# Slicer errors that mean "move the prime tower" (docs/BAMBUDDY_API.md,
# docs/SLICERAPI_API.md): "G-code conflicts detected… try moving the wipe tower",
# "Found G-code outside of the printable area" (single nozzle) and "Found G-code in
# unprintable area of multi-extruder printers" (H2D).
TOWER_ERRORS = ("conflict", "printable area", "wipe tower", "prime tower")

log = logging.getLogger(__name__)
T = TypeVar("T")


def bed_of(printer: PrinterInfo) -> tuple[float, float]:
    """The printer's bed: its own `bed_mm`, else the table by model, else 256 x 256."""
    if printer.bed_mm:
        return printer.bed_mm
    return BED_MM.get(printer.model, DEFAULT_BED)


def copy_offsets(
    size: tuple[float, float], copies: int, bed: tuple[float, float], gap: float = COPY_GAP
) -> list[tuple[float, float]]:
    """X/Y offsets that lay `copies` of a `size` (w, d) footprint out in a grid centred
    on its current position, row by row from the front left; the squarest grid that fits.
    """
    w, d = size
    room_w, room_d = bed[0] - 2 * BED_MARGIN, bed[1] - 2 * BED_MARGIN
    best: tuple[float, int, int] | None = None
    for cols in range(1, copies + 1):
        rows = -(-copies // cols)
        grid_w, grid_d = cols * w + (cols - 1) * gap, rows * d + (rows - 1) * gap
        if grid_w <= room_w and grid_d <= room_d:
            score = max(grid_w / room_w, grid_d / room_d)
            if best is None or score < best[0]:
                best = (score, cols, rows)
    if best is None:
        raise BadRequest(
            f"{copies} copies don't fit on the plate",
            f"Each copy is {w:.0f} x {d:.0f} mm; print fewer copies",
        )
    _, cols, rows = best
    pitch_x, pitch_y = w + gap, d + gap
    x0, y0 = -(cols - 1) * pitch_x / 2, -(rows - 1) * pitch_y / 2
    return [(x0 + (n % cols) * pitch_x, y0 + (n // cols) * pitch_y) for n in range(copies)]


def tower_spots(
    model: str, footprint: Footprint, bed: tuple[float, float] | None = None
) -> list[dict[str, str]]:
    """Prime tower positions beside a part footprint (x0, y0, x1, y1), best first.

    Returned as Bambu Studio process keys (`wipe_tower_x`, `wipe_tower_y`).
    """
    w, d = bed or BED_MM.get(model, DEFAULT_BED)
    lo_x, hi_x = SHARED_X.get(model, (0, w))
    x0, y0, x1, y1 = footprint
    cy = min(max((y0 + y1) / 2 - TOWER_D / 2, 5), d - TOWER_D - 5)
    cx = (x0 + x1) / 2 - TOWER_W / 2
    spots = [
        (x1 + GAP, max(y1 - TOWER_D, 5)),  # right, towards the back (like Bambu Studio)
        (x0 - GAP - TOWER_W, max(y1 - TOWER_D, 5)),  # left
        (x1 + GAP, cy),  # right, middle
        (cx, y1 + GAP),  # behind
        (cx, y0 - GAP - TOWER_D),  # in front
    ]
    ok = [
        (x, y) for x, y in spots
        if lo_x <= x and x + TOWER_W <= hi_x and y >= 0 and y + TOWER_D <= d
    ]  # fmt: skip
    return [{"wipe_tower_x": f"{x:.1f}", "wipe_tower_y": f"{y:.1f}"} for x, y in ok]


def is_tower_error(message: str) -> bool:
    """Whether a slicer's error says the prime tower is in the way or off the plate."""
    text = message.lower()
    return any(w in text for w in TOWER_ERRORS)


def with_tower_retries(
    spots: Sequence[Mapping[str, str]],
    attempt: Callable[[dict[str, str]], T],
    progress: Progress = lambda s: None,
) -> T:
    """`attempt(tower)` with each prime-tower spot in turn until one slices (D-20).

    `tower` is the spot's process keys (`wipe_tower_x`/`wipe_tower_y`; `{}` = the
    slicer's default), to put on top of the job's process overrides. A ModuleError that
    isn't about the tower, or one on the last spot, propagates unchanged.
    """
    spots = list(spots) or [{}]
    for n, tower in enumerate(spots, start=1):
        if n > 1:
            progress(f"Slicing again, prime tower moved ({n})")
        try:
            return attempt(dict(tower))
        except ModuleError as e:
            if n == len(spots) or not is_tower_error(e.message):
                raise
            log.info("prime tower at %s rejected: %s", tower, e.message)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


@dataclass(frozen=True)
class Project:
    threemf: bytes  # the parts as one object (one instance per copy), centred on the bed
    filaments: tuple[Material, ...]  # filament n is filaments[n-1]; empty = profile filament
    filament_profiles: tuple[str, ...]  # slicer filament profile per filament
    footprint: Footprint  # all copies on the bed

    def tower_spots(self, printer: PrinterInfo) -> list[dict[str, str]]:
        """Prime tower spots for this layout; [{}] (slicer default) with one filament."""
        if len(self.filaments) <= 1:
            return [{}]
        return tower_spots(printer.model, self.footprint, bed_of(printer)) or [{}]


def layout(job: SliceInput, *, tower: tuple[float, float] | None = None) -> Project:
    """Pack the job's parts into one Bambu-style 3MF object.

    Parts keep their relative placement (they were oriented and dropped together);
    the assembly, or the grid of its copies, is centred on the printer's bed. Each
    distinct material becomes a filament, pinned to its nozzle on dual-nozzle
    printers (Material.extruder: 1 = left, 0 = right); with a filament changer's tools
    (SliceInput.tools) every tool is a filament, in tool order. Without materials every
    part is filament 1.

    `tower` is the front-left corner of the prime tower the caller will ask the slicer
    for (x, y, mm). It isn't written into the file (the tower position goes to the
    slicer as process settings); it's checked to be on the bed and clear of the parts,
    and a ValueError says when it isn't.
    """
    if not job.parts:
        raise ValueError("no parts to print")
    bed_w, bed_d = bed_of(job.printer)
    boxes = [bounding_box(p.stl) for p in job.parts]
    x0, y0 = min(b[0][0] for b in boxes), min(b[0][1] for b in boxes)
    x1, y1 = max(b[1][0] for b in boxes), max(b[1][1] for b in boxes)
    dx, dy = bed_w / 2 - (x0 + x1) / 2, bed_d / 2 - (y0 + y1) / 2
    placed = [translate_xy(p.stl, dx, dy) for p in job.parts]
    gap = COPY_GAP + (BRIM_GAP if job.settings.brim else 0.0)
    offsets = copy_offsets((x1 - x0, y1 - y0), job.copies, (bed_w, bed_d), gap)
    filaments = job.tools or distinct_materials(job.parts)
    index = {m.id: n for n, m in enumerate(filaments, start=1)}
    profiles = tuple(m.profile for m in filaments) or (job.profiles.filament,)
    parts = [
        Part(p.name, stl, index[p.material.id] if p.material else 1)
        for p, stl in zip(job.parts, placed, strict=True)
    ]
    dual = job.printer.nozzle_count > 1
    # Only when every filament has a nozzle: a pool's loaded filaments carry the one that
    # feeds them on most of its printers (D-35), but a bare preset, or a slot whose AMS
    # isn't in ams_extruder_map, has none. No Manual filament map then, the slicer chooses.
    pinned = dual and bool(filaments) and all(m.extruder is not None for m in filaments)
    maps = [1 if m.extruder == 1 else 2 for m in filaments] if pinned else None
    ox0, oy0 = min(o[0] for o in offsets), min(o[1] for o in offsets)
    ox1, oy1 = max(o[0] for o in offsets), max(o[1] for o in offsets)
    footprint = (x0 + dx + ox0, y0 + dy + oy0, x1 + dx + ox1, y1 + dy + oy1)
    if tower is not None:
        tx, ty = tower
        on_bed = tx >= 0 and tx + TOWER_W <= bed_w and ty >= 0 and ty + TOWER_D <= bed_d
        overlaps = (
            tx < footprint[2]
            and tx + TOWER_W > footprint[0]
            and ty < footprint[3]
            and ty + TOWER_D > footprint[1]
        )
        if not on_bed or overlaps:
            raise ValueError(f"prime tower at ({tx:.0f}, {ty:.0f}) isn't clear of the parts")
    name = job.parts[0].name
    return Project(build_3mf(parts, name, maps, offsets), filaments, profiles, footprint)


def build_project(job: SliceInput, *, tower: tuple[float, float] | None = None) -> bytes:
    """The job as a Bambu Studio / OrcaSlicer project 3MF (geometry and filament map)."""
    return layout(job, tower=tower).threemf
