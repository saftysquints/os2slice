"""Module kinds → classes, and the configured modules of one process (`Modules`).

To add a module: implement it in its own file, then register its class below under
`SLICERS` and/or `TARGETS` by its `spec.kind` (a module whose role is "both", like
BamBuddy, goes in both). Config validation, the config page and `doctor` find it
through `spec_for`; nothing else needs to know the kind.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from os2slice.errors import AuthError, BadRequest, ConfigError
from os2slice.modules.bambuddy import BambuddyModule
from os2slice.modules.base import (
    Field,
    Material,
    ModuleError,
    ModuleSpec,
    PrinterInfo,
    ProfileCatalog,
    Slicer,
    Target,
)
from os2slice.modules.moonraker import Moonraker
from os2slice.modules.preform import PreformServer
from os2slice.modules.prusalink import PrusaLink
from os2slice.modules.slicerapi import BambuStudioApi, OrcaSlicerApi

if TYPE_CHECKING:
    from os2slice.config import Config, PrinterConfig

log = logging.getLogger(__name__)

SLICERS: dict[str, type[Any]] = {
    "bambuddy": BambuddyModule,
    "bambu-studio-api": BambuStudioApi,
    "orca-slicer-api": OrcaSlicerApi,
    "preform-server": PreformServer,
}
TARGETS: dict[str, type[Any]] = {
    "bambuddy": BambuddyModule,
    "moonraker": Moonraker,
    "prusalink": PrusaLink,
    "preform-server": PreformServer,
}

# The local hand-off (`[slicers.<key>]` with `argv`, D-3): not a server-side module,
# but listed so the config page and doctor can describe it like the others.
DESKTOP_SPEC = ModuleSpec(
    kind="desktop",
    label="Desktop slicer (opens the file on this computer)",
    role="slicer",
    technology="fdm",
    fields=(
        Field("name", "Name", "str"),
        Field("argv", "Command", "list", required=True, help='"{file}" is the exported path'),
    ),
    help="The local mode: export the part and launch a slicer with it.",
)

SecretLookup = Callable[[str], "str | None"]


def kinds() -> dict[str, ModuleSpec]:
    """Every known kind and its spec, desktop included."""
    out = {k: c.spec for k, c in {**SLICERS, **TARGETS}.items()}
    return {**out, DESKTOP_SPEC.kind: DESKTOP_SPEC}


def spec_for(kind: str) -> ModuleSpec:
    known = kinds()
    if kind not in known:
        raise ConfigError(f"Unknown module kind {kind!r}", f"Known kinds: {', '.join(known)}")
    return known[kind]


def secret_name(section: str, key: str, field_key: str) -> str:
    """Where a module's secret field lives in the secret store, e.g. targets.farm.api_key."""
    return f"{section}.{key}.{field_key}"


def resolve_secrets(
    section: str, key: str, spec: ModuleSpec, values: Mapping[str, Any], secrets: SecretLookup
) -> dict[str, Any]:
    """The config values plus the module's secret fields, looked up in the secret store."""
    out = dict(values)
    for f in spec.fields:
        if f.type != "secret":
            continue
        name = secret_name(section, key, f.key)
        out[f.key] = secrets(name)
        if f.required and not out[f.key]:
            raise AuthError(
                f"No {f.label} for [{section}.{key}] in the secret store ({name})",
                "Run `os2slice setup-keys --bambuddy`"
                if name == "targets.bambuddy.api_key"
                else f"Run `os2slice setup-keys --secret {name}`",
            )
    return out


def _construct(
    cls: type[Any], values: Mapping[str, Any], key: str, transport: httpx.BaseTransport | None
) -> Any:
    """`cls.from_values(...)` when the class has it, else `cls(...)`, with `values`,
    `transport` and, only for factories that take it, `key`."""
    make = getattr(cls, "from_values", cls)
    if "key" in inspect.signature(make).parameters:
        return make(values, key=key, transport=transport)
    return make(values, transport=transport)


def build_slicer(
    kind: str, values: Mapping[str, Any], *, key: str, transport: httpx.BaseTransport | None = None
) -> Slicer:
    """A slicer module from validated values (secrets resolved)."""
    if kind not in SLICERS:
        raise ConfigError(f"{kind!r} isn't a slicer module")
    return _construct(SLICERS[kind], values, key, transport)  # type: ignore[no-any-return]


def build_target(
    kind: str, values: Mapping[str, Any], *, key: str, transport: httpx.BaseTransport | None = None
) -> Target:
    """A target module from validated values (secrets resolved)."""
    if kind not in TARGETS:
        raise ConfigError(f"{kind!r} isn't a target module")
    return _construct(TARGETS[kind], values, key, transport)  # type: ignore[no-any-return]


def configured_printer(p: PrinterConfig, technology: str) -> PrinterInfo:
    """A [printers.<key>] entry as a PrinterInfo (complete, or an override by name)."""
    extra = dict(p.extra)
    if p.bed_type:
        extra["bed_type"] = p.bed_type
    return PrinterInfo(
        key=p.key,
        name=p.name or p.key,
        technology=p.technology or technology,  # type: ignore[arg-type]
        model=p.model,
        target=p.target or "",
        slicer=p.slicer,
        profiles=p.profiles,
        bed_mm=p.bed_mm,
        nozzle_count=p.nozzle_count,
        extra=extra,
        materials=tuple(
            Material(id=str(n), label=m, kind=m.split()[0].upper() if m.split() else "")
            for n, m in enumerate(p.materials, start=1)
        ),
    )


@dataclass
class Modules:
    """The slicer and target modules this config names, built once per process.

    A role-"both" module (BamBuddy) is one instance, under its [targets.<key>] name
    in `targets` and in `slicers`, so a printer's `slicer` may name it.
    """

    slicers: dict[str, Any] = field(default_factory=dict)
    targets: dict[str, Any] = field(default_factory=dict)
    configured: dict[str, PrinterConfig] = field(default_factory=dict)
    default_printer: str = ""

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        secrets: SecretLookup | None = None,
        transport: httpx.BaseTransport | None = None,
        transports: Mapping[str, httpx.BaseTransport] | None = None,
    ) -> Modules:
        """Build every configured module. `transport(s)` replace the network in tests."""
        if secrets is None:
            from os2slice.auth import get_secret

            secrets = get_secret
        per_key = transports or {}
        mods = cls(configured=dict(cfg.printers), default_printer=cfg.default_printer)
        for key, t in cfg.targets.items():
            spec = spec_for(t.kind)
            values = {
                **resolve_secrets("targets", key, spec, t.values, secrets),
                "models": t.models,
            }
            module = build_target(t.kind, values, key=key, transport=per_key.get(key, transport))
            mods.targets[key] = module
            if spec.role == "both":
                mods.slicers[key] = module
        for key, s in cfg.slicer_modules.items():
            spec = spec_for(s.kind)
            values = resolve_secrets("slicers", key, spec, s.values, secrets)
            mods.slicers[key] = build_slicer(
                s.kind, values, key=key, transport=per_key.get(key, transport)
            )
        return mods

    def close(self) -> None:
        """Close every module that has a `close()` (pooled connections); once each."""
        seen: set[int] = set()
        for module in [*self.targets.values(), *self.slicers.values()]:
            if id(module) in seen:
                continue
            seen.add(id(module))
            close = getattr(module, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # closing is best effort; never mask the real outcome
                    log.warning("closing module %s failed", module.spec.kind, exc_info=True)

    def __enter__(self) -> Modules:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def printers(self) -> list[PrinterInfo]:
        """Every target's printers, discovered and configured, in config order.

        A target that can't list its printers is logged and left out, so the others stay
        printable; when none answers, its error is raised.
        """
        out: list[PrinterInfo] = []
        failed: ModuleError | None = None
        for key, target in self.targets.items():
            spec = target.spec
            mine = tuple(
                configured_printer(p, spec.technology)
                for p in self.configured.values()
                if p.target == key or (p.target is None and spec.discovers_printers)
            )
            try:
                out.extend(target.printers(mine))
            except ModuleError as e:
                log.warning("%s: printers unavailable: %s", key, e.one_line())
                failed = failed or e
        if failed is not None and not out:
            raise failed
        return out

    def find(self, wanted: str | None, printers: list[PrinterInfo] | None = None) -> PrinterInfo:
        """An active printer by key or name (default: `default_printer`)."""
        active = [p for p in (self.printers() if printers is None else printers) if p.active]
        listing = ", ".join(f"{p.name} ({p.key})" for p in active) or "none"
        wanted = wanted or self.default_printer
        if not wanted:
            raise BadRequest("No printer chosen", f"Pick one of: {listing}")
        for p in active:
            if wanted == p.key:
                return p
        for p in active:
            if wanted == p.name:
                return p
        raise BadRequest(f"No active printer called {wanted!r}", f"Printers: {listing}")

    def target_for(self, printer: PrinterInfo) -> Target:
        if printer.target not in self.targets:
            raise ConfigError(f"{printer.name}: no target {printer.target!r} is configured")
        return self.targets[printer.target]  # type: ignore[no-any-return]

    def slicer_for(self, printer: PrinterInfo) -> Slicer:
        if printer.slicer not in self.slicers:
            raise ConfigError(
                f"{printer.name}: no slicer {printer.slicer!r} is configured",
                f"Set slicer for {printer.model or printer.name} in the config",
            )
        return self.slicers[printer.slicer]  # type: ignore[no-any-return]

    def own_profiles(self, printer: PrinterInfo) -> ProfileCatalog:
        """The user's own profiles in the printer's slicer (a sidecar's `profile_dir`), for
        the panel's menus. Empty for a slicer without them, or when they can't be read."""
        own = getattr(self.slicers.get(printer.slicer), "own_profiles", None)
        if own is None:
            return ProfileCatalog()
        try:
            return own()  # type: ignore[no-any-return]
        except ModuleError as e:
            log.warning("%s: own profiles unreadable: %s", printer.slicer, e.one_line())
            return ProfileCatalog()

    def pool_presets(self, printer: PrinterInfo) -> tuple[str, ...]:
        """For a printer pool: the filament presets its target offers besides what is
        loaded (BamBuddy: every preset made for the model). Empty for other printers,
        targets without the method, or when they can't be read."""
        if not printer.pool:
            return ()
        presets = getattr(self.targets.get(printer.target), "filament_presets", None)
        if presets is None:
            return ()
        try:
            return tuple(presets(printer))
        except ModuleError as e:
            log.warning("%s: filament presets unreadable: %s", printer.target, e.one_line())
            return ()

    def ui_links(self, request_host: str = "") -> list[tuple[str, str]]:
        """(label, url) of each target's own UI that has one, for the panel and job pages."""
        links = []
        for target in self.targets.values():
            ui = getattr(target, "ui_url", None)  # a method, or a plain configured URL
            url = ui(request_host) if callable(ui) else str(ui or "")
            if url:
                links.append((target.spec.label, url))
        return links
