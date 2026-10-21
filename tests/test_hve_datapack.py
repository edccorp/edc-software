"""EDC vehicle data from inside HVE: /v1/updates and /v1/datapack.

hve-next docs/DISTRIBUTION.md, "The EDC data pack": a licensed computer asks
/v1/updates for the signed list of the packs its key may have, checks the
signature against the update key it trusts, and downloads the newest one from
/v1/datapack on the same origin. A pack is a published "data-<version>"
release of hve-next carrying EDCVehicles-<version>.zip -- which must never be
mistaken for a program release on hve.edccorp.com.
"""

import asyncio
import base64
import importlib.util
import json
import pathlib
import sys

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import Response

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "gateway"))

import build_index  # noqa: E402

KEYS = {
    "edc_full": {"name": "Full Lab", "products": {"hve": None, "simon": None, "edcdb": "2999-01-01"}},
    "edc_no_data": {"name": "Program Only", "products": {"hve": None}},
    "edc_lapsed": {"name": "Lapsed Data", "products": {"hve": None, "edcdb": "2020-01-01"}},
    "edc_blender": {"name": "Blender Only", "products": ["*"]},
}
PACKS = [{"version": "2026.11", "name": "EDCVehicles-2026.11.zip", "asset_id": 41,
          "size": 85_000_000, "sha256": "c" * 64},
         {"version": "2026.10", "name": "EDCVehicles-2026.10.zip", "asset_id": 40,
          "size": 84_000_000, "sha256": "b" * 64}]
IP = {"x-forwarded-for": "198.51.100.7"}


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_main():
    spec = importlib.util.spec_from_file_location("edc_gateway_datapack", ROOT / "gateway" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    return main


@pytest.fixture
def gw(monkeypatch):
    from fastapi.testclient import TestClient
    main = _load_main()
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(main, "UPDATE_KEY", key)

    async def tokens():
        return KEYS

    async def packs():
        return PACKS

    streamed = []

    async def stream(repo, asset_id, filename, who, product_id, media_type):
        streamed.append((repo, asset_id, filename, who, product_id))
        return Response(b"zip-bytes", media_type=media_type)

    monkeypatch.setattr(main, "_customer_tokens", tokens)
    monkeypatch.setattr(main, "_datapacks", packs)
    monkeypatch.setattr(main, "_stream_release_asset", stream)
    main._LICENSE_HITS.clear()
    client = TestClient(main.app, base_url="http://license.edccorp.com")
    client.streamed, client.key, client.main = streamed, key, main
    return client


def _listing(client, secret, **headers):
    r = client.get("/v1/updates", headers={"authorization": f"Bearer {secret}", **IP, **headers})
    assert r.status_code == 200, r.text
    payload, sig = r.json()["updates"].split(".")
    raw = _b64d(payload)
    client.key.public_key().verify(_b64d(sig), raw)       # raises if it does not verify
    return json.loads(raw)


def test_the_list_is_signed_by_the_update_key_and_names_its_kid(gw):
    doc = _listing(gw, "edc_full")
    assert doc["v"] == 1 and doc["kid"] == "upd-2026b"
    assert [d["version"] for d in doc["data"]] == ["2026.11", "2026.10"]
    assert doc["data"][0] == {
        "pack": "edc-vehicles", "version": "2026.11", "feature": "edcdb",
        "url": "http://license.edccorp.com/v1/datapack/EDCVehicles-2026.11.zip",
        "sha256": "c" * 64, "size": 85_000_000}


def test_the_pack_url_is_https_behind_railways_tls(gw):
    doc = _listing(gw, "edc_full", **{"x-forwarded-proto": "https"})
    assert doc["data"][0]["url"].startswith("https://license.edccorp.com/v1/datapack/")


def test_a_key_without_current_vehicle_data_gets_an_empty_list(gw):
    assert _listing(gw, "edc_no_data")["data"] == []
    assert _listing(gw, "edc_lapsed")["data"] == []


def test_a_key_without_hve_or_an_unknown_key_is_refused(gw):
    r = gw.get("/v1/updates", headers={"authorization": "Bearer edc_blender", **IP})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_hve"
    r = gw.get("/v1/updates", headers={"authorization": "Bearer edc_nobody", **IP})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "bad_secret"
    r = gw.get("/v1/updates", headers=IP)
    assert r.status_code == 401


def test_without_the_update_key_the_list_says_so_instead_of_signing_with_another(gw, monkeypatch):
    monkeypatch.setattr(gw.main, "UPDATE_KEY", None)
    r = gw.get("/v1/updates", headers={"authorization": "Bearer edc_full", **IP})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_configured"


def test_the_pack_downloads_only_with_current_vehicle_data(gw):
    name = "EDCVehicles-2026.11.zip"
    r = gw.get(f"/v1/datapack/{name}", headers={"authorization": "Bearer edc_full", **IP})
    assert r.status_code == 200 and r.content == b"zip-bytes"
    assert gw.streamed == [("edccorp/hve-next", 41, name, "Full Lab", "edcdb")]
    for key in ("edc_no_data", "edc_lapsed"):
        r = gw.get(f"/v1/datapack/{name}", headers={"authorization": f"Bearer {key}", **IP})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "no_data", key
    r = gw.get("/v1/datapack/EDCVehicles-1999.1.zip", headers={"authorization": "Bearer edc_full", **IP})
    assert r.status_code == 404
    assert len(gw.streamed) == 1


def test_packs_are_read_from_published_data_releases_with_a_hash(monkeypatch):
    main = _load_main()
    releases = [
        {"tag_name": "v1.0.0-alpha.2", "draft": False, "assets": [{"name": "x.exe", "id": 1}]},
        {"tag_name": "data-2026.12", "draft": True,
         "assets": [{"name": "EDCVehicles-2026.12.zip", "id": 9, "size": 3, "digest": "sha256:" + "d" * 64}]},
        {"tag_name": "data-2026.11", "draft": False,
         "assets": [{"name": "EDCVehicles-2026.11.zip", "id": 8, "size": 7, "digest": "sha256:" + "E" * 64}]},
        {"tag_name": "data-2026.9", "draft": False,
         "assets": [{"name": "EDCVehicles-2026.9.zip", "id": 6, "size": 5},
                    {"name": "EDCVehicles-2026.9.zip.sha256", "id": 7, "size": 90}]},
        {"tag_name": "data-2026.8", "draft": False,
         "assets": [{"name": "EDCVehicles-2026.8.zip", "id": 5, "size": 5}]},   # no hash at all
        {"tag_name": "data-next", "draft": False, "assets": []},
    ]

    def handler(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases)
        if request.url.path.endswith("/releases/assets/7"):
            return httpx.Response(200, text="f" * 64 + "  EDCVehicles-2026.9.zip\n")
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    packs = asyncio.run(main._datapacks())
    # A draft, a release with no hash and a non-version tag are not offered;
    # 2026.9 sorts BELOW 2026.11 because versions compare as numbers.
    assert [(p["version"], p["asset_id"], p["sha256"]) for p in packs] == [
        ("2026.11", 8, "e" * 64), ("2026.9", 6, "f" * 64)]


def test_a_data_release_never_becomes_the_program_on_the_page(monkeypatch):
    program = {"tag_name": "v1.0.0-alpha.2", "draft": False, "prerelease": True, "assets": []}
    data = {"tag_name": "data-2026.10", "draft": False, "prerelease": False, "assets": []}
    monkeypatch.setattr(build_index, "_fetch", lambda url, **_: json.dumps([data, program]).encode())
    assert build_index._newest_release("edccorp/hve-next")["tag_name"] == "v1.0.0-alpha.2"
