"""hve.edccorp.com: HVE's landing page and its downloads.

The page is built from content/hve.toml and the newest PUBLISHED release of
edccorp/hve-next. The release workflow there ends with a DRAFT so nothing is
announced before someone publishes it, and this page is an announcement: a
draft must never reach it, and with no release at all it must still render
and say when downloads open.

The downloads go through the gateway, which asks a browser for the EDC
Software key (a browser cannot send Blender's Bearer header) and streams only
the files the published page lists.
"""

import importlib.util
import json
import pathlib
import sys

import pytest
from fastapi.responses import Response

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "gateway"))   # main.py imports licensing, mail

import build_index  # noqa: E402

RELEASE = {
    "tag_name": "v2027.1.0",
    "draft": False,
    "published_at": "2027-01-15T12:00:00Z",
    "body": "## Changes\n- First release",
    "assets": [
        {"name": "HVEStudio-2027.1.0-setup.exe", "id": 11, "size": 9_000_000,
         "digest": "sha256:" + "a" * 64},
        {"name": "HVEStudio-2027.1.0-linux-x86_64.tar.gz", "id": 12, "size": 12_000_000,
         "digest": "sha256:" + "b" * 64},
        {"name": "SHA256SUMS.txt", "id": 13, "size": 200},
        {"name": "ctest.log", "id": 14, "size": 5000},
    ],
}


def content():
    c = build_index.load_hve_content()
    assert c is not None, "content/hve.toml is missing"
    return c


# ------------------------------------------------------------- the page

def test_the_page_renders_with_no_release_and_says_when_downloads_open():
    page = build_index.render_hve_page(content(), None, {})
    assert "Downloads open at release" in page
    assert "/download/" not in page            # nothing to download yet
    assert page.count("Not available yet") == 3


def test_a_release_lists_each_platform_its_size_and_checksum():
    downloads = build_index.hve_downloads(RELEASE)
    page = build_index.render_hve_page(content(), RELEASE, downloads)
    assert "Version 2027.1.0" in page and "released 2027-01-15" in page
    assert "https://hve.edccorp.com/download/HVEStudio-2027.1.0-setup.exe" in page
    assert "https://hve.edccorp.com/download/HVEStudio-2027.1.0-linux-x86_64.tar.gz" in page
    assert "a" * 64 in page and "b" * 64 in page
    assert "9 MB" in page and "12 MB" in page
    assert "SHA256SUMS.txt" in page
    assert page.count("Not available yet") == 1   # macOS only
    assert "First release" in page                 # the release notes


def test_only_platform_files_and_the_checksums_are_downloadable():
    downloads = build_index.hve_downloads(RELEASE)
    assert set(downloads) == {"HVEStudio-2027.1.0-setup.exe",
                              "HVEStudio-2027.1.0-linux-x86_64.tar.gz", "SHA256SUMS.txt"}
    assert downloads["HVEStudio-2027.1.0-setup.exe"]["platform"] == "windows"
    assert downloads["HVEStudio-2027.1.0-setup.exe"]["sha256"] == "a" * 64
    assert all(d["repo"] == "edccorp/hve-next" for d in downloads.values())


def test_a_draft_release_never_reaches_the_page(monkeypatch, tmp_path):
    draft = dict(RELEASE, draft=True, tag_name="v2027.2.0")
    monkeypatch.setattr(build_index, "_fetch", lambda url, **_: json.dumps([draft]).encode())
    assert build_index.write_hve_site(str(tmp_path))
    page = (tmp_path / "hve.html").read_text()
    assert "2027.2.0" not in page and "Downloads open at release" in page
    assert json.loads((tmp_path / "hve-downloads.json").read_text()) == {}

    monkeypatch.setattr(build_index, "_fetch",
                        lambda url, **_: json.dumps([draft, RELEASE]).encode())
    build_index.write_hve_site(str(tmp_path))
    page = (tmp_path / "hve.html").read_text()
    assert "Version 2027.1.0" in page and "2027.2.0" not in page


def test_a_prerelease_is_badged_by_its_stage_and_says_it_is_a_test_build():
    pre = dict(RELEASE, tag_name="v2027.0.0-alpha.1", prerelease=True,
               assets=RELEASE["assets"] + [
                   {"name": "HVEStudio-2027.0.0-alpha.1-macos.dmg", "id": 15,
                    "size": 23_000_000, "digest": "sha256:" + "c" * 64}])
    page = build_index.render_hve_page(content(), pre, build_index.hve_downloads(pre))
    assert '<span class="badge">Alpha</span>' in page
    assert "Test build." in page and "Run anyway" in page and "right-click" in page
    assert "Version 2027.0.0-alpha.1" in page
    assert "/download/HVEStudio-2027.0.0-alpha.1-macos.dmg" in page
    assert "Not available yet" not in page        # all three platforms offered
    final = build_index.render_hve_page(content(), RELEASE, build_index.hve_downloads(RELEASE))
    assert "Test build." not in final
    assert f'<span class="badge">{content()["badge"]}</span>' in final


def test_page_text_is_escaped():
    c = dict(content(), tagline="<script>x</script>")
    assert "<script>x</script>" not in build_index.render_hve_page(c, None, {})


# ------------------------------------------------------------- the gateway

SECRET = "edc_hve_download_secret"


PRE = dict(RELEASE, tag_name="v2027.0.0-alpha.1", prerelease=True,
           assets=[{"name": "HVEStudio-2027.0.0-alpha.1-setup.exe", "id": 21,
                    "size": 9_000_000, "digest": "sha256:" + "d" * 64}])
PRE_EXE = "HVEStudio-2027.0.0-alpha.1-setup.exe"
KEYS = {
    SECRET: {"name": "Example Lab", "products": {"recon_toolkit": None}},
    "edc_everything_label": "Old Customer",                  # a label: every product
    "edc_everything_star": {"name": "Star Lab", "products": ["*"]},
    "edc_tester": {"name": "HVE tester", "products": {"hve_testing": "2999-12-31"}},
    "edc_tester_expired": {"name": "Former tester", "products": {"hve_testing": "2020-01-01"}},
}


def _gateway(monkeypatch, release):
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("edc_gateway_hve_site",
                                                  ROOT / "gateway" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    downloads = build_index.hve_downloads(release)
    origin = {"hve.html": b"<h1>HVE Studio page</h1>",
              "assets/launch.html": b"<h1>extensions launch</h1>",
              "hve-downloads.json": json.dumps(downloads).encode()}

    async def origin_get(path):
        return origin[path]

    async def tokens():
        return KEYS

    streamed = []

    async def stream(repo, asset_id, filename, who, product_id, media_type):
        streamed.append((repo, asset_id, filename, who, product_id))
        return Response(b"file-bytes", media_type=media_type)

    monkeypatch.setattr(main, "_origin_get", origin_get)
    monkeypatch.setattr(main, "_customer_tokens", tokens)
    monkeypatch.setattr(main, "_stream_release_asset", stream)
    main._LICENSE_HITS.clear()
    client = TestClient(main.app)
    client.streamed = streamed
    return client


@pytest.fixture
def gw(monkeypatch):
    return _gateway(monkeypatch, RELEASE)


@pytest.fixture
def gw_pre(monkeypatch):
    return _gateway(monkeypatch, PRE)


def test_a_prerelease_downloads_only_with_a_named_unexpired_testing_grant(gw_pre):
    # Every existing customer -- a label, a "*" grant, a product grant -- is
    # kept off a test build, and is not counted toward the wrong-key lockout.
    for key in (SECRET, "edc_everything_label", "edc_everything_star", "edc_tester_expired"):
        r = gw_pre.post(f"/download/{PRE_EXE}", data={"secret": key}, headers=IP)
        assert r.status_code == 403 and "testers only" in r.text, key
    assert gw_pre.streamed == []
    r = gw_pre.post(f"/download/{PRE_EXE}", data={"secret": "edc_tester"}, headers=IP)
    assert r.status_code == 200 and r.content == b"file-bytes"
    assert gw_pre.streamed == [("edccorp/hve-next", 21, PRE_EXE, "HVE tester", "hve")]
    # The same refusal for a script sending the key as a Bearer header.
    r = gw_pre.get(f"/download/{PRE_EXE}", headers={"authorization": f"Bearer {SECRET}"})
    assert r.status_code == 403


def test_a_final_release_still_downloads_with_any_key(gw):
    for key in KEYS:
        r = gw.post(f"/download/{EXE}", data={"secret": key}, headers=IP)
        assert r.status_code == 200, key


EXE = "HVEStudio-2027.1.0-setup.exe"
IP = {"x-forwarded-for": "198.51.100.1, 203.0.113.9"}


def test_hve_host_serves_the_hve_page_and_others_the_launch_page(gw):
    assert "HVE Studio page" in gw.get("/", headers={"host": "hve.edccorp.com"}).text
    assert "HVE Studio page" in gw.get("/", headers={"host": "HVE.edccorp.com:443"}).text
    assert "extensions launch" in gw.get("/", headers={"host": "extensions.edccorp.com"}).text
    assert "HVE Studio page" in gw.get("/hve", headers={"host": "extensions.edccorp.com"}).text


def test_a_browser_is_asked_for_its_key(gw):
    r = gw.get(f"/download/{EXE}")
    assert r.status_code == 200
    assert 'type="password"' in r.text and f'action="/download/{EXE}"' in r.text
    assert gw.streamed == []


def test_only_listed_files_can_be_downloaded(gw):
    for name in ("ctest.log", "HVEStudio-2027.2.0-setup.exe", "..%2Fhve.html"):
        assert gw.get(f"/download/{name}").status_code == 404
        assert gw.post(f"/download/{name}", data={"secret": SECRET}).status_code == 404
    assert gw.streamed == []


def test_a_wrong_key_is_refused_counted_and_streams_nothing(gw):
    r = gw.post(f"/download/{EXE}", data={"secret": "edc_wrong"}, headers=IP)
    assert r.status_code == 401 and "not recognised" in r.text
    assert gw.post(f"/download/{EXE}", data={"secret": ""}, headers=IP).status_code == 401
    assert gw.streamed == []
    for _ in range(10):
        gw.post(f"/download/{EXE}", data={"secret": "edc_wrong"}, headers=IP)
    # Locked out after repeated failures -- even with the right key.
    assert gw.post(f"/download/{EXE}", data={"secret": SECRET}, headers=IP).status_code == 429
    assert gw.streamed == []


def test_any_key_on_file_downloads_the_program(gw):
    # The customer holds no HVE license: having the program is not licensed,
    # running it is.
    r = gw.post(f"/download/{EXE}", data={"secret": SECRET}, headers=IP)
    assert r.status_code == 200 and r.content == b"file-bytes"
    assert gw.streamed == [("edccorp/hve-next", 11, EXE, "Example Lab", "hve")]


def test_a_script_may_send_the_key_as_a_bearer_header(gw):
    r = gw.get(f"/download/{EXE}", headers={"authorization": f"Bearer {SECRET}", **IP})
    assert r.status_code == 200 and r.content == b"file-bytes"
    r = gw.get(f"/download/{EXE}", headers={"authorization": "Bearer edc_wrong", **IP})
    assert r.status_code == 401


def test_the_page_carries_edc_and_hve_branding():
    page = build_index.render_hve_page(content(), None, {})
    # The EDC name lockup and mark are inlined in currentColor, so dark mode
    # can lighten them; maroon on a dark page all but disappears.
    # The design of extensions.edccorp.com's launch page: the lockup in the
    # footer, the company named over the hero, and a way back to edccorp.com.
    assert page.count("<svg") == 1 and "ENGINEERING" in page
    assert "Engineering Dynamics Company</p>" in page and "Back to edccorp.com" in page
    assert ">EDC Blender Extensions</a>" in page
    assert "#6b0739\"" not in page.split("<style>", 1)[1].split("</style>", 1)[1]
    assert 'src="assets/hve-cube.svg"' in page and 'rel="icon" href="assets/hve-cube.svg"' in page


@pytest.mark.parametrize("name", ["edc-mark.svg", "edc-lockup.svg", "hve-cube.svg"])
def test_brand_assets_are_valid_svg_the_gateway_serves(name):
    import xml.dom.minidom
    path = ROOT / "content" / "assets" / name
    doc = xml.dom.minidom.parse(str(path))
    assert doc.documentElement.getAttribute("viewBox")
    assert "inkscape" not in path.read_text() and "sodipodi" not in path.read_text()


def test_a_site_notice_tops_every_page_and_is_escaped():
    page = build_index.render_hve_page(content(), None, {})
    shown = build_index.with_site_notice(page, "Store <down> for maintenance")
    assert shown.index("Store &lt;down&gt; for maintenance") < shown.index("Downloads open at release")
    assert "<down>" not in shown
    assert build_index.with_site_notice(page, "") == page        # no notice, page untouched
