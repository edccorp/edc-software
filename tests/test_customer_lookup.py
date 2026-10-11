"""Answering a customer who has lost their repository secret.

The gateway refuses to re-display a secret to anyone who only proved they
can type an email address -- deliberately, and it says so on the page --
and points them at Engineering Dynamics Company instead. Nothing on this
side answered that: `list` printed every customer's secret at once, which
is not what you want on screen while writing a support reply, and there was
no way to give somebody a new secret without losing the rest of their
record.
"""

import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "tools" / "customer.py"
SOURCE = SOURCE_PATH.read_text()


@pytest.fixture
def cli(monkeypatch):
    """The CLI with its GitHub calls replaced by a customers dict in memory."""
    monkeypatch.setenv("CUSTOMERS_ADMIN_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location("customer_cli", SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state = {"customers": {}, "saved": [], "sha": "sha-1"}
    monkeypatch.setattr(module, "fetch",
                        lambda: (state["customers"], state["sha"]))

    def save(customers, sha, message):
        state["customers"] = customers
        state["saved"].append(message)
    monkeypatch.setattr(module, "save", save)
    module.state = state
    return module


def customer(name, email=None, products=None, **extra):
    entry = {"name": name}
    if email:
        entry["email"] = email
    if products:
        entry["products"] = products
    entry.update(extra)
    return entry


# ------------------------------------------------------- finding a customer

def test_a_customer_is_found_by_the_email_they_wrote_in_from(cli):
    # Which is how a support request actually arrives: from the address
    # they bought with, not from a name they may spell differently.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "buyer@acme.com") == ["edc_aaa"]


def test_an_email_is_matched_whatever_case_it_arrives_in(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "Buyer@Acme.com")}
    assert cli.find(cli.state["customers"], "buyer@acme.COM") == ["edc_aaa"]


def test_the_secret_itself_still_wins(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "edc_aaa") == ["edc_aaa"]


def test_a_name_still_matches(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "acme llc") == ["edc_aaa"]


def test_a_plain_string_entry_has_no_email_to_match(cli):
    # The compact form is just a name; asking it for an email must not raise.
    cli.state["customers"] = {"edc_aaa": "Acme LLC"}
    assert cli.email_of("Acme LLC") == ""
    assert cli.find(cli.state["customers"], "buyer@acme.com") == []


def test_an_unknown_customer_says_what_it_looked_for(cli, capsys):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    with pytest.raises(SystemExit):
        cli.resolve_one(cli.state["customers"], "nobody@example.com")
    assert "name or email" in capsys.readouterr().err


# ------------------------------------------------------------ showing one

def test_show_prints_that_customer_and_not_the_others(cli, capsys):
    """`list` puts every customer's secret on screen, which is not what you
    want while writing a reply to one of them."""
    # The one asked for is deliberately not the first in the file, so a
    # lookup that ignored its argument could not pass by luck.
    cli.state["customers"] = {
        "edc_bbb": customer("Other Co", "someone@other.com"),
        "edc_aaa": customer("Acme LLC", "buyer@acme.com"),
    }
    cli.cmd_show(types.SimpleNamespace(customer="buyer@acme.com"))
    out = capsys.readouterr().out
    assert "edc_aaa" in out
    assert "edc_bbb" not in out
    assert "Other Co" not in out


def test_show_carries_the_setup_the_customer_needs(cli, capsys):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    out = capsys.readouterr().out
    assert "extensions.edccorp.com/index.json" in out
    assert "Requires Access Token" in out


def test_show_changes_nothing(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["saved"] == []
    assert list(cli.state["customers"]) == ["edc_aaa"]


def test_show_names_the_products_so_the_reply_can_be_checked(cli, capsys):
    cli.state["customers"] = {
        "edc_aaa": customer("Acme LLC", products=["recon_toolkit"])}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    assert "recon_toolkit" in capsys.readouterr().out


# --------------------------------------------------------- reissuing one

def test_reissue_replaces_the_secret(cli, capsys):
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    tokens = list(cli.state["customers"])
    assert len(tokens) == 1
    assert tokens[0] != "edc_old"
    assert tokens[0].startswith("edc_")
    assert tokens[0] in capsys.readouterr().out


def test_reissue_keeps_everything_else_about_them(cli):
    """Rebuilding the entry from a name and products would quietly drop the
    email address, the purchase history and a dated term."""
    entry = customer("Acme LLC", "buyer@acme.com",
                     products={"recon_toolkit": "2027-07-03"},
                     stripe_sessions=["cs_test_123"])
    cli.state["customers"] = {"edc_old": entry}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    kept = list(cli.state["customers"].values())[0]
    assert kept == entry


def test_reissue_retires_the_old_secret(cli):
    # The whole point when a secret has been seen by somebody else: sending
    # the same one back cannot help, because the problem is who else has it.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert "edc_old" not in cli.state["customers"]


def test_reissue_is_committed_with_a_readable_message(cli):
    # The repo history is the audit trail, so the commit has to say what
    # happened and to whom.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["saved"] == ["Reissue repository secret: Acme LLC"]


def test_reissue_says_the_old_one_stops_working(cli, capsys):
    # And what the customer will see in the meantime, because they will
    # write in about it otherwise.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    out = capsys.readouterr().out
    assert "old secret stops working" in out
    assert "access error" in out


def test_reissue_leaves_other_customers_alone(cli):
    cli.state["customers"] = {
        "edc_old": customer("Acme LLC"),
        "edc_bbb": customer("Other Co"),
    }
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["customers"]["edc_bbb"] == customer("Other Co")


def test_a_reissued_secret_is_not_the_one_it_replaced(cli):
    # Generated the same way `add` generates one, rather than derived from
    # anything about the customer.
    body = SOURCE.split("def cmd_reissue(", 1)[1].split("\ndef ", 1)[0]
    assert 'secrets.token_urlsafe(18)' in body


# ------------------------------------------------------------ the commands

@pytest.mark.parametrize("command", ["show", "reissue"])
def test_the_commands_are_reachable(command):
    assert f'sub.add_parser("{command}"' in SOURCE


@pytest.mark.parametrize("command", ["show", "reissue", "revoke", "set-products"])
def test_every_lookup_accepts_an_email(command):
    """All four take a customer, and all four are reached from a support
    request that arrives as an email address."""
    block = SOURCE.split(f'sub.add_parser("{command}"', 1)[1].split("p.set_defaults", 1)[0]
    assert "email" in block, f"{command} does not say it accepts an email"


# ----------------------------------------------------- recording an address

def added(name, email=None, products=None, expires=None):
    return types.SimpleNamespace(name=name, email=email, products=products,
                                 expires=expires)


def test_add_records_the_address_it_was_given(cli):
    # Which is the whole point: /recover finds a customer by the address
    # they bought with, and finds nothing for one that was never stored.
    cli.cmd_add(added("Acme LLC", "buyer@acme.com"))
    entry = list(cli.state["customers"].values())[0]
    assert entry["email"] == "buyer@acme.com"
    assert entry["name"] == "Acme LLC"


def test_add_keeps_the_address_alongside_a_dated_term(cli):
    # The two are stored in the same record and neither may displace the
    # other -- a subscription customer is exactly who writes in later.
    cli.cmd_add(added("Acme LLC", "buyer@acme.com",
                      products="recon_toolkit", expires="2027-07-03"))
    entry = list(cli.state["customers"].values())[0]
    assert entry["email"] == "buyer@acme.com"
    assert entry["products"] == {"recon_toolkit": "2027-07-03"}


def test_add_without_an_address_stays_the_compact_form(cli):
    # A name and nothing else is still a plain string; the dict form is for
    # entries that have something to carry.
    cli.cmd_add(added("Acme LLC"))
    assert list(cli.state["customers"].values()) == ["Acme LLC"]


def test_add_warns_when_no_address_was_given(cli, capsys):
    """Otherwise the omission is silent, and surfaces months later as a
    support request that could have answered itself."""
    cli.cmd_add(added("Acme LLC"))
    out = capsys.readouterr().out
    assert "No email recorded" in out
    assert "set-email" in out


def test_add_does_not_warn_when_it_has_one(cli, capsys):
    cli.cmd_add(added("Acme LLC", "buyer@acme.com"))
    assert "No email recorded" not in capsys.readouterr().out


def test_a_blank_address_counts_as_none(cli, capsys):
    cli.cmd_add(added("Acme LLC", "   "))
    assert list(cli.state["customers"].values()) == ["Acme LLC"]
    assert "No email recorded" in capsys.readouterr().out


def test_surrounding_space_is_trimmed_off_an_address(cli):
    cli.cmd_add(added("Acme LLC", "  buyer@acme.com  "))
    entry = list(cli.state["customers"].values())[0]
    assert entry["email"] == "buyer@acme.com"


def test_set_email_fills_in_a_customer_who_had_none(cli):
    # Every entry made before the gateway could send is in this state.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    assert cli.state["customers"]["edc_aaa"]["email"] == "buyer@acme.com"


def test_set_email_turns_a_plain_string_entry_into_a_record(cli):
    # The compact form has nowhere to put an address, so it has to grow one
    # -- without losing the name it was.
    cli.state["customers"] = {"edc_aaa": "Acme LLC"}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    assert cli.state["customers"]["edc_aaa"] == {"name": "Acme LLC",
                                                 "email": "buyer@acme.com"}


def test_set_email_keeps_the_rest_of_the_record(cli):
    entry = customer("Acme LLC", products={"recon_toolkit": "2027-07-03"},
                     stripe_sessions=["cs_test_123"])
    cli.state["customers"] = {"edc_aaa": entry}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    stored = cli.state["customers"]["edc_aaa"]
    assert stored["products"] == {"recon_toolkit": "2027-07-03"}
    assert stored["stripe_sessions"] == ["cs_test_123"]


def test_set_email_replaces_one_that_has_changed(cli, capsys):
    # A customer who has moved address is the other reason to run this, and
    # the reply has to say which address the secret will now go to.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "old@acme.com")}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="new@acme.com"))
    assert cli.state["customers"]["edc_aaa"]["email"] == "new@acme.com"
    out = capsys.readouterr().out
    assert "old@acme.com" in out
    assert "new@acme.com" in out


def test_set_email_trims_an_address_pasted_with_space_around_it(cli):
    # Which is how it arrives: copied out of the email they wrote in with.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email=" buyer@acme.com\n"))
    assert cli.state["customers"]["edc_aaa"]["email"] == "buyer@acme.com"


def test_set_email_refuses_something_that_is_not_an_address(cli):
    # A typo stored here is worse than no address at all: /recover then has
    # somewhere to send that nobody reads.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    with pytest.raises(SystemExit):
        cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                                email="buyer.acme.com"))
    assert cli.state["saved"] == []
    assert "email" not in cli.state["customers"]["edc_aaa"]


def test_set_email_is_committed_with_a_readable_message(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    assert cli.state["saved"] == ["Set email for Acme LLC: buyer@acme.com"]


def test_set_email_leaves_other_customers_alone(cli):
    cli.state["customers"] = {
        "edc_bbb": customer("Other Co", "someone@other.com"),
        "edc_aaa": customer("Acme LLC"),
    }
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    assert cli.state["customers"]["edc_bbb"]["email"] == "someone@other.com"


def test_a_customer_can_be_found_again_by_the_address_just_set(cli):
    # The address is only worth storing if it is the thing /recover and
    # `show` then match on.
    cli.state["customers"] = {"edc_aaa": "Acme LLC"}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC",
                                            email="buyer@acme.com"))
    assert cli.find(cli.state["customers"], "buyer@acme.com") == ["edc_aaa"]


def test_set_email_is_reachable_and_takes_both_arguments(cli):
    block = SOURCE.split('sub.add_parser("set-email"', 1)[1].split("p.set_defaults", 1)[0]
    assert 'p.add_argument("customer"' in block
    assert 'p.add_argument("email"' in block


def test_add_offers_the_address_on_the_command_line(cli):
    block = SOURCE.split('sub.add_parser("add"', 1)[1].split("p.set_defaults", 1)[0]
    assert 'p.add_argument("--email"' in block


# ------------------------------------------------------- the backfill list

def test_needs_email_lists_the_customers_who_have_none(cli, capsys):
    # They are the ones /recover cannot help, and there is no other way to
    # see who they are without reading the whole file.
    cli.state["customers"] = {
        "edc_aaa": customer("Acme LLC", "buyer@acme.com"),
        "edc_bbb": customer("Other Co"),
        "edc_ccc": "Third Party Ltd",
    }
    cli.cmd_needs_email(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "Other Co" in out
    assert "Third Party Ltd" in out
    assert "Acme LLC" not in out


def test_needs_email_counts_them_against_the_whole_list(cli, capsys):
    cli.state["customers"] = {
        "edc_aaa": customer("Acme LLC", "buyer@acme.com"),
        "edc_bbb": customer("Other Co"),
    }
    cli.cmd_needs_email(types.SimpleNamespace())
    assert "1 of 2" in capsys.readouterr().out


def test_needs_email_puts_no_secrets_on_screen(cli, capsys):
    """Working through the backfill does not need a single one, and `list`
    putting them all there is what `show` exists to avoid."""
    cli.state["customers"] = {"edc_bbb": customer("Other Co")}
    cli.cmd_needs_email(types.SimpleNamespace())
    assert "edc_bbb" not in capsys.readouterr().out


def test_needs_email_says_so_when_there_is_nothing_to_do(cli, capsys):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    cli.cmd_needs_email(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "All 1 customers" in out
    assert "Acme LLC" not in out


def test_needs_email_on_an_empty_list_does_not_claim_success(cli, capsys):
    cli.cmd_needs_email(types.SimpleNamespace())
    assert "no customers yet" in capsys.readouterr().out


def test_needs_email_changes_nothing(cli):
    cli.state["customers"] = {"edc_bbb": customer("Other Co")}
    cli.cmd_needs_email(types.SimpleNamespace())
    assert cli.state["saved"] == []


def test_needs_email_names_the_command_that_fixes_it(cli, capsys):
    cli.state["customers"] = {"edc_bbb": customer("Other Co")}
    cli.cmd_needs_email(types.SimpleNamespace())
    assert "set-email" in capsys.readouterr().out


def test_an_empty_address_counts_as_missing(cli, capsys):
    # A record can carry the key with nothing in it; /recover cannot send
    # there either.
    cli.state["customers"] = {"edc_bbb": {"name": "Other Co", "email": ""}}
    cli.cmd_needs_email(types.SimpleNamespace())
    assert "Other Co" in capsys.readouterr().out


def test_needs_email_is_reachable(cli):
    assert 'sub.add_parser("needs-email"' in SOURCE


# ------------------------------------- granting the internal tools by name

def test_a_restricted_product_can_be_granted():
    """A "*" deliberately does not reach them, so there has to be a way to
    name one -- otherwise the master account cannot be given the tools it
    is the only account meant to have."""
    cli_src = SOURCE
    assert "RESTRICTED_IDS" in cli_src
    for pid in ("video_forensics_toolkit", "audio_forensics_toolkit",
                "visibility_toolkit", "recon_calculations", "blendmotion"):
        assert pid in cli_src, pid


def test_a_wildcard_can_be_combined_with_named_products(cli):
    # The master account's shape: everything published, plus the internal
    # tools a wildcard does not cover.
    assert cli.parse_products("*,video_forensics_toolkit") == [
        "*", "video_forensics_toolkit"]


def test_a_wildcard_on_its_own_is_still_the_compact_form(cli):
    assert cli.parse_products("*") is None
    assert cli.parse_products("") is None
    assert cli.parse_products(None) is None


def test_a_published_product_is_still_accepted(cli):
    assert cli.parse_products("recon_toolkit") == ["recon_toolkit"]


def test_an_unknown_product_is_still_refused(cli, capsys):
    with pytest.raises(SystemExit):
        cli.parse_products("recon_toolkit,not_a_product")
    err = capsys.readouterr().err
    assert "not_a_product" in err
    # And the message lists what it would have accepted, internal ones too.
    assert "video_forensics_toolkit" in err


def test_the_two_restricted_lists_agree():
    """The CLI's list and the gateway's are separate files; a product in one
    and not the other is either ungrantable or silently public."""
    gateway = (ROOT / "gateway" / "main.py").read_text()
    restricted = SOURCE.split("RESTRICTED_IDS = (", 1)[1].split(")", 1)[0]
    for pid in ("video_forensics_toolkit", "audio_forensics_toolkit",
                "visibility_toolkit", "recon_calculations", "blendmotion"):
        assert pid in restricted, f"{pid} cannot be granted"
        assert pid in gateway, f"{pid} is not restricted by the gateway"


# ------------------------------------- HVE, and the master account's "all"

def test_hve_is_grantable_by_name(cli):
    assert cli.parse_products("hve,simon,edsmac4") == ["hve", "simon", "edsmac4"]
    assert cli.parse_products("*,hve") == ["*", "hve"]


def test_all_is_everything_and_star_is_not(cli):
    everything = cli.parse_products("all")
    assert "*" in everything
    for pid in (*cli.RESTRICTED_IDS, *cli.HVE_IDS):
        assert pid in everything, pid
    # "*" alone stays the published four: the ordinary customer's shape,
    # which must not quietly include HVE or the internal tools.
    assert cli.parse_products("*") is None


def test_all_reaches_hve_through_the_gateway_rules(cli):
    """The CLI's "all" is only everything if the gateway reads it so."""
    import sys
    sys.path.insert(0, str(ROOT / "gateway"))
    import licensing
    products = {p: None for p in cli.parse_products("all")}
    features, until = licensing.hve_entitlement(products)
    assert features == list(licensing.HVE_FEATURES) and until is None


def test_the_cli_and_gateway_hve_lists_agree(cli):
    import sys
    sys.path.insert(0, str(ROOT / "gateway"))
    import licensing
    assert tuple(cli.HVE_IDS) == tuple(licensing.HVE_FEATURES)
