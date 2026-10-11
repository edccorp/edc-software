"""EDC Software extensions gateway.

A small authentication gateway for the Blender extensions repository at
extensions.edccorp.com, designed to run on Railway (or any container
host). Blender sends the per-customer repository secret from the repository's
"Secret" field as ``Authorization: Bearer <repository-secret>`` on every index fetch
and download; this app validates it and serves:

    GET /                  landing page   (public, proxied from Pages)
    GET /index.json        extension index (repository secret required)
    GET /packages/<file>   add-on zips     (repository secret required, streamed from
                                            the private GitHub release
                                            asset via GH_TOKEN)
    GET /healthz           liveness probe  (public)

Environment variables:
    CUSTOMER_TOKENS  JSON object mapping repository secret -> customer. A plain string
                     label includes every product with that repository access:
                       {"edc_a1b2c3...": "Acme Reconstruction LLC"}
                     An object form scopes it to specific products (ids from
                     the extension manifests: cammatch, hve_toolkit,
                     point_cloud_toolkit, recon_toolkit; "*" = all):
                       {"edc_a1b2c3...": {"name": "Acme Reconstruction LLC",
                                          "products": ["recon_toolkit"]}}
    GH_TOKEN         fine-grained GitHub PAT with Contents:read on the
                     product repos (used to stream private release assets)
    CUSTOMERS_REPO   optional "owner/repo" of a private repo holding the
                     customer list as a JSON file. When set, the file is
                     fetched with GH_TOKEN and re-read within CUSTOMERS_TTL
                     seconds of any change — add/revoke customers by
                     editing the file, no redeploy. Entries there override
                     CUSTOMER_TOKENS, which remains as a bootstrap/fallback.
    CUSTOMERS_PATH   path of the file in CUSTOMERS_REPO (default customers.json)
    CUSTOMERS_TTL    customer-list cache seconds (default 60)
    ADMIN_API_TOKEN  enables POST /admin/customers (used by the purchase
                     automation, e.g. Power Automate) when set; callers
                     must send it as a Bearer repository secret
    ADMIN_GH_TOKEN   fine-grained PAT with Contents:read&write on
                     CUSTOMERS_REPO, used by /admin/customers and the
                     Stripe purchase flow to commit new customers (keep
                     GH_TOKEN itself read-only)
    STRIPE_SECRET_KEY    Stripe API key (a restricted key with read on
                     Checkout Sessions + Products is enough) — enables
                     GET /welcome, the post-payment page that provisions
                     the buyer's repository secret and shows it on screen
    STRIPE_WEBHOOK_SECRET  signing secret (whsec_...) of a Stripe webhook
                     pointed at POST /webhook/stripe for
                     checkout.session.completed — a backstop that
                     provisions the purchase even if the buyer never
                     reaches /welcome
    ORIGIN_BASE      Pages origin serving index.json/packages.json
                     (default https://edccorp.github.io/edc-software)
    CACHE_TTL        origin cache seconds (default 300)
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import mail

ORIGIN_BASE = os.environ.get(
    "ORIGIN_BASE", "https://edccorp.github.io/edc-software"
).rstrip("/")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))
CUSTOMERS_REPO = os.environ.get("CUSTOMERS_REPO", "")
CUSTOMERS_PATH = os.environ.get("CUSTOMERS_PATH", "customers.json")
CUSTOMERS_TTL = int(os.environ.get("CUSTOMERS_TTL", "60"))
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
ADMIN_GH_TOKEN = os.environ.get("ADMIN_GH_TOKEN", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Default repository access term (days of update service) for purchases whose Payment
# Link has no term_days metadata; empty = perpetual.
LICENSE_TERM_DAYS = os.environ.get("LICENSE_TERM_DAYS", "")
# Product id -> Stripe Price id, e.g. {"cammatch": "price_1ABC..."}; enables
# the /store page where customers pick any combination in one checkout.
try:
    STRIPE_PRICES = json.loads(os.environ.get("STRIPE_PRICES", "{}"))
    PRICES_ERROR = None
except json.JSONDecodeError as exc:
    STRIPE_PRICES = {}
    PRICES_ERROR = f"STRIPE_PRICES is not valid JSON: {exc}"
    print(f"[gateway] ERROR: {PRICES_ERROR}")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://extensions.edccorp.com").rstrip("/")
ACCESS_URL = f"{PUBLIC_BASE}/access-and-licensing.html"
# Products currently offered free: included with every token, and obtainable
# without payment via /register (name + email -> instant token).
FREE_PRODUCTS = [
    p.strip() for p in os.environ.get("FREE_PRODUCTS", "").split(",") if p.strip()
]
# Products a wildcard does NOT reach: internal and beta tools, which are
# kept off the public catalogue by HIDDEN_PRODUCTS in tools/build_index.py
# and have to be named on a customer outright to be downloadable.
#
# "*" meant literally everything, so every customer holding one -- which is
# the compact plain-string form, and therefore most of them -- could fetch
# an unreleased internal tool the moment it entered the index. Being absent
# from the catalogue is not access control: the index still lists it and
# Blender will install anything the index offers and the token allows.
#
# Naming one explicitly still works, which is how the master account and a
# named beta tester get it.
RESTRICTED_PRODUCTS = frozenset(
    p.strip() for p in os.environ.get(
        "RESTRICTED_PRODUCTS",
        "video_forensics_toolkit,audio_forensics_toolkit,visibility_toolkit,"
        "recon_calculations,blendmotion",
    ).split(",") if p.strip()
)

# Update-service term (days) stamped on free registrations, so "free for
# now" has a built-in sunset if a product is later charged for. Empty =
# perpetual. Invalid values fail loudly rather than silently granting forever.
try:
    FREE_TERM_DAYS = int(os.environ.get("FREE_TERM_DAYS", "") or 0) or None
    FREE_TERM_ERROR = None
except ValueError as exc:
    FREE_TERM_DAYS = None
    FREE_TERM_ERROR = f"FREE_TERM_DAYS is not a number: {exc}"
    print(f"[gateway] ERROR: {FREE_TERM_ERROR}")
# Display order (store rows, welcome-page listings).
PRODUCT_IDS = ("cammatch", "point_cloud_toolkit", "recon_toolkit", "hve_toolkit")
PRODUCT_NAMES = {
    "cammatch": "CamMatch™",
    "hve_toolkit": "HVE Toolkit™",
    "point_cloud_toolkit": "Point Cloud Toolkit™",
    "recon_toolkit": "Recon Toolkit™",
}
PRODUCT_TAGLINES = {
    "cammatch": "Camera matching from 2D-3D correspondences",
    "hve_toolkit": "Pre- and post-simulation tools for HVE",
    "point_cloud_toolkit": "Import, filter, and surface laser-scan point clouds",
    "recon_toolkit": "Data-driven animation and analysis for accident recon",
}

_raw_tokens = os.environ.get("CUSTOMER_TOKENS", "{}")
try:
    CUSTOMER_TOKENS = json.loads(_raw_tokens)
    TOKENS_ERROR = None
except json.JSONDecodeError as exc:
    # Fail loudly: a malformed variable would otherwise silently lock out
    # every customer with 401s. healthz surfaces the error too.
    CUSTOMER_TOKENS = {}
    TOKENS_ERROR = f"CUSTOMER_TOKENS is not valid JSON: {exc}"
    print(f"[gateway] ERROR: {TOKENS_ERROR}")
if not CUSTOMER_TOKENS and _raw_tokens.strip() not in ("", "{}"):
    TOKENS_ERROR = TOKENS_ERROR or "CUSTOMER_TOKENS parsed to an empty object"
    print(f"[gateway] WARNING: {TOKENS_ERROR}")

app = FastAPI(title="EDC Software extensions gateway", docs_url=None, redoc_url=None)

_origin_cache: dict[str, tuple[float, bytes]] = {}
_customers_cache: dict = {"at": 0.0, "data": None, "error": None}


async def _customer_tokens() -> dict:
    """Current repository secret -> customer mapping.

    With CUSTOMERS_REPO set, the JSON file is fetched via the GitHub
    contents API using GH_TOKEN and cached for CUSTOMERS_TTL seconds; on
    fetch/parse errors the last good copy keeps serving and the error is
    surfaced on /healthz. CUSTOMER_TOKENS entries are merged underneath,
    so the env var still works as a bootstrap or emergency override —
    though a repository secret revoked in the file stays active if it also lives in
    the env var, so clear CUSTOMER_TOKENS once you migrate.
    """
    if not CUSTOMERS_REPO:
        return CUSTOMER_TOKENS
    now = time.time()
    if _customers_cache["data"] is None or now - _customers_cache["at"] >= CUSTOMERS_TTL:
        url = f"https://api.github.com/repos/{CUSTOMERS_REPO}/contents/{CUSTOMERS_PATH}"
        headers = {"Accept": "application/vnd.github.raw+json"}
        if GH_TOKEN:
            headers["Authorization"] = f"Bearer {GH_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"GitHub returned {resp.status_code}")
            data = json.loads(resp.content)
            if not isinstance(data, dict):
                raise ValueError("customer file must be a JSON object")
            _customers_cache.update(data=data, error=None)
        except Exception as exc:
            _customers_cache["error"] = (
                f"customer list fetch failed ({CUSTOMERS_REPO}/{CUSTOMERS_PATH}): {exc}"
            )
            print(f"[gateway] ERROR: {_customers_cache['error']}")
        # Stamp even on failure so a broken origin isn't hit on every request.
        _customers_cache["at"] = now
    return {**CUSTOMER_TOKENS, **(_customers_cache["data"] or {})}


def _normalize_products(products) -> dict:
    """Normalize entitlements to {product_id: expiry}.

    expiry is "YYYY-MM-DD" (update service runs through that date, UTC,
    inclusive) or None for perpetual. The list form (["recon_toolkit"])
    and "*" keep working and mean perpetual.
    """
    if isinstance(products, dict):
        return dict(products)
    return {p: None for p in (products or ["*"])}


def _entitlements(raw):
    """Normalize a customer entry to {"name": str, "products": {id: expiry}}.

    A plain string label means every product, forever. The object form
    scopes it — products as a list (perpetual) or a map with expiry dates:
        {"name": "Acme LLC", "products": {"recon_toolkit": "2027-07-03"}}
    """
    if isinstance(raw, str):
        return {"name": raw, "products": {"*": None}}
    if isinstance(raw, dict):
        return {"name": raw.get("name", "unnamed"),
                "products": _normalize_products(raw.get("products"))}
    return None


def _active(expiry) -> bool:
    return expiry is None or str(expiry) >= date.today().isoformat()


#: Other names a grant for a product may have been made under. The
#: Visibility Toolkit was restricted as "edc_visibility_toolkit", an id no
#: release ever carried, so grants were written with that name; they keep
#: working now that the real id is restricted.
GRANT_ALIASES = {
    "visibility_toolkit": ("edc_visibility_toolkit",),
}


def _entitlement_keys(product_id: str) -> tuple:
    """Which grants can cover this product.

    A wildcard covers the published catalogue. It does not reach a
    restricted product: those have to be named, or every customer holding
    a "*" would get an internal tool the moment it entered the index.
    """
    names = (product_id,) + GRANT_ALIASES.get(product_id, ())
    if product_id in RESTRICTED_PRODUCTS:
        return names
    return ("*",) + names


def _entitled(customer: dict, product_id: str) -> bool:
    if product_id in FREE_PRODUCTS:
        return True  # free products are included with every valid repository secret
    products = customer["products"]
    return any(k in products and _active(products[k])
               for k in _entitlement_keys(product_id))


def _entitlement_state(customer: dict, product_id: str) -> str:
    """'active', 'expired' (was included, repository access term lapsed), or 'none'."""
    products = customer["products"]
    expiries = [products[k] for k in _entitlement_keys(product_id)
                if k in products]
    if not expiries:
        return "none"
    return "active" if any(_active(e) for e in expiries) else "expired"


async def _authed_customer(request: Request) -> dict | None:
    """Return {"name", "products"} for the request's Bearer repository secret, or None."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tokens = await _customer_tokens()
        raw = tokens.get(auth[7:].strip())
        if raw is not None:
            return _entitlements(raw)
    return None


async def _require_customer(request: Request) -> dict:
    customer = await _authed_customer(request)
    if customer is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "A valid EDC Software repository secret is required. In Blender, "
                "enable 'Requires Access Token' on the extensions.edccorp.com "
                "repository and paste your repository secret into the Secret field. "
                # Blender renders this in a dialog, so the URL is text to
                # type rather than a link -- worth naming anyway, because
                # this is the message a customer sees at the exact moment
                # they discover the secret they had is gone.
                + ("Lost the one you had? Visit "
                   "https://extensions.edccorp.com/recover to have it "
                   "emailed to you."
                   if mail.configured() else
                   "Contact Engineering Dynamics Company for a "
                   "repository secret.")
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return customer


async def _origin_get(path: str) -> bytes:
    """Fetch a small file from the Pages origin, cached for CACHE_TTL.

    The origin (GitHub Pages) sits behind a Fastly CDN that can keep
    serving an old copy after a deploy. A per-minute cache-busting query
    param makes the CDN treat each minute's request as a fresh object, so
    the gateway never lags the origin by more than ~1 minute plus its own
    CACHE_TTL. A no-store request header reinforces it.
    """
    now = time.time()
    hit = _origin_cache.get(path)
    if hit is not None and now - hit[0] < CACHE_TTL:
        return hit[1]
    bust = int(now // 60)
    sep = "&" if "?" in path else "?"
    url = f"{ORIGIN_BASE}/{path}{sep}v={bust}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url, headers={"Cache-Control": "no-cache"})
    if resp.status_code == 404:
        raise HTTPException(status_code=404)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"origin returned {resp.status_code} for {path}")
    _origin_cache[path] = (now, resp.content)
    return resp.content


async def _customers_file_request(method: str, payload: dict | None = None) -> httpx.Response:
    """Read or write the customer file in CUSTOMERS_REPO via the contents API."""
    url = f"https://api.github.com/repos/{CUSTOMERS_REPO}/contents/{CUSTOMERS_PATH}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {ADMIN_GH_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, headers=headers, json=payload)


async def _read_customers_file() -> tuple[dict, str | None]:
    """Return (customers, file sha); a missing file is an empty list."""
    resp = await _customers_file_request("GET")
    if resp.status_code == 200:
        payload = resp.json()
        return json.loads(base64.b64decode(payload["content"])), payload.get("sha")
    if resp.status_code == 404:
        return {}, None
    raise HTTPException(
        status_code=502, detail=f"GitHub returned {resp.status_code} reading the customer file"
    )


async def _write_customers_file(customers: dict, sha: str | None, message: str) -> None:
    payload = {
        "message": message,
        "content": base64.b64encode(
            (json.dumps(customers, indent=2, sort_keys=True) + "\n").encode()
        ).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = await _customers_file_request("PUT", payload)
    if resp.status_code == 409:
        # Someone else committed between our read and write (e.g. /welcome
        # and the webhook racing); callers re-read and retry.
        raise HTTPException(status_code=409, detail="customer file changed concurrently")
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502, detail=f"GitHub returned {resp.status_code} writing the customer file"
        )
    _customers_cache["at"] = 0.0  # the new repository secret must work on the very next sync


@app.post("/admin/customers")
async def admin_add_customer(request: Request):
    """Provision a customer: generate a repository secret and commit it to CUSTOMERS_REPO.

    Called by the purchase automation (Power Automate) after payment is
    approved. Body: {"name": "Acme LLC", "email": "buyer@acme.com",
    "products": ["recon_toolkit"]} — email optional, products optional
    (omitted/empty/"*" = all). Retry-safe: a name that already exists
    returns the existing entry instead of minting a duplicate repository secret.
    """
    if not (ADMIN_API_TOKEN and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(
            status_code=503,
            detail="admin API not configured (ADMIN_API_TOKEN, ADMIN_GH_TOKEN, CUSTOMERS_REPO)",
        )
    auth = request.headers.get("authorization", "")
    if not (
        auth.lower().startswith("bearer ")
        and secrets.compare_digest(auth[7:].strip(), ADMIN_API_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="admin token required")

    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        raise HTTPException(status_code=400, detail="a JSON object body is required")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    email = str(body.get("email") or "").strip()
    products = body.get("products") or []
    if isinstance(products, str):
        products = [p.strip() for p in products.split(",") if p.strip()]
    unknown = [p for p in products if p not in PRODUCT_IDS and p != "*"]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown product ids {unknown}; valid: {', '.join(PRODUCT_IDS)} or *",
        )
    if "*" in products:
        products = []
    term_days = body.get("term_days")
    if term_days is not None:
        try:
            term_days = int(term_days)
            if term_days <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'term_days' must be a positive integer")

    customers, sha = await _read_customers_file()

    for existing_token, value in customers.items():
        entry = _entitlements(value)
        if entry and entry["name"].lower() == name.lower():
            print(f"[gateway] admin: {name} already exists, returning existing entry")
            return {
                "token": existing_token,
                "name": entry["name"],
                "products": entry["products"],
                "repository_url": "https://extensions.edccorp.com/index.json",
                "existing": True,
            }

    token = "edc_" + secrets.token_urlsafe(18)
    if email or products or term_days:
        customers[token] = {
            "name": name,
            "products": _merge_products({}, products or ["*"], term_days),
        }
        if email:
            customers[token]["email"] = email
    else:
        customers[token] = name
    await _write_customers_file(customers, sha, f"Add customer: {name}")
    print(f"[gateway] admin: added customer {name} ({', '.join(products) or 'all products'})")
    return {
        "token": token,
        "name": name,
        "products": _entitlements(customers[token])["products"],
        "repository_url": "https://extensions.edccorp.com/index.json",
        "existing": False,
    }


# --------------------------------------------------------------------------
# Stripe purchase flow: Payment Link -> /welcome (repository secret on screen), with
# /webhook/stripe as a backstop if the buyer never reaches the redirect.

async def _stripe_checkout_session(session_id: str) -> dict:
    """Fetch a checkout session, with line-item products expanded."""
    if not session_id.startswith("cs_"):
        raise HTTPException(status_code=404, detail="unknown checkout session")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            params=[("expand[]", "line_items.data.price.product")],
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
        )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown checkout session")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Stripe returned {resp.status_code}")
    return resp.json()


def _session_products(session: dict) -> list[str]:
    """Product ids for a purchase ([] = all products).

    Read from the checkout session's metadata (Payment Link metadata is
    copied there — set `products` on each link), falling back to a
    `products` metadata key on the Stripe Products themselves.
    """
    raw = (session.get("metadata") or {}).get("products", "")
    ids = [p.strip() for p in raw.split(",") if p.strip()]
    if not ids:
        for item in ((session.get("line_items") or {}).get("data") or []):
            product = (item.get("price") or {}).get("product") or {}
            if isinstance(product, dict):
                raw = (product.get("metadata") or {}).get("products", "")
                ids += [p.strip() for p in raw.split(",") if p.strip()]
    ids = list(dict.fromkeys(ids))
    if "*" in ids:
        return ["*"]
    unknown = [p for p in ids if p not in PRODUCT_IDS]
    if not ids or unknown:
        print(
            f"[gateway] ERROR: session {session.get('id')} has "
            f"{'unknown product ids ' + str(unknown) if unknown else 'no product metadata'} "
            "— set a 'products' metadata key on the Payment Link or Product in Stripe"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "This purchase is missing product information. Your payment went "
                "through — contact Engineering Dynamics Company and your access "
                "will be set up right away."
            ),
        )
    return ids


def _session_term_days(session: dict) -> int | None:
    """Repository access term for a purchase, in days (None = perpetual).

    From a term_days metadata key on the Payment Link (copied onto the
    session), else the LICENSE_TERM_DAYS env default.
    """
    raw = str((session.get("metadata") or {}).get("term_days", "") or LICENSE_TERM_DAYS).strip()
    if not raw:
        return None
    try:
        days = int(raw)
        if days <= 0:
            raise ValueError(raw)
    except ValueError:
        print(f"[gateway] ERROR: session {session.get('id')} has invalid term_days {raw!r}")
        raise HTTPException(
            status_code=500,
            detail=(
                "This purchase is missing repository-access-term information. Your payment "
                "went through — contact Engineering Dynamics Company and your "
                "access will be set up right away."
            ),
        )
    return days


def _entry_emails(value) -> list[str]:
    """Every address on an entry: "email" first, then any in "emails".

    "email" stays the one address an entry is written with and the one a
    purchase is mailed to; "emails" holds the others, for a company
    license several people should be able to recover and renew. Kept as a
    separate key so a gateway that predates it reads such an entry exactly
    as before. Anything that is not a non-empty string is ignored rather
    than trusted -- this file is edited by hand.
    """
    if not isinstance(value, dict):
        return []          # the compact form carries no address
    extra = value.get("emails")
    candidates = [value.get("email"), *(extra if isinstance(extra, list) else [])]
    seen, found = set(), []
    for address in candidates:
        if isinstance(address, str) and address.strip():
            if address.strip().lower() not in seen:
                seen.add(address.strip().lower())
                found.append(address.strip())
    return found


def _email_on_file(value, wanted: str) -> str | None:
    """The address on this entry that matches `wanted`, as recorded, or None."""
    wanted = wanted.strip().lower()
    for address in _entry_emails(value):
        if address.lower() == wanted:
            return address
    return None


def _purchase_target(customers: dict, email: str) -> str | None:
    """Which customer a purchase made with `email` extends, or None for new.

    An address can be on more than one customer on purpose: a person's own
    licence, and as an extra on their firm's company licence. Their
    purchase belongs to the licence where the address is the main "email"
    -- their own -- rather than whichever entry the file happens to sort
    first. Only if no entry has it as the main address does an entry
    carrying it as an extra take the purchase.
    """
    wanted = email.strip().lower()
    fallback = None
    for token, value in customers.items():
        if not isinstance(value, dict) or not _email_on_file(value, wanted):
            continue
        main = value.get("email")
        if isinstance(main, str) and main.strip().lower() == wanted:
            return token
        if fallback is None:
            fallback = token
    return fallback


def _entry_sessions(value: dict) -> list:
    sessions = value.get("stripe_sessions")
    if isinstance(sessions, list):
        return sessions
    legacy = value.get("stripe_session")  # entries written before the merge logic
    return [legacy] if legacy else []


_MISSING = object()


def _merge_products(existing: dict, ids: list, term_days: int | None) -> dict:
    """Fold a purchase of `ids` (for `term_days` of updates) into an entry.

    Perpetual (None) always wins over a dated term. A renewal of a dated
    product extends from its current expiry when still active — renewing
    three months early doesn't forfeit the three months — and from today
    when it already lapsed.
    """
    today = date.today()
    new_expiry = (today + timedelta(days=term_days)).isoformat() if term_days else None
    merged = dict(existing)
    for pid in ids:
        current = merged.get(pid, _MISSING)
        if current is _MISSING:
            merged[pid] = new_expiry
        elif current is None or term_days is None:
            merged[pid] = None
        else:
            base = max(date.fromisoformat(current), today)
            merged[pid] = (base + timedelta(days=term_days)).isoformat()
    return merged


async def _email_new_purchase(name: str, email: str, token: str, products) -> None:
    """Send a new buyer their secret, without letting that hold up anything.

    Only on a genuinely new provisioning. _provision_purchase runs again on
    every /welcome reload and on a webhook replay, and both return early
    from the already-processed branch above -- so this cannot turn into one
    email per page refresh.

    Awaited rather than fired and forgotten, so the log line lands in the
    same request as the purchase it belongs to. The customer's page is not
    waiting on the result: the token is saved before this runs and the page
    shows it whether or not the mail got out.
    """
    if not (email and mail.configured()):
        return
    sent = await mail.send(
        email,
        "Your EDC Software repository secret",
        mail.purchase_body(name, token, _normalize_products(products)),
    )
    print(f"[gateway] mail: purchase secret to <{email}>: "
          f"{'sent' if sent else 'NOT SENT'}")


async def _provision_purchase(session: dict) -> dict:
    """Turn a paid checkout session into a customer entry, exactly once.

    A returning buyer (matched by email) keeps their existing repository secret and
    has the purchased products added to it — Blender holds one Secret per
    repository, so a second repository secret would strand their first purchase.
    Result flags: merged (this purchase extended existing repository access, so
    the UI must not reveal the full repository secret) and already_processed (this
    session was provisioned before — welcome revisit or webhook replay).
    """
    details = session.get("customer_details") or {}
    email = (details.get("email") or "").strip()
    name = details.get("name") or email or "Unknown customer"
    session_id = session["id"]

    for attempt in (1, 2):
        customers, sha = await _read_customers_file()

        for token, value in customers.items():
            if isinstance(value, dict) and session_id in _entry_sessions(value):
                entry = _entitlements(value)
                return {
                    "token": token, "name": entry["name"], "products": entry["products"],
                    "merged": _entry_sessions(value)[0] != session_id,
                    "already_processed": True,
                }

        ids = _session_products(session)
        term_days = _session_term_days(session)
        try:
            token = _purchase_target(customers, email) if email else None
            if token is not None:
                value = customers[token]
                value["products"] = _merge_products(
                    _entitlements(value)["products"], ids, term_days
                )
                value["stripe_sessions"] = [*_entry_sessions(value), session_id]
                value.pop("stripe_session", None)
                entry_name = value.get("name", name)
                await _write_customers_file(customers, sha, f"Extend repository access: {entry_name}")
                print(f"[gateway] stripe: extended {entry_name} <{email}> with {', '.join(ids)}")
                return {"token": token, "name": entry_name, "products": value["products"],
                        "merged": True, "already_processed": False}

            token = "edc_" + secrets.token_urlsafe(18)
            entry = {
                "name": name,
                "products": _merge_products({}, ids, term_days),
                "stripe_sessions": [session_id],
            }
            if email:
                entry["email"] = email
            customers[token] = entry
            await _write_customers_file(customers, sha, f"Add customer: {name} (Stripe purchase)")
            print(f"[gateway] stripe: provisioned {name} <{email}> ({', '.join(ids)})")
            await _email_new_purchase(name, email, token, entry["products"])
            return {"token": token, "name": name, "products": entry["products"],
                    "merged": False, "already_processed": False}
        except HTTPException as exc:
            if exc.status_code == 409 and attempt == 1:
                continue  # /welcome and the webhook raced; re-read and retry
            raise


def _stripe_signature_ok(payload: bytes, header: str) -> bool:
    """Verify a Stripe-Signature header (t=...,v1=... HMAC-SHA256 scheme)."""
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    timestamp = next((v for k, v in pairs if k == "t"), "")
    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in pairs if k == "v1")


def _lost_secret_line() -> str:
    """"I have lost my secret" -- pointed at the page that answers it.

    /recover exists, is tested and is live, and until this was written
    nothing on the site linked to it: every page a customer reads when they
    have lost their secret told them to write in instead, which is the
    support request /recover was built to remove. The page is noindex, so
    being linked from here is the only way anyone finds it.

    Still conditional on mail being configured. With no key set nothing is
    sent, /recover answers "on its way" regardless -- it must, or it becomes
    a way to ask which firms buy from us -- and sending a customer there
    would be sending them somewhere that cannot help and will not say so.
    """
    if mail.configured():
        return ('Lost it, or on a new computer? '
                '<a href="/recover">Have it emailed to you</a>.')
    return ('Lost it, or on a new computer? Contact '
            'Engineering Dynamics Company and we will send it to you.')


def _welcome_html(result: dict) -> str:
    products = _normalize_products(result["products"])
    if "*" in products:
        star = products.pop("*")
        # Each product shows its best coverage: perpetual beats any date,
        # otherwise the later of its own expiry and the bundle's.
        products = {
            p: (None if star is None or products.get(p, star) is None
                else max(products.get(p, star), star))
            for p in PRODUCT_IDS
        }
    items = "".join(
        f"<li>{html.escape(PRODUCT_NAMES.get(p, p))}"
        + (f' <span class="muted">— updates through {html.escape(str(exp))}</span>' if exp else "")
        + "</li>"
        for p, exp in products.items()
    )
    if result["merged"]:
        # Never print the full existing repository secret on an upgrade: purchases only
        # prove control of a card, not ownership of this customer's email.
        masked = html.escape(result["token"][:8]) + "…"
        heading = "Purchase added to your repository access"
        token_block = f"""
<p>You already have an EDC repository secret (it starts with
<code>{masked}</code>) — this purchase has been added to it
automatically, so there is <strong>nothing to change in Blender</strong>:</p>
<ol>
<li>Edit &rsaquo; Preferences &rsaquo; Get Extensions</li>
<li>Press the refresh (&#10227;) button on the <b>extensions.edccorp.com</b> repository</li>
<li>Your new product appears — click <b>Install</b>, and make sure the add-on is
<b>enabled</b> (tick its checkbox under Preferences &rsaquo; Add-ons if its tab doesn't show)</li>
</ol>
<p class="muted"><strong>Keep your repository secret confidential.</strong> {_lost_secret_line()}</p>"""
    else:
        heading = "Payment received — welcome!"
        token_block = f"""
<p><strong>Your repository secret</strong> (click to select, then copy):</p>
<code class="token">{html.escape(result["token"])}</code>
<p><strong>Set up Blender</strong> (4.2 or newer):</p>
<ol>
<li>Edit &rsaquo; Preferences &rsaquo; Get Extensions &rsaquo; Repositories &rsaquo; <b>+</b> &rsaquo; Add Remote Repository</li>
<li>URL: <code>https://extensions.edccorp.com/index.json</code></li>
<li>Tick <b>Requires Access Token</b> and paste your repository secret into <b>Secret</b></li>
<li>Your products appear under Get Extensions — click <b>Install</b></li>
<li><b>Make sure the add-on is enabled.</b> Installing usually activates it, but if
the product's sidebar tab doesn't appear, tick its checkbox under
Edit &rsaquo; Preferences &rsaquo; Add-ons. Updates then arrive automatically.</li>
<li><b>Save Preferences</b> (the <b>&#8801;</b> menu at the bottom-left of the Preferences
window) so you don't have to re-enable it next time you open Blender.</li>
</ol>
<p class="muted"><strong>Keep your repository secret confidential.</strong> Anyone with it can reach your EDC Software
downloads. Save it somewhere safe now — bookmarking this page is not a
substitute, and it is shown nowhere else. {_lost_secret_line()}</p>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Welcome — EDC Software</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2530; --muted: #5b6773;
    --accent: #6b2440; --border: #e2e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #10151b; --card: #1a2129; --ink: #e8edf2;
      --muted: #9aa7b2; --accent: #d98aa0; --border: #2a333d; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 40rem; margin: 3rem auto; padding: 0 1.25rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  .muted {{ color: var(--muted); }}
  code.token {{ display: block; margin: 1rem 0; padding: .9rem 1rem;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    font-size: 1.05rem; word-break: break-all; user-select: all; }}
  ol li, ul li {{ margin: .35rem 0; }}
  a {{ color: var(--accent); }}
</style></head><body><main><div class="card">
<h1>{heading}</h1>
<p class="muted">Engineering Dynamics Company</p>
<p>Hi {html.escape(result["name"])}, your repository access now covers:</p>
<ul>{items}</ul>
{token_block}
<p class="muted">For setup details and repository access terms, see <a href="/access-and-licensing.html">Access and licensing</a>.</p>
</div></main></body></html>"""


@app.get("/welcome")
async def welcome(session_id: str = ""):
    if not (STRIPE_SECRET_KEY and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(status_code=503, detail="Stripe provisioning is not configured")
    if not session_id:
        raise HTTPException(status_code=404)
    session = await _stripe_checkout_session(session_id)
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        # An email is promised only where one will actually be sent. With
        # EMAIL_HOST unset nothing sends, and telling a buyer who has just
        # paid to wait for a message that never comes is the worst moment
        # to be wrong -- which is exactly what this page used to do.
        by_email = (
            " We will also email it to you as soon as it exists."
            if mail.configured() else ""
        )
        return HTMLResponse(
            "<h1>Payment still processing</h1>"
            "<p>Your payment hasn't settled yet, so there is no access token "
            "yet either. <b>Bookmark this page</b> and reload it in a few "
            "minutes — your token will appear here as soon as the payment "
            f"clears.{by_email}</p>"
            "<p>If it still isn't here an hour from now, contact "
            "Engineering Dynamics Company and we will send it to you.</p>",
            status_code=202,
        )
    return HTMLResponse(_welcome_html(await _provision_purchase(session)))


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    if not (STRIPE_WEBHOOK_SECRET and STRIPE_SECRET_KEY and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")
    payload = await request.body()
    if not _stripe_signature_ok(payload, request.headers.get("stripe-signature", "")):
        raise HTTPException(status_code=400, detail="invalid signature")
    event = json.loads(payload)
    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get("metadata") or {}
        if metadata.get("donation"):
            # Donations (pay-what-you-want links tagged donation=1) carry no
            # repository access — acknowledge and skip provisioning.
            donor = (obj.get("customer_details") or {}).get("email", "someone kind")
            print(f"[gateway] stripe: donation received from {donor} — thank you")
            return {"received": True}
        if obj.get("payment_status") in ("paid", "no_payment_required"):
            # Re-fetch (with expanded line items) rather than trusting the
            # event body, then provision — a no-op if /welcome already did.
            session = await _stripe_checkout_session(obj["id"])
            result = await _provision_purchase(session)
            if not result["already_processed"]:
                print(f"[gateway] stripe: webhook provisioned session {obj['id']}")
    return {"received": True}


# --------------------------------------------------------------------------
# Store: pick any combination of products, one Stripe Checkout for the lot.

_price_cache: dict[str, tuple[float, dict]] = {}


async def _stripe_price(price_id: str) -> dict:
    """Amount/currency for a Stripe Price, cached for an hour."""
    hit = _price_cache.get(price_id)
    if hit is not None and time.time() - hit[0] < 3600:
        return hit[1]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.stripe.com/v1/prices/{price_id}",
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Stripe returned {resp.status_code} for {price_id}")
    price = resp.json()
    info = {"amount": price.get("unit_amount") or 0, "currency": price.get("currency", "usd")}
    _price_cache[price_id] = (time.time(), info)
    return info


def _money(amount: int, currency: str) -> str:
    if amount == 0:
        return "Free"
    if currency.lower() == "usd":
        return f"${amount / 100:,.2f}".replace(".00", "")
    return f"{amount / 100:,.2f} {currency.upper()}"


def _term_labels() -> tuple[str | None, str | None]:
    """(compact, prose) labels for the paid repository-access term, or (None, None).

    Derived from LICENSE_TERM_DAYS so the store copy always matches what
    checkout actually stamps — empty/invalid renders no term (perpetual).
    """
    raw = LICENSE_TERM_DAYS.strip()
    if not raw:
        return None, None
    try:
        days = int(raw)
    except ValueError:
        return None, None
    if days <= 0:
        return None, None
    if days % 365 == 0:
        years = days // 365
        prose = "one year" if years == 1 else f"{years} years"
        compact = "1‑yr access" if years == 1 else f"{years}‑yr access"
        return compact, prose
    return f"{days}‑day access", f"{days} days"


@app.get("/store")
async def store():
    if not (STRIPE_SECRET_KEY and STRIPE_PRICES):
        raise HTTPException(status_code=503, detail="store is not configured (STRIPE_PRICES)")
    term_compact, term_prose = _term_labels()
    rows = ""
    for pid in PRODUCT_IDS:
        if pid not in STRIPE_PRICES:
            continue
        price = await _stripe_price(STRIPE_PRICES[pid])
        price_html = html.escape(_money(price["amount"], price["currency"]))
        if price["amount"] and term_compact:
            price_html += f'<br><span class="term">{html.escape(term_compact)}</span>'
        rows += f"""
<label class="row"><input type="checkbox" name="products" value="{pid}"
  data-amount="{price['amount']}" onchange="total()">
<span><b>{html.escape(PRODUCT_NAMES[pid])}</b><br>
<span class="muted">{html.escape(PRODUCT_TAGLINES[pid])}</span></span>
<span class="price">{price_html}</span></label>"""
    term_text = (
        f"a fixed {term_prose} authenticated repository access term"
        if term_prose else "the repository access shown at checkout"
    )
    term_note = (
        f'<p class="muted note"><strong>Access term:</strong> Checkout purchases '
        f'{html.escape(term_text)}, not an auto-renewing subscription. You will receive '
        f'a repository secret for Blender; your add-ons keep working afterward, and you '
        f'can renew anytime to restore authenticated repository access.</p>'
    )
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Store — EDC Software</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2530; --muted: #5b6773;
    --accent: #6b2440; --border: #e2e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #10151b; --card: #1a2129; --ink: #e8edf2;
      --muted: #9aa7b2; --accent: #d98aa0; --border: #2a333d; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 40rem; margin: 3rem auto; padding: 0 1.25rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  .muted {{ color: var(--muted); }}
  .row {{ display: flex; gap: .9rem; align-items: center; padding: .9rem 0;
    border-bottom: 1px solid var(--border); cursor: pointer; }}
  .row input {{ width: 1.15rem; height: 1.15rem; accent-color: var(--accent); }}
  .row .price {{ margin-left: auto; font-weight: 600; white-space: nowrap;
    text-align: right; }}
  .row .price .term {{ font-weight: 400; font-size: .78em; color: var(--muted); }}
  .note {{ font-size: .9em; margin-top: 1rem; }}
  .notice {{ margin: 1rem 0; padding: .9rem 1rem; border: 1px solid var(--accent);
    border-radius: 8px; background: color-mix(in srgb, var(--accent) 10%, transparent); }}
  .foot {{ display: flex; align-items: center; margin-top: 1.25rem; }}
  #sum {{ font-size: 1.15rem; font-weight: 700; }}
  button {{ margin-left: auto; background: var(--accent); color: #fff;
    border: 0; border-radius: 8px; padding: .7rem 1.6rem; font-size: 1rem;
    cursor: pointer; }}
  button:disabled {{ opacity: .45; cursor: default; }}
  a {{ color: var(--accent); }}
</style></head><body><main><div class="card">
<h1>EDC Software — Store</h1>
<p class="muted">Blender add-ons by Engineering Dynamics Company.
Pick any combination — one checkout, one repository secret for all authenticated repository access.
Already a customer? Buy with the same email and your existing repository secret is
upgraded automatically.</p>
<p class="notice"><strong>Need setup or licensing details?</strong>
<a href="{html.escape(ACCESS_URL)}">Read Access &amp; Licensing</a> for repository-secret setup, term details, and renewal notes.</p>
<form action="/checkout" method="get">
{rows}
<div class="foot"><span id="sum">Select products</span>
<button type="submit" id="buy" disabled>Checkout</button></div>
</form>
{term_note}
<p class="muted"><a href="/">&larr; Back to products</a></p>
</div></main>
<script>
function total() {{
  let cents = 0, n = 0;
  document.querySelectorAll('input[name=products]:checked').forEach(b => {{
    cents += +b.dataset.amount; n++;
  }});
  document.getElementById('buy').disabled = n === 0;
  document.getElementById('sum').textContent = !n ? 'Select products'
    : cents === 0 ? 'Total: Free'
    : 'Total: $' + (cents / 100).toLocaleString('en-US',
        {{minimumFractionDigits: cents % 100 ? 2 : 0}});
}}
</script></body></html>""")


@app.get("/checkout")
async def checkout(request: Request):
    if not (STRIPE_SECRET_KEY and STRIPE_PRICES):
        raise HTTPException(status_code=503, detail="store is not configured (STRIPE_PRICES)")
    ids = [
        p for p in request.query_params.getlist("products")
        if p in STRIPE_PRICES or p in FREE_PRODUCTS
    ]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return RedirectResponse("/store", status_code=303)
    if all(p in FREE_PRODUCTS for p in ids):
        # Nothing to pay for — Stripe won't take a $0 checkout anyway.
        return RedirectResponse(f"/register?products={','.join(ids)}", status_code=303)
    all_ids = ids
    ids = [p for p in ids if p in STRIPE_PRICES]
    form: dict[str, str] = {
        "mode": "payment",
        "metadata[products]": ",".join(all_ids),
        "success_url": f"{PUBLIC_BASE}/welcome?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{PUBLIC_BASE}/store",
        "allow_promotion_codes": "true",
    }
    if LICENSE_TERM_DAYS.strip():
        form["metadata[term_days]"] = LICENSE_TERM_DAYS.strip()
    for i, pid in enumerate(ids):
        form[f"line_items[{i}][price]"] = STRIPE_PRICES[pid]
        form[f"line_items[{i}][quantity]"] = "1"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=form,
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
        )
    if resp.status_code != 200:
        print(f"[gateway] ERROR: checkout session creation failed: {resp.status_code} {resp.text[:300]}")
        raise HTTPException(status_code=502, detail="could not start checkout — try again shortly")
    print(f"[gateway] store: checkout started for {', '.join(ids)}")
    return RedirectResponse(resp.json()["url"], status_code=303)


def _register_form_html(ids: list, error: str = "") -> str:
    names = ", ".join(PRODUCT_NAMES.get(p, p) for p in ids)
    err = f'<p style="color:#c0392b"><strong>{html.escape(error)}</strong></p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Get free access — EDC Software</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2530; --muted: #5b6773;
    --accent: #6b2440; --border: #e2e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #10151b; --card: #1a2129; --ink: #e8edf2;
      --muted: #9aa7b2; --accent: #d98aa0; --border: #2a333d; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 30rem; margin: 3rem auto; padding: 0 1.25rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.4rem; }}
  .muted {{ color: var(--muted); }}
  label {{ display: block; margin: 1rem 0 .3rem; font-weight: 600; }}
  input[type=text], input[type=email] {{ width: 100%; padding: .6rem .8rem;
    border: 1px solid var(--border); border-radius: 8px; background: var(--bg);
    color: var(--ink); font-size: 1rem; }}
  button {{ margin-top: 1.4rem; width: 100%; background: var(--accent); color: #fff;
    border: 0; border-radius: 8px; padding: .75rem; font-size: 1.05rem;
    font-weight: 600; cursor: pointer; }}
</style></head><body><main><div class="card">
<h1>Get free access</h1>
<p class="muted">{html.escape(names)} — free from Engineering Dynamics Company.
Enter your details and your personal access token is created instantly.</p>
{err}
<form action="/register" method="get">
<input type="hidden" name="products" value="{html.escape(','.join(ids))}">
<label for="name">Name / Company</label>
<input type="text" id="name" name="name" required>
<label for="email">Email</label>
<input type="email" id="email" name="email" required>
<button type="submit">Create my access token</button>
</form>
</div></main></body></html>"""


#: When each address was last sent a recovery email. Enough to stop the
#: route being used to hammer a mailbox; not a defence against a determined
#: attacker, which is what the identical answers below are for. In memory,
#: so a deploy clears it -- acceptable, because the cost of the rare extra
#: email is one extra email.
_RECOVERY_SENT: dict[str, float] = {}
RECOVERY_INTERVAL = 300.0


def _recovery_page(message: str, sent: bool = False) -> str:
    heading = "Check your email" if sent else "Recover your repository secret"
    form = "" if sent else """
<form action="/recover" method="get">
<label for="email">The email address you bought with</label>
<input type="email" id="email" name="email" required>
<button type="submit">Email me my secret</button>
</form>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Recover your secret — EDC Software</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2530; --muted: #5b6773;
    --accent: #6b2440; --border: #e2e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #10151b; --card: #1a2129; --ink: #e8edf2;
      --muted: #9aa7b2; --accent: #d98aa0; --border: #2a333d; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 34rem; margin: 3rem auto; padding: 0 1.25rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; }}
  h1 {{ margin: 0 0 .5rem; font-size: 1.4rem; }}
  .muted {{ color: var(--muted); }}
  label {{ display: block; margin-top: 1rem; font-weight: 600; }}
  input {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem;
    border: 1px solid var(--border); border-radius: 8px; background: var(--bg);
    color: var(--ink); font-size: 1rem; }}
  button {{ margin-top: 1.4rem; width: 100%; background: var(--accent);
    color: #fff; border: 0; border-radius: 8px; padding: .75rem;
    font-size: 1.05rem; font-weight: 600; cursor: pointer; }}
</style></head><body><main><div class="card">
<h1>{heading}</h1>
<p class="muted">{message}</p>{form}
</div></main></body></html>"""


@app.get("/recover")
async def recover(email: str = ""):
    """Email a customer their repository secret, to the address on file.

    Two rules, and the whole design is in them.

    It only ever sends to the address recorded on the customer, never to
    the one typed in -- they are the same address when the rightful owner
    asks, and typing somebody else's only mails that person. So the secret
    is never shown on this page and never reaches whoever filled the form.

    And the answer is the same whether or not the address belongs to a
    customer. Saying "no such customer" would turn this into a way to ask
    which firms buy from us, one address at a time.
    """
    if not email:
        return HTMLResponse(_recovery_page(
            "Type the address you bought with and we will email your "
            "repository secret to it."))

    same_answer = _recovery_page(
        "If that address belongs to an EDC Software customer, their "
        "repository secret is on its way to it. It can take a few minutes. "
        "If nothing arrives, check spam, then contact Engineering Dynamics "
        "Company.", sent=True)

    if not mail.configured():
        print("[gateway] recover: asked for but email is not configured")
        return HTMLResponse(same_answer)

    wanted = email.strip().lower()
    now = time.time()
    if now - _RECOVERY_SENT.get(wanted, 0.0) < RECOVERY_INTERVAL:
        # Already sent one recently. The answer does not change, so someone
        # holding the button down learns nothing and the mailbox is spared.
        return HTMLResponse(same_answer)

    # Every customer with this address, not just the first: an address on
    # two entries would otherwise only ever recover whichever secret sorts
    # first in the file, and the other could not be recovered at all.
    customers, _sha = await _read_customers_file()
    matches = []
    for token, value in customers.items():
        if not isinstance(value, dict):
            continue        # the compact form carries no address to send to
        address = _email_on_file(value, wanted)
        if address is not None:
            matches.append((address, value.get("name", "there"), token))

    if not matches:
        print(f"[gateway] recover: no customer for <{wanted}>")
        return HTMLResponse(same_answer)

    _RECOVERY_SENT[wanted] = now
    address = matches[0][0]
    if len(matches) == 1:
        body = mail.recovery_body(matches[0][1], matches[0][2])
    else:
        body = mail.recovery_body_several([(name, token) for _a, name, token in matches])
    sent = await mail.send(address, "Your EDC Software repository secret", body)
    print(f"[gateway] recover: {len(matches)} secret(s) to <{wanted}>: "
          f"{'sent' if sent else 'NOT SENT'}")
    return HTMLResponse(same_answer)


@app.get("/register")
async def register(request: Request):
    """Free-product registration: name + email -> instant repository secret, no payment."""
    if not (ADMIN_GH_TOKEN and CUSTOMERS_REPO and FREE_PRODUCTS) or FREE_TERM_ERROR:
        raise HTTPException(status_code=503, detail="free registration is not configured")
    q = request.query_params
    ids = [p.strip() for p in q.get("products", "").split(",") if p.strip() in FREE_PRODUCTS]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return RedirectResponse("/store", status_code=303)
    name = (q.get("name") or "").strip()
    email = (q.get("email") or "").strip()
    if not name and not email:
        return HTMLResponse(_register_form_html(ids))
    if not name or "@" not in email:
        return HTMLResponse(_register_form_html(ids, "Please provide a name and a valid email."))

    customers, sha = await _read_customers_file()
    for token, value in customers.items():
        if isinstance(value, dict) and _email_on_file(value, email):
            # Existing customer: free products are already included with their repository secret.
            # Never re-display a repository secret to someone who only proved they can
            # type an email address.
            return HTMLResponse(f"""<h1>You're already set up</h1>
<p>The token you already have (starts with <code>{html.escape(token[:8])}…</code>)
includes every free EDC product — press the refresh button on the
extensions.edccorp.com repository in Blender and
{html.escape(', '.join(PRODUCT_NAMES.get(p, p) for p in ids))} will appear —
click <b>Install</b>, and enable the add-on under Preferences &rsaquo; Add-ons
if its tab doesn't show. {_lost_secret_line()}</p>""")

    token = "edc_" + secrets.token_urlsafe(18)
    products = _merge_products({}, ids, FREE_TERM_DAYS)
    customers[token] = {"name": name, "email": email, "products": products}
    await _write_customers_file(customers, sha, f"Free registration: {name}")
    print(f"[gateway] free registration: {name} <{email}> ({', '.join(ids)})")
    return HTMLResponse(_welcome_html(
        {"token": token, "name": name, "products": products, "merged": False}
    ))


@app.get("/healthz")
async def healthz():
    tokens = await _customer_tokens()
    body = {
        "ok": TOKENS_ERROR is None and PRICES_ERROR is None and FREE_TERM_ERROR is None,
        "tokens_configured": len(tokens),
        "store_products": len(STRIPE_PRICES),
        "stripe_key": bool(STRIPE_SECRET_KEY),
        # Mail is the one subsystem whose failure is silent: a customer who
        # never receives their secret does not complain for days, and an
        # unconfigured gateway behaves exactly like a working one right up
        # until it drops a purchase email. False here is not an error --
        # sending is optional and off by default -- but it is the answer to
        # "did that variable actually take?" without redeploying to look.
        "mail": mail.configured(),
        # The domain only. It is already public in DNS and on every message
        # sent, and it is where the mistakes are: a From on the wrong domain
        # is refused by Resend and shows up here rather than in a log
        # nobody reads. The local part stays off a public endpoint.
        "mail_from_domain": mail.from_domain(),
        "cache_ttl": CACHE_TTL,
        "free_products": FREE_PRODUCTS,
        "free_term_days": FREE_TERM_DAYS,
        "license_term_days": LICENSE_TERM_DAYS.strip() or None,
    }
    if PRICES_ERROR:
        body["store_error"] = PRICES_ERROR
    if FREE_TERM_ERROR:
        body["free_term_error"] = FREE_TERM_ERROR
    if CUSTOMERS_REPO:
        body["customers_source"] = CUSTOMERS_REPO
        if _customers_cache["error"]:
            body["ok"] = False
            body["customers_error"] = _customers_cache["error"]
    if TOKENS_ERROR:
        body["error"] = TOKENS_ERROR
    return body


@app.get("/")
async def landing():
    # The launch announcement is the homepage; the product-grid catalog lives
    # at /software.
    page = await _origin_get("assets/launch.html")
    return Response(content=page, media_type="text/html")


@app.get("/software")
async def software_catalog():
    page = await _origin_get("index.html")
    return Response(content=page, media_type="text/html")


@app.get("/index.html")
async def software_catalog_html():
    page = await _origin_get("index.html")
    return Response(content=page, media_type="text/html")


@app.get("/access-and-licensing.html")
async def access_and_licensing_page():
    page = await _origin_get("access-and-licensing.html")
    return Response(content=page, media_type="text/html")


@app.get("/products/{pid}")
async def product_page(pid: str):
    """Static product pages, proxied from the Pages origin."""
    pid = pid.removesuffix(".html")
    if not pid.replace("_", "").isalnum():
        raise HTTPException(status_code=404)
    page = await _origin_get(f"products/{pid}.html")
    return Response(content=page, media_type="text/html")


@app.get("/guide/{pid}")
async def guide_page(pid: str):
    """Public user guides, extracted from each add-on at publish time and
    proxied from the Pages origin. This is the extension's "Website" target."""
    pid = pid.removesuffix(".html")
    if not pid.replace("_", "").isalnum():
        raise HTTPException(status_code=404)
    page = await _origin_get(f"guides/{pid}.html")
    return Response(content=page, media_type="text/html")


@app.get("/launch")
async def launch_page():
    """Standalone launch announcement — a clean URL to link from elsewhere."""
    page = await _origin_get("assets/launch.html")
    return Response(content=page, media_type="text/html")


_ASSET_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".html": "text/html", ".htm": "text/html",  # hosted sample reports
    ".blend": "application/octet-stream",  # sample project files
}


@app.get("/assets/{name}")
async def asset(name: str):
    ext = os.path.splitext(name)[1].lower()
    if "/" in name or ".." in name or ext not in _ASSET_TYPES:
        raise HTTPException(status_code=404)
    data = await _origin_get(f"assets/{name}")
    return Response(
        content=data,
        media_type=_ASSET_TYPES[ext],
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/index.json")
async def index_json(request: Request):
    customer = await _require_customer(request)
    body = await _origin_get("index.json")
    products = customer["products"]
    # The wildcard shortcut used to skip the filter outright, which was true
    # while "*" meant everything. It no longer does: a restricted product is
    # not covered by one, so a "*" customer would still have been shown every
    # internal tool in Blender's Get Extensions list and then refused at the
    # download with a 403 -- an offer that cannot be taken up.
    covers_everything = ("*" in products and _active(products["*"])
                         and not RESTRICTED_PRODUCTS)
    if not covers_everything:
        index = json.loads(body)
        index["data"] = [
            e for e in index.get("data", []) if _entitled(customer, e.get("id", ""))
        ]
        body = json.dumps(index, indent=2).encode()
    print(f"[gateway] index fetch by {customer['name']}")
    return Response(content=body, media_type="application/json")



# --- who has which version --------------------------------------------------
#
# GitHub's release download_count is not an answer: on a private repo it
# counts authenticated fetches of every kind, and it carries no identity at
# all. The gateway is the only place that knows who asked, and it was saying
# so to stdout, where the answer lives as long as the log retention and is
# not queryable even then.
#
# So the stamp goes on the customer, in the file that is already the record
# of who may have what. Not on every download, though: that file's git
# history is the entitlement audit trail, and a commit per download would
# bury the commits that matter under the ones that do not. Pending stamps
# are coalesced in memory -- many downloads by one customer collapse to one
# field -- and written at most once every DOWNLOAD_FLUSH_SECONDS, after the
# response, so a download never waits on a commit and never fails because
# one failed.

#: Seconds between writes. A download is not urgent news; losing the last
#: few minutes of them to a restart costs less than a commit per download.
DOWNLOAD_FLUSH_SECONDS = float(os.environ.get("DOWNLOAD_FLUSH_SECONDS", "600"))

#: Stamps seen but not yet written, keyed by customer *name* rather than by
#: repository secret. This dict outlives the request that filled it, and a
#: secret sitting in it would be one more place a secret lives for no reason.
_PENDING_DOWNLOADS: dict = {}
_DOWNLOADS_WRITTEN_AT = 0.0


def _record_download(name: str, filename: str, product_id: str = "") -> None:
    """Note that this customer fetched this file, for the next flush.

    "file"/"at" are the latest download of anything; "products" keeps the
    latest per product, so installing three toolkits inside one flush
    window records all three rather than whichever came last.

    A new dict every time, never an update in place: the flush compares
    what it wrote against what is pending by value, and a nested dict
    mutated under it would compare equal and be dropped unwritten.
    """
    stamp = {"file": filename,
             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    products = dict((_PENDING_DOWNLOADS.get(name) or {}).get("products") or {})
    if product_id:
        products[product_id] = dict(stamp)
    _PENDING_DOWNLOADS[name] = {**stamp, "products": products}


def _stamp_customers(customers: dict, pending: dict) -> bool:
    """Write the stamps into the customer map. True if anything changed.

    A plain-string entry is upgraded to the object form to carry the stamp.
    That is safe and not merely convenient: a string means every product
    forever, and an object with no products key normalises to exactly the
    same thing, so the upgrade cannot quietly narrow what someone may
    install.
    """
    changed = False
    for token, raw in list(customers.items()):
        name = raw.get("name") if isinstance(raw, dict) else raw
        stamp = pending.get(name) if isinstance(name, str) else None
        if stamp is None:
            continue
        if isinstance(raw, str):
            raw = {"name": raw}
            customers[token] = raw
            changed = True
        latest = {"file": stamp["file"], "at": stamp["at"]}
        if raw.get("last_download") != latest:
            raw["last_download"] = latest
            changed = True
        # One entry per product, so a customer's other toolkits are not
        # forgotten each time they download a different one.
        per_product = stamp.get("products") or {}
        if per_product:
            existing = raw.get("last_downloads")
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(per_product)
            if merged != existing:
                raw["last_downloads"] = merged
                changed = True
    return changed


async def _flush_downloads(force: bool = False) -> None:
    """Write pending stamps, if it is time and there is anywhere to write.

    Every failure here is swallowed deliberately. The customer has their
    file; a gateway that returned an error because it could not record the
    fact would be trading the thing that matters for the note about it. The
    stamps stay pending and the next download tries again.
    """
    global _DOWNLOADS_WRITTEN_AT
    if not _PENDING_DOWNLOADS or not CUSTOMERS_REPO or not ADMIN_GH_TOKEN:
        return
    now = time.monotonic()
    if not force and now - _DOWNLOADS_WRITTEN_AT < DOWNLOAD_FLUSH_SECONDS:
        return
    _DOWNLOADS_WRITTEN_AT = now

    pending = dict(_PENDING_DOWNLOADS)
    try:
        customers, sha = await _read_customers_file()
        if _stamp_customers(customers, pending):
            await _write_customers_file(
                customers, sha,
                f"Record last download for {len(pending)} customer(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[gateway] last_download not written ({exc}); still pending")
        return
    for name, stamp in pending.items():
        if _PENDING_DOWNLOADS.get(name) == stamp:
            _PENDING_DOWNLOADS.pop(name, None)


@app.get("/packages/{filename}")
async def package(filename: str, request: Request):
    customer = await _require_customer(request)
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=404)

    packages = json.loads(await _origin_get("packages.json"))
    entry = packages.get(filename)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown package {filename}")
    product_id = entry.get("id", "")
    state = _entitlement_state(customer, product_id) if product_id else "active"
    if state != "active":
        detail = (
            f"Your update service for {product_id} has expired — installed "
            "versions keep working, but updates require a renewal. Visit "
            "https://extensions.edccorp.com or contact Engineering Dynamics Company."
            if state == "expired"
            else f"This repository secret does not include access to {product_id}. "
            "Contact Engineering Dynamics Company to add it."
        )
        raise HTTPException(status_code=403, detail=detail)

    # The API asset endpoint works for private repos; the public
    # browser_download_url does not.
    asset_url = f"https://api.github.com/repos/{entry['repo']}/releases/assets/{entry['asset_id']}"
    headers = {"Accept": "application/octet-stream"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    upstream = await client.send(
        client.build_request("GET", asset_url, headers=headers), stream=True
    )
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream returned {upstream.status_code}")

    print(f"[gateway] {filename} download by {customer['name']}")
    _record_download(customer["name"], filename, product_id)

    async def _close():
        await upstream.aclose()
        await client.aclose()
        # After the bytes, never before: the stamp is a note about the
        # download and must not be able to delay or fail it.
        await _flush_downloads()

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **(
                {"Content-Length": upstream.headers["content-length"]}
                if "content-length" in upstream.headers
                else {}
            ),
        },
        background=BackgroundTask(_close),
    )
