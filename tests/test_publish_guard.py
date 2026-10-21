"""The publish refuses an index that would drop a product.

A product whose release cannot be read is skipped, and the index was
published regardless. A revoked token skips every product, so the next
hourly run would have replaced the live index with an empty one and every
customer's Blender would have listed nothing.
"""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "build_index", ROOT / "tools" / "build_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entries(*ids):
    return [{"id": i} for i in ids]


def test_an_empty_build_is_refused_even_with_nothing_to_compare():
    problem = _module().publish_problem([], None)
    assert problem and "PRODUCTS_TOKEN" in problem


def test_an_empty_build_is_refused_even_with_allow_drop():
    assert _module().publish_problem([], {"cammatch"}, allow_drop=True)


def test_a_build_that_drops_a_product_is_refused_and_names_it():
    problem = _module().publish_problem(
        _entries("cammatch"), {"cammatch", "hve_toolkit", "recon_toolkit"})
    assert problem
    assert "hve_toolkit" in problem and "recon_toolkit" in problem
    assert "Allow Drop" in problem


def test_allow_drop_publishes_an_intended_removal():
    assert _module().publish_problem(
        _entries("cammatch"), {"cammatch", "hve_toolkit"}, allow_drop=True) is None


def test_a_build_with_every_product_publishes():
    assert _module().publish_problem(
        _entries("cammatch", "hve_toolkit"), {"cammatch", "hve_toolkit"}) is None


def test_a_new_product_is_not_a_problem():
    assert _module().publish_problem(
        _entries("cammatch", "new_toolkit"), {"cammatch"}) is None


def test_an_unreadable_live_index_leaves_only_the_empty_check():
    assert _module().publish_problem(_entries("cammatch"), None) is None


def test_the_check_runs_before_anything_is_written():
    source = (ROOT / "tools" / "build_index.py").read_text()
    main = source.split("def main():", 1)[1]
    assert main.index("problem = publish_problem(") < main.index('open(os.path.join(out_dir, "index.json")')
    assert "sys.exit(1)" in main.split('open(os.path.join(out_dir, "index.json")', 1)[0]


def test_the_live_index_is_read_without_the_products_token():
    source = (ROOT / "tools" / "build_index.py").read_text()
    body = source.split("def _previous_ids():", 1)[1].split("\ndef ", 1)[0]
    assert "urllib.request.urlopen(PREVIOUS_INDEX_URL" in body
    assert "_fetch(" not in body and "Authorization" not in body
    assert "edccorp.github.io/edc-software/index.json" in source


def test_the_workflow_offers_allow_drop_on_a_manual_run_only():
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "allow_drop:" in workflow and "default: false" in workflow
    assert "ALLOW_DROP: ${{ inputs.allow_drop && 'true' || 'false' }}" in workflow
