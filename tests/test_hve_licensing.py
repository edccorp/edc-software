"""HVE licenses: activate a computer, refresh it, release it.

Two halves. The rules -- seats, two-of-three fingerprints, which grants
make a license -- are plain functions in gateway/licensing.py and are
tested directly. The endpoints are tested through the real FastAPI app
with the customer file held in memory, because what matters there is what
reaches the file and what reaches the computer: a seat taken twice, a
refresh that commits, or a license whose signature HVE cannot check would
all pass a test of the rules alone.

Every refusal test also asserts the refusal's CODE, since a request that
fails for the wrong reason fails just the same.
"""

import base64
import copy
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY_DIR = ROOT / "gateway"
sys.path.insert(0, str(GATEWAY_DIR))

import licensing  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
FP = ["a" * 64, "b" * 64, "c" * 64]
OTHER = ["d" * 64, "e" * 64, "f" * 64]
SECRET = "edc_hve_customer_secret"


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def entry(**extra):
    base = {"name": "Example Reconstruction LLC", "email": "it@example.com",
            "products": {"hve": "2027-10-01", "simon": "2027-10-01",
                         "recon_toolkit": None}}
    base.update(extra)
    return base


# ------------------------------------------------------------- the rules

def test_two_of_three_is_the_same_computer():
    assert licensing.same_computer(FP, FP)
    assert licensing.same_computer(FP, [FP[0], FP[1], "9" * 64])   # disk swapped
    assert not licensing.same_computer(FP, [FP[0], "9" * 64, "8" * 64])
    assert not licensing.same_computer(FP, OTHER)


def test_missing_components_never_count_as_a_match():
    assert not licensing.same_computer(["", "", FP[2]], ["", "", FP[2]])


@pytest.mark.parametrize("bad", [None, "abc", FP[:2], [*FP, "a" * 64],
                                 [1, 2, 3], ["xyz" * 8, FP[1], FP[2]],
                                 ["", "", FP[2]]])
def test_a_bad_fingerprint_is_refused_as_one(bad):
    with pytest.raises(licensing.LicenseError) as err:
        licensing.parse_fingerprint(bad)
    assert err.value.code == "bad_fingerprint"


def test_fingerprints_are_read_lowercase():
    assert licensing.parse_fingerprint([p.upper() for p in FP]) == FP


@pytest.mark.parametrize("seats, limit", [(None, 1), (3, 3), ("unlimited", None),
                                          (0, 1), (-2, 1), ("5", 1), (True, 1),
                                          (2.5, 1)])
def test_seat_limit_reads_a_typo_as_one_seat_never_as_unlimited(seats, limit):
    e = entry() if seats is None else entry(seats=seats)
    assert licensing.seat_limit(e) == limit


def test_a_site_license_is_unlimited_whatever_seats_says():
    assert licensing.seat_limit(entry(license_type="site", seats=2)) is None


def test_hve_must_be_named_and_a_wildcard_does_not_reach_it():
    with pytest.raises(licensing.LicenseError) as err:
        licensing.hve_entitlement({"*": None})
    assert err.value.code == "no_hve"
    with pytest.raises(licensing.LicenseError):
        licensing.hve_entitlement({"simon": None})   # an engine without the product


def test_features_keep_lapsed_grants_and_take_maintenance_from_hve():
    features, until = licensing.hve_entitlement(
        {"hve": "2027-10-01", "simon": "2020-01-01", "recon_toolkit": None})
    assert features == ["hve", "simon"]      # lapsed simon still runs old builds
    assert until == "2027-10-01"
    assert licensing.hve_entitlement({"hve": None}) == (["hve"], None)


def test_activation_takes_a_seat_then_reuses_it():
    e = entry()
    first, changed = licensing.activate(e, FP, "LAB-1", "2027.1", NOW)
    assert changed and first["id"].startswith("A-")
    again, changed = licensing.activate(e, list(FP), "LAB-1", "2027.1", NOW)
    assert again["id"] == first["id"] and not changed
    assert len(e["machines"]) == 1


def test_a_changed_part_keeps_the_seat_and_is_remembered():
    e = entry()
    first, _ = licensing.activate(e, FP, "LAB-1", "2027.1", NOW)
    swapped = [FP[0], FP[1], "9" * 64]
    again, changed = licensing.activate(e, swapped, "LAB-1", "2027.1", NOW)
    assert again["id"] == first["id"] and changed
    # The next change after the swap must still match on two parts.
    later = [FP[0], "8" * 64, "9" * 64]
    assert licensing.activate(e, later, "LAB-1", "2027.1", NOW)[0]["id"] == first["id"]


def test_a_full_license_refuses_a_new_computer_and_names_the_holders():
    e = entry()
    licensing.activate(e, FP, "LAB-1", "2027.1", NOW)
    before = copy.deepcopy(e)
    with pytest.raises(licensing.LicenseError) as err:
        licensing.activate(e, OTHER, "LAB-2", "2027.1", NOW)
    assert err.value.code == "seats_full" and err.value.status == 409
    assert [m["hostname"] for m in err.value.extra["machines"]] == ["LAB-1"]
    assert "fp" not in err.value.extra["machines"][0]   # never shown
    assert e == before


def test_a_site_license_takes_any_number_of_computers():
    e = entry(license_type="site")
    for n in range(25):
        licensing.activate(e, [f"{n:064x}", f"{n + 100:064x}", ""], f"PC{n}", "", NOW)
    assert len(e["machines"]) == 25


def test_release_then_activate_elsewhere_moves_the_seat_with_no_cap():
    e = entry()
    for n in range(10):            # a dongle user moving back and forth
        fp = FP if n % 2 == 0 else OTHER
        machine, _ = licensing.activate(e, fp, f"PC{n % 2}", "", NOW)
        assert licensing.deactivate(e, machine["id"])
    assert e["machines"] == []
    assert not licensing.deactivate(e, "A-nothere")


def test_malformed_machine_rows_are_skipped_not_fatal():
    e = entry(machines=[None, "x", {"id": 3}, {"id": "A-1", "fp": FP}])
    assert [m["id"] for m in licensing.machines(e)] == ["A-1"]


def test_claims_carry_the_document_hve_reads():
    e = entry()
    machine, _ = licensing.activate(e, FP, "LAB-1", "2027.1", NOW)
    c = licensing.claims(SECRET, e, e["products"], machine, "2026a", NOW)
    assert c["v"] == 1 and c["kid"] == "2026a"
    assert c["features"] == ["hve", "simon"]
    assert c["maintenance_until"] == "2027-10-01"
    assert c["fingerprint"] == FP and c["activation"] == machine["id"]
    assert c["refresh_by"] == "2026-11-10T12:00:00Z"
    assert c["offline_until"] == "2026-12-10T12:00:00Z"
    assert SECRET not in json.dumps(c)          # the secret never rides along
    assert c["license"].startswith("L-")


def test_an_explicit_license_id_wins():
    assert licensing.license_id(SECRET, {"license_id": "L-0042"}) == "L-0042"


def test_the_signature_verifies_and_covers_every_byte():
    key = Ed25519PrivateKey.generate()
    doc = licensing.encode_license({"v": 1, "x": "y"}, key)
    payload, sig = doc.split(".")
    key.public_key().verify(_b64d(sig), _b64d(payload))
    tampered = bytearray(_b64d(payload))
    tampered[-2] ^= 1
    with pytest.raises(Exception):
        key.public_key().verify(_b64d(sig), bytes(tampered))


def test_a_flattened_pem_still_loads():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    loaded = licensing.load_signing_key(pem.replace("\n", "\\n"))
    assert licensing.public_key_fingerprint(loaded) == licensing.public_key_fingerprint(key)


def test_the_key_fingerprint_is_the_one_hve_next_lists():
    # hve-next licensing/keys/README.md: SHA-256 of the DER public key.
    key = Ed25519PrivateKey.generate()
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    import hashlib
    assert licensing.public_key_fingerprint(key) == hashlib.sha256(der).hexdigest()[:16]


# ------------------------------------------------------------- the endpoints

KEY = Ed25519PrivateKey.generate()
KEY_PEM = KEY.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()


@pytest.fixture
def gw(monkeypatch):
    """The gateway app over an in-memory customer file."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LICENSE_SIGNING_KEY", KEY_PEM)
    monkeypatch.setenv("ADMIN_GH_TOKEN", "gh_test")
    monkeypatch.setenv("CUSTOMERS_REPO", "edccorp/test-customers")
    monkeypatch.delenv("LICENSE_KEY_ID", raising=False)
    spec = importlib.util.spec_from_file_location("edc_gateway_main", GATEWAY_DIR / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    state = {"customers": {SECRET: entry(), "edc_blender_only": "Blender Customer LLC",
                           "edc_site": entry(name="Big Lab", license_type="site")},
             "writes": 0}

    async def read():
        return copy.deepcopy(state["customers"]), "sha"

    async def write(customers, sha, message):
        state["writes"] += 1
        state["customers"] = copy.deepcopy(customers)

    async def tokens():
        return copy.deepcopy(state["customers"])

    monkeypatch.setattr(main, "_read_customers_file", read)
    monkeypatch.setattr(main, "_write_customers_file", write)
    monkeypatch.setattr(main, "_customer_tokens", tokens)
    main._LICENSE_HITS.clear()
    client = TestClient(main.app)
    client.state = state
    client.main = main
    return client


def call(gw, path, secret=SECRET, ip="203.0.113.5", **body):
    headers = {"x-forwarded-for": f"198.51.100.1, {ip}"}
    if secret is not None:
        headers["authorization"] = f"Bearer {secret}"
    return gw.post(path, json=body, headers=headers)


def verified_claims(response) -> dict:
    payload, sig = response.json()["license"].split(".")
    KEY.public_key().verify(_b64d(sig), _b64d(payload))
    return json.loads(_b64d(payload))


def test_activate_returns_a_license_hve_can_verify(gw):
    r = call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1", app_version="2027.1")
    assert r.status_code == 200, r.text
    c = verified_claims(r)
    assert c["licensee"] == "Example Reconstruction LLC"
    assert c["features"] == ["hve", "simon"] and c["fingerprint"] == FP
    assert c["activation"] == r.json()["activation"]
    assert gw.state["writes"] == 1
    assert gw.state["customers"][SECRET]["machines"][0]["hostname"] == "LAB-1"


def test_activating_the_same_computer_again_does_not_write(gw):
    call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1")
    r = call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1")
    assert r.status_code == 200 and gw.state["writes"] == 1


def test_a_second_computer_on_one_seat_is_refused_and_nothing_is_written(gw):
    call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1")
    r = call(gw, "/v1/activate", fingerprint=OTHER, hostname="LAB-2")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "seats_full"
    assert gw.state["writes"] == 1


def test_refresh_resigns_and_never_writes(gw):
    a = call(gw, "/v1/activate", fingerprint=FP).json()["activation"]
    gw.state["customers"][SECRET]["products"]["hve"] = "2028-10-01"   # renewed
    r = call(gw, "/v1/refresh", activation=a, fingerprint=FP)
    assert r.status_code == 200, r.text
    assert verified_claims(r)["maintenance_until"] == "2028-10-01"
    assert gw.state["writes"] == 1


def test_refresh_from_a_copied_activation_on_another_computer_is_refused(gw):
    a = call(gw, "/v1/activate", fingerprint=FP).json()["activation"]
    r = call(gw, "/v1/refresh", activation=a, fingerprint=OTHER)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "different_computer"


def test_release_frees_the_seat_for_another_computer(gw):
    a = call(gw, "/v1/activate", fingerprint=FP).json()["activation"]
    r = call(gw, "/v1/deactivate", activation=a)
    assert r.status_code == 200 and r.json()["was_active"] is True
    assert call(gw, "/v1/refresh", activation=a, fingerprint=FP).json()["detail"]["code"] == "released"
    assert call(gw, "/v1/activate", fingerprint=OTHER).status_code == 200


def test_releasing_twice_is_success_and_writes_once(gw):
    a = call(gw, "/v1/activate", fingerprint=FP).json()["activation"]
    call(gw, "/v1/deactivate", activation=a)
    r = call(gw, "/v1/deactivate", activation=a)
    assert r.status_code == 200 and r.json()["was_active"] is False
    assert gw.state["writes"] == 2


def test_machines_lists_hostnames_never_fingerprints(gw):
    call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1")
    r = gw.get("/v1/machines", headers={"authorization": f"Bearer {SECRET}"})
    body = r.json()
    assert body["seats"] == 1 and body["license_type"] == "seat"
    assert [m["hostname"] for m in body["machines"]] == ["LAB-1"]
    assert FP[0] not in r.text


def test_a_site_license_activates_many(gw):
    for n in range(5):
        r = call(gw, "/v1/activate", secret="edc_site",
                 fingerprint=[f"{n:064x}", f"{n + 9:064x}", ""])
        assert r.status_code == 200, r.text
        assert verified_claims(r)["license_type"] == "site"


def test_a_blender_only_secret_gets_no_hve_and_no_seat(gw):
    r = call(gw, "/v1/activate", secret="edc_blender_only", fingerprint=FP)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_hve"
    assert gw.state["writes"] == 0


def test_a_blender_customer_written_in_full_form_takes_no_seat_either(gw):
    # The compact string form is refused before the file is touched; an
    # object entry with only Blender products must be refused before a seat
    # is written, not after.
    gw.state["customers"]["edc_recon"] = {"name": "Recon Only",
                                          "products": {"recon_toolkit": None, "*": None}}
    r = call(gw, "/v1/activate", secret="edc_recon", fingerprint=FP)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_hve"
    assert gw.state["writes"] == 0
    assert "machines" not in gw.state["customers"]["edc_recon"]


def test_an_unknown_secret_is_refused_as_one(gw):
    for path, body in (("/v1/activate", {"fingerprint": FP}),
                       ("/v1/refresh", {"activation": "A-x", "fingerprint": FP}),
                       ("/v1/deactivate", {"activation": "A-x"})):
        r = call(gw, path, secret="edc_wrong", **body)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "bad_secret", path
    r = call(gw, "/v1/activate", secret=None, fingerprint=FP)
    assert r.json()["detail"]["code"] == "bad_secret"


def test_guessing_secrets_is_cut_off_but_another_address_is_not(gw):
    limit = gw.main.LICENSE_LIMITS["fail"][1]
    for _ in range(limit):
        assert call(gw, "/v1/activate", secret="edc_guess", fingerprint=FP).status_code == 401
    # Even the right secret is refused from that address now ...
    r = call(gw, "/v1/activate", fingerprint=FP)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    # ... and the limit is keyed on the address Railway saw, not one the
    # caller chose: a forged first X-Forwarded-For entry changes nothing.
    r = gw.post("/v1/activate", json={"fingerprint": FP},
                headers={"authorization": f"Bearer {SECRET}",
                         "x-forwarded-for": "1.2.3.4, 203.0.113.5"})
    assert r.status_code == 429
    assert call(gw, "/v1/activate", ip="203.0.113.99", fingerprint=FP).status_code == 200


def test_without_a_signing_key_every_endpoint_says_so(gw):
    gw.main.LICENSE_KEY = None
    r = call(gw, "/v1/activate", fingerprint=FP)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_configured"


def test_healthz_shows_the_public_key_fingerprint_and_never_the_key(gw):
    body = gw.get("/healthz").json()
    assert body["license_signing"] == {
        "kid": "2026a", "public_key": licensing.public_key_fingerprint(KEY)}
    assert "PRIVATE" not in json.dumps(body)


def test_hve_ids_are_valid_purchase_products(gw):
    session = {"id": "cs_test", "metadata": {"products": "hve,simon"}}
    assert gw.main._session_products(session) == ["hve", "simon"]


# ------------------------------------------------------------- /offline

from urllib.parse import urlencode  # noqa: E402

REQUEST = json.dumps({"type": "hve-activation-request", "v": 1, "fingerprint": FP,
                      "hostname": "AIRGAP-1", "app_version": "2027.1",
                      "created": "2026-10-11T12:00:00Z"})


def form(gw, ip="203.0.113.5", **fields):
    return gw.post("/offline", content=urlencode(fields),
                   headers={"content-type": "application/x-www-form-urlencoded",
                            "x-forwarded-for": ip})


def test_the_offline_page_is_a_form(gw):
    r = gw.get("/offline")
    assert r.status_code == 200 and 'name="secret"' in r.text and 'name="request"' in r.text


def test_offline_activation_downloads_a_year_long_license_for_that_computer(gw):
    r = form(gw, secret=SECRET, action="activate", request=REQUEST)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    assert "hve-license-AIRGAP-1.hvelic" in r.headers["content-disposition"]
    payload, sig = r.text.strip().split(".")
    KEY.public_key().verify(_b64d(sig), _b64d(payload))
    c = json.loads(_b64d(payload))
    assert c["fingerprint"] == FP and c["features"] == ["hve", "simon"]
    issued = datetime.strptime(c["issued"], "%Y-%m-%dT%H:%M:%SZ")
    until = datetime.strptime(c["offline_until"], "%Y-%m-%dT%H:%M:%SZ")
    assert (until - issued).days == licensing.OFFLINE_FILE_DAYS
    assert c["refresh_by"] == c["offline_until"]     # nothing to refresh from
    m = gw.state["customers"][SECRET]["machines"]
    assert len(m) == 1 and m[0]["offline"] is True and m[0]["hostname"] == "AIRGAP-1"
    assert gw.state["writes"] == 1


def test_offline_activation_takes_a_seat_like_any_other(gw):
    call(gw, "/v1/activate", fingerprint=OTHER, hostname="ONLINE-PC")
    r = form(gw, secret=SECRET, action="activate", request=REQUEST)
    assert r.status_code == 409
    assert "ONLINE-PC" in r.text and "Release" in r.text     # the holder, releasable
    assert len(gw.state["customers"][SECRET]["machines"]) == 1


def test_a_file_that_is_not_a_request_is_refused_as_one(gw):
    for bad in ("hello", json.dumps({"fingerprint": FP}), REQUEST.replace(FP[0], "zz")):
        r = form(gw, secret=SECRET, action="activate", request=bad)
        assert r.status_code == 422, bad
        assert "attachment" not in r.headers.get("content-disposition", "")
    assert gw.state["writes"] == 0


def test_offline_page_refuses_an_unknown_secret_and_counts_it(gw):
    r = form(gw, secret="edc_nope", action="activate", request=REQUEST)
    assert r.status_code == 401 and "not recognised" in r.text
    assert gw.state["writes"] == 0
    assert len(gw.main._LICENSE_HITS[("fail", "203.0.113.5")]) == 1


def test_the_page_lists_and_releases_computers_and_never_shows_a_fingerprint(gw):
    call(gw, "/v1/activate", fingerprint=FP, hostname="DEAD-PC")
    r = form(gw, secret=SECRET, action="list")
    assert r.status_code == 200 and "DEAD-PC" in r.text and FP[0] not in r.text
    activation = gw.state["customers"][SECRET]["machines"][0]["id"]
    r = form(gw, secret=SECRET, action="release", activation=activation)
    assert r.status_code == 200 and "Released." in r.text
    assert gw.state["customers"][SECRET]["machines"] == []


def test_a_hostile_hostname_is_escaped_on_the_page(gw):
    call(gw, "/v1/activate", fingerprint=FP, hostname="<script>x</script>")
    r = form(gw, secret=SECRET, action="list")
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


def test_offline_page_gives_a_blender_only_customer_no_seat(gw):
    gw.state["customers"]["edc_recon"] = {"name": "Recon Only",
                                          "products": {"recon_toolkit": None, "*": None}}
    r = form(gw, secret="edc_recon", action="activate", request=REQUEST)
    assert r.status_code == 403 and "does not include HVE" in r.text
    assert gw.state["writes"] == 0


def test_enter_in_the_secret_box_shows_the_computers_not_an_error(gw):
    # Enter submits with the first button, "Get the license file", and an
    # empty request: show the computers and say what the upload is for.
    call(gw, "/v1/activate", fingerprint=FP, hostname="LAB-1")
    r = form(gw, secret=SECRET, action="activate", request="")
    assert r.status_code == 200
    assert "LAB-1" in r.text and "choose the activation request" in r.text
    assert "not an HVE activation request" not in r.text
    assert gw.state["writes"] == 1          # the online activation only
