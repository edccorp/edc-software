# EDC Software extensions gateway (Railway)

Per-customer authentication for the Blender extensions channel. Blender
sends the repository secret from the repository's **Secret** field as an
`Authorization: Bearer` header on every index fetch and download; this
FastAPI service validates it, serves the index, and streams the add-on
zips **directly from the private GitHub release assets** — no public
copies anywhere.

```
Blender ──repository secret──▶ extensions.edccorp.com (this gateway on Railway)
                     ├── /            landing page   (public)
                     ├── /index.json  index          (repository secret required)
                     └── /packages/*  zips           (repository secret required,
                                                      streamed from private
                                                      GitHub releases)
```

## Deploy on Railway (one time)

1. **Create the service**: Railway → New Project → Deploy from GitHub repo →
   `edccorp/edc-software`. Set the service **root directory** to
   `gateway/` (Settings → Source). Railway detects Python and uses
   `railway.json` for the start command.
2. **Set environment variables** (service → Variables):
   - `GH_TOKEN` — a fine-grained PAT with **Contents: read** on every
     product repo, including any hidden ones (same scope as the
     `PRODUCTS_TOKEN` Actions secret). When you add a new product repo,
     grant it to **both** this token and `PRODUCTS_TOKEN`, or installs of
     that product 502 (the gateway can't stream the private release asset).
   - `CUSTOMER_TOKENS` — JSON mapping repository secret → customer. A plain label
     includes **every** product with that repository access; an object form scopes it to
     specific products (each customer's Blender then only sees and can
     download the products included with their repository access):
     ```json
     {"edc_Xk39fj2mQ8vL5nR7tY1wZ4": "Acme Reconstruction LLC",
      "edc_P2hN8cV6bM4xK9sD3fG7jQ": {"name": "Smith Engineering",
                                     "products": ["recon_toolkit", "point_cloud_toolkit"]}}
     ```
     Product ids: `cammatch`, `hve_toolkit`, `point_cloud_toolkit`,
     `recon_toolkit` (or `"*"` for all). Generate repository secrets with:
     `python -c "import secrets; print('edc_' + secrets.token_urlsafe(18))"`
   - `CUSTOMERS_REPO` (optional, recommended once you have more than a
     couple of customers) — see **Managing customers at scale** below;
     repository secrets then live in a private repo instead of this variable.
3. **Attach the domain**: service → Settings → Networking → Custom Domain →
   `extensions.edccorp.com`. Railway shows a CNAME target; update the DNS
   record for `extensions` (currently pointing at `edccorp.github.io`) to
   that target. Remove the custom domain from the GitHub Pages settings of
   this repo (Pages keeps serving at `edccorp.github.io/edc-software`
   as the gateway's origin).
4. **Flip the publisher to gateway mode**: in this repo's publish workflow,
   set `MIRROR_ZIPS: "0"` on the build step (zips stop being copied to the
   public Pages site; only `index.json`, `packages.json`, and the landing
   page remain there). Run **Publish extensions index**.

## Customer instructions

Preferences → Get Extensions → Repositories → `extensions.edccorp.com` →
tick **Requires Access Token** → paste the customer's repository secret into
**Secret**. Everything else (install, update notifications) works as
before — unauthenticated clients get a 401 with a friendly message.

## Managing customers at scale (recommended)

Editing `CUSTOMER_TOKENS` in the Railway dashboard is fine for one or two
repository secrets, but past that, move the list into a **private GitHub repo**. The
gateway re-reads the file within a minute of any change — no redeploy —
and the repo's commit history is a free audit trail of every add, scope
change, and revocation.

One-time setup:

1. Create a **private** repo `edccorp/edc-extensions-customers` (empty is fine).
2. Add that repo to the fine-grained PAT used as the gateway's `GH_TOKEN`
   (github.com → Settings → Developer settings → Fine-grained tokens →
   edit → Repository access). Contents: read is already enough.
3. In Railway (service → Variables) add `CUSTOMERS_REPO=edccorp/edc-extensions-customers`.
4. For the management CLI, create a **second** fine-grained PAT with
   **Contents: read & write** on `edc-extensions-customers` only, and set it on your
   machine: `setx CUSTOMERS_ADMIN_TOKEN github_pat_...`
5. Migrate any repository secrets out of `CUSTOMER_TOKENS` (re-add them via the CLI),
   then set `CUSTOMER_TOKENS={}` — entries left there stay active even if
   revoked in the file, so don't keep both.

Day-to-day, from the repo root:

```
python tools/customer.py add "Acme Reconstruction LLC" --email buyer@acme.com
python tools/customer.py add "Smith Engineering" --products recon_toolkit,point_cloud_toolkit
python tools/customer.py list
python tools/customer.py needs-email
python tools/customer.py set-email "Smith Engineering" ops@smitheng.com
python tools/customer.py add-email "Acme Reconstruction LLC" it@acme.com cad@acme.com
python tools/customer.py remove-email "Acme Reconstruction LLC" cad@acme.com
python tools/customer.py show buyer@acme.com
python tools/customer.py reissue "Acme Reconstruction LLC"
python tools/customer.py set-products "Smith Engineering" --products "*"
python tools/customer.py revoke "Acme Reconstruction LLC"
```

`needs-email` prints the customers with no address on file — the backfill
list, names only, no secrets on screen. They are the ones `/recover`
cannot help: it finds nothing for them and answers "on its way" anyway,
because it must, so nothing arrives and nothing says why.

Give `add` an `--email` whenever you know it. It is what `/recover`
matches on, so a customer with an address on file can have their secret
mailed back to them without writing in; one without it cannot, and `add`
says so when you leave it off. `set-email` fills in the entries made
before this existed. `show` looks one customer up — by name, email or the
secret — without putting every other customer's secret on screen, and
`reissue` gives them a new secret when the old one may have been seen by
somebody else rather than merely mislaid.

**What each customer downloaded.** The gateway records downloads on the
customer's entry: `last_downloads` holds the latest file and time for each
product, and `last_download` the latest of anything. They are written at
most every `DOWNLOAD_FLUSH_SECONDS` (default 600), so downloads in the last
few minutes before a restart can be lost. `show` lists them per product,
newest first.

**Several addresses on one secret (company licenses).** `add-email` gives
a customer more addresses; they are stored beside the main one:

```json
{"edc_...": {"name": "Acme LLC (company license)", "email": "it@acme.com",
             "emails": ["cad@acme.com", "boss@acme.com"],
             "products": {"recon_toolkit": "2027-10-05"}}}
```

Any of them can recover the secret at `/recover` (it is mailed to the
address that asked), a Stripe purchase made with any of them is added to
this secret rather than creating a new one, and `show` finds the customer
by any of them. `email` stays the main address -- new-purchase emails go
there -- and `remove-email` on it promotes the next one. `add-email` refuses an
address that is already on another customer unless you pass `--shared` --
for someone with their own licence who is also covered by their firm's.
A shared address is stored as an extra, `/recover` sends it every secret
it is on, and a purchase made with it goes to the customer whose *main*
address it is (their own licence), whatever order the file is in.
Keep `email` a single string when editing by hand: extra addresses go in
the `emails` list.

`add` generates the repository secret, commits the change, and prints the secret once
along with the customer-facing Blender setup steps. Changes go live on
the gateway within ~60 seconds (`CUSTOMERS_TTL`). You can also edit
`customers.json` directly on github.com — same format as
`CUSTOMER_TOKENS`, repository secret → label or `{"name", "products"}` object.

- **Revoke**: their Blender shows an authentication error on the next
  sync; already-installed add-ons keep working but stop updating.
- Downloads and index fetches are logged with the customer label
  (Railway → Deployments → Logs) — who is updating, and when.
- If the customers file ever fails to fetch or parse, the gateway keeps
  serving the **last good copy** and reports the problem at `/healthz`
  (`"ok": false` + `customers_error`).

## Selling with Stripe Payment Links (recommended — fully automatic)

A purchase provisions itself: the buyer pays on a Stripe-hosted checkout
page, gets redirected to `/welcome`, and the gateway verifies the payment
with Stripe, commits them to the customers repo, and shows their repository secret on
screen — seconds after paying, no human involved. A signed webhook
provisions as a backstop if they close the browser early (same repository secret,
never a duplicate — provisioning is keyed to the checkout session).

```
Payment Link ──paid──▶ /welcome?session_id=...   (repository secret on screen)
                └─────▶ /webhook/stripe          (backstop, signature-verified)
```

One-time setup, in the **EDC Stripe account** (use a separate Stripe
account for EDC — dashboard account picker → Create new account):

1. **Products**: Stripe → Product catalog → add each product with its
   price (CamMatch, HVE Toolkit, Point Cloud Toolkit, Recon Toolkit —
   plus a bundle if you like).
2. **Payment Links**: create a Payment Link per product. On each link:
   - **After payment** → *Don't show confirmation page* → redirect to
     `https://extensions.edccorp.com/welcome?session_id={CHECKOUT_SESSION_ID}`
     (the `{CHECKOUT_SESSION_ID}` placeholder is literal — Stripe fills it).
   - **Metadata**: add key `products` = the product id(s), e.g.
     `recon_toolkit` or `cammatch,hve_toolkit` or `*` for a bundle.
3. **Webhook**: Developers → Webhooks → Add endpoint →
   `https://extensions.edccorp.com/webhook/stripe`, event
   `checkout.session.completed`. Copy its signing secret (`whsec_...`).
4. **Railway → Variables**:
   - `STRIPE_SECRET_KEY` — from Developers → API keys. Best practice:
     create a **restricted key** with read access to Checkout Sessions
     and Products only.
   - `STRIPE_WEBHOOK_SECRET` — the `whsec_...` from step 3.
   - `ADMIN_GH_TOKEN` — write PAT on the customers repo (shared with the
     admin API; already set if you configured that).
5. **Test in test mode first**: Stripe's test-mode keys + a test-mode
   payment link, card `4242 4242 4242 4242`, any future expiry/CVC. Check
   the welcome page shows a repository secret and the customer appears in
   `customers.json`. Then swap the live keys into Railway.

Put the payment links on the landing page / your site — they *are* the
purchase page. Refunds: revoke with `tools/customer.py revoke` (the
Stripe receipt email is the customer's proof of purchase; the
`customers.json` entry records their checkout session ids).

**Repeat purchases**: a buyer whose checkout email matches an existing
customer keeps their repository secret — the new product is added to it, and the
welcome page says "nothing to change in Blender, just refresh" (showing
only a masked repository secret prefix; the full repository secret is never re-displayed). A
different email means a fresh customer entry — if someone buys twice
under two emails, merge by hand: `set-products` on the entry to keep,
`revoke` the other.

## The store page (any combination, one checkout)

`https://extensions.edccorp.com/store` lets a customer tick any set of
products and pay for them in a single Stripe Checkout — the gateway
creates the checkout session itself with the right `products` metadata,
so the purchase flows through the same welcome/repository-secret/merge logic as the
payment links. Promotion codes are enabled at checkout. To turn it on:

1. Railway → Variables: `STRIPE_PRICES` — JSON mapping product id to the
   Stripe **Price id** (Product catalog → product → price → `price_...`):
   ```json
   {"cammatch": "price_1ABC...", "hve_toolkit": "price_1DEF...",
    "point_cloud_toolkit": "price_1GHI...", "recon_toolkit": "price_1JKL..."}
   ```
   Products missing from the map simply don't appear in the store.
2. The gateway's `STRIPE_SECRET_KEY` must be allowed to **create**
   checkout sessions — if you used a restricted key, give it Checkout
   Sessions: Write and Prices: Read on top of the read permissions.
3. Prices display straight from Stripe (cached an hour), so a price
   change in the dashboard is the only edit you ever make.

`LICENSE_TERM_DAYS` (if set) is stamped on store checkouts as the repository access term,
same as `term_days` metadata on payment links. Individual payment links
keep working alongside the store.

## Free products

`FREE_PRODUCTS` (Railway variable, comma-separated ids, e.g.
`hve_toolkit,recon_toolkit`) marks products as free:

- They come with **every** valid repository secret automatically.
- `/register` hands them out without payment — name + email → instant
  repository secret (each registration is a commit to the customers repo, so the
  free user list doubles as a mailing list). The store and `/checkout`
  route free-only selections there; mixed free+paid selections still go
  through one Stripe checkout.
- `FREE_TERM_DAYS` (e.g. `365`) stamps an update-service expiry on each
  free registration, so "free for now" has a built-in sunset if the
  product is later charged for. Empty = perpetual.
- To start charging later: remove the id from `FREE_PRODUCTS`, price it
  in Stripe + `STRIPE_PRICES`, set its `price` in `content/products/`.
  Already-registered users keep whatever term their entry carries.

## Time-limited repository access (one year of updates)

To sell "the add-on plus N days of updates", add a second metadata key
to the Payment Link: `term_days` = `365`. The customer's entry then
stores an expiry date per product:

```json
{"edc_...": {"name": "Acme LLC", "email": "buyer@acme.com",
             "products": {"recon_toolkit": "2027-07-03"}}}
```

Nothing has to run when the year is up — the gateway checks the date on
every request. Through the expiry date the product is served normally;
after it, the product drops out of the customer's index (their
**installed add-on keeps working forever**, it just stops receiving
updates) and downloads return a friendly "renew to keep receiving
updates" message.

- **Renewals**: buying the same product again (same email — a dedicated
  "renewal" Payment Link with the same `products` + `term_days` metadata
  works nicely) extends the date by the term. Renewing early extends
  from the current expiry, so no time is lost; renewing after a lapse
  extends from today.
- Links **without** `term_days` sell perpetual update access, and
  perpetual always wins over a dated term when purchases mix. Set the
  `LICENSE_TERM_DAYS` Railway variable to give links without metadata a
  default term instead.
- Manual equivalent: `customer.py add "Acme" --products recon_toolkit
  --expires 2027-07-03` (and the same flag on `set-products`);
  `customer.py list` shows the dates. The admin API accepts
  `"term_days": 365` in the POST body.

## Alternative: Microsoft Forms + Power Automate (manual approval)

The gateway has a provisioning API so a purchase can turn into a working
repository secret without touching the CLI:

```
POST https://extensions.edccorp.com/admin/customers
Authorization: Bearer <ADMIN_API_TOKEN>
Content-Type: application/json

{"name": "Acme LLC", "email": "buyer@acme.com", "products": ["recon_toolkit"]}
```

Response: `{"token": "edc_...", "name", "products", "repository_url",
"existing"}`. Products may be a list or comma-separated string; omit for
all products. Retry-safe — re-posting the same name returns the existing
entry instead of minting a duplicate.

One-time setup:

1. Railway → Variables: add `ADMIN_API_TOKEN` (generate like a customer
   repository secret: `python -c "import secrets; print('adm_' + secrets.token_urlsafe(24))"`)
   and `ADMIN_GH_TOKEN` (a fine-grained PAT with **Contents: read & write**
   on the customers repo — same scope as the CLI's `CUSTOMERS_ADMIN_TOKEN`;
   keep `GH_TOKEN` itself read-only).
2. **Microsoft Form** with fields: Name / Company, Email, Product
   (choice: CamMatch, HVE Toolkit, Point Cloud Toolkit, Recon Toolkit,
   All), and show the payment link in the form description.
3. **Power Automate flow**:
   - Trigger: *When a new response is submitted* → *Get response details*.
   - *Start and wait for an approval* addressed to you ("Payment received
     from …?") — payment links don't notify the flow, so this approval
     is the payment check; approve from the phone app after the payment
     notification arrives.
   - *Condition*: outcome is Approve.
   - *HTTP* action (premium connector): POST to the URL above,
     `Authorization` header `Bearer <ADMIN_API_TOKEN>`, JSON body mapping
     the form fields; map the product choice to ids `cammatch`,
     `hve_toolkit`, `point_cloud_toolkit`, `recon_toolkit`, or `*`.
   - *Parse JSON* on the response, then *Send an email (V2)* to the
     customer containing the repository secret (`token`) and the Blender steps (add repository
     `https://extensions.edccorp.com/index.json`, tick *Requires Access
     Token*, paste the repository secret into *Secret*).

Notes: the HTTP action needs a Power Automate premium license; if that's
a blocker, skip the HTTP step and run `tools/customer.py add` yourself —
the approval email still gives you a queue. For fully hands-off sales
(payment-verified, no approval tap), switch the payment link to Stripe
Payment Links and point a Stripe webhook at the gateway — say the word
and that endpoint can be added.

## Notes

- The Pages origin still serves `index.json`/`packages.json` publicly at
  the `github.io` URL. That is metadata only (names, versions, hashes);
  the binaries live solely in private GitHub releases once
  `MIRROR_ZIPS=0`.
- Rolling back to the unauthenticated setup: point the `extensions` DNS
  CNAME back at `edccorp.github.io`, re-add the custom domain in Pages
  settings, and set `MIRROR_ZIPS` back to `"1"`.

## Emailing customers their repository secret

Off unless configured. With `RESEND_API_KEY` unset nothing is sent and every
page behaves exactly as it did before — including the checkout pages, which
only promise an email where one will actually go out.

Railway → Variables:

| Variable | Example | Notes |
|---|---|---|
| `RESEND_API_KEY` | `re_…` | Resend → API keys; sending permission is enough |
| `EMAIL_FROM` | `software@edccorp.com` | must be on a domain verified in Resend |
| `EMAIL_REPLY_TO` | `support@edccorp.com` | optional; unset, replies go to `EMAIL_FROM` |
| `EMAIL_TIMEOUT` | `15` | seconds; the default |

`/healthz` reports `"mail"` (is it configured at all) and
`"mail_from_domain"` (which domain it sends from) — so "did that variable
actually take?" is one URL rather than a redeploy, and a `EMAIL_FROM`
typoed onto a domain Resend has not verified shows up before a customer
does. Mail being off does not make the gateway unhealthy: `"ok"` stays
true, because downloads still work.

Verify `edccorp.com` under Resend → Domains first. It gives DKIM and SPF
records to add at GoDaddy, where the DNS is. Until the domain is verified
Resend refuses anything sent from an address on it, and the refusal is
logged with its reason.

Sending from the company's own Microsoft mailbox over SMTP was the other
route, and the history has that implementation. It needed a licensed
Microsoft seat this tenant had none spare of, and SMTP reports delivery to
the next hop and nothing after — so a licence key that silently failed to
arrive looked exactly like one that was read.

Two things send:

- **A new purchase** — the secret is emailed when `_provision_purchase`
  creates it, which is what finally answers a payment that settles after the
  buyer has closed the tab. Only on a genuinely new provisioning, so a
  reloaded `/welcome` or a replayed webhook cannot send it twice.
The page is linked from every place a customer reads about a lost secret:
both `/welcome` variants, the already-registered `/register` page, the 401
Blender shows when the secret is missing or wrong, and — on the public
site — the Access & Licensing page, in the setup steps and as its own FAQ
entry. That last one is the only route for somebody who has uninstalled or
is setting up a second machine and never sees a 401 at all. It is built by
`tools/build_index.py` and must use the absolute `RECOVER_URL`: those
pages are also served straight from GitHub Pages, where `/recover` would
land on `edccorp.github.io/recover`. All four fall back
to "contact Engineering Dynamics Company" when mail is not configured —
`/recover` answers "on its way" whether or not it sent anything, so
pointing customers at it while nothing can send would send them somewhere
that cannot help and will not say so.

- **`/recover`** — a customer enters the address they bought with and the
  secret is emailed **to the address on the record**, never shown on the
  page and never sent to the address typed in. The answer is identical
  whether or not that address belongs to a customer, so the form cannot be
  used to ask which firms buy from us; and one address gets at most one
  email every five minutes.

`[gateway] mail:` and `[gateway] recover:` lines in Railway's log say `sent`
or `NOT SENT` for every attempt, and a refusal is logged with Resend's own
reason. Delivery, bounces and complaints are in Resend's own dashboard,
which is the thing SMTP could not have told us.
