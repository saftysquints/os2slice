"""Validated print settings and their BamBuddy/Bambu Studio process overrides (D-14)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from os2slice import extra_settings
from os2slice.errors import BadRequest

Supports = Literal["off", "normal", "tree"]
SUPPORTS: tuple[Supports, ...] = ("off", "normal", "tree")
SUPPORT_TYPE = {"normal": "normal(auto)", "tree": "tree(auto)"}
# A resin printer's supports: none (the part sits on the build platform) or the slicer's own.
SLA_SUPPORTS: tuple[tuple[Supports, str], ...] = (
    ("off", "None (on the build platform)"),
    ("normal", "Auto supports"),
)
SLA_DEFAULT_SUPPORTS: Supports = "normal"  # resin parts print tilted on supports

# BamBuddy's canonical build plate names (SliceRequest.bed_type), docs/BAMBUDDY_API.md.
BED_TYPES = (
    "Textured PEI Plate",
    "Smooth PEI Plate",
    "High Temp Plate",
    "Engineering Plate",
    "Cool Plate",
    "Cool Plate (SuperTack)",
    "Supertack Plate",
)


# What the plate menus show. Bambu Studio writes the smooth PEI plate as "High Temp Plate"
# (Josh's own Carriage print has curr_bed_type 'High Temp Plate'); the separate
# "Smooth PEI Plate" value slices differently (it refused PETG), so it's only an alias.
PLATE_LABELS = {
    "High Temp Plate": "Smooth PEI (High Temp Plate)",
    "Textured PEI Plate": "Textured PEI",
    "Engineering Plate": "Engineering",
    "Cool Plate": "Cool Plate",
    "Cool Plate (SuperTack)": "Cool Plate SuperTack",
    "Supertack Plate": "SuperTack",
}
PLATE_ALIASES = {"Smooth PEI Plate": "High Temp Plate"}


def check_bed_type(value: str | None) -> str | None:
    """'' / None → None (preset default); otherwise one of BED_TYPES ("Smooth PEI" aliases)."""
    if not value:
        return None
    value = PLATE_ALIASES.get(value, value)
    if value not in BED_TYPES:
        raise BadRequest(
            f"Unknown build plate {value[:40]!r}", f"Use one of: {', '.join(BED_TYPES)}"
        )
    return value


WALLS_RANGE = (1, 10)
INFILL_RANGE = (0, 100)
SHELL_RANGE = (0, 30)  # top/bottom solid layers
COPIES_RANGE = (1, 25)
TRUE_WORDS = ("1", "true", "on", "yes")
FALSE_WORDS = ("0", "false", "off", "no", "")


@dataclass(frozen=True)
class PrintSettings:
    walls: int = 2
    infill: int = 15  # percent
    supports: Supports = "off"
    build_plate_only: bool = False
    top_layers: int = 5  # the Bambu "0.20mm Standard" presets' own values
    bottom_layers: int = 3
    brim: bool = False  # True: outer brim; False: none (overrides the preset's auto brim)
    copies: int = 1  # of the whole selection on one plate; laid out by os2slice, not a slicer key
    # Extra slicer settings from the panel, (catalog key, value) (extra_settings.py).
    extras: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _check_int("walls", self.walls, WALLS_RANGE)
        _check_int("infill", self.infill, INFILL_RANGE)
        _check_int("top_layers", self.top_layers, SHELL_RANGE)
        _check_int("bottom_layers", self.bottom_layers, SHELL_RANGE)
        _check_int("copies", self.copies, COPIES_RANGE)
        if self.supports not in SUPPORTS:
            raise BadRequest(f"supports must be one of {', '.join(SUPPORTS)}")
        for name in ("build_plate_only", "brim"):
            if not isinstance(getattr(self, name), bool):
                raise BadRequest(f"{name} must be true or false")
        for key, value in self.extras:
            if key not in extra_settings.BY_KEY:
                raise BadRequest(f"Unknown extra setting {key[:40]!r}")
            extra_settings.BY_KEY[key].parse(value)

    def process_overrides(self) -> dict[str, Any]:
        """Bambu Studio process keys, in the shapes verified in docs/BAMBUDDY_API.md."""
        overrides: dict[str, Any] = {
            "wall_loops": self.walls,
            "sparse_infill_density": f"{self.infill}%",
            "enable_support": 1 if self.supports != "off" else 0,
            "top_shell_layers": self.top_layers,
            "bottom_shell_layers": self.bottom_layers,
            "brim_type": "outer_only" if self.brim else "no_brim",
        }
        if self.supports != "off":
            overrides["support_type"] = SUPPORT_TYPE[self.supports]
            overrides["support_on_build_plate_only"] = 1 if self.build_plate_only else 0
        overrides.update(extra_settings.overrides(self.extras, "process"))
        return overrides

    def filament_overrides(self, bed_type: str | None = None) -> dict[str, str]:
        """The panel's extra filament settings (one value, for every filament)."""
        return extra_settings.overrides(self.extras, "filament", bed_type)

    def describe(self, technology: str = "fdm") -> str:
        if technology == "sla":
            return self.describe_sla()
        sup = "no supports" if self.supports == "off" else f"{self.supports} supports"
        if self.supports != "off" and self.build_plate_only:
            sup += " (build plate only)"
        shells = f"{self.top_layers} top / {self.bottom_layers} bottom layers"
        text = f"{self.walls} walls, {self.infill}% infill, {sup}, {shells}"
        text += ", brim" if self.brim else ", no brim"
        if self.copies > 1:
            text += f", {self.copies} copies"
        if self.extras:
            text += f"; {extra_settings.describe(self.extras)}"
        return text

    def describe_sla(self) -> str:
        """The settings a resin printer uses: supports and copies."""
        text = "no supports (on the build platform)" if self.supports == "off" else "auto supports"
        if self.copies > 1:
            text += f", {self.copies} copies"
        return text

    def for_sla(self) -> PrintSettings:
        """Only what a resin printer uses (supports off or auto, copies); the rest default."""
        supports: Supports = "off" if self.supports == "off" else SLA_DEFAULT_SUPPORTS
        return PrintSettings(supports=supports, copies=self.copies)

    @classmethod
    def from_strings(cls, values: Mapping[str, str], defaults: PrintSettings) -> PrintSettings:
        """Build from untrusted form/CLI strings; missing keys keep `defaults`."""
        walls = _parse_int("walls", values.get("walls"), defaults.walls)
        infill = _parse_int("infill", values.get("infill"), defaults.infill)
        supports = values.get("supports") or defaults.supports
        if supports not in SUPPORTS:
            raise BadRequest(f"supports must be one of {', '.join(SUPPORTS)}")
        return cls(
            walls,
            infill,
            cast(Supports, supports),
            _parse_bool(
                "build_plate_only", values.get("build_plate_only"), defaults.build_plate_only
            ),
            _parse_int("top_layers", values.get("top_layers"), defaults.top_layers),
            _parse_int("bottom_layers", values.get("bottom_layers"), defaults.bottom_layers),
            _parse_bool("brim", values.get("brim"), defaults.brim),
            _parse_int("copies", values.get("copies"), defaults.copies),
        )


def _check_int(name: str, value: object, bounds: tuple[int, int]) -> None:
    lo, hi = bounds
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise BadRequest(f"{name} must be a whole number from {lo} to {hi}")


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    """A checkbox: absent from the form = unchecked, so callers pass "" for that."""
    if raw is None:
        return default
    if raw in TRUE_WORDS:
        return True
    if raw in FALSE_WORDS:
        return False
    raise BadRequest(f"{name} must be true or false")


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw == "":
        return default
    raw = raw.strip().removesuffix("%")
    if not raw.isdigit() or len(raw) > 3:
        raise BadRequest(f"{name} must be a whole number")
    return int(raw)
