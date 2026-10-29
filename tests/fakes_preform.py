"""An in-memory PreForm Server, shaped like the replies in docs/PREFORM_API.md.

Shapes are the ✅ rows recorded live against release-3.64.1.664; the upload
(`scene/print`) is the spec's plus one real upload's `{job_id}`. Bodies are checked
against each call's fields (the server refuses unknown ones with JSON_SCHEMA_MATCH_ERROR);
files are read and written at the paths requests name (the shared folder, here one
disk); an async call answers 202 and its operation reads IN_PROGRESS `polls` times, then
goes once read finished with `mode=oneshot`. The real Form 4 is listed only once it's
been discovered by IP (by serial name only after that: mDNS doesn't reach the container).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PREFORM_URL = "http://preform.test:44388"
VERSION = "3.64.1.664"
FORM4 = "Form4-ClairvoyantAnt"
FORM4_IP = "192.168.1.142"
# A fresh server lists 14 virtual printers, each with its product label as its id.
VIRTUAL_PRINTERS = ("Form 2", "Form 3", "Form 3B", "Form 3+", "Form 3B+", "Form 3L",
                    "Form 3BL", "Form 4", "Form 4B", "Form 4L", "Form 4BL", "Fuse 1",
                    "Fuse 1+ 30W", "Form Cure")  # fmt: skip
FIELDS: dict[str, set[str]] = {
    "/v1/print-setting/list/": {"compatible_with"},
    "/v1/device/list/": {"devices"},
    "/v1/device/discover/": {"timeout_seconds", "printer"},
    "/v1/scene/create/": {"machine_type", "material_code", "layer_thickness_mm", "print_setting"},
    "/v1/scene/get/": {"scene_id"},
    "/v1/scene/delete/": {"scene_id"},
    "/v1/scene/model/import/": {"scene_id", "file", "repair_behavior", "name", "units", "lock",
                                "chordal_tolerance_um", "angle_tolerance_deg",
                                "split_multi_model_file"},
    "/v1/scene/model/auto-orient/": {"scene_id", "model_ids", "mode", "tilt"},
    "/v1/scene/model/orient/": {"scene_id", "model_ids", "orientation"},
    "/v1/scene/model/auto-support/": {"scene_id", "model_ids", "density", "slope_multiplier",
                                      "touchpoint_size_mm", "raft_thickness_mm",
                                      "height_above_raft_mm", "only_minima", "raft_type",
                                      "raft_label_enabled", "breakaway_structure_enabled",
                                      "internal_supports_enabled"},
    "/v1/scene/model/duplicate/": {"scene_id", "model_ids", "count"},
    "/v1/scene/model/auto-layout/": {"scene_id", "model_ids", "model_spacing_mm",
                                     "allow_overlapping_supports", "lock_rotation",
                                     "on_layout_failure", "max_ejections"},
    "/v1/scene/model/validate/in-bounds/": {"scene_id", "model_ids"},
    "/v1/scene/model/validate/manifoldness/": {"scene_id", "model_ids"},
    "/v1/scene/model/validate/cups/": {"scene_id"},
    "/v1/scene/model/validate/minima/": {"scene_id"},
    "/v1/scene/model/validate/supportedness/": {"scene_id"},
    "/v1/scene/calculate-print-time/": {"scene_id"},
    "/v1/scene/save/": {"file", "scene_ids", "project_name"},
    "/v1/scene/open/": {"file"},
    "/v1/scene/print/": {"scene_id", "printer", "job_name", "find_printer_timeout_seconds",
                         "print_now"},
}  # fmt: skip
ASYNC = {
    "/v1/scene/model/import/", "/v1/scene/model/auto-orient/", "/v1/scene/model/auto-support/",
    "/v1/scene/model/auto-layout/", "/v1/scene/model/validate/manifoldness/",
    "/v1/scene/model/validate/cups/", "/v1/scene/model/validate/minima/",
    "/v1/scene/model/validate/supportedness/", "/v1/scene/calculate-print-time/",
    "/v1/scene/save/", "/v1/scene/open/", "/v1/scene/print/", "/v1/device/discover/",
}  # fmt: skip
FORM_MAGIC = b"\xad\x8fFormlabs Form"  # a real .form starts so (✅)


def _material(label: str, code: str, *layers: tuple[float, str], machine: str = "FORM-4-0",
              default: str = "DEFAULT") -> dict[str, Any]:  # fmt: skip
    """A catalog material, its default setting labelled "(Default settings)"."""
    return {
        "label": label,
        "description": "",
        "material_settings": [
            {
                "label": f"{t:g} mm ({'Default settings' if ps == default else ps.title()})",
                "scene_settings": {
                    "machine_type": machine,
                    "material_code": code,
                    "layer_thickness_mm": t,
                    "print_setting": ps,
                },
            }
            for t, ps in layers
        ],
    }


def catalog() -> dict[str, Any]:
    """print-setting/list's answer (✅ shape), cut down: Form 4 and 4B (both FORM-4-0) with
    Grey V5 (DEFAULT and FINERTOUCHPOINTS at 0.1 mm), Rigid 4000 V1, Tough 2000 V2 (no
    DEFAULT: SUPPORTSV1 is labelled the default, ✅), Black V5 and Rigid 10K V1.1 (made up
    here: 0.05 mm only); the Form 4L (FRML-4-0) with Grey V5."""
    form4 = [
        _material("Grey V5", "FLGPGR05", (0.025, "DEFAULT"), (0.05, "DEFAULT"),
                  (0.1, "DEFAULT"), (0.1, "FINERTOUCHPOINTS"), (0.16, "DEFAULT")),
        _material("Rigid 4000 V1", "FLRGWH01", (0.05, "DEFAULT"), (0.1, "DEFAULT")),
        _material("Tough 2000 V2", "FLTO2002", (0.05, "SUPPORTSV2"), (0.1, "SUPPORTSV2"),
                  (0.1, "SUPPORTSV1"), default="SUPPORTSV1"),
        _material("Black V5", "FLGPBK05", (0.05, "DEFAULT"), (0.1, "DEFAULT"),
                  (0.16, "DEFAULT")),
        _material("Rigid 10K V1.1", "FLRG1011", (0.05, "DEFAULT")),
    ]  # fmt: skip

    def printer(label: str, volume: list[int], materials: list[dict[str, Any]]) -> dict:
        return {"label": label, "build_volume_dimensions_mm": volume, "materials": materials}

    return {
        "printer_types": [
            printer("Form 4", [200, 125, 210], form4),
            printer("Form 4B", [200, 125, 210], form4),
            printer("Form 4L", [353, 196, 350],
                    [_material("Grey V5", "FLGPGR05", (0.1, "DEFAULT"), (0.2, "DEFAULT"),
                               machine="FRML-4-0")]),
        ]
    }  # fmt: skip


def form4_record(**kw: Any) -> dict[str, Any]:
    """Josh's Form 4 as device/list had it, idle (✅ 2026-10-16): Rigid 4000 V1 loaded."""
    return {
        "id": FORM4,
        "product_name": "Form 4",
        "status": "Not ready",
        "is_connected": True,
        "ready_to_print_now": False,
        "ip_address": FORM4_IP,
        "connection_type": "WIFI",
        "firmware_version": "1.17.1-3085",
        "tank_id": "T0000000",
        "tank_material_code": "FLRGWH01",
        "cartridge_data": {
            "C0000000": {
                "cartridgeMaterialCode": "FLRGWH01",
                "cartridgeOriginalVolume_mL": 5000,
                "cartridgeEstimatedVolumeDispensed_mL": 2848.1,
            }
        },
        "estimated_print_time_remaining_ms": 0,
        "is_remote_print_enabled": True,
        **kw,
    }


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


@dataclass
class FakePreform:
    version: str = VERSION
    printer: dict[str, Any] | None = field(default_factory=form4_record)
    discovered: bool = False  # whether the printer was looked up (device/list has it then)
    polls: int = 1  # IN_PROGRESS answers before an operation is done
    fits: int = 100  # models that fit on the platform; auto-layout fails beyond
    in_bounds: bool = True
    manifold: bool = True
    cups: int = 0
    minima: int = 0
    unsupported: float = 0.0
    down: bool = False  # every request fails to connect
    fail: dict[str, tuple[int, str, str]] = field(default_factory=dict)  # path -> error
    calls: list[str] = field(default_factory=list)  # "POST /v1/scene/create/" (+ "?async=true")
    bodies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # path -> bodies
    scenes: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> settings, models
    deleted: list[str] = field(default_factory=list)
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    imported: list[bytes] = field(default_factory=list)  # each imported file's bytes
    uploads: list[dict[str, Any]] = field(default_factory=list)  # jobs in the printer's queue

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        """The calls in order, without the operation polls."""
        return [c for c in self.calls if "/v1/operation/get/" not in c]

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        p = req.url.path
        is_async = req.url.params.get("async") == "true"
        self.calls.append(f"{req.method} {p}" + ("?async=true" if is_async else ""))
        if p == "/v1/operation/get/":
            return self._operation(
                req.url.params.get("operation_id", ""), req.url.params.get("mode") == "oneshot"
            )
        body = json.loads(req.content) if req.content else {}
        if req.content:
            self.bodies.setdefault(p, []).append(body)
        if is_async and p not in ASYNC:
            return _error(400, "INVALID_QUERY_PARAMETER", "async isn't taken here")
        if unknown := set(body) - FIELDS.get(p, set(body)):
            return _error(400, "JSON_SCHEMA_MATCH_ERROR", f"unrecognized {sorted(unknown)[0]}")
        fail = self.fail.get(p)
        r = _error(*fail) if fail else self._answer(req.method, p, body)
        if not is_async:
            return r
        if r.status_code == 200:
            return self._accept(r.json() if r.content else None, None)
        err = r.json().get("error", {})
        if err.get("code") == "OPERATION_FAILED":  # the operation started, then failed
            return self._accept(None, err)
        return r

    def _accept(self, result: Any, error: dict[str, Any] | None) -> httpx.Response:
        op = str(uuid.uuid4())
        self.ops[op] = {"left": self.polls, "result": result, "error": error}
        url = f"/v1/operation/get/?operation_id={op}"
        return httpx.Response(
            202,
            json={"operation_id": op, "operation_url": url, "message": f"Poll GET {url}"},
            headers={"location": url},
        )

    def _operation(self, op_id: str, oneshot: bool) -> httpx.Response:
        op = self.ops.get(op_id)
        if op is None:
            return _error(404, "NOT_FOUND", "Operation with given ID not found")
        if op["left"] > 0:
            op["left"] -= 1
            return httpx.Response(200, json={"id": op_id, "status": "IN_PROGRESS", "progress": 0.5})
        if oneshot:
            del self.ops[op_id]
        if op["error"]:
            return httpx.Response(
                200,
                json={"id": op_id, "status": "FAILED", "progress": 1.0,
                      "result": {"error": op["error"]}},
            )  # fmt: skip
        out: dict[str, Any] = {"id": op_id, "status": "SUCCEEDED", "progress": 1.0}
        if op["result"] is not None:
            out["result"] = op["result"]
        return httpx.Response(200, json=out)

    def _answer(self, method: str, p: str, body: dict[str, Any]) -> httpx.Response:
        if p == "/v1/":
            return httpx.Response(200, json={"version": self.version})
        if p == "/v1/print-setting/list/":
            return httpx.Response(200, json=catalog())
        if p == "/v1/device/list/":
            virtual = [
                {"id": n, "product_name": n, "status": "Virtual Printer", "is_connected": True,
                 "connection_type": "VIRTUAL", "ip_address": "192.0.2.1", "firmware_version": ""}
                for n in VIRTUAL_PRINTERS
            ]  # fmt: skip
            real = [self.printer] if self.printer and self.discovered else []
            devices = virtual + real
            return httpx.Response(200, json={"count": len(devices), "devices": devices})
        if p == "/v1/device/discover/":
            return self._discover(str(body.get("printer", "")))
        if p == "/v1/scene/create/":
            return self._create(body)
        if p == "/v1/scene/open/":
            return self._open(Path(body["file"]))
        scene_ids = body.get("scene_ids") or [body.get("scene_id")]
        if not all(s in self.scenes for s in scene_ids):
            return _error(400, "INPUT_ERROR", "Scene with given scene_id not found")
        sid = str(scene_ids[0])
        models: list[dict[str, Any]] = self.scenes[sid]["models"]
        if p == "/v1/scene/delete/" and method == "DELETE":
            del self.scenes[sid]
            self.deleted.append(sid)
            return httpx.Response(200)
        if p == "/v1/scene/get/":
            n = len(models)
            return httpx.Response(
                200,
                json={"id": sid, "layer_count": 363 if n else 0,
                      "material_usage": {"volume_ml": round(7.37 * n, 2),
                                         "unsupported_volume_ml": round(5.55 * n, 2)},
                      "models": models, "scene_settings": self.scenes[sid]["settings"]},
            )  # fmt: skip
        if p == "/v1/scene/model/import/":
            path = Path(body["file"])
            if not path.is_file():
                return _error(400, "OPERATION_FAILED", f"Model file not found: {path}")
            self.imported.append(path.read_bytes())
            model = {"id": str(uuid.uuid4()), "name": body.get("name") or path.stem,
                     "has_supports": False}  # fmt: skip
            models.append(model)
            return httpx.Response(
                200,
                json={"new_models": [{"model_id": model["id"], "source_part_index": 0,
                                      "source_part_name": path.stem}]},
            )  # fmt: skip
        if p in ("/v1/scene/model/auto-orient/", "/v1/scene/model/orient/"):
            for m in models:
                m["has_supports"] = False  # orienting removes supports (✅)
            return httpx.Response(200, json={"modified_model_ids": [m["id"] for m in models]})
        if p == "/v1/scene/model/auto-support/":
            for m in models:
                m["has_supports"] = True
            return httpx.Response(200, json={"modified_model_ids": [m["id"] for m in models]})
        if p == "/v1/scene/model/duplicate/":
            results = []
            for m in [m for m in models if m["id"] in body.get("model_ids", [])]:
                new = [{**m, "id": str(uuid.uuid4())} for _ in range(int(body["count"]))]
                models.extend(new)
                new_ids = [n["id"] for n in new]
                results.append({"source_model_id": m["id"], "new_model_ids": new_ids})
            return httpx.Response(200, json={"results": results})
        if p == "/v1/scene/model/auto-layout/":
            if len(models) > self.fits:
                return _error(
                    400,
                    "OPERATION_FAILED",
                    "The layout tool was unable to fit the models into the work area.",
                )
            ids = [m["id"] for m in models]
            return httpx.Response(200, json={"laid_out_model_ids": ids, "ejected_model_ids": []})
        if p.startswith("/v1/scene/model/validate/"):
            return httpx.Response(200, json=self._validate(p.split("/")[-2], models))
        if p == "/v1/scene/calculate-print-time/":
            n = len(models)
            return httpx.Response(
                200,
                json={"preprint_time_s": 300, "printing_time_s": 2691 * n,
                      "total_print_time_s": 300 + 2691 * n},
            )  # fmt: skip
        if p == "/v1/scene/save/":
            path = Path(body["file"])
            if not path.parent.is_dir():
                return _error(400, "OPERATION_FAILED", "Failed to open file.")
            path.write_bytes(FORM_MAGIC + json.dumps(self.scenes[sid]["settings"]).encode())
            return httpx.Response(200)
        if p == "/v1/scene/print/":
            if not isinstance(body.get("print_now"), bool):
                return _error(400, "FAKE_UNSAFE", "os2slice must send print_now explicitly")
            if self._find(str(body.get("printer", ""))) is None:
                return _error(400, "OPERATION_FAILED", "Couldn't find printer, timeout occurred.")
            job = {**body, "job_id": str(uuid.uuid4()), "models": len(models)}
            self.uploads.append(job)
            return httpx.Response(200, json={"job_id": job["job_id"]})
        return _error(404, "NOT_FOUND", f"fake: no route {p}")

    def _find(self, printer: str) -> dict[str, Any] | None:
        known = self.printer
        if not known or not printer:
            return None
        if printer == known.get("ip_address") or (self.discovered and printer == known["id"]):
            return known
        return None

    def _discover(self, printer: str) -> httpx.Response:
        found = self._find(printer)
        if printer and found is None:
            return _error(400, "OPERATION_FAILED", "Couldn't find printer, timeout occurred.")
        if found is None:  # a broad discovery: mDNS doesn't reach the container (✅)
            return httpx.Response(200, json={"count": 0, "devices": []})
        if not self.discovered:  # the first answer is partial (✅)
            self.discovered = True
            found = {**found, "tank_material_code": "", "estimated_print_time_remaining_ms": -1}
        return httpx.Response(200, json={"count": 1, "devices": [found]})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        rows = [
            (ms["scene_settings"], "(Default settings)" in ms["label"])
            for t in catalog()["printer_types"]
            for m in t["materials"]
            for ms in m["material_settings"]
        ]
        layer = float(body.get("layer_thickness_mm") or 0)
        asked = (body.get("machine_type"), body.get("material_code"), layer)
        default = next(
            (s["print_setting"] for s, d in rows
             if d and (s["machine_type"], s["material_code"], s["layer_thickness_mm"]) == asked),
            "",
        )  # fmt: skip
        want = (*asked, body.get("print_setting", default))
        offered = {
            (s["machine_type"], s["material_code"], s["layer_thickness_mm"], s["print_setting"])
            for s, _ in rows
        }
        if want not in offered:
            return _error(400, "INPUT_ERROR", "Scene type not supported")
        return httpx.Response(200, json={"scene_id": self._new_scene(want, []),
                                         "scene_settings": self.scenes_settings(want)})  # fmt: skip

    @staticmethod
    def scenes_settings(want: tuple[Any, ...]) -> dict[str, Any]:
        return {"machine_type": want[0], "material_code": want[1],
                "layer_thickness_mm": f"{want[2]:g}", "print_setting": want[3]}  # fmt: skip

    def _new_scene(self, want: tuple[Any, ...], models: list[dict[str, Any]]) -> str:
        sid = str(uuid.uuid4())
        self.scenes[sid] = {"settings": self.scenes_settings(want), "models": models}
        return sid

    def _open(self, path: Path) -> httpx.Response:
        if path.suffix != ".form":
            return _error(400, "INPUT_ERROR", "File is not a .form file.")
        data = path.read_bytes() if path.is_file() else b""
        if not data.startswith(FORM_MAGIC):
            return _error(
                400,
                "OPERATION_FAILED",
                f"Could not load form file {path}: the file could not be read as a PreForm project",
            )
        s = json.loads(data[len(FORM_MAGIC) :] or b"{}")
        want = (s.get("machine_type"), s.get("material_code"),
                float(s.get("layer_thickness_mm") or 0.1), s.get("print_setting"))  # fmt: skip
        model = {"id": str(uuid.uuid4()), "name": path.stem, "has_supports": True}
        sid = self._new_scene(want, [model])
        return httpx.Response(
            200,
            json={"project_name": path.stem,
                  "scenes": [{"scene_id": sid, "scene_name": "",
                              "scene_settings": self.scenes[sid]["settings"],
                              "new_model_ids": [model["id"]]}]},
        )  # fmt: skip

    def _validate(self, check: str, models: list[dict[str, Any]]) -> dict[str, Any]:
        def per(result: dict[str, Any]) -> dict[str, Any]:
            return {"per_model_results": {m["id"]: dict(result) for m in models}}

        if check == "in-bounds":
            return {**per({"in_bounds": self.in_bounds}), "all_in_bounds": self.in_bounds}
        if check == "manifoldness":
            status = "MANIFOLD" if self.manifold else "NOT_MANIFOLD"
            return per({"is_manifold": self.manifold, "status": status})
        if check == "cups":
            return per({"cup_count": self.cups})
        if check == "minima":
            return per({"minima_count": self.minima})
        return per({"percent_unsupported": self.unsupported})
