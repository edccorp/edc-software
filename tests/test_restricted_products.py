"""A wildcard covers the catalogue, not the internal tools.

HIDDEN_PRODUCTS keeps a product off the public site. It does not keep it
out of the index, and it never kept it out of anyone's hands: "*" meant
literally everything, so every customer holding one -- which is the
compact plain-string form, and therefore most of them -- could install an
internal or beta tool the moment it entered the index. Absence from a
catalogue is not access control.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()
BUILD = (ROOT / "tools" / "build_index.py").read_text()


def _entitlement():
    from datetime import date
    ns = {"os": __import__("os"), "FREE_PRODUCTS": [], "date": date}
    for node in ast.parse(GATEWAY).body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if name in {"RESTRICTED_PRODUCTS", "GRANT_ALIASES", "_entitlement_keys", "_entitled",
                    "_active", "_entitlement_state"}:
            exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    return ns


NS = _entitlement()
entitled = NS["_entitled"]
RESTRICTED = NS["RESTRICTED_PRODUCTS"]


def customer(**products):
    return {"products": dict(products)}


# --------------------------------------------- what a wildcard now covers

def test_the_internal_tools_are_restricted_by_default():
    """Named outright, not merely parametrized over. An empty set would
    restrict nothing and every test that loops over it would pass with no
    cases at all -- which is the bug, wearing a green suite."""
    for pid in ("audio_forensics_toolkit",
                "visibility_toolkit", "recon_calculations", "blendmotion"):
        assert pid in RESTRICTED, pid


def test_the_published_products_are_not_restricted():
    for pid in ("cammatch", "hve_toolkit", "point_cloud_toolkit", "recon_toolkit",
                "video_forensics_toolkit"):
        assert pid not in RESTRICTED, pid


def test_a_wildcard_still_covers_the_published_products():
    everything = customer(**{"*": None})
    for pid in ("cammatch", "hve_toolkit", "point_cloud_toolkit", "recon_toolkit"):
        assert entitled(everything, pid), pid


@pytest.mark.parametrize("pid", sorted(RESTRICTED))
def test_a_wildcard_does_not_reach_a_restricted_product(pid):
    assert not entitled(customer(**{"*": None}), pid)


@pytest.mark.parametrize("pid", sorted(RESTRICTED))
def test_naming_it_outright_still_grants_it(pid):
    """Which is how the master account and a named beta tester get one."""
    assert entitled(customer(**{pid: None}), pid)


def test_a_named_grant_beside_a_wildcard_works():
    # The realistic shape of the master account: everything, plus the
    # internal tools spelled out.
    who = customer(**{"*": None, "video_forensics_toolkit": None})
    assert entitled(who, "video_forensics_toolkit")
    assert entitled(who, "cammatch")


def test_a_named_grant_for_one_does_not_open_the_others():
    who = customer(**{"video_forensics_toolkit": None})
    assert not entitled(who, "visibility_toolkit")


def test_a_grant_under_the_old_visibility_id_still_covers_it():
    """Grants were written as "edc_visibility_toolkit" while that was the
    restricted name; the master account should not lose the tool when the
    real id takes its place."""
    who = customer(**{"*": None, "edc_visibility_toolkit": None})
    assert entitled(who, "visibility_toolkit")
    assert not entitled(customer(**{"*": None}), "visibility_toolkit")


def test_the_restricted_ids_are_the_ones_the_releases_ship():
    """An id that matches no manifest restricts and hides nothing."""
    assert "edc_visibility_toolkit" not in RESTRICTED
    assert '"edc_visibility_toolkit"' not in BUILD.split("HIDDEN_PRODUCTS = {", 1)[1].split("}", 1)[0]


def test_an_expired_named_grant_does_not_still_let_them_in():
    who = customer(**{"video_forensics_toolkit": "2020-01-01"})
    assert not entitled(who, "video_forensics_toolkit")


# ------------------------------------------- the two lists agree

def test_every_restricted_product_is_also_off_the_public_site():
    """The two lists answer different questions -- one hides, one refuses --
    but a product that is hidden and freely downloadable is the state this
    fixes, and a product refused while advertised would be the reverse."""
    hidden = BUILD.split("HIDDEN_PRODUCTS = {", 1)[1].split("}", 1)[0]
    for pid in RESTRICTED:
        assert f'"{pid}"' in hidden, f"{pid} is restricted but advertised"


def test_the_restricted_set_can_be_changed_without_a_deploy():
    # A product graduating to public should not need a code change.
    assert 'os.environ.get(\n        "RESTRICTED_PRODUCTS"' in GATEWAY


def test_the_free_products_check_still_comes_first():
    """A free product is included with every secret, restricted or not --
    though nothing is both today."""
    body = GATEWAY.split("def _entitled(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("FREE_PRODUCTS") < body.index("_entitlement_keys")


def test_the_state_report_uses_the_same_rule():
    """Otherwise a restricted product would be refused by _entitled and
    reported as 'expired' rather than 'none', which reads as a lapsed
    purchase and sends the customer to renew something they never had."""
    body = GATEWAY.split("def _entitlement_state(", 1)[1].split("\ndef ", 1)[0]
    assert "_entitlement_keys(product_id)" in body


# ------------------------------------- what Blender is shown, not just served

def index_route():
    return GATEWAY.split("async def index_json(", 1)[1].split("\n@app", 1)[0]


def test_a_wildcard_no_longer_skips_the_index_filter():
    """It used to return the whole index unfiltered to any "*" customer.
    That was true while "*" meant everything; now a restricted product is
    not covered by one, so they would have been shown every internal tool
    in Get Extensions and then refused at the download with a 403 -- an
    offer that cannot be taken up."""
    body = index_route()
    assert "not RESTRICTED_PRODUCTS" in body
    assert 'if not ("*" in products and _active(products["*"])):' not in body


def test_the_listing_and_the_download_agree():
    """Both go through _entitled, so anything Blender lists can be
    installed and anything refused was never offered."""
    assert "_entitled(customer, e.get(\"id\", \"\"))" in index_route()
    package = GATEWAY.split("async def package(", 1)[1].split("\n@app", 1)[0]
    assert "_entitlement_state(customer, product_id)" in package


def test_the_shortcut_still_applies_when_nothing_is_restricted():
    # With RESTRICTED_PRODUCTS empty a wildcard does cover everything, and
    # filtering an index down to itself is wasted work on every fetch.
    body = index_route()
    assert "covers_everything" in body
    assert '"*" in products' in body


def test_a_download_refusal_names_the_product():
    package = GATEWAY.split("async def package(", 1)[1].split("\n@app", 1)[0]
    assert "does not include access to {product_id}" in package
