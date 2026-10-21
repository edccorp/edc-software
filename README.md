# EDC Software — Blender Extensions Repository

The Blender extensions repository for the products of **Engineering
Dynamics Company**: add it to Blender once, and Blender offers installs
and automatic update notifications for every EDC product.

| Product | Repository |
|---------|------------|
| CamMatch™ | [edccorp/CamMatch](https://github.com/edccorp/CamMatch) |
| HVE Toolkit™ | [edccorp/HVEToolkit](https://github.com/edccorp/HVEToolkit) |
| Point Cloud Toolkit™ | [edccorp/PointCloudToolkit](https://github.com/edccorp/PointCloudToolkit) |
| Recon Toolkit™ | [edccorp/ReconToolkit](https://github.com/edccorp/ReconToolkit) |

## Add to Blender (4.2+)

1. Open **Edit → Preferences → Get Extensions**.
2. Open the **Repositories** dropdown (top-right) and click **+ → Add Remote Repository**.
3. Paste the repository URL:

   ```
   https://extensions.edccorp.com/index.json
   ```

4. Name it **EDC Software** and confirm.

The EDC products then appear in the extensions list to install, and
Blender notifies you when updates are available.

## How it works

- `tools/build_index.py` fetches the **latest GitHub release** of each
  product, downloads its zip, reads the `blender_manifest.toml` inside it
  (the manifest is the source of truth for id/name/version), and writes
  `index.json` in Blender's remote-repository format plus an `index.html`
  landing page.
- The add-on zips are **not** published on the public site. The workflow
  runs with `MIRROR_ZIPS=0` (gateway mode): `index.json` points at
  `/packages/*.zip` URLs, and the Railway gateway at
  `extensions.edccorp.com` serves those URLs to authenticated customers by
  streaming the private GitHub release assets (see
  [`gateway/README.md`](gateway/README.md)). Setting `MIRROR_ZIPS=1`
  instead copies the zips onto the public Pages site, which makes them
  downloadable by anyone.
- **Required setup while the product repos are private:** add a
  `PRODUCTS_TOKEN` repository secret (a fine-grained personal access
  token with **Contents: read** on every product repo, hidden ones
  included). The default
  workflow token can only see this repo, so without the secret every
  product is skipped and the index publishes empty.
- The **Publish extensions index** workflow runs the script and deploys
  the result to GitHub Pages. It runs every 6 hours, on every push to
  `main`, and on demand (**Actions → Publish extensions index → Run
  workflow**) — run it manually right after cutting a product release to
  publish the update immediately.
- Products whose latest release zip contains no manifest (releases cut
  before extension support) are skipped with a log message and appear
  automatically once a new release is published.
- **The publish refuses to drop a product.** Before writing anything, the
  build compares its products with the live index and fails the run if
  any would disappear, or if it read none at all. Pages then keeps serving
  the last good index. A revoked or expired `PRODUCTS_TOKEN` used to skip
  every product and publish an empty index, so every customer's Blender
  listed nothing. To remove a product on purpose, run **Publish extensions
  index** by hand with **Allow Drop** ticked.

To publish instantly from a product repo instead of waiting for the
schedule, send a `repository_dispatch` event (type `product-release`) to
this repo from the product's release workflow.

## Maintainer notes

- Add a new product by appending its `owner/repo` to `PRODUCTS` in
  `tools/build_index.py`, and add the repo to both the `PRODUCTS_TOKEN`
  Actions secret and the gateway's `GH_TOKEN` PAT.
- Keep a product off the public site (internal or beta tools) by adding
  its manifest `id` to `HIDDEN_PRODUCTS`. It still ships in `index.json`
  and `packages.json`, so a repository secret entitled to it installs and
  updates it in Blender as usual — it just gets no catalog listing and no
  product page.
- If a product publishes its releases as GitHub **pre-releases**, also add
  its `owner/repo` to `PRERELEASE_PRODUCTS`; otherwise the "latest
  release" lookup finds nothing and the product is skipped.
- Test locally: `python tools/build_index.py /tmp/site` (set
  `GITHUB_TOKEN` to avoid API rate limits), then open `/tmp/site/index.html`.
- The site is served at the custom domain `extensions.edccorp.com`
  (configured in the Pages settings); the underlying
  `edccorp.github.io/edc-software` URL redirects to it. If the
  domain ever changes, update `PAGES_BASE` in `tools/build_index.py`.
