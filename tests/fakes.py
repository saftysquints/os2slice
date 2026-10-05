"""In-memory fakes of the Onshape and BamBuddy APIs, shaped like the recorded responses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.conftest import DOC, ELEM, WS, make_stl

PRINTERS = [
    {"id": 1, "name": "A1 Mini", "model": "A1 Mini", "is_active": True, "nozzle_count": 1},
    {"id": 2, "name": "X1C_01", "model": "X1C", "is_active": True, "nozzle_count": 1},
    {"id": 5, "name": "Old", "model": "P1S", "is_active": False, "nozzle_count": 1},
    {"id": 3, "name": "H2D_01", "model": "H2D", "is_active": True, "nozzle_count": 2},
]
# Shaped like the live /printers/{id}/status responses (docs/BAMBUDDY_API.md).
TRAY = {"tray_sub_brands": "", "tray_id_name": "", "remain": 0, "state": 11}
STATUS_FILAMENT = {
    1: {
        "ams": [],
        "vt_tray": [
            {
                "id": 254,
                "tray_type": "PETG",
                "tray_color": "000000FF",
                "tray_info_idx": "GFG99",
                **TRAY,
            }
        ],
    },
    2: {
        "ams": [
            {
                "id": 0,
                "tray": [
                    {"id": 0, "tray_type": "ASA", "tray_color": "FFF144FF", "exists": True, **TRAY},
                    {"id": 1, "tray_type": "PLA", "tray_color": "FFFFFFFF", "exists": True, **TRAY},
                    {
                        "id": 2,
                        "tray_type": "PEEK",
                        "tray_color": "161616FF",
                        "exists": True,
                        **TRAY,
                    },
                    {"id": 3, "tray_type": "", "tray_color": "", "exists": False, **TRAY},
                ],
            }
        ],
        "vt_tray": [{"id": 254, "tray_type": "PETG", "tray_color": "161616FF", **TRAY}],
    },
    3: {  # H2D: AMS 0 feeds the left nozzle, AMS 1 the right (like H2D_01)
        "ams_extruder_map": {"0": 1, "1": 0},
        "ams": [
            {
                "id": 0,
                "tray": [
                    {"id": 0, "tray_type": "PLA", "tray_color": "FFFFFFFF", "exists": True, **TRAY}
                ],
            },
            {
                "id": 1,
                "tray": [
                    {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "exists": True, **TRAY}
                ],
            },
        ],
        "vt_tray": [
            {"id": 254, "tray_type": "PLA", "tray_color": "000000FF", **TRAY},
            {"id": 255, "tray_type": "PLA", "tray_color": "0000FFFF", **TRAY},
        ],
    },
}
FILAMENT_PRESETS = [
    "Bambu PLA Basic @BBL A1M",
    "Generic PETG @BBL A1M",
    "Bambu PLA Basic @BBL X1C",
    "Bambu ASA @BBL X1C",
    "Bambu PETG Basic @BBL X1C",
    "Generic PLA @BBL H2D",
]


@dataclass
class FakeBambuddy:
    job_states: list[str] = field(default_factory=lambda: ["pending", "running", "completed"])
    requests: list[httpx.Request] = field(default_factory=list)
    folders: list[dict[str, Any]] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)
    slice_bodies: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[bytes] = field(default_factory=list)
    status: str = "IDLE"
    sliced_3mf: bytes = b""  # what GET /library/files/31/download returns
    auth_enabled: bool = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, m = request.url.path, request.method
        if path == "/api/v1/printers/":
            return httpx.Response(200, json=PRINTERS)
        if path.endswith("/status") and path.startswith("/api/v1/printers/"):
            pid = int(path.split("/")[-2])
            body = {"id": pid, "connected": True, "state": self.status, **STATUS_FILAMENT[pid]}
            return httpx.Response(200, json=body)
        if path == "/api/v1/slicer/presets":
            items = [{"id": n, "name": n, "source": "standard"} for n in FILAMENT_PRESETS]
            empty = {"printer": [], "process": [], "filament": []}
            tiers = {"standard": {**empty, "filament": items}, "cloud": empty, "local": empty}
            return httpx.Response(200, json=tiers)
        if path == "/api/v1/auth/status":
            return httpx.Response(200, json={"auth_enabled": self.auth_enabled})
        if path == "/api/v1/library/folders/" and m == "GET":
            # Like the real endpoint: a tree of root folders with nested children.
            def node(f: dict[str, Any]) -> dict[str, Any]:
                kids = [node(c) for c in self.folders if c.get("parent_id") == f["id"]]
                return {**f, "children": kids}

            roots = [node(f) for f in self.folders if f.get("parent_id") is None]
            return httpx.Response(200, json=roots)
        if path == "/api/v1/library/folders/" and m == "POST":
            body = json.loads(request.content)
            folder = {"id": len(self.folders) + 1, **body}
            self.folders.append(folder)
            return httpx.Response(200, json=folder)
        if path == "/api/v1/library/files/" and m == "POST":
            self.uploads.append(request.content)
            return httpx.Response(200, json={"id": 30, "filename": "x.stl", "file_type": "stl"})
        if path == "/api/v1/library/files/30/slice":
            self.slice_bodies.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": 7, "status": "pending"})
        if path == "/api/v1/slice-jobs/7":
            state = self.job_states.pop(0) if len(self.job_states) > 1 else self.job_states[0]
            body: dict[str, Any] = {"job_id": 7, "status": state}
            if state == "completed":
                body["result"] = {
                    "library_file_id": 31,
                    "name": "x.gcode.3mf",
                    "print_time_seconds": 709,
                    "filament_used_g": 2.92,
                }
            if state == "failed":  # shaped like the live failure (docs/BAMBUDDY_API.md)
                body["error_status"] = 400
                body["error_detail"] = "slicer crashed"
            return httpx.Response(200, json=body)
        if path == "/api/v1/library/files/31/download":
            return httpx.Response(200, content=self.sliced_3mf)
        if path == "/api/v1/queue/" and m == "POST":
            body = json.loads(request.content)
            self.queued.append(body)
            return httpx.Response(200, json={"id": 99, "status": "pending", **body})
        return httpx.Response(404, json={"detail": "Not Found"})


def fake_modules(cfg: Any, handler: Any) -> Any:
    """The config's slicer and target modules, all talking to `handler` (e.g. FakeBambuddy)."""
    from os2slice.modules.registry import Modules

    return Modules.from_config(
        cfg, secrets=lambda name: "k", transport=httpx.MockTransport(handler)
    )


def fake_onshape(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    base = f"/api/partstudios/d/{DOC}/w/{WS}/e/{ELEM}"
    if path == f"/api/documents/{DOC}":
        return httpx.Response(200, json={"name": "test"})
    if path.startswith("/api/parts/"):
        return httpx.Response(
            200, json=[{"partId": "JHD", "name": "Part 1"}, {"partId": "JKD", "name": "Text"}]
        )
    if path == f"{base}/bodydetails":
        return httpx.Response(
            200,
            json={
                "bodies": [
                    {
                        "id": "JHD",
                        "faces": [
                            {"id": "JHG", "orientation": False,
                             "surface": {"type": "plane", "normal": [0, 0, 1]}},
                            {"id": "JHO", "orientation": False,
                             "surface": {"type": "plane", "normal": [0, 1, 0]}},
                            {"id": "CYL", "orientation": True,
                             "surface": {"type": "cylinder"}},
                        ],
                    }
                ]
            },
        )  # fmt: skip
    if path == f"{base}/stl":
        return httpx.Response(307, headers={"Location": "https://cad-usw2.onshape.com/modelexport"})
    if path == "/modelexport":
        return httpx.Response(200, content=make_stl())
    return httpx.Response(404, json={"message": "Not found."})


def dual_nozzle_3mf(
    group: int = 0, extruder_id: int = 1, physical=("1", "0"), kind: str = "PLA"
) -> bytes:
    """A minimal sliced .gcode.3mf with BamBuddy's H2D nozzle metadata."""
    import io
    import zipfile

    settings = {"physical_extruder_map": list(physical), "filament_map": ["1"]}
    info = (
        '<?xml version="1.0"?><config><plate>'
        f'<filament id="1" group_id="{group}" type="{kind}"/>'
        f'<nozzle id="{group}" extruder_id="{extruder_id}" nozzle_diameter="0.4"/>'
        "</plate></config>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/slice_info.config", info)
    return buf.getvalue()


def uploaded_zip(fake: FakeBambuddy, n: int = -1):
    """The 3MF inside a recorded multipart upload body."""
    import io
    import zipfile

    body = fake.uploads[n]
    start = body.index(b"PK\x03\x04")
    end = body.rindex(b"PK\x05\x06") + 22
    return zipfile.ZipFile(io.BytesIO(body[start:end]))


def dual_nozzle_multi_3mf(extruder_ids: list[int], physical=("1", "0")) -> bytes:
    """A sliced .gcode.3mf where filament n is in group n-1 on slicer extruder extruder_ids[n-1]."""
    import io
    import zipfile

    fils = "".join(
        f'<filament id="{n}" group_id="{n - 1}"/>' for n in range(1, len(extruder_ids) + 1)
    )
    nozzles = "".join(f'<nozzle id="{n}" extruder_id="{e}"/>' for n, e in enumerate(extruder_ids))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps({"physical_extruder_map": list(physical)}),
        )
        z.writestr("Metadata/slice_info.config", f"<config><plate>{fils}{nozzles}</plate></config>")
    return buf.getvalue()
