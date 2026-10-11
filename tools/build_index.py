#!/usr/bin/env python3
"""Build the EDC Software extensions repository index for Blender.

For each product repo, fetches the latest GitHub release, downloads its
add-on zip, reads the ``blender_manifest.toml`` inside it (the manifest is
the source of truth for id/name/version), and writes:

    <out>/index.json      Blender remote-repository listing (schema "v1")
    <out>/index.html      Human-readable landing page with instructions
    <out>/packages/*.zip  The add-on zips (when MIRROR_ZIPS is on)

Zips are mirrored onto the published site by default so customers can
download them even while the product repos are private. Set MIRROR_ZIPS=0
to instead point archive_url at the GitHub release assets directly (only
useful once the product repos are public).

Products whose latest release has no manifest in the zip (releases cut
before extension support) are skipped with a warning and appear once a
new release is published.

Usage:  python tools/build_index.py [out_dir]        # default: site/

GITHUB_TOKEN must be able to read the product repos (while they are
private, the default workflow token cannot — CI uses the PRODUCTS_TOKEN
secret when it is configured).
"""

import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile

CONTENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content")

# Current repository names. Four of these were renamed with EDC prefixes;
# GitHub redirects the old names, but a redirect is not a guarantee -- it
# stops working the moment anyone creates a repository under an old name,
# and the product would then vanish from index.json with no error, just
# a skip. Name the repositories as they actually are.
PRODUCTS = [
    "edccorp/CamMatch",
    "edccorp/EDCHVEToolkit",
    "edccorp/EDCPointCloudToolkit",
    "edccorp/EDCReconToolkit",
    "edccorp/EDCVisibilityToolkit",
    "edccorp/EDCVideoForensicsToolkit",
    "edccorp/EDCAudioForensicsToolkit",
    "edccorp/EDC-Recon-Calculations",
    "edccorp/BlendMotion",
]

# Product repos that publish their releases as GitHub *pre-releases*. The
# "latest release" endpoint ignores those (it 404s when a repo has nothing
# else), so for these repos we take the newest published release of any kind
# instead.
PRERELEASE_PRODUCTS = {
    "edccorp/BlendMotion",
}

# Product ids (matching each add-on's blender_manifest.toml `id`) to keep OUT of
# the public site — the landing/catalog page and product pages. They still ship
# in index.json and packages.json, so a token entitled to them can install them
# in Blender; they're just not advertised (internal/beta tools). Add ids to the
# literal set, or override at build time via the HIDDEN_PRODUCTS env var
# (comma-separated).
#
# The id has to be the one in the released zip, not the repository's name:
# an id that matches nothing hides nothing. The Visibility Toolkit was
# listed here as "edc_visibility_toolkit" while every release since v1.16.0
# has shipped as "visibility_toolkit", so it was advertised all along.
HIDDEN_PRODUCTS = {
    "video_forensics_toolkit",
    "audio_forensics_toolkit",
    "visibility_toolkit",
    "recon_calculations",
    "blendmotion",
} | set(filter(None, (os.environ.get("HIDDEN_PRODUCTS") or "").replace(" ", "").split(",")))

PAGES_BASE = "https://extensions.edccorp.com"
REPOSITORY_URL = f"{PAGES_BASE}/index.json"
STORE_URL = f"{PAGES_BASE}/store"
ACCESS_URL = f"{PAGES_BASE}/access-and-licensing.html"
#: Absolute, not "/recover". These pages are also served straight from
#: GitHub Pages, where a root-relative link would land on
#: edccorp.github.io/recover -- which does not exist. The route only
#: lives on the gateway.
RECOVER_URL = f"{PAGES_BASE}/recover"

MIRROR_ZIPS = os.environ.get("MIRROR_ZIPS", "1").lower() not in ("0", "false", "no")

# Manifest fields copied into each index entry when present.
MANIFEST_FIELDS = (
    "schema_version",
    "id",
    "name",
    "tagline",
    "version",
    "type",
    "maintainer",
    "license",
    "blender_version_min",
    "blender_version_max",
    "website",
    "tags",
    "platforms",
    "permissions",
)


def _fetch(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def _latest_release(repo):
    if repo in PRERELEASE_PRODUCTS:
        return _newest_release(repo)
    try:
        return json.loads(_fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    except urllib.error.HTTPError as err:
        print(f"SKIP {repo}: cannot fetch latest release ({err.code} {err.reason})")
        return None
    except urllib.error.URLError as err:
        print(f"SKIP {repo}: cannot fetch latest release ({err.reason})")
        return None


def _newest_release(repo):
    """Newest published release, pre-releases included (drafts excluded)."""
    try:
        releases = json.loads(
            _fetch(f"https://api.github.com/repos/{repo}/releases?per_page=10")
        )
    except urllib.error.HTTPError as err:
        print(f"SKIP {repo}: cannot list releases ({err.code} {err.reason})")
        return None
    except urllib.error.URLError as err:
        print(f"SKIP {repo}: cannot list releases ({err.reason})")
        return None
    # The API returns releases newest-first.
    for release in releases:
        if not release.get("draft"):
            return release
    print(f"SKIP {repo}: no published releases")
    return None


def _manifest_from_zip(data):
    """Return the parsed blender_manifest.toml from the zip, or None."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            parts = name.split("/")
            if parts[-1] == "blender_manifest.toml" and len(parts) <= 2:
                return tomllib.loads(zf.read(name).decode("utf-8"))
    return None


def _guide_from_zip(data):
    """Return the bundled docs/USER_GUIDE.html bytes from the zip, or None.

    Each add-on ships its self-contained (logo embedded) user guide; we host
    it publicly so the extension's "Website" link points at real docs instead
    of a private GitHub repo.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.endswith("docs/USER_GUIDE.html"):
                return zf.read(name)
    return None


def build_entries(out_dir):
    entries = []
    packages = {}
    releases = {}  # product id -> {"version", "notes", "published"} for pages
    for repo in PRODUCTS:
        release = _latest_release(repo)
        if release is None:
            print(f"SKIP {repo}: no releases (or no access with this token)")
            continue
        asset = next(
            (a for a in release.get("assets", []) if a["name"].endswith(".zip")),
            None,
        )
        if asset is None:
            print(f"SKIP {repo}: latest release ({release.get('tag_name')}) has no zip asset")
            continue
        # The API asset endpoint works for private repos too (the public
        # browser_download_url does not).
        data = _fetch(asset["url"], accept="application/octet-stream")
        manifest = _manifest_from_zip(data)
        if manifest is None:
            print(
                f"SKIP {repo}: {asset['name']} has no blender_manifest.toml "
                f"(release predates extension support; cut a new release)"
            )
            continue
        entry = {k: manifest[k] for k in MANIFEST_FIELDS if k in manifest}
        # Host the add-on's bundled user guide on the public site and point the
        # extension's "Website" link at it, instead of a (private) GitHub repo.
        guide = _guide_from_zip(data)
        if guide is not None:
            guide_dir = os.path.join(out_dir, "guides")
            os.makedirs(guide_dir, exist_ok=True)
            with open(os.path.join(guide_dir, f"{manifest.get('id', '')}.html"), "wb") as fh:
                fh.write(guide)
            entry["website"] = f"{PAGES_BASE}/guide/{manifest.get('id', '')}"
        if MIRROR_ZIPS:
            # Public mode: the zips are copied onto the Pages site itself.
            pkg_dir = os.path.join(out_dir, "packages")
            os.makedirs(pkg_dir, exist_ok=True)
            with open(os.path.join(pkg_dir, asset["name"]), "wb") as fh:
                fh.write(data)
        # In gateway mode (MIRROR_ZIPS=0) the same /packages/ URL is served by
        # the authenticated Railway gateway, which resolves the download via
        # packages.json and streams the private GitHub release asset.
        entry["archive_url"] = f"{PAGES_BASE}/packages/{asset['name']}"
        entry["archive_size"] = len(data)
        entry["archive_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
        packages[asset["name"]] = {
            "id": manifest.get("id", ""),
            "repo": repo,
            "asset_id": asset["id"],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        releases[manifest.get("id", "")] = {
            "version": manifest.get("version", ""),
            "notes": release.get("body") or "",
            "published": (release.get("published_at") or "")[:10],
        }
        entries.append(entry)
        print(f"OK   {repo}: {entry['id']} {entry['version']} ({asset['name']})")
    return sorted(entries, key=lambda e: e["name"].lower()), packages, releases


SHARED_CSS = """
:root {
  color-scheme: light dark;
  --accent: #6b2440; --accent2: #b85c00;
  --card: rgba(127,127,127,.08); --border: rgba(127,127,127,.25);
}
@media (prefers-color-scheme: dark) {
  :root { --accent: #d98aa0; }
}
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.6;
       margin: 0; padding: 0 1.25rem 4rem; }
main { max-width: 960px; margin: 0 auto; }
header.hero { text-align: center; padding: 3.5rem 0 2rem; }
header.hero h1 { font-size: 2.2rem; margin: 0 0 .3rem; letter-spacing: .01em; }
header.hero .sub { font-size: 1.15rem; opacity: .75; margin: 0; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: .3rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1rem; margin-top: 1.5rem; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
        padding: 1.1rem 1.25rem; display: flex; flex-direction: column; }
.card h3 { margin: 0 0 .2rem; font-size: 1.2rem; }
.card .tm { font-size: .7em; vertical-align: super; opacity: .7; }
.card .tagline { margin: 0 0 .5rem; font-weight: 600; font-size: .92rem; color: var(--accent); }
.card .desc { margin: 0 0 .7rem; font-size: .9rem; opacity: .85; }
.card .meta { display: flex; gap: .8rem; font-size: .82rem; opacity: .7; margin-bottom: .7rem; }
.card .actions { margin-top: auto; display: flex; align-items: center; gap: .9rem; }
.card .price { font-weight: 700; }
.price .term { font-size: .68em; font-weight: 400; opacity: .7; white-space: nowrap; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
a.btn { display: inline-block; background: var(--accent); color: #fff; border-radius: 8px;
        padding: .45rem 1.1rem; font-weight: 600; }
a.btn:hover { text-decoration: none; filter: brightness(1.1); }
a.btn.ghost { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
.callout { margin: 1.5rem 0; padding: 1rem 1.2rem; border: 1px solid var(--border);
           border-left: 5px solid var(--accent); border-radius: 10px; background: var(--card); }
.callout p { margin: 0; }
ol li, ul li { margin: .35rem 0; }
code { background: rgba(127,127,127,.15); padding: .12em .4em; border-radius: 5px; }
.repo-url { display: block; text-align: center; font-size: 1.05rem; margin: 1rem 0;
            padding: .6rem; background: rgba(127,127,127,.1); border-radius: 8px; }
footer { margin-top: 3.5rem; padding-top: 1rem; border-top: 1px solid var(--border);
         font-size: .88rem; opacity: .75; text-align: center; }

.notice, .faq, .terms, .callout {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 1rem 1.25rem; margin: 1rem 0;
}
.notice { border-left: 4px solid var(--accent); }
.callout { text-align: center; padding: 1.4rem 1.25rem; }
.callout h2, .callout h3 { margin-top: 0; border-bottom: 0; padding-bottom: 0; }
.faq h3, .terms h3 { margin: 1.1rem 0 .25rem; }
.faq h3:first-child, .terms h3:first-child { margin-top: 0; }
.terms { font-size: .95rem; }
.support-cta { text-align: center; margin-top: 2rem; padding: 1.6rem 1.25rem;
               background: var(--card); border: 1px solid var(--border); border-radius: 10px; }
.support-cta h3 { margin: 0 0 .3rem; font-size: 1.25rem; }
.support-cta p { margin: 0 auto 1rem; max-width: 40rem; opacity: .85; }
"""

FOOTER_HTML = """<footer>
\u00a9 Engineering Dynamics Company. CamMatch\u2122, HVE Toolkit\u2122,
Point Cloud Toolkit\u2122, and Recon Toolkit\u2122 are trademarks of
Engineering Dynamics Company. Blender\u00ae is a registered trademark of the
Blender Foundation. The software is free software under the GNU GPL;
see each product's repository and <a href="/access-and-licensing.html">Access
and licensing</a> for details.
</footer>"""

# Extra CSS for the detail pages (product pages and the support page), on top
# of SHARED_CSS. Kept as a plain string (single braces) so it can be dropped
# straight into an f-string template.
DETAIL_CSS = """
.crumb { margin: 1.4rem 0 0; font-size: .9rem; }
/* Release notes: several screens of headings and lists, so they are set
   apart from the page's own prose and kept from running the full text
   width, which is what makes a long changelog unreadable. */
.notes { border-left: 3px solid var(--accent); padding: .2rem 0 .2rem 1.1rem;
         margin: 1rem 0 2rem; max-width: 46rem; }
.notes h3 { font-size: 1.05rem; margin: 1.4rem 0 .4rem; }
.notes h3:first-child { margin-top: .2rem; }
.notes h4 { font-size: .95rem; margin: 1.1rem 0 .3rem; opacity: .85; }
.notes p, .notes li { font-size: .95rem; }
.notes ul { margin: .3rem 0 .8rem; padding-left: 1.2rem; }
.notes code { background: rgba(0,0,0,.06); border-radius: 4px; padding: .05rem .3rem;
         font-size: .9em; }
.phero { display: grid; grid-template-columns: 1.1fr .9fr; gap: 2rem;
         align-items: center; padding: 2rem 0 1rem; }
@media (max-width: 720px) { .phero { grid-template-columns: 1fr; } }
.phero h1, .support-hero h1 { font-size: 2rem; margin: .2rem 0 .4rem; }
.badge { display: inline-block; font-size: .8rem; font-weight: 600; letter-spacing: .03em;
         color: var(--accent); border: 1px solid var(--accent); border-radius: 999px;
         padding: .15rem .7rem; }
.phero .tagline, .support-hero .tagline { font-size: 1.15rem; font-weight: 600;
         margin: 0 0 .8rem; opacity: .85; }
.phero .actions, .support-hero .actions { display: flex; align-items: center; gap: 1rem;
         margin-top: 1.2rem; flex-wrap: wrap; }
.phero .price { font-size: 1.5rem; font-weight: 700; }
.hero-img { width: 100%; border-radius: 12px; border: 1px solid var(--border); }
.demo-video { width: 100%; border-radius: 12px; border: 1px solid var(--border); background: #000; }
.video-wrap { position: relative; padding-top: 56.25%; border-radius: 12px; overflow: hidden;
  border: 1px solid var(--border); }
.video-wrap iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
.support-hero { padding: 2rem 0 1rem; max-width: 42rem; }
ul.checks { list-style: none; padding: 0; columns: 2; column-gap: 2rem; }
@media (max-width: 720px) { ul.checks { columns: 1; } }
ul.checks li { break-inside: avoid; padding-left: 1.4rem; position: relative; }
ul.checks li::before { content: "✓"; position: absolute; left: 0; color: var(--accent);
                       font-weight: 700; }
.includes { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
            padding: 1.1rem 1.5rem; }
.gallery { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.2rem; margin: .6rem 0 .4rem; }
@media (max-width: 720px) { .gallery { grid-template-columns: 1fr; } }
.shot { margin: 0; border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
        background: var(--card); }
.shot a { display: block; line-height: 0; }
.shot img { width: 100%; height: auto; display: block; background: #000; transition: opacity .15s; }
.shot a:hover img { opacity: .92; }
.shot figcaption { line-height: 1.4; font-size: .9rem; opacity: .85; padding: .7rem .9rem; }
.shot figcaption strong { display: block; opacity: 1; margin-bottom: .1rem; }
"""


def load_products_content():
    """{product id: parsed toml} for every file in content/products/."""
    content = {}
    products_dir = os.path.join(CONTENT_DIR, "products")
    if not os.path.isdir(products_dir):
        return content
    for fname in sorted(os.listdir(products_dir)):
        if fname.endswith(".toml"):
            with open(os.path.join(products_dir, fname), "rb") as fh:
                content[fname[:-5]] = tomllib.load(fh)
    return content


def load_support_content():
    """Parsed content/support.toml (the general donation page), or None."""
    path = os.path.join(CONTENT_DIR, "support.toml")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# Shown next to every paid price. Keep in sync with the license term the
# gateway stamps (LICENSE_TERM_DAYS) and the "Includes" list on the pages.
UPDATE_TERM_SHORT = "1‑yr updates"


def _buy_actions(pid, price, big=False):
    if price and price.strip().lower() == "free":
        # /checkout sends free-only selections to the /register form.
        return ('<span class="price">Free</span>\n'
                f'<a class="btn" href="{PAGES_BASE}/checkout?products={pid}">Get it free</a>')
    if price:
        return (f'<span class="price">{html.escape(price)}'
                f'<span class="term"> · {UPDATE_TERM_SHORT}</span></span>\n'
                f'<a class="btn" href="{PAGES_BASE}/checkout?products={pid}">Buy now</a>')
    return f'<a class="btn ghost" href="{STORE_URL}">See the store</a>'


DISPLAY_ORDER = ["cammatch", "point_cloud_toolkit", "recon_toolkit", "hve_toolkit"]


def render_landing_page(entries, content, support=None):
    def order(e):
        return DISPLAY_ORDER.index(e["id"]) if e["id"] in DISPLAY_ORDER else len(DISPLAY_ORDER)

    cards = []
    for e in sorted(entries, key=order):
        pid = e["id"]
        c = content.get(pid, {})
        learn = (f'<a href="products/{pid}.html">Learn more \u2192</a>'
                 if pid in content else
                 f'<a href="{html.escape(e.get("website", "#"))}">Documentation</a>')
        cards.append(f"""<div class="card">
  <h3>{html.escape(c.get('name', e['name']))}</h3>
  <p class="tagline">{html.escape(c.get('tagline', e.get('tagline', '')))}</p>
  <p class="desc">{html.escape(e.get('tagline', ''))}</p>
  <div class="meta">
    <span>v{html.escape(e['version'])}</span>
    <span>Blender {html.escape(e.get('blender_version_min', '4.2.0'))}+</span>
  </div>
  <p class="desc">{learn}</p>
  <div class="actions">{_buy_actions(pid, c.get('price', ''))}</div>
</div>""")
    cards = "\n".join(cards) or "<p>No published products yet.</p>"
    hero_support_btn = ""
    support_cta = ""
    if support:
        hero_support_btn = (
            ' <a class="btn ghost" href="products/support.html">Support development ♥</a>'
        )
        support_cta = f"""
<div class="support-cta">
  <h3>{html.escape(support.get('name', 'Support development'))}</h3>
  <p>{html.escape(support.get('tagline', ''))}</p>
  <a class="btn ghost" href="products/support.html">Learn more &amp; donate →</a>
</div>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDC Software \u2014 Blender Extensions</title>
<style>{SHARED_CSS}</style>
</head>
<body>
<main>
<header class="hero">
  <h1>EDC Software</h1>
  <p class="sub">Professional Blender tools by Engineering Dynamics Company</p>
  <p style="margin-top:1.2rem"><a class="btn" href="{STORE_URL}">Build your bundle \u2192</a> <a class="btn ghost" href="#install">Add the Blender repository</a>{hero_support_btn}</p>
</header>

<div class="callout">
  <p><strong>One repository, your selected tools.</strong> Build a bundle or register for free products, then use the same access-token workflow in Blender to install every product included with your repository access.</p>
</div>

<div class="grid">
{cards}
</div>
{support_cta}

<h2 id="install">Install &amp; automatic updates</h2>
<p>Add the EDC Software repository to Blender once, and every product installs
from the extensions list and updates automatically:</p>
<ol>
<li>In Blender (4.2 or later), open <strong>Edit \u2192 Preferences \u2192 Get Extensions</strong>.</li>
<li>Open the <strong>Repositories</strong> dropdown (top right) and choose <strong>+ \u2192 Add Remote Repository</strong>.</li>
<li>Paste the repository URL and name it <strong>EDC Software</strong>:</li>
</ol>
<code class="repo-url">{REPOSITORY_URL}</code>
<p>The repository requires an access token &mdash; every product, free or paid,
is delivered this way. Enable <strong>Requires Access Token</strong> on the
repository and paste the token you received into the <strong>Secret</strong>
field. Products included with your repository access then appear under <em>Available</em> to install.
After installing, <strong>make sure the add-on is enabled</strong> &mdash;
installing usually ticks its checkbox, but if its sidebar tab doesn't appear,
enable it under <strong>Edit → Preferences → Add-ons</strong>, then
<strong>Save Preferences</strong> (the <strong>≡</strong> menu at the bottom-left of the
Preferences window) so it stays enabled next time you open Blender. Blender
notifies you when updates are published.</p>
<p>Already have an older <strong>CamMatch</strong> or <strong>HVE Menu</strong> installed the old
way (as a manual add-on)? You can disable or remove it anytime from <strong>Edit &rarr; Preferences
&rarr; Add-ons</strong> &mdash; search for it, untick it to disable, or use <strong>Remove</strong>
to uninstall. The new extension replaces it.</p>

<h2 id="support">Support</h2>
<p>Official builds and updates for these products are provided by
<strong>Engineering Dynamics Company</strong>. Training and support are available
when included with your purchase, repository access, or a separate support
agreement. Visit <a href="https://www.edccorp.com">edccorp.com</a> or contact
EDC support for licensing, training, and assistance.</p>

{FOOTER_HTML}
</main>
</body>
</html>
"""


def render_access_licensing_page():
    """Standalone access and licensing page for repository tokens and terms."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Access &amp; Licensing — EDC Software</title>
<style>{SHARED_CSS}{DETAIL_CSS}</style>
</head>
<body>
<main>
<p class="crumb"><a href="./">← EDC Software</a></p>
<header class="support-hero">
  <span class="badge">Access &amp; licensing</span>
  <h1>Install EDC Software with a repository access token</h1>
  <p class="tagline">One Blender repository, automatic updates, and token-based access for free and paid products.</p>
  <p>EDC Software extensions are delivered through the Blender Extensions repository at
  <code>{REPOSITORY_URL}</code>. Access tokens tell Blender which products your account can install and update.</p>
  <div class="actions"><a class="btn" href="{STORE_URL}">Get access →</a><a class="btn ghost" href="./#install">Installation steps</a></div>
</header>

<section class="notice">
  <strong>Keep your token private.</strong> Treat it like a password: paste it only into Blender's
  <strong>Secret</strong> field for the EDC Software repository, and do not post it in screenshots,
  forum threads, issue reports, or shared project files.
</section>

<h2>How access works</h2>
<ol>
  <li>Choose products in the <a href="{STORE_URL}">EDC Software store</a>. Free products also use this flow so updates stay tied to your token.</li>
  <li>After checkout or registration, copy the access token shown on the confirmation page.</li>
  <li>In Blender 4.2 or later, add <code>{REPOSITORY_URL}</code> as a remote repository.</li>
  <li>Enable <strong>Requires Access Token</strong> and paste your token into the repository <strong>Secret</strong> field.</li>
  <li>Install your licensed products from <strong>Get Extensions</strong>. Blender will notify you when updates are available.</li>
</ol>
<p>Lost the token, or setting up a new computer? <a href="{RECOVER_URL}">Have it emailed
to you</a> — enter the address you bought with and it goes to the address on your account.</p>

<div class="callout">
  <h2>Need another product?</h2>
  <p>Your repository URL stays the same. Add products through the store, then refresh the EDC Software repository in Blender.</p>
  <a class="btn" href="{STORE_URL}">Build your bundle →</a>
</div>

<h2>License terms</h2>
<div class="terms">
  <h3>Paid products</h3>
  <p>Paid purchases include the listed product, repository access, and one year of updates for that product unless a product page states a different term.</p>
  <h3>Free products</h3>
  <p>Free products may require registration so the repository can issue a token and deliver updates reliably.</p>
  <h3>After the update term</h3>
  <p>Installed versions keep working after an update term ends. Renewing access restores eligibility for newly published updates.</p>
  <h3>Software license</h3>
  <p>The extensions are free software under the GNU GPL. Commercial purchases support official builds, packaging, documentation, updates, and support from Engineering Dynamics Company.</p>
</div>

<h2>Frequently asked questions</h2>
<div class="faq">
  <h3>Do I need a separate repository URL for each product?</h3>
  <p>No. Use <code>{REPOSITORY_URL}</code> for every EDC Software product. Your token controls which products appear.</p>
  <h3>Where do I paste the token?</h3>
  <p>Paste it into the <strong>Secret</strong> field after enabling <strong>Requires Access Token</strong> on the EDC Software repository in Blender preferences.</p>
  <h3>Can I replace an old manual add-on install?</h3>
  <p>Yes. Disable or remove the old add-on from <strong>Edit → Preferences → Add-ons</strong>, then install the extension version from the EDC Software repository.</p>
  <h3>I lost my access token. How do I get it back?</h3>
  <p><a href="{RECOVER_URL}">Recover it here</a>. Enter the email address you bought with
  and the token is sent to the address on your account — it is never shown on the page, so
  a token cannot be read off it by someone who only knows your email. If nothing arrives,
  check spam, then contact EDC support.</p>
  <h3>Who should I contact for help?</h3>
  <p>Contact EDC support for licensing, training, and installation assistance.</p>
</div>

{FOOTER_HTML}
</main>
</body>
</html>
"""

def render_notes(text):
    """Release notes as HTML, from the Markdown subset the changelogs use.

    Each product's CHANGELOG.md section reaches this as the GitHub release
    body: headings, bold, inline code, bullet lists, paragraphs. Anything
    else is escaped and shown as text rather than dropped -- a note that
    renders plainly still tells the reader what changed, whereas a silently
    swallowed line does not.

    Headings shift down one level: the page already spends h2 on its own
    sections, so the notes' own ## sits under "What's new" rather than
    beside it.
    """
    def inline(chunk):
        out = html.escape(chunk)
        out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
        out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
        return out

    html_parts = []
    bullets = []

    def flush_bullets():
        if bullets:
            items = "\n".join(f"<li>{inline(b)}</li>" for b in bullets)
            html_parts.append(f"<ul>\n{items}\n</ul>")
            bullets.clear()

    paragraph = []

    def flush_paragraph():
        if paragraph:
            html_parts.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
            continue
        if stripped.startswith("### "):
            flush_paragraph(); flush_bullets()
            html_parts.append(f"<h4>{inline(stripped[4:])}</h4>")
        elif stripped.startswith("## "):
            flush_paragraph(); flush_bullets()
            html_parts.append(f"<h3>{inline(stripped[3:])}</h3>")
        elif stripped.startswith("- "):
            flush_paragraph()
            bullets.append(stripped[2:])
        elif bullets:
            # A wrapped continuation of the bullet above it.
            bullets[-1] = f"{bullets[-1]} {stripped}"
        else:
            paragraph.append(stripped)
    flush_paragraph()
    flush_bullets()
    return "\n".join(html_parts)


def render_product_page(pid, c, release):
    """One product page from its content/products/<pid>.toml."""
    def esc(key, default=""):
        return html.escape(str(c.get(key, default)))

    def paragraphs(text):
        return "\n".join(f"<p>{html.escape(p.strip())}</p>"
                         for p in text.split("\n\n") if p.strip())

    bmin = html.escape(str(c.get("blender_min", "4.2")))
    donate = ""
    support_notice = ""
    if c.get("support_notice"):
        support_notice = (
            f'<section class="notice"><strong>Compatibility notice:</strong> '
            f'{html.escape(str(c["support_notice"]))}</section>'
        )
    access_link = (
        '\n<a class="btn ghost" href="../access-and-licensing.html">'
        'Repository Access &amp; Licensing</a>'
    )
    if c.get("donate_url"):
        donate = (f'\n<a class="btn ghost" href="{html.escape(c["donate_url"])}">'
                  f'{esc("donate_label", "Support development ♥")}</a>')
    if c.get("sample_report"):
        donate += (f'\n<a class="btn ghost" href="../assets/{html.escape(c["sample_report"])}" '
                   f'target="_blank" rel="noopener">{esc("sample_report_label", "See a sample report")}</a>')
    hero_img = ""
    if c.get("hero_image"):
        hero_img = (f'<img class="hero-img" src="../assets/{html.escape(c["hero_image"])}" '
                    f'alt="{esc("name")}">')

    # Demo video: a self-hosted file in content/assets/ (video = "clip.mp4",
    # optional video_poster = "poster.jpg") or a YouTube embed (youtube = "ID").
    video_block = ""
    vheading = html.escape(c.get("video_title", "See it in action"))
    if c.get("youtube"):
        yt = html.escape(str(c["youtube"]).rsplit("/", 1)[-1].split("=")[-1])
        video_block = (
            f'<h2>{vheading}</h2>\n<div class="video-wrap"><iframe '
            f'src="https://www.youtube-nocookie.com/embed/{yt}" title="{esc("name")} demo" '
            'loading="lazy" allow="fullscreen; picture-in-picture" allowfullscreen></iframe></div>'
        )
    elif c.get("video"):
        poster = (f' poster="../assets/{html.escape(c["video_poster"])}"'
                  if c.get("video_poster") else "")
        video_block = (
            f'<h2>{vheading}</h2>\n<video class="demo-video" controls preload="metadata"'
            f'{poster}>\n<source src="../assets/{html.escape(c["video"])}" type="video/mp4">\n'
            "Your browser can't play this video.</video>"
        )
    # Screenshot gallery: [[gallery]] entries with image + optional title/caption.
    gallery_block = ""
    gitems = [g for g in c.get("gallery", []) if g.get("image")]
    if gitems:
        gheading = html.escape(c.get("gallery_title", "See it in action"))
        cards = []
        for g in gitems:
            src = f'../assets/{html.escape(g["image"])}'
            alt = html.escape(g.get("title") or g.get("caption", ""))
            cap = ""
            if g.get("title"):
                cap += f'<strong>{html.escape(g["title"])}</strong>'
            if g.get("caption"):
                cap += html.escape(g["caption"])
            cards.append(
                f'<figure class="shot"><a href="{src}" target="_blank" rel="noopener">'
                f'<img src="{src}" alt="{alt}" loading="lazy"></a>'
                f'<figcaption>{cap}</figcaption></figure>'
            )
        gallery_block = f'<h2>{gheading}</h2>\n<div class="gallery">\n' + "\n".join(cards) + "\n</div>"

    features = "\n".join(f"<li>{html.escape(f)}</li>" for f in c.get("features", []))
    includes = "\n".join(f"<li>{html.escape(i)}</li>" for i in c.get("includes", []))
    requirements = "\n".join(f"<li>{html.escape(r)}</li>" for r in c.get("requirements", []))
    sections = "\n".join(
        f"<h2>{html.escape(s.get('title', ''))}</h2>\n{paragraphs(s.get('body', ''))}"
        for s in c.get("sections", [])
    )
    version_box = ""
    if release:
        # The notes used to be suppressed here because they were GitHub's
        # pull-request-derived auto-notes, which referenced pre-launch
        # internal work. They are hand-written changelog sections now, so
        # the reason to hide them is gone -- and someone deciding whether to
        # update has nowhere else to read what changed.
        published = html.escape(release.get("published", ""))
        released_on = f" &middot; released {published}" if published else ""
        notes = render_notes(release.get("notes", ""))
        whats_new = f'<h2>What\u2019s new</h2>\n<div class="notes">{notes}</div>' if notes else ""
        version_box = f"""<h2>Latest version</h2>
<p><strong>v{html.escape(release['version'])}</strong>{released_on}
&middot; delivered through the <a href="{PAGES_BASE}/">EDC Software repository</a>
with automatic update notifications in Blender.</p>
{whats_new}"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc('name')} \u2014 EDC Software</title>
<style>{SHARED_CSS}{DETAIL_CSS}</style>
</head>
<body>
<main>
<p class="crumb"><a href="../">\u2190 EDC Software</a></p>
<div class="phero">
  <div>
    <span class="badge">{esc('badge')}</span>
    <h1>{esc('name')}</h1>
    <p class="tagline">{esc('tagline')}</p>
    {paragraphs(c.get('pitch', ''))}
    <div class="actions">{_buy_actions(pid, c.get('price', ''), big=True)}{access_link}{donate}</div>
  </div>
  <div>{hero_img}</div>
</div>

{support_notice}

{video_block}

{gallery_block}

<h2>Capabilities</h2>
<ul class="checks">
{features}
</ul>

<h2>What’s included</h2>
<p>Your purchase provides access to the product’s repository and official
update channel for the stated update term. It is not a proprietary software
license; the installed add-on remains free software under the GNU GPL.</p>
<div class="includes"><ul class="checks">
{includes}
</ul>
<p><a href="../access-and-licensing.html">Learn more about repository access &amp; licensing →</a></p>
</div>

{sections}

<h2>Requirements</h2>
<ul>
{requirements}
</ul>

{version_box}

<h2>Install &amp; updates</h2>
<p>Your repository secret is delivered instantly &mdash; it appears on the
confirmation page as soon as you check out (and free products issue one after
a quick sign-up). Then, in Blender {bmin} or newer:</p>
<ol>
<li>Open <strong>Edit \u2192 Preferences \u2192 Get Extensions</strong>.</li>
<li>Open the <strong>Repositories</strong> dropdown (top right) \u2192
<strong>+ \u2192 Add Remote Repository</strong>, and paste
<code>{PAGES_BASE}/index.json</code>.</li>
<li><strong>Enable "Requires Access Token"</strong> on that repository and
<strong>paste the repository secret you received into the Secret field</strong>.</li>
<li>Your products appear under <em>Available</em> &mdash; click <strong>Install</strong>.</li>
<li><strong>Make sure the add-on is enabled.</strong> Installing usually ticks its
checkbox for you, but if the product's sidebar tab doesn't appear, open
<strong>Edit → Preferences → Add-ons</strong>, search for the product,
and tick its checkbox to activate it. Updates then arrive automatically.</li>
<li><strong>Save your preferences</strong> so this sticks. Blender normally saves
them automatically; if it doesn't, open the <strong>≡</strong> menu at the bottom-left
of the Preferences window and choose <strong>Save Preferences</strong> &mdash; otherwise
you may have to re-enable the add-on next time you open Blender.</li>
</ol>
<p>Upgrading from an older, manually-installed version of this add-on? You can disable or remove
it anytime from <strong>Edit &rarr; Preferences &rarr; Add-ons</strong> &mdash; search for it,
untick it to disable, or use <strong>Remove</strong> to uninstall. The new extension replaces it.</p>
<p>Because the installed add-on is GPL-licensed free software, copies you have
already received remain yours to use, study, modify, and share under the GPL.
When the update term ends, repository access for future official builds and
automatic updates may require renewal.</p>

{FOOTER_HTML}
</main>
</body>
</html>
"""


def render_support_page(c):
    """The general 'Support development' donation page from content/support.toml.

    A peer of the product pages (rendered to products/support.html) but with
    no price, version, or install section — its call to action is the
    donation button pointing at the pay-what-you-want Stripe link.
    """
    def esc(key, default=""):
        return html.escape(str(c.get(key, default)))

    def paragraphs(text):
        return "\n".join(f"<p>{html.escape(p.strip())}</p>"
                         for p in text.split("\n\n") if p.strip())

    donate_url = c.get("donate_url", "")
    donate = (f'<a class="btn" href="{html.escape(donate_url)}">'
              f'{esc("donate_label", "Support development ♥")}</a>') if donate_url else ""
    hero_img = ""
    if c.get("hero_image"):
        hero_img = (f'<img class="hero-img" src="../assets/{html.escape(c["hero_image"])}" '
                    f'alt="{esc("name")}">')
    funds = "\n".join(f"<li>{html.escape(f)}</li>" for f in c.get("funds", []))
    funds_box = (f'<h2>What your support funds</h2>\n<ul class="checks">\n{funds}\n</ul>'
                 if funds else "")
    sections = "\n".join(
        f"<h2>{html.escape(s.get('title', ''))}</h2>\n{paragraphs(s.get('body', ''))}"
        for s in c.get("sections", [])
    )
    intro = f"""<span class="badge">{esc('badge', 'Support development')}</span>
<h1>{esc('name')}</h1>
<p class="tagline">{esc('tagline')}</p>
{paragraphs(c.get('pitch', ''))}
<div class="actions">{donate}</div>"""
    if hero_img:
        hero = f'<div class="phero"><div>{intro}</div><div>{hero_img}</div></div>'
    else:
        hero = f'<div class="support-hero">{intro}</div>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc('name')} — EDC Software</title>
<style>{SHARED_CSS}{DETAIL_CSS}</style>
</head>
<body>
<main>
<p class="crumb"><a href="../">← EDC Software</a></p>
{hero}

{funds_box}

{sections}

{FOOTER_HTML}
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- HVE 2027
#
# hve.edccorp.com: HVE's own landing page and downloads. The gateway serves it
# as "/" for that host (and at /hve on any host). The release comes from
# edccorp/hve-next: the newest PUBLISHED one, never a draft -- the release
# workflow ends with a draft precisely so nothing is announced before someone
# publishes it, and this page is an announcement.

HVE_REPO = "edccorp/hve-next"
#: Absolute for the same reason as RECOVER_URL: Pages serves these files too.
HVE_BASE = "https://hve.edccorp.com"
OFFLINE_URL = f"{HVE_BASE}/offline"

#: Which download a release asset is, by its name. Order is display order.
HVE_PLATFORMS = (
    ("windows", "Windows", "64-bit installer",
     lambda n: n.lower().endswith("-setup.exe")),
    ("linux", "Linux", "64-bit .tar.gz",
     lambda n: n.lower().endswith(".tar.gz") and "linux" in n.lower()),
    ("macos", "macOS", "disk image",
     lambda n: n.lower().endswith(".dmg")),
)


def _brand_svg(name):
    """An EDC brand SVG from content/assets, for inlining: its maroon becomes
    currentColor so the page's CSS can lighten it in dark mode (an <img>
    cannot be recoloured, and maroon on a dark page all but vanishes)."""
    path = os.path.join(CONTENT_DIR, "assets", name)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("#6b0739", "currentColor")


def load_hve_content():
    path = os.path.join(CONTENT_DIR, "hve.toml")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def hve_downloads(release):
    """{filename: {...}} for the release's downloadable files: what the page
    lists and what the gateway may stream. Empty without a release."""
    out = {}
    if not release:
        return out
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        platform = next((p[0] for p in HVE_PLATFORMS if p[3](name)), None)
        if platform is None and name != "SHA256SUMS.txt":
            continue
        digest = asset.get("digest") or ""
        out[name] = {
            "repo": HVE_REPO,
            "asset_id": asset.get("id"),
            "size": asset.get("size", 0),
            "platform": platform or "checksums",
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
        }
    return out


def _mb(size):
    return f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{max(size, 1) / 1e3:.0f} KB"


def render_hve_page(c, release, downloads):
    """HVE's landing page from content/hve.toml and its newest published
    release. With no release it still renders, saying downloads open then."""
    def esc(key, default=""):
        return html.escape(str(c.get(key, default)))

    def paragraphs(text):
        return "\n".join(f"<p>{html.escape(p.strip())}</p>"
                         for p in text.split("\n\n") if p.strip())

    version = (release or {}).get("tag_name", "").lstrip("v")
    published = ((release or {}).get("published_at") or "")[:10]
    cards = []
    for key, label, kind, _match in HVE_PLATFORMS:
        files = [(n, d) for n, d in downloads.items() if d["platform"] == key]
        if files:
            name, d = files[0]
            sha = (f'<p class="sha">SHA-256 <code>{html.escape(d["sha256"])}</code></p>'
                   if d["sha256"] else "")
            action = (f'<a class="btn" href="{HVE_BASE}/download/{html.escape(name)}">'
                      f'Download</a>')
            meta = f"{html.escape(kind)} &middot; {_mb(d['size'])}"
        else:
            sha = ""
            action = '<span class="soon">Not available yet</span>'
            meta = html.escape(kind)
        cards.append(f"""<div class="card dl">
  <h3>{label}</h3>
  <div class="meta"><span>{meta}</span></div>
  {sha}
  <div class="actions">{action}</div>
</div>""")
    sums = downloads.get("SHA256SUMS.txt")
    sums_link = (f' &middot; <a href="{HVE_BASE}/download/SHA256SUMS.txt">SHA256SUMS.txt</a>'
                 if sums else "")
    if release:
        status = (f'<p><strong>Version {html.escape(version)}</strong>'
                  f'{" &middot; released " + html.escape(published) if published else ""}'
                  f'{sums_link}</p>')
        notes = render_notes(release.get("body") or "")
        whats_new = f'<h2>What’s new</h2>\n<div class="notes">{notes}</div>' if notes else ""
    else:
        status = ('<div class="notice"><strong>Downloads open at release.</strong> '
                  'HVE 2027 is in testing; this page will offer it here as soon as it is '
                  'released.</div>')
        whats_new = ""

    features = "\n".join(f"<li>{html.escape(f)}</li>" for f in c.get("features", []))
    requirements = "\n".join(f"<li>{html.escape(r)}</li>" for r in c.get("requirements", []))
    faq = "\n".join(f"<h3>{html.escape(q.get('q', ''))}</h3>\n{paragraphs(q.get('a', ''))}"
                    for q in c.get("faq", []))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc('name')} — Engineering Dynamics Company</title>
<link rel="icon" href="assets/hve-cube.svg" type="image/svg+xml">
<style>{SHARED_CSS}{DETAIL_CSS}
:root {{ --accent: #6b0739; --brand: #6b0739; }}
@media (prefers-color-scheme: dark) {{ :root {{ --accent: #e0a3b6; --brand: #e0a3b6; }}
  a.btn:not(.ghost) {{ color: #2a0716; }} }}
.topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem;
          padding: 1.1rem 0 .9rem; border-bottom: 1px solid var(--border); }}
.topbar .lockup {{ color: var(--brand); display: block; line-height: 0; }}
.topbar .lockup svg {{ height: 44px; width: auto; }}
.topbar nav {{ display: flex; gap: 1.2rem; font-size: .95rem; flex-wrap: wrap; }}
.cube {{ width: 100%; max-width: 230px; display: block; margin: 0 auto; }}
@media (max-width: 720px) {{ .topbar .lockup svg {{ height: 36px; }}
  .topbar nav {{ display: none; }} .cube {{ max-width: 140px; }} }}
footer .mark {{ color: var(--brand); display: inline-block; line-height: 0; margin-bottom: .5rem; }}
footer .mark svg {{ height: 28px; width: auto; }}
.dl h3 {{ margin-bottom: .1rem; }}
.dl .sha {{ font-size: .72rem; opacity: .7; overflow-wrap: anywhere; margin: 0 0 .7rem; }}
.dl .sha code {{ background: none; padding: 0; }}
.soon {{ opacity: .65; font-style: italic; }}
</style>
</head>
<body>
<main>
<header class="topbar">
  <a class="lockup" href="https://www.edccorp.com">{_brand_svg("edc-lockup.svg")}</a>
  <nav><a href="#download">Download</a><a href="#licensing">Licensing</a>
  <a href="#support">Support</a><a href="{PAGES_BASE}/">EDC Software</a></nav>
</header>
<div class="phero">
  <div>
    <span class="badge">{esc('badge')}</span>
    <h1>{esc('name')}</h1>
    <p class="tagline">{esc('tagline')}</p>
    {paragraphs(c.get('pitch', ''))}
    <div class="actions"><a class="btn" href="#download">Download</a>
    <a class="btn ghost" href="#licensing">Licensing</a></div>
  </div>
  <div><img class="cube" src="assets/hve-cube.svg" alt="HVE"></div>
</div>

<h2 id="download">Download</h2>
{status}
<div class="grid">
{chr(10).join(cards)}
</div>
<p>Downloading needs your EDC Software key, the same one that installs the
EDC Blender tools. Lost it? <a href="{RECOVER_URL}">Have it emailed to you</a>.</p>

{whats_new}

<h2>Capabilities</h2>
<ul class="checks">
{features}
</ul>

<h2 id="licensing">Licensing</h2>
{paragraphs(c.get('licensing', ''))}
<p><a href="{OFFLINE_URL}">Activate a computer without internet, or release one you no
longer have →</a></p>

<h2>Requirements</h2>
<ul>
{requirements}
</ul>

<h2>Questions</h2>
<div class="faq">
{faq}
</div>

<h2 id="support">Support</h2>
<p>HVE is developed and supported by <strong>Engineering Dynamics Company</strong>.
Visit <a href="https://www.edccorp.com">edccorp.com</a> or contact EDC support for
licensing, training and assistance.</p>

<footer>
<a class="mark" href="https://www.edccorp.com">{_brand_svg("edc-mark.svg")}</a><br>
© Engineering Dynamics Company. Blender® is a registered trademark of the
Blender Foundation.
</footer>
</main>
</body>
</html>
"""


def write_hve_site(out_dir):
    """hve.html and hve-downloads.json. The page is written with or without a
    release; the JSON is what the gateway's /download route may stream."""
    c = load_hve_content()
    if c is None:
        return False
    release = _newest_release(HVE_REPO)
    downloads = hve_downloads(release)
    with open(os.path.join(out_dir, "hve.html"), "w", encoding="utf-8") as fh:
        fh.write(render_hve_page(c, release, downloads))
    with open(os.path.join(out_dir, "hve-downloads.json"), "w", encoding="utf-8") as fh:
        json.dump(downloads, fh, indent=2)
        fh.write("\n")
    print(f"Wrote {out_dir}/hve.html "
          + (f"({release.get('tag_name')}, {len(downloads)} file(s))" if release
             else "(no published release: downloads open at release)"))
    return True


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "site"
    os.makedirs(out_dir, exist_ok=True)
    entries, packages, releases = build_entries(out_dir)
    content = load_products_content()
    support = load_support_content()
    index = {"version": "v1", "blocklist": [], "data": entries}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "packages.json"), "w", encoding="utf-8") as fh:
        json.dump(packages, fh, indent=2)
        fh.write("\n")
    # index.json / packages.json keep every product (hidden ones stay
    # installable by an entitled token); the public site does not.
    public_entries = [e for e in entries if e.get("id") not in HIDDEN_PRODUCTS]
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_landing_page(public_entries, content, support))
    with open(os.path.join(out_dir, "access-and-licensing.html"), "w", encoding="utf-8") as fh:
        fh.write(render_access_licensing_page())

    pages_dir = os.path.join(out_dir, "products")
    os.makedirs(pages_dir, exist_ok=True)
    for pid, c in content.items():
        if pid in HIDDEN_PRODUCTS:
            continue  # no public product page for internal/beta tools
        with open(os.path.join(pages_dir, f"{pid}.html"), "w", encoding="utf-8") as fh:
            fh.write(render_product_page(pid, c, releases.get(pid)))
    if support:
        with open(os.path.join(pages_dir, "support.html"), "w", encoding="utf-8") as fh:
            fh.write(render_support_page(support))
    write_hve_site(out_dir)
    assets_src = os.path.join(CONTENT_DIR, "assets")
    if os.path.isdir(assets_src):
        shutil.copytree(assets_src, os.path.join(out_dir, "assets"), dirs_exist_ok=True)
    print(
        f"Wrote {out_dir}/index.json ({len(entries)} extension(s)), index.html, "
        f"and {len(content)} product page(s)"
    )


if __name__ == "__main__":
    main()
