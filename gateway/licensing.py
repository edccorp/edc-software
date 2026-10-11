"""HVE licenses: which computers a customer has activated, and the signed
document each one gets.

Kept apart from main.py, with no web framework in it, so every rule here
can be tested as a plain function. main.py only reads the customer file,
calls these, and writes it back.

The customer entry gains three keys, all optional:

    "license_type": "seat" (the default) or "site"
    "seats":        a number (default 1), or "unlimited"; a site license
                    is always unlimited whatever this says
    "machines":     [{"id": "A-...", "fp": [h1, h2, h3], "hostname": "...",
                      "app_version": "...", "first": "...Z", "last": "...Z"}]

A computer's fingerprint is three hashes the application computes (system
UUID, OS installation id, system disk serial), each already salted on the
computer, so no raw hardware identifier ever reaches this server. Two of
three matching is the same computer: a disk swap or an OS reinstall keeps
the seat, a new computer does not.

HVE features are product ids in the same "products" map as the Blender
add-ons, so one store and one checkout sell both. A "*" grant never
reaches them: "*" is how most Blender customers are written, and it must
not quietly hand every one of them HVE.
"""

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: Licensable HVE features, in the order a license lists them. "hve" is the
#: product itself and is required; the rest are engines and add-on modules
#: (hve-next docs/LICENSING.md, "Features").
HVE_FEATURES = (
    "hve", "edcrash", "edsmac", "edsmac4", "simon", "dymesh", "edsvs",
    "edvts", "edvsm", "edvds", "edhis", "edgen", "gatb", "singletrack",
    "damagestudio", "brakedesigner", "edcdb",
)

#: Days after issue when the application starts asking for a fresh
#: license, and when an unrefreshed one stops covering a run.
REFRESH_DAYS = 30
OFFLINE_DAYS = 60

DOCUMENT_VERSION = 1

_HASH = re.compile(r"^[0-9a-f]{16,128}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LicenseError(Exception):
    """A refusal the application shows its user. `code` is what the
    application branches on; the message is for the person."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


# ------------------------------------------------------------ signing key

def load_signing_key(pem: str) -> Ed25519PrivateKey:
    """The Ed25519 private key from LICENSE_SIGNING_KEY.

    Accepts the PEM as pasted, including with its line breaks flattened to
    literal "\\n" -- the usual result of pasting a multi-line value into a
    one-line field.
    """
    text = pem.strip().replace("\\n", "\n")
    key = serialization.load_pem_private_key(text.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("LICENSE_SIGNING_KEY is not an Ed25519 key")
    return key


def public_key_fingerprint(key: Ed25519PrivateKey) -> str:
    """First 16 hex of SHA-256 over the public key's DER: the same figure
    hve-next's licensing/keys/README.md lists, so the deployed key can be
    checked against the one HVE trusts without touching the private half."""
    der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:16]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def encode_license(claims: dict, key: Ed25519PrivateKey) -> str:
    """base64url(json) "." base64url(Ed25519 signature over those json bytes)."""
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64url(payload)}.{_b64url(key.sign(payload))}"


# ------------------------------------------------------------ entitlements

def license_id(token: str, entry: dict) -> str:
    """A stable name for the license that is not the secret.

    An explicit "license_id" on the entry wins (an imported classic
    customer keeps their number); otherwise one is derived from the secret,
    which cannot be turned back into it.
    """
    explicit = entry.get("license_id") if isinstance(entry, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return "L-" + hashlib.sha256(token.encode()).hexdigest()[:12]


def hve_entitlement(products: dict) -> tuple[list[str], str | None]:
    """(features, maintenance_until) from a normalized products map.

    Features are every HVE id the customer was ever granted, lapsed or
    not: a lapsed date ends maintenance -- newer builds -- not the right
    to run the builds already covered. maintenance_until is the "hve"
    grant's date; None means perpetual.

    Raises when "hve" itself is absent: an engine without the product is
    nothing the application can run.
    """
    if "hve" not in products:
        raise LicenseError(
            403, "no_hve",
            "This EDC Software secret does not include HVE. Contact "
            "Engineering Dynamics Company to add it.")
    features = [f for f in HVE_FEATURES if f in products]
    expiry = products["hve"]
    return features, (str(expiry) if expiry is not None else None)


def license_type(entry: dict) -> str:
    return "site" if entry.get("license_type") == "site" else "seat"


def seat_limit(entry: dict) -> int | None:
    """How many computers may be active at once; None = unlimited.

    A value that is not a positive whole number reads as one seat rather
    than as unlimited: this file is edited by hand, and a typo should cost
    a customer a phone call, not hand them every seat there is.
    """
    if license_type(entry) == "site":
        return None
    seats = entry.get("seats", 1)
    if seats == "unlimited":
        return None
    if isinstance(seats, bool) or not isinstance(seats, int) or seats < 1:
        return 1
    return seats


# ------------------------------------------------------------ machines

def parse_fingerprint(value) -> list[str]:
    """Three lowercase hex hashes; "" for a component the computer lacks.

    At least two must be present, or two-of-three matching could not tell
    one computer from another.
    """
    if not isinstance(value, list) or len(value) != 3:
        raise LicenseError(422, "bad_fingerprint",
                           "The computer's fingerprint must have three parts.")
    parts = []
    for part in value:
        if not isinstance(part, str):
            raise LicenseError(422, "bad_fingerprint",
                               "Each fingerprint part must be text.")
        part = part.strip().lower()
        if part and not _HASH.match(part):
            raise LicenseError(422, "bad_fingerprint",
                               "Each fingerprint part must be a hex hash.")
        parts.append(part)
    if sum(1 for p in parts if p) < 2:
        raise LicenseError(422, "bad_fingerprint",
                           "This computer reported too little to identify it.")
    return parts


def same_computer(a: list, b: list) -> bool:
    """Two of three present components equal."""
    if not (isinstance(a, list) and isinstance(b, list)):
        return False
    return sum(1 for x, y in zip(a, b) if x and x == y) >= 2


def clean_text(value, limit: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:limit]


def machines(entry: dict) -> list:
    """The entry's activations, skipping anything malformed."""
    found = entry.get("machines")
    if not isinstance(found, list):
        return []
    return [m for m in found
            if isinstance(m, dict) and isinstance(m.get("id"), str)
            and isinstance(m.get("fp"), list)]


def public_machines(entry: dict) -> list:
    """What a customer is shown about their computers: never a fingerprint."""
    return [{"activation": m["id"], "hostname": m.get("hostname", ""),
             "first": m.get("first", ""), "last": m.get("last", ""),
             "app_version": m.get("app_version", "")}
            for m in machines(entry)]


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def activate(entry: dict, fp: list, hostname: str, app_version: str,
             now: datetime) -> tuple[dict, bool]:
    """Find or add this computer on the entry. Returns (machine, changed).

    The same computer activating again keeps its activation, with its
    fingerprint brought up to date -- so a disk swap that still matched on
    two parts now matches on three, and the next change after it does not
    lose the seat. A new computer takes a seat if one is free and is
    refused, naming the computers that hold them, if not.

    `entry` is changed in place only when `changed` is True.
    """
    current = machines(entry)
    for machine in current:
        if same_computer(machine["fp"], fp):
            update = {"fp": fp, "hostname": hostname or machine.get("hostname", ""),
                      "app_version": app_version or machine.get("app_version", "")}
            if all(machine.get(k) == v for k, v in update.items()):
                return machine, False
            machine.update(update)
            machine["last"] = _stamp(now)
            entry["machines"] = current
            return machine, True

    limit = seat_limit(entry)
    if limit is not None and len(current) >= limit:
        raise LicenseError(
            409, "seats_full",
            f"All {limit} seat(s) on this license are in use. Release one of "
            "these computers (in HVE: Help > License > Release this computer, "
            "or on the license page), then try again.",
            machines=public_machines(entry))

    machine = {"id": "A-" + secrets.token_hex(6), "fp": fp,
               "hostname": hostname, "app_version": app_version,
               "first": _stamp(now), "last": _stamp(now)}
    entry["machines"] = [*current, machine]
    return machine, True


def find_activation(entry: dict, activation: str) -> dict:
    for machine in machines(entry):
        if machine["id"] == activation:
            return machine
    raise LicenseError(
        410, "released",
        "This computer's activation has been released. Activate it again "
        "to use HVE here.")


def deactivate(entry: dict, activation: str) -> bool:
    """Remove an activation. False if it was not there (already released)."""
    current = machines(entry)
    kept = [m for m in current if m["id"] != activation]
    if len(kept) == len(current):
        return False
    entry["machines"] = kept
    return True


def claims(token: str, entry: dict, products: dict, machine: dict,
           kid: str, now: datetime) -> dict:
    """The license document's claims for one activated computer."""
    features, maintenance_until = hve_entitlement(products)
    return {
        "v": DOCUMENT_VERSION,
        "kid": kid,
        "license": license_id(token, entry),
        "license_type": license_type(entry),
        "activation": machine["id"],
        "licensee": clean_text(entry.get("name", ""), 200) or "unnamed",
        "fingerprint": list(machine["fp"]),
        "features": features,
        "maintenance_until": maintenance_until,
        "issued": _stamp(now),
        "refresh_by": _stamp(now + timedelta(days=REFRESH_DAYS)),
        "offline_until": _stamp(now + timedelta(days=OFFLINE_DAYS)),
    }
