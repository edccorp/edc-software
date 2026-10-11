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
        {"name": "HVE-2027-2027.1.0-setup.exe", "id": 11, "size": 9_000_000,
         "digest": "sha256:" + "a" * 64},
        {"name": "HVE-2027-2027.1.0-linux-x86_64.tar.gz", "id": 12, "size": 12_000_000,
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
    assert "https://hve.edccorp.com/download/HVE-2027-2027.1.0-setup.exe" in page
    assert "https://hve.edccorp.com/download/HVE-2027-2027.1.0-linux-x86_64.tar.gz" in page
    assert "a" * 64 in page and "b" * 64 in page
    assert "9 MB" in page and "12 MB" in page
    assert "SHA256SUMS.txt" in page
    assert page.count("Not available yet") == 1   # macOS only
    assert "First release" in page                 # the release notes


def test_only_platform_files_and_the_checksums_are_downloadable():
    downloads = build_index.hve_downloads(RELEASE)
    assert set(downloads) == {"HVE-2027-2027.1.0-setup.exe",
                              "HVE-2027-2027.1.0-linux-x86_64.tar.gz", "SHA256SUMS.txt"}
    assert downloads["HVE-2027-2027.1.0-setup.exe"]["platform"] == "windows"
    assert downloads["HVE-2027-2027.1.0-setup.exe"]["sha256"] == "a" * 64
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


def test_page_text_is_escaped():
    c = dict(content(), tagline="<script>x</script>")
    assert "<script>x</script>" not in build_index.render_hve_page(c, None, {})


# ------------------------------------------------------------- the gateway

SECRET = "edc_hve_download_secret"


@pytest.fixture
def gw(monkeypatch):
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("edc_gateway_hve_site",
                                                  ROOT / "gateway" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    downloads = build_index.hve_downloads(RELEASE)
    origin = {"hve.html": b"<h1>HVE 2027 page</h1>",
              "assets/launch.html": b"<h1>extensions launch</h1>",
              "hve-downloads.json": json.dumps(downloads).encode()}

    async def origin_get(path):
        return origin[path]

    async def tokens():
        return {SECRET: {"name": "Example Lab", "products": {"recon_toolkit": None}}}

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


EXE = "HVE-2027-2027.1.0-setup.exe"
IP = {"x-forwarded-for": "198.51.100.1, 203.0.113.9"}


def test_hve_host_serves_the_hve_page_and_others_the_launch_page(gw):
    assert "HVE 2027 page" in gw.get("/", headers={"host": "hve.edccorp.com"}).text
    assert "HVE 2027 page" in gw.get("/", headers={"host": "HVE.edccorp.com:443"}).text
    assert "extensions launch" in gw.get("/", headers={"host": "extensions.edccorp.com"}).text
    assert "HVE 2027 page" in gw.get("/hve", headers={"host": "extensions.edccorp.com"}).text


def test_a_browser_is_asked_for_its_key(gw):
    r = gw.get(f"/download/{EXE}")
    assert r.status_code == 200
    assert 'type="password"' in r.text and f'action="/download/{EXE}"' in r.text
    assert gw.streamed == []


def test_only_listed_files_can_be_downloaded(gw):
    for name in ("ctest.log", "HVE-2027-2027.2.0-setup.exe", "..%2Fhve.html"):
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
