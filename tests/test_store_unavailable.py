"""A buyer never sees a raw Stripe error.

With the Stripe key rolled and not yet updated on the gateway, /store
showed {"detail": "Stripe returned 401 for price_..."} to someone about to
pay. Every Stripe failure on the way to checkout now shows one plain page
and logs the real reason.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()


def _function(name):
    tree = ast.parse(GATEWAY)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(GATEWAY, node)
    raise AssertionError(f"{name} not found")


def test_the_page_is_plain_and_says_nothing_was_charged():
    body = _function("_store_unavailable")
    assert "Store temporarily unavailable" in body
    assert "Nothing has been charged" in body
    assert "contact Engineering Dynamics Company" in body
    assert 'href="/store"' in body


def test_the_reason_is_logged_not_shown():
    body = _function("_store_unavailable")
    assert 'print(f"[gateway] ERROR: store unavailable: {reason}")' in body
    page = body.split('page = """', 1)[1].split('"""', 1)[0]
    assert "{reason}" not in page and "Stripe returned" not in page


def test_it_is_a_503_that_asks_to_be_retried():
    body = _function("_store_unavailable")
    assert "status_code=503" in body and '"Retry-After": "300"' in body


def test_the_store_catches_a_failed_price_lookup():
    body = _function("store")
    assert "except HTTPException as exc:\n            return _store_unavailable(" in body
    assert "except httpx.HTTPError as exc:" in body
    assert "raise HTTPException" not in body


def test_checkout_catches_a_failed_session():
    body = _function("checkout")
    assert "raise HTTPException" not in body
    assert body.count("return _store_unavailable(") == 3
