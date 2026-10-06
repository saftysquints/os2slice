from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import httpx
import pytest

from os2slice import server
from os2slice.auth import Keys
from os2slice.config import Config, ServerConfig
from os2slice.onshape import OnshapeClient
from os2slice.settings import PrintSettings
from tests.conftest import DOC, ELEM, QUERY_WITH_CONFIG, WS
from tests.fakes import FakeBambuddy, fake_modules, fake_onshape, uploaded_zip

HOST = "localhost:8765"
NAV = {
    "Host": HOST,
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
}
PRINT_QUERY = QUERY_WITH_CONFIG.replace("slicer=orca&", "")
# A black filament's icon markup (filament_icons.option_content).
BLACK_ICON = (
    '<span class="fc-emoji">⬛ </span><span class="fc-swatch" style="background:#000000"></span>'
)


class Running:
    def __init__(self, cfg: Config, fake: FakeBambuddy) -> None:
        svc = server.Service(
            cfg,
            onshape=lambda: OnshapeClient(
                "https://cad.onshape.com",
                Keys("a", "b"),
                transport=httpx.MockTransport(fake_onshape),
            ),
            modules=fake_modules(cfg, fake),
        )
        self.svc, self.fake = svc, fake
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(svc))
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.httpd.server_port}", follow_redirects=False
        )

    def get_form(self, headers: dict | None = None) -> tuple[httpx.Response, dict[str, str]]:
        r = self.http.get(f"/print?{PRINT_QUERY}", headers=headers or NAV)
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', r.text))
        return r, {
            k: v.replace("&amp;", "&").replace("&#x27;", "'").replace("&quot;", '"')
            for k, v in fields.items()
        }

    def post(self, form: dict[str, str], **headers: str) -> httpx.Response:
        h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}", **headers}
        return self.http.post("/print", data=form, headers={k: v for k, v in h.items() if v})


@pytest.fixture
def srv(cfg: Config) -> Iterator[Running]:
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    yield run
    run.httpd.shutdown()


def choices(**extra: str) -> dict[str, str]:
    return {
        "printer": "A1 Mini",
        "orient": "y-",
        "walls": "3",
        "infill": "25",
        "supports": "tree",
        **extra,
    }


def wait_job(srv: Running, location: str, headers: dict | None = None) -> httpx.Response:
    for _ in range(100):
        r = srv.http.get(location, headers=headers or {"Host": HOST})
        if "refresh" not in r.text:
            return r
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_confirmation_page(srv: Running) -> None:
    r, fields = srv.get_form()
    assert r.status_code == 200, r.text
    assert "Part 1" in r.text and "A1 Mini (A1 Mini)" in r.text and "idle" in r.text
    assert "List_zrSB7lcyzQWXqq=_1" in r.text  # the configuration is shown
    assert fields["p"] == "JHD" and fields["c"] == "List_zrSB7lcyzQWXqq=_1" and fields["csrf"]
    assert "No presets configured for: X1C_01" in r.text
    assert all(req.method == "GET" for req in srv.fake.requests), "GET must not write to BamBuddy"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    # no-referrer would make browsers send Origin: null on our own POST (seen live).
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_full_print_flow(srv: Running) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, **choices(manual_start="on")})  # Wait for Start checked
    assert r.status_code == 303
    done = wait_job(srv, r.headers["Location"])
    assert "Queued ✓" in done.text, done.text
    assert srv.fake.queued == [{"library_file_id": 31, "printer_id": 1, "manual_start": True}]
    assert srv.fake.slice_bodies[0]["process_overrides"]["support_type"] == "tree(auto)"


def test_token_is_single_use(srv: Running) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, **choices()})
    assert r.status_code == 303
    again = srv.post({**form, **choices()})
    assert again.status_code == 403 and "already used" in again.text
    wait_job(srv, r.headers["Location"])


def test_token_is_bound_to_the_part(srv: Running) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, "p": "OTHER", **choices()})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": ""},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ],
)
def test_cross_site_posts_are_refused(srv: Running, headers: dict[str, str]) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, **choices()}, **headers)
    assert r.status_code == 403
    assert srv.fake.queued == [] and srv.fake.uploads == []


def test_bad_token_and_bad_fields(srv: Running) -> None:
    _, form = srv.get_form()
    assert srv.post({**form, "csrf": "1.2.3", **choices()}).status_code == 403
    assert srv.post({**form, **choices(walls="50")}).status_code == 400
    assert srv.post({**form, **choices(orient="face:../x")}).status_code == 400
    assert srv.post({**form, **choices(), "shell": "rm"}).status_code == 400
    assert srv.fake.queued == []
    # Validation errors don't burn the token: the same form still works.
    r = srv.post({**form, **choices()})
    assert r.status_code == 303
    wait_job(srv, r.headers["Location"])  # don't let the job outlive the test's log capture


def test_get_needs_a_navigation(srv: Running) -> None:
    r, _ = srv.get_form({"Host": HOST})
    assert r.status_code == 403
    r, _ = srv.get_form({**NAV, "Sec-Fetch-Dest": "image"})
    assert r.status_code == 403
    r, _ = srv.get_form({**NAV, "Sec-Fetch-Dest": "iframe"})
    assert r.status_code == 403


def test_wrong_host(srv: Running) -> None:
    r, _ = srv.get_form({**NAV, "Host": "evil.example"})
    assert r.status_code == 403
    assert srv.http.get("/health", headers={"Host": "rebind.example:8765"}).status_code == 403


def test_bad_query(srv: Running) -> None:
    r = srv.http.get(f"/print?{PRINT_QUERY}&slicer=orca", headers=NAV)
    assert r.status_code == 400
    r = srv.http.get(f"/print?d={DOC}&wv=w&wvid={WS}&e={ELEM}", headers=NAV)
    assert r.status_code == 400 and "No part selected" in r.text


def test_methods_and_misc(srv: Running) -> None:
    assert srv.http.put("/print", headers={"Host": HOST}).status_code == 405
    assert srv.http.get("/nope", headers={"Host": HOST}).status_code == 404
    assert srv.http.get("/health", headers={"Host": HOST}).json()["ok"] is True
    assert srv.http.get("/favicon.ico").status_code == 204
    assert srv.http.get("/jobs/" + "a" * 22, headers={"Host": HOST}).status_code == 404


def test_failed_job_is_reported(cfg: Config) -> None:
    run = Running(cfg, FakeBambuddy(job_states=["failed"]))
    try:
        _, form = run.get_form()
        r = run.post({**form, **choices()})
        page = wait_job(run, r.headers["Location"])
        assert "Failed" in page.text and "slicer crashed" in page.text
        assert run.fake.queued == []
    finally:
        run.httpd.shutdown()


@pytest.fixture
def ts_cfg(cfg: Config) -> Config:
    return dataclasses.replace(
        cfg,
        server=ServerConfig(
            hosts=("os2slice.tail.ts.net",),
            identity="tailscale",
            allowed_users=("josh@example.com",),
        ),
    )


def test_tailscale_identity(ts_cfg: Config) -> None:
    run = Running(ts_cfg, FakeBambuddy(job_states=["completed"]))
    host = {**NAV, "Host": "os2slice.tail.ts.net"}
    try:
        assert run.get_form(host)[0].status_code == 403  # no identity header
        assert (
            run.get_form({**host, "Tailscale-User-Login": "eve@example.com"})[0].status_code == 403
        )
        r, form = run.get_form({**host, "Tailscale-User-Login": "josh@example.com"})
        assert r.status_code == 200
        # The token and the job belong to josh; eve can use neither.
        post_h = {"Host": "os2slice.tail.ts.net", "Sec-Fetch-Site": "same-origin"}
        eve = run.http.post(
            "/print",
            data={**form, **choices()},
            headers={**post_h, "Tailscale-User-Login": "eve@example.com"},
        )
        assert eve.status_code == 403
        ok = run.http.post(
            "/print",
            data={**form, **choices()},
            headers={**post_h, "Tailscale-User-Login": "josh@example.com"},
        )
        assert ok.status_code == 303
        job = ok.headers["Location"]
        assert (
            run.http.get(
                job,
                headers={
                    "Host": "os2slice.tail.ts.net",
                    "Tailscale-User-Login": "josh@example.com",
                },
            ).status_code
            == 200
        )
    finally:
        run.httpd.shutdown()


# -- the print panel (Element right panel iframe) --------------------------------

PANEL_QUERY = (
    f"d={DOC}&wv=w&wvid={WS}&e={ELEM}&c=%7B$configuration%7D"
    "&companyId=aaaaaaaaaaaaaaaaaaaaaaaa&server=https%3A%2F%2Fcad.onshape.com&locale=en-US"
)
FRAME = {
    "Host": HOST,
    "Sec-Fetch-Dest": "iframe",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
}


def panel_form(srv: Running) -> tuple[httpx.Response, dict[str, str]]:
    r = srv.http.get(f"/panel?{PANEL_QUERY}", headers=FRAME)
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', r.text))
    return r, fields


def post_panel(srv: Running, form: dict[str, str]) -> httpx.Response:
    h = {
        "Host": HOST,
        "Sec-Fetch-Site": "same-origin",
        "Origin": f"http://{HOST}",
        "Sec-Fetch-Dest": "iframe",
    }
    return srv.http.post("/panel/print", data=form, headers=h)


def test_panel_page(srv: Running) -> None:
    r, fields = panel_form(srv)
    assert r.status_code == 200, r.text
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors https://cad.onshape.com" in csp and "script-src 'self'" in csp
    assert 'data-onshape="https://cad.onshape.com"' in r.text
    assert "&quot;JHD&quot;: &quot;Part 1&quot;" in r.text  # part names for the selection text
    # The preview lays out copies with the print's own spacing (printing.copy_offsets).
    assert 'data-layout="{&quot;gap&quot;: 6.0, &quot;brimGap&quot;: 10.0' in r.text
    assert fields["p"] == "" and fields["face"] == "" and fields["csrf"]
    assert all(req.method == "GET" for req in srv.fake.requests)
    js = srv.http.get("/static/panel.js", headers={"Host": HOST})
    assert js.status_code == 200 and js.headers["Content-Type"].startswith("text/javascript")
    assert "ev.origin !== ONSHAPE" in js.text
    # Script URLs carry a content hash, so a deploy isn't hidden by the day-long cache.
    m = re.search(r'src="(/static/panel\.js\?v=[0-9a-f]{12})"', r.text)
    assert m and srv.http.get(m.group(1), headers={"Host": HOST}).text == js.text


def test_panel_refusals(srv: Running) -> None:
    assert (
        srv.http.get(
            f"/panel?{PANEL_QUERY}", headers={**FRAME, "Sec-Fetch-Dest": "document"}
        ).status_code
        == 403
    )
    evil = PANEL_QUERY.replace("cad.onshape.com", "evil.example")
    assert srv.http.get(f"/panel?{evil}", headers=FRAME).status_code == 403
    # The confirmation page and error pages stay unframeable.
    r = srv.http.get(f"/print?{PRINT_QUERY}", headers=NAV)
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_panel_face_only_prints_the_faces_part(srv: Running) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "face": "JHO", **choices(orient="face")})
    assert r.status_code == 303, r.text
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "Queued ✓" in page.text and "face JHO down" in page.text
    assert "frame-ancestors https://cad.onshape.com" in page.headers["Content-Security-Policy"]
    assert 'href="/panel?' in page.text  # "Print another"
    # Wait for Start unchecked (not sent): the print starts by itself.
    assert srv.fake.queued == [{"library_file_id": 31, "printer_id": 1, "manual_start": False}]
    assert srv.fake.slice_bodies[0]["auto_orient"] is False


def test_panel_needs_a_selection(srv: Running) -> None:
    _, form = panel_form(srv)
    assert post_panel(srv, {**form, **choices(orient="as-modeled")}).status_code == 400
    assert post_panel(srv, {**form, "p": "JHD", **choices(orient="face")}).status_code == 400
    assert post_panel(srv, {**form, "face": "../x", **choices(orient="face")}).status_code == 400
    assert srv.fake.queued == []


def test_tokens_dont_cross_between_page_and_panel(srv: Running) -> None:
    _, page = srv.get_form()
    _, panel = panel_form(srv)
    assert (
        post_panel(srv, {**panel, "csrf": page["csrf"], "p": "JHD", **choices()}).status_code == 403
    )
    assert srv.post({**page, "csrf": panel["csrf"], **choices()}).status_code == 403


def test_non_panel_job_is_not_frameable(srv: Running) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, **choices()})
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


# -- 3D preview ----------------------------------------------------------------

PREVIEW = f"/panel/preview?d={DOC}&wv=w&wvid={WS}&e={ELEM}&c=&p=JHD&face=&orient=x%2B"
FETCH = {
    "Host": HOST,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
}


def test_preview_returns_the_oriented_part_and_caches(
    srv: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    exports: list[str] = []
    real = OnshapeClient.export_stl

    def counting(self, req, units="millimeter"):  # type: ignore[no-untyped-def]
        exports.append(req.part_id)
        return real(self, req, units)

    monkeypatch.setattr(OnshapeClient, "export_stl", counting)
    r = srv.http.get(PREVIEW, headers=FETCH)
    assert r.status_code == 200, r.text
    assert r.headers["Content-Type"] == "model/stl"
    assert int.from_bytes(r.content[80:84], "little") == 12
    assert srv.http.get(PREVIEW, headers=FETCH).content == r.content
    assert exports == ["JHD"]  # second request served from the cache
    assert all(req.method == "GET" for req in srv.fake.requests)  # never touches BamBuddy


def test_preview_from_a_face_alone(srv: Running) -> None:
    url = (
        PREVIEW.replace("p=JHD", "p=")
        .replace("face=", "face=JHO")
        .replace("orient=x%2B", "orient=face")
    )
    assert srv.http.get(url, headers=FETCH).status_code == 200


@pytest.mark.parametrize(
    ("url", "headers", "status"),
    [
        (PREVIEW, {**FETCH, "Sec-Fetch-Site": "cross-site"}, 403),
        (PREVIEW, {"Host": HOST}, 403),
        (PREVIEW + "&slicer=orca", FETCH, 400),
        (PREVIEW.replace("p=JHD", "p="), FETCH, 400),
        (PREVIEW.replace("face=", "face=..%2Fx"), FETCH, 400),
        (PREVIEW.replace("orient=x%2B", "orient=sideways"), FETCH, 400),
    ],
)
def test_preview_refusals(srv: Running, url: str, headers: dict, status: int) -> None:
    assert srv.http.get(url, headers=headers).status_code == status


def test_static_allow_list(srv: Running) -> None:
    ok = srv.http.get("/static/vendor/three.module.js", headers={"Host": HOST})
    assert ok.status_code == 200 and "three.core.js" in ok.text
    assert ok.headers["Cache-Control"] == "public, max-age=86400"
    for bad in (
        "/static/../server.py",
        "/static/vendor/three.LICENSE",
        "/static/nope.js",
        "/static/%2e%2e/cli.py",
    ):
        assert srv.http.get(bad, headers={"Host": HOST}).status_code == 404, bad


def test_panel_allows_its_own_fetches(srv: Running) -> None:
    r, _ = panel_form(srv)
    csp = r.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp and "script-src 'self'" in csp
    assert 'src="/static/preview.js?v=' in r.text and "data-beds=" in r.text
    assert "[180, 180]" in r.text.replace("&quot;", '"')  # A1 Mini bed


# -- printer + filament menu -------------------------------------------------


def test_menu_lists_loaded_filament_and_preselects_it(srv: Running) -> None:
    r, _ = srv.get_form()
    assert '<optgroup label="A1 Mini (A1 Mini), idle">' in r.text
    # The A1 Mini's only loaded spool (external PETG) is chosen over the PLA preset.
    assert (
        '<option value="bambuddy/1|254" data-color="#000000" selected>'
        f"{BLACK_ICON}A1 Mini · External: PETG · black</option>" in r.text
    )
    # Customizable selects show the chosen option's swatch in the closed menu.
    assert (
        '<select name="printer" class="fc" required><button><selectedcontent></selectedcontent>'
        "</button><optgroup" in r.text
    )
    assert (
        '<option value="bambuddy/1">A1 Mini · preset filament (Bambu PLA Basic)</option>' in r.text
    )


def test_menu_posts_a_slot(srv: Running) -> None:
    _, form = srv.get_form()
    r = srv.post({**form, **choices(printer="bambuddy/1|254")})
    assert r.status_code == 303
    page = wait_job(srv, r.headers["Location"])
    assert "External: PETG · black" in page.text
    assert srv.fake.queued[0]["use_ams"] is False
    assert srv.fake.slice_bodies[0]["filament_preset"]["id"] == "Generic PETG @BBL A1M"


def test_panel_has_separate_printer_and_filament_menus(srv: Running) -> None:
    r, _ = panel_form(srv)
    assert '<label>Printer <select name="printer" required>' in r.text
    assert '<option value="bambuddy/1" selected>A1 Mini (A1 Mini), idle</option>' in r.text
    assert "|" not in re.search(r'<select name="printer".*?</select>', r.text, re.S).group(0)
    # Filament options for the default printer, its loaded spool preselected...
    menu = re.search(
        r'<select name="filament" class="fc" data-choices="([^"]*)">(.*?)</select>', r.text, re.S
    )
    assert menu, r.text
    assert menu.group(2).startswith("<button><selectedcontent></selectedcontent></button>")
    assert (
        f'<option value="254" data-color="#000000" selected>{BLACK_ICON}External: PETG · black'
        "</option>" in menu.group(2)
    )
    assert '<option value="">Preset filament (Bambu PLA Basic)</option>' in menu.group(2)
    # ...and every printer's choices for panel.js to switch to.
    choices_json = json.loads(menu.group(1).replace("&quot;", '"').replace("&#x27;", "'"))
    # The only printer with presets here, and its model's pool.
    assert set(choices_json) == {"bambuddy/1", "bambuddy/any:A1 Mini"}
    assert [c["value"] for c in choices_json["bambuddy/1"]] == ["", "254"]
    assert [c["default"] for c in choices_json["bambuddy/1"]] == [False, True]


def test_panel_posts_printer_and_filament_separately(srv: Running) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", **choices(filament="254")})
    assert r.status_code == 303, r.text
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "External: PETG · black" in page.text
    assert srv.fake.slice_bodies[0]["filament_preset"]["id"] == "Generic PETG @BBL A1M"


@pytest.mark.parametrize(
    "bad",
    [
        {"filament": "x/y"},
        {"filament": "1" * 41},
        {"printer": "A1 Mini|254", "filament": "254"},
        {"printer": "A" * 101},
    ],
)
def test_panel_filament_refusals(srv: Running, bad: dict[str, str]) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", **choices(**bad)})
    assert r.status_code == 400 and srv.fake.queued == []


def test_unknown_material_fails_the_job_before_anything_is_uploaded(srv: Running) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", **choices(printer="bambuddy/1", filament="7")})
    assert r.status_code == 303
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "Nothing is loaded in slot 7 on A1 Mini" in page.text
    assert srv.fake.uploads == [] and srv.fake.queued == []


def test_panel_preset_filament_is_an_empty_choice(srv: Running) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", **choices(filament="")})
    assert r.status_code == 303
    wait_job(srv, r.headers["Location"], headers=FRAME)
    assert srv.fake.slice_bodies[0]["filament_preset"]["id"] == "Bambu PLA Basic @BBL A1M"


def test_plate_field(srv: Running) -> None:
    _, form = srv.get_form()
    assert srv.post({**form, **choices(plate="Glass")}).status_code == 400
    r = srv.post({**form, **choices(plate="Textured PEI Plate")})
    assert r.status_code == 303
    wait_job(srv, r.headers["Location"])
    assert srv.fake.slice_bodies[0]["bed_type"] == "Textured PEI Plate"


def test_brim_shells_and_copies_fields(srv: Running) -> None:
    r, form = srv.get_form()
    for name in ("top_layers", "bottom_layers", "copies", "brim"):
        assert f'name="{name}"' in r.text
    for bad in ({"copies": "0"}, {"copies": "26"}, {"top_layers": "x"}, {"brim": "outer"}):
        assert srv.post({**form, **choices(**bad)}).status_code == 400
    extra = {"brim": "true", "top_layers": "6", "bottom_layers": "4", "copies": "2"}
    r = srv.post({**form, **choices(**extra)})
    assert r.status_code == 303
    wait_job(srv, r.headers["Location"])
    o = srv.fake.slice_bodies[0]["process_overrides"]
    assert (o["brim_type"], o["top_shell_layers"], o["bottom_shell_layers"]) == ("outer_only", 6, 4)
    assert uploaded_zip(srv.fake).read("3D/3dmodel.model").decode().count("<item ") == 2


def test_unchecked_box_beats_a_checked_default(cfg: Config) -> None:
    cfg = dataclasses.replace(
        cfg, print_defaults=PrintSettings(brim=True, supports="tree", build_plate_only=True)
    )
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    try:
        r, form = run.get_form()
        assert 'name="brim" value="true" checked' in r.text
        r = run.post({**form, **choices()})  # both boxes unticked: not in the form at all
        wait_job(run, r.headers["Location"])
        o = run.fake.slice_bodies[0]["process_overrides"]
        assert o["brim_type"] == "no_brim" and o["support_on_build_plate_only"] == 0
    finally:
        run.httpd.shutdown()


# -- multi-material in the panel -------------------------------------------------


def test_panel_multi_material_post(srv: Running) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", "extra": "JKD:254", **choices(filament="254")})
    assert r.status_code == 303, r.text
    page = wait_job(srv, r.headers["Location"], headers=FRAME)
    assert "Queued ✓" in page.text and "2 parts, one object" in page.text
    body = srv.fake.slice_bodies[0]
    # Both parts on the same spool: one filament, so the single-external-spool path applies.
    assert [f["id"] for f in body["filament_presets"]] == ["Generic PETG @BBL A1M"]
    assert srv.fake.queued[0]["use_ams"] is False


@pytest.mark.parametrize(
    "extra", ["JKD", "JKD:x/y", "../a:1", ",".join(f"P{i}:1" for i in range(20))]
)
def test_panel_rejects_bad_extra(srv: Running, extra: str) -> None:
    _, form = panel_form(srv)
    r = post_panel(srv, {**form, "p": "JHD", "extra": extra, **choices(printer="A1 Mini|254")})
    assert r.status_code == 400 and srv.fake.queued == []


def test_page_ignores_extra(srv: Running) -> None:
    # The right-click page prints one part; an injected `extra` doesn't change that.
    _, form = srv.get_form()
    r = srv.post({**form, "extra": "JKD:254", **choices(printer="A1 Mini|254")})
    assert r.status_code == 303
    wait_job(srv, r.headers["Location"])
    assert srv.fake.uploads and b"PK\x03\x04" not in srv.fake.uploads[0]  # plain STL


def test_multi_preview(srv: Running) -> None:
    base = PREVIEW + "&extra=JKD"
    a = srv.http.get(base + "&only=JHD", headers=FETCH)
    b = srv.http.get(base + "&only=JKD", headers=FETCH)
    assert a.status_code == b.status_code == 200
    assert srv.http.get(base + "&only=ZZZ", headers=FETCH).status_code == 400
    assert srv.http.get(PREVIEW + "&extra=..%2Fx&only=JHD", headers=FETCH).status_code == 400


# -- Open in Bambu Studio / BamBuddy links ------------------------------------------


def test_model_link_and_download(srv: Running) -> None:
    import io
    import json
    import zipfile

    sliced = io.BytesIO()
    with zipfile.ZipFile(sliced, "w") as z:
        settings = {"version": "02.08.04.57", "printer_settings_id": "Bambu Lab X1 Carbon"}
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
    srv.fake.sliced_3mf = sliced.getvalue()
    _, form = panel_form(srv)
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
    r = srv.http.post("/panel/model-link", data={**form, "p": "JHD", "orient": "x+"}, headers=h)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(f"http://{HOST}/models/") and url.endswith("/os2slice.3mf")
    path = url.split(HOST, 1)[1]
    # Bambu Studio fetches it with no browser headers at all.
    got = srv.http.get(path, headers={"Host": HOST})
    assert got.status_code == 200 and got.headers["Content-Type"] == "model/3mf"
    z = zipfile.ZipFile(io.BytesIO(got.content))
    assert "Metadata/model_settings.config" in z.namelist()
    # The slicer settings come from a BamBuddy slice, and the file says Bambu Studio wrote
    # them (else Bambu Studio loads geometry only).
    assert json.loads(z.read("Metadata/project_settings.config")) == settings
    app = '<metadata name="Application">BambuStudio-02.08.04.57</metadata>'
    assert app in z.read("3D/3dmodel.model").decode()
    assert srv.http.get(path, headers={"Host": HOST}).content == got.content  # cached
    assert len(srv.fake.uploads) == 1 and srv.fake.queued == []  # sliced once, never queued


def test_model_link_refusals(srv: Running) -> None:
    _, form = panel_form(srv)
    cross = {"Host": HOST, "Sec-Fetch-Site": "cross-site"}
    assert (
        srv.http.post("/panel/model-link", data={**form, "p": "JHD"}, headers=cross).status_code
        == 403
    )
    assert (
        srv.http.get("/models/" + "a" * 22 + "/os2slice.3mf", headers={"Host": HOST}).status_code
        == 404
    )
    assert (
        srv.http.get("/models/" + "a" * 22 + "/evil.3mf", headers={"Host": HOST}).status_code == 404
    )


def test_bambuddy_links(srv: Running) -> None:
    r, _ = panel_form(srv)
    assert 'href="http://localhost:8000/queue" target="_blank"' in r.text  # derived from Host
    assert 'id="studio-link"' in r.text
    _, form = srv.get_form()
    job = srv.post({**form, **choices()}).headers["Location"]
    assert (
        "Open BamBuddy&#x27;s queue" in wait_job(srv, job).text
        or "Open BamBuddy's queue" in wait_job(srv, job).text
    )


# -- the shared web Bambu Studio (D-21) ----------------------------------------------


@pytest.fixture
def web(cfg: Config, tmp_path) -> Iterator[Running]:
    from os2slice.config import WebStudioConfig

    ws = WebStudioConfig("https://bambu.test:3001", tmp_path / "inbox", tmp_path / "status.json")
    cfg = dataclasses.replace(cfg, web_studio=ws, panel_web_slicers=("bambu-studio",))
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    yield run
    run.httpd.shutdown()


def test_web_studio_handoff(web: Running) -> None:
    ws = web.svc.cfg.web_studio
    r, form = panel_form(web)
    assert 'id="web-studio-link"' in r.text and 'href="https://bambu.test:3001"' in r.text
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
    got = web.http.post("/panel/web-studio", data={**form, "p": "JHD", "orient": "z+"}, headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["url"] == "https://bambu.test:3001"
    dropped = list(ws.inbox.glob("*.3mf"))
    assert len(dropped) == 1 and dropped[0].name.startswith("Part 1-")
    assert not list(ws.inbox.glob(".*.part"))  # written atomically
    # The fake's slice result has no settings: the part still opens, geometry only.
    assert len(web.fake.uploads) == 1 and web.fake.queued == []


def test_web_studio_status(web: Running) -> None:
    import json
    import time

    ws = web.svc.cfg.web_studio
    fetch = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    assert web.http.get("/panel/web-studio/status", headers=fetch).json()["state"] == "unknown"
    ws.status.write_text(json.dumps({"viewers": 0, "updated": time.time()}))
    assert web.http.get("/panel/web-studio/status", headers=fetch).json()["state"] == "free"
    ws.status.write_text(json.dumps({"viewers": 2, "updated": time.time()}))
    assert web.http.get("/panel/web-studio/status", headers=fetch).json() == {
        "state": "busy",
        "viewers": 2,
    }
    ws.status.write_text(json.dumps({"viewers": 0, "app_running": False, "updated": time.time()}))
    assert web.http.get("/panel/web-studio/status", headers=fetch).json()["state"] == "unknown"
    ws.status.write_text(json.dumps({"viewers": 0, "updated": time.time() - 60}))  # add-on stopped
    assert web.http.get("/panel/web-studio/status", headers=fetch).json()["state"] == "unknown"
    assert web.http.get("/panel/web-studio/status", headers={"Host": HOST}).status_code == 403


def test_web_studio_down(web: Running, caplog: pytest.LogCaptureFixture) -> None:
    """The app keeps exiting after each restart: "down", why, and one log warning."""
    import json
    import time

    ws = web.svc.cfg.web_studio
    fetch = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    since = time.mktime((2026, 10, 2, 10, 13, 0, 0, 0, -1))
    failing = {
        "viewers": 0,
        "app_running": False,
        "failing": True,
        "last_exit": 255,
        "failures": 5,
        "failing_since": since,
        "next_try": since + 32 * 60,
    }
    ws.status.write_text(json.dumps({**failing, "updated": time.time()}))
    with caplog.at_level("WARNING", logger="os2slice.server"):
        for _ in range(3):
            got = web.http.get("/panel/web-studio/status", headers=fetch).json()
            assert got == {
                "state": "down",
                "viewers": 0,
                "reason": "Bambu Studio keeps exiting (status 255, 5 tries since 10:13); "
                "retrying at 10:45",
            }
    warnings = [r for r in caplog.records if "is down" in r.getMessage()]
    assert len(warnings) == 1 and "status 255" in warnings[0].getMessage()
    # A new streak is logged again.
    caplog.clear()
    ws.status.write_text(
        json.dumps({**failing, "failing_since": since + 1, "updated": time.time()})
    )
    with caplog.at_level("WARNING", logger="os2slice.server"):
        web.http.get("/panel/web-studio/status", headers=fetch)
    assert "is down" in caplog.text
    # A stale file says nothing about now: unknown, as before.
    ws.status.write_text(json.dumps({**failing, "updated": time.time() - 60}))
    assert web.http.get("/panel/web-studio/status", headers=fetch).json() == {
        "state": "unknown",
        "viewers": None,
    }


def test_web_studio_refusals(srv: Running, web: Running) -> None:
    _, form = panel_form(web)
    cross = {"Host": HOST, "Sec-Fetch-Site": "cross-site"}
    assert (
        web.http.post("/panel/web-studio", data={**form, "p": "JHD"}, headers=cross).status_code
        == 403
    )
    assert not list(web.svc.cfg.web_studio.inbox.glob("*"))
    # Not configured: no browser link, and the endpoint says so.
    r, form2 = panel_form(srv)
    assert 'id="web-studio-link"' not in r.text and 'id="studio-link"' in r.text
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
    assert (
        srv.http.post("/panel/web-studio", data={**form2, "p": "JHD"}, headers=h).status_code == 500
    )


@pytest.fixture
def web_orca(cfg: Config, tmp_path) -> Iterator[Running]:
    from os2slice.config import WebStudioConfig

    ws = WebStudioConfig("https://orca.test:3444", tmp_path / "orca-inbox", tmp_path / "orca.json")
    cfg = dataclasses.replace(cfg, web_orca=ws, panel_web_slicers=("orcaslicer",))
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    yield run
    run.httpd.shutdown()


def test_web_orca_handoff_beside_the_desktop_bambu_link(web_orca: Running) -> None:
    import json
    import time

    ws = web_orca.svc.cfg.web_orca
    r, form = panel_form(web_orca)
    assert 'id="web-orca-link"' in r.text and 'href="https://orca.test:3444"' in r.text
    assert '<p class="links">Open in OrcaSlicer: <a id="web-orca-link"' in r.text
    assert 'data-label="OrcaSlicer"' in r.text and 'id="web-studio-link"' not in r.text
    assert 'Open in Bambu Studio: <a id="studio-link"' in r.text  # still offered
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
    got = web_orca.http.post("/panel/web-orca", data={**form, "p": "JHD"}, headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["url"] == "https://orca.test:3444"
    assert len(list(ws.inbox.glob("Part 1-*.3mf"))) == 1
    fetch = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    ws.status.write_text(json.dumps({"viewers": 1, "updated": time.time()}))
    status = web_orca.http.get("/panel/web-orca/status", headers=fetch).json()
    assert status == {"state": "busy", "viewers": 1}
    # The web Bambu Studio isn't set up here: its endpoint says so.
    assert (
        web_orca.http.post("/panel/web-studio", data={**form, "p": "JHD"}, headers=h).status_code
        == 500
    )


def test_client_going_away_is_not_a_crash(
    srv: Running, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import contextlib
    import ssl

    def gone(*a: object, **k: object) -> None:
        raise ssl.SSLError("bad length")

    monkeypatch.setattr(server._Handler, "_get_static", gone)
    # No response at all is fine; the server must just not crash.
    with caplog.at_level("INFO", logger="os2slice.server"), contextlib.suppress(httpx.HTTPError):
        srv.http.get("/static/panel.js", headers={"Host": HOST})
    assert "client went away (SSLError)" in caplog.text and "crashed" not in caplog.text


# -- the panel's process menu (own profiles in the printer's slicer) ---------------------


def own_view(processes: tuple[str, ...]) -> server.PrinterView:
    from os2slice.modules.base import PrinterInfo, Profiles
    from os2slice.printing import profile_material

    printer = PrinterInfo(
        "joshprint", "JoshPrint", "fdm", "RatRig V-Core 3 300", "joshprint", "orca",
        Profiles("JoshPrint 0.5 MMU", "0.2 Strong", "PM ASA"),
    )  # fmt: skip
    materials = (profile_material("BL ASA-CF"), profile_material("PM ASA"))
    return server.PrinterView(printer, "ready", True, materials, processes)


def test_panel_process_menu_lists_own_profiles_with_the_preset_first() -> None:
    html = server._panel_printer_selects([own_view(("0.2 Solid", "0.2 Strong"))], "joshprint")
    menu = re.search(
        r'<label>Process <select name="process" data-choices="[^"]*">(.*?)</select>', html
    )
    assert menu, html
    assert menu.group(1) == (
        '<option value="" selected>0.2 Strong</option><option value="0.2 Solid">0.2 Solid</option>'
    )
    # The filament menu preselects the profile the printer is configured with.
    filament = re.search(r'<select name="filament"[^>]*>(.*?)</select>', html).group(1)
    assert re.search(r'<option value="f-[0-9a-f]{12}" selected>PM ASA</option>', filament)


def test_panel_process_menu_hidden_without_own_profiles() -> None:
    html = server._panel_printer_selects([own_view(())], "joshprint")
    assert '<label hidden>Process <select name="process"' in html


def test_selection_carries_the_process_choice() -> None:
    form = {"d": DOC, "wv": "w", "wvid": WS, "e": ELEM, "p": "JHD", "printer": "joshprint"}
    assert server._selection({**form, "process": "0.2 Solid"}, panel=True)[6] == "0.2 Solid"
    assert server._selection(form, panel=True)[6] == ""
    assert server._selection({**form, "process": "0.2 Solid"}, panel=False)[6] == ""
    for bad in ("x" * 201, "0.2\nSolid"):
        with pytest.raises(server.BadRequest, match="Invalid process choice"):
            server._selection({**form, "process": bad}, panel=True)


def test_panel_extra_settings_reach_the_slicer(cfg: Config) -> None:
    run = Running(dataclasses.replace(cfg, panel_extras=("layer_height", "seam_position")),
                  FakeBambuddy(job_states=["completed"]))  # fmt: skip
    try:
        r, form = panel_form(run)
        assert '<details class="extras"><summary>More settings</summary>' in r.text  # rolled up
        assert '<select name="x_seam_position"><option value="" selected>From the profile' in r.text
        bad = post_panel(run, {**form, "p": "JHD", "x_layer_height": "5"})
        assert bad.status_code == 400 and run.fake.uploads == []
        _, form = panel_form(run)
        ok = post_panel(run, {**form, "p": "JHD", "x_layer_height": "0.12", "x_seam_position": ""})
        assert ok.status_code == 303
        page = wait_job(run, ok.headers["Location"], headers=FRAME)
        assert "layer height 0.12 mm" in page.text
        overrides = run.fake.slice_bodies[0]["process_overrides"]
        assert overrides["layer_height"] == "0.12" and "seam_position" not in overrides
    finally:
        run.httpd.shutdown()


def test_orca_for_both_links_shares_one_line(cfg: Config, tmp_path) -> None:
    from os2slice.config import WebStudioConfig

    ws = WebStudioConfig("https://orca.test:3444", tmp_path / "in", tmp_path / "s.json")
    cfg = dataclasses.replace(cfg, web_orca=ws, panel_web_slicers=("orcaslicer",),
                              panel_local_slicer="orcaslicer")  # fmt: skip
    links = server._studio_links(cfg)
    assert links.count('<p class="links">') == 1 and "Bambu" not in links
    assert links.startswith('<p class="links">Open in OrcaSlicer: <a id="web-orca-link"')
    assert links.endswith('data-scheme="orcaslicer">on this computer</a></p>')


def test_face_part_names_the_part_a_face_belongs_to(srv: Running) -> None:
    _, form = panel_form(srv)
    ids = {k: form[k] for k in ("d", "wv", "wvid", "e", "c")}
    fetch = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    r = srv.http.get("/panel/face-part", params={**ids, "face": "JHG"}, headers=fetch)
    assert r.status_code == 200, r.text
    assert r.json() == {"part": "JHD", "name": "Part 1"}
    assert (
        srv.http.get("/panel/face-part", params={**ids, "face": "NOPE"}, headers=fetch).status_code
        == 400
    )
    assert (
        srv.http.get("/panel/face-part", params={**ids, "face": "x'y"}, headers=fetch).status_code
        == 400
    )
    bad = {**ids, "face": "JHG", "evil": "1"}
    assert srv.http.get("/panel/face-part", params=bad, headers=fetch).status_code == 400
    cross = {"Host": HOST, "Sec-Fetch-Site": "cross-site"}
    assert (
        srv.http.get("/panel/face-part", params={**ids, "face": "JHG"}, headers=cross).status_code
        == 403
    )


def test_panel_printer_profile_menu_and_changer_tools() -> None:
    import html as _html

    from os2slice.modules.base import Material, PrinterInfo, Profiles
    from os2slice.printing import profile_material

    printer = PrinterInfo(
        "joshprint", "JoshPrint", "fdm", "RatRig V-Core 3 300", "joshprint", "orca",
        Profiles("JoshPrint 0.5 MMU", "0.2 Strong", "PM ASA"),
    )  # fmt: skip
    t0 = Material("t0", "T0: Black (ASA)", "ASA", "#000000", profile="PM ASA", raw={"tool": 0})
    t4 = Material("t4", "T4: empty", "ASA", "#FF8400", raw={"tool": 4, "empty": True})
    view = server.PrinterView(
        printer, "ready", True, (t0, t4, profile_material("PM ASA")), ("0.2 Strong",),
        ("JoshPrint 0.5", "JoshPrint 0.5 MMU"), ("JoshPrint 0.5 MMU",),
    )  # fmt: skip
    html = server._panel_printer_selects([view], "joshprint")
    menu = re.search(r'<label>Printer profile <select name="machine"[^>]*>(.*?)</select>', html)
    assert menu, html
    assert menu.group(1) == (
        '<option value="" selected data-mmu>JoshPrint 0.5 MMU</option>'
        '<option value="JoshPrint 0.5">JoshPrint 0.5</option>'
    )
    filament = re.search(
        r'<select name="filament" class="fc" data-choices="([^"]*)">(.*?)</select>', html
    )
    assert (
        f'<option value="t0" data-color="#000000" selected>{BLACK_ICON}T0: Black (ASA) → PM ASA'
        "</option>" in filament.group(2)
    )
    # An empty gate keeps its data-color but gets no icon: it has no filament.
    assert '<option value="t4" data-color="#FF8400" disabled>T4: empty</option>' in (
        filament.group(2)
    )
    choices = json.loads(_html.unescape(filament.group(1)))["joshprint"]
    assert [(c["value"], c["tool"], c.get("disabled", False)) for c in choices] == [
        ("", False, False), ("t0", True, False), ("t4", True, True),
        (choices[3]["value"], False, False),
    ]  # fmt: skip


def test_selection_carries_the_printer_profile() -> None:
    form = {"d": DOC, "wv": "w", "wvid": WS, "e": ELEM, "p": "JHD", "printer": "joshprint"}
    assert server._selection({**form, "machine": "JoshPrint 0.5"}, panel=True)[7] == "JoshPrint 0.5"
    with pytest.raises(server.BadRequest, match="Invalid printer profile choice"):
        server._selection({**form, "machine": "a\nb"}, panel=True)


def test_load_into_a_tool_makes_a_slot_choice() -> None:
    assert server._printer_choice("joshprint", "f-0123456789ab", "t2") == (
        "joshprint", "t2.f-0123456789ab",
    )  # fmt: skip
    assert server._printer_choice("joshprint", "", "t2") == ("joshprint", "t2.preset")
    assert server._printer_choice("joshprint", "t1", "t2") == ("joshprint", "t1")  # a tool already
    assert server._printer_choice("joshprint", "f-0123456789ab") == ("joshprint", "f-0123456789ab")
    with pytest.raises(server.BadRequest, match="Invalid tool choice"):
        server._printer_choice("joshprint", "f-0123456789ab", "gate 2")


def test_hidden_beats_the_label_display_rule() -> None:
    # `label { display:block }` would otherwise keep [hidden] labels ("Load into",
    # Process, Printer profile) on screen.
    css = server._layout("t", "", False)
    assert "label { display:block;" in css and "[hidden] { display:none !important; }" in css
