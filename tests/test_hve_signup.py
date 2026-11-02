"""hve.edccorp.com: "What's coming" and the HVE Studio update list.

Before release the HVE page leads with what is coming -- a picture each,
from content/assets -- and a form for people who want news. The form posts
to the gateway's /signup, which keeps the list as a JSON file in the private
customer repository (SIGNUPS_PATH), emails a confirmation with a link to
leave, and tells EDC (SIGNUP_NOTIFY).

What has to hold: a second sign-up updates the first rather than adding a
duplicate; the hidden field drops bots; the answer is the same whether an
address was new, already there or dropped, so the form cannot be used to ask
who is on the list; nothing is saved when the repository is not configured,
and the page says so instead of thanking someone whose address was lost;
and the unsubscribe link takes the address off the list.
"""

import base64
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "gateway"))

import build_index  # noqa: E402


def content():
    return build_index.load_hve_content()


# ------------------------------------------------------------- the page

def test_before_release_the_page_leads_with_what_is_coming_and_the_form():
    page = build_index.render_hve_page(content(), None, {})
    assert 'id="coming"' in page and 'id="updates"' in page
    assert '<a class="btn" href="#coming">' in page
    assert '<a class="btn ghost" href="#updates">Get updates</a>' in page
    for g in content()["gallery"]:
        assert f'src="assets/{g["image"]}"' in page
        assert (ROOT / "content" / "assets" / g["image"]).is_file(), g["image"]
    # Both sections sit above Download, and the form posts to the gateway.
    assert page.index('id="coming"') < page.index('id="updates"') < page.index('id="download"')
    assert 'action="https://hve.edccorp.com/signup" method="post"' in page
    assert 'name="website"' in page and 'name="current_hve_user"' in page


def test_after_release_the_hero_leads_with_download_again():
    release = {"tag_name": "v2027.1.0", "draft": False, "assets": []}
    page = build_index.render_hve_page(content(), release, {})
    assert '<a class="btn" href="#download">Download</a>' in page
    assert 'id="coming"' in page            # still shown below


def test_the_sections_disappear_when_their_text_is_removed():
    c = dict(content(), coming_intro="", gallery=[], signup_intro="")
    page = build_index.render_hve_page(c, None, {})
    assert 'id="coming"' not in page and 'id="updates"' not in page
    assert '<a class="btn" href="#download">Download</a>' in page


def test_the_gallery_is_kept_out_of_the_other_settings():
    """[[gallery]] tables must come last in hve.toml, or TOML reads every key
    after the first one as part of the last picture."""
    c = content()
    assert "licensing" in c and "requirements" in c and "signup_intro" in c
    for g in c["gallery"]:
        assert set(g) == {"image", "title", "text"}, g


def test_gallery_text_is_escaped():
    c = dict(content(), gallery=[{"image": "x.jpg", "title": "<b>t</b>", "text": "<i>"}])
    page = build_index.render_hve_page(c, None, {})
    assert "<b>t</b>" not in page and "&lt;b&gt;t&lt;/b&gt;" in page


def test_the_page_no_longer_names_edhis():
    assert "EDHIS" not in build_index.render_hve_page(content(), None, {})


# ------------------------------------------------------------- the gateway

class GitHubFile:
    """The signups file in the customer repository, as the contents API has it."""

    def __init__(self, data=None):
        self.data = data
        self.sha = "sha0" if data is not None else None
        self.writes = 0
        self.conflicts = 0          # answer this many PUTs with 409 first

    async def request(self, method, payload=None, path=None):
        assert path == "hve-signups.json"
        if method == "GET":
            if self.data is None:
                return Resp(404)
            body = base64.b64encode(json.dumps(self.data).encode()).decode()
            return Resp(200, {"content": body, "sha": self.sha})
        if self.conflicts:
            self.conflicts -= 1
            return Resp(409)
        assert payload.get("sha") == self.sha
        self.data = json.loads(base64.b64decode(payload["content"]))
        self.writes += 1
        self.sha = f"sha{self.writes}"
        return Resp(200, {})


class Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _gateway(monkeypatch, repo=True, notify=""):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SIGNUP_NOTIFY", notify)
    spec = importlib.util.spec_from_file_location("edc_gateway_signup", ROOT / "gateway" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    store = GitHubFile()
    sent = []

    async def send(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(main, "CUSTOMERS_REPO", "edccorp/customers" if repo else "")
    monkeypatch.setattr(main, "ADMIN_GH_TOKEN", "token" if repo else "")
    monkeypatch.setattr(main, "_customers_file_request", store.request)
    monkeypatch.setattr(main.mail, "send", send)
    main._SIGNUP_HITS.clear()
    client = TestClient(main.app, base_url="https://hve.edccorp.com")
    client.store, client.sent, client.main = store, sent, main
    return client


FORM = {"name": "Pat Analyst", "email": "Pat@Example.com", "company": "Example Lab",
        "current_hve_user": "1", "website": ""}


def test_a_sign_up_is_saved_confirmed_and_reported(monkeypatch):
    gw = _gateway(monkeypatch, notify="sales@edccorp.com")
    r = gw.post("/signup", data=FORM)
    assert r.status_code == 200 and "Thanks for signing up" in r.text
    entry = gw.store.data["pat@example.com"]               # keyed lowercased
    assert entry["name"] == "Pat Analyst" and entry["company"] == "Example Lab"
    assert entry["current_hve_user"] is True and entry["source"] == "hve.edccorp.com"
    assert entry["unsubscribe_id"] and entry["signed_up"]
    (to1, subj1, body1), (to2, subj2, body2) = gw.sent
    assert to1 == "pat@example.com" and "update list" in subj1
    assert f"/signup/unsubscribe?id={entry['unsubscribe_id']}" in body1
    assert to2 == "sales@edccorp.com" and "Current HVE user: yes" in body2


def test_signing_up_again_updates_the_entry_and_sends_nothing_more(monkeypatch):
    gw = _gateway(monkeypatch)
    gw.post("/signup", data=dict(FORM, current_hve_user=""))
    first = dict(gw.store.data["pat@example.com"])
    gw.post("/signup", data=dict(FORM, email="pat@example.com", company="New Lab"))
    assert list(gw.store.data) == ["pat@example.com"]
    again = gw.store.data["pat@example.com"]
    assert again["company"] == "New Lab" and again["current_hve_user"] is True
    assert again["unsubscribe_id"] == first["unsubscribe_id"]
    assert again["signed_up"] == first["signed_up"]
    assert len(gw.sent) == 1                               # one confirmation only


def test_the_hidden_field_drops_a_bot_with_the_same_answer(monkeypatch):
    gw = _gateway(monkeypatch)
    r = gw.post("/signup", data=dict(FORM, website="http://spam.example"))
    assert r.status_code == 200 and "Thanks for signing up" in r.text
    assert gw.store.data is None and gw.sent == []


def test_a_malformed_address_is_refused_and_nothing_saved(monkeypatch):
    gw = _gateway(monkeypatch)
    r = gw.post("/signup", data=dict(FORM, email="not-an-address"))
    assert r.status_code == 400 and gw.store.data is None


def test_one_address_cannot_be_used_to_flood_a_mailbox(monkeypatch):
    gw = _gateway(monkeypatch)
    for _ in range(6):
        r = gw.post("/signup", data=FORM, headers={"x-forwarded-for": "203.0.113.9"})
        assert "Thanks for signing up" in r.text
    assert gw.store.writes == 3                            # the per-address limit
    assert len(gw.sent) == 1


def test_a_conflicting_write_is_retried(monkeypatch):
    gw = _gateway(monkeypatch)
    gw.store.conflicts = 1
    gw.post("/signup", data=FORM)
    assert "pat@example.com" in gw.store.data


def test_without_the_repository_nothing_is_saved_and_the_page_says_so(monkeypatch):
    gw = _gateway(monkeypatch, repo=False)
    r = gw.post("/signup", data=FORM)
    assert r.status_code == 503 and "not available" in r.text
    assert gw.store.data is None and gw.sent == []


def test_the_unsubscribe_link_takes_the_address_off_and_signing_up_rejoins(monkeypatch):
    gw = _gateway(monkeypatch)
    gw.post("/signup", data=FORM)
    uid = gw.store.data["pat@example.com"]["unsubscribe_id"]
    assert "left the list" in gw.get(f"/signup/unsubscribe?id={uid}").text
    assert gw.store.data["pat@example.com"]["unsubscribed"]
    # A wrong id changes nothing and answers the same.
    writes = gw.store.writes
    assert "left the list" in gw.get("/signup/unsubscribe?id=nope").text
    assert gw.store.writes == writes
    gw.main._SIGNUP_HITS.clear()
    gw.post("/signup", data=FORM)
    assert "unsubscribed" not in gw.store.data["pat@example.com"]
