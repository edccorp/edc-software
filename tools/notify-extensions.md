# Making product releases appear in Blender promptly

The extensions index (`index.json`, served at extensions.edccorp.com) is
rebuilt by `.github/workflows/publish.yml`, which reads each product
repo's **latest release**. Cutting a product release does **not** rebuild
the index by itself — so there is a lag between publishing a release and
customers seeing it in Blender.

The index rebuilds when any of these happen:

- **Hourly schedule** (cron) — the default; worst-case lag ~1 hour.
- **Manual**: Actions → *Publish extensions index* → *Run workflow*.
- **repository_dispatch** (type `product-release`) — instant, and the
  recommended setup below.

## Instant propagation (recommended)

Have each product repo ping this repo the moment it publishes a release.
`publish.yml` already listens for `repository_dispatch: [product-release]`.

**One-time setup:**

1. Create a fine-grained PAT (github.com → Settings → Developer settings →
   Fine-grained tokens) with **Contents: read and write** on
   **`edccorp/edc-software`** only. Call it e.g. `EXTENSIONS_DISPATCH`.
2. Add it as an Actions secret named `EXTENSIONS_DISPATCH_TOKEN` in each
   product repo (CamMatch, EDCHVEToolkit, EDCPointCloudToolkit,
   EDCReconToolkit) — Settings → Secrets and variables → Actions.
3. Add this final step to each product repo's `.github/workflows/release.yml`,
   after the "Publish release" step:

   ```yaml
   - name: Notify the extensions index
     env:
       GH_TOKEN: ${{ secrets.EXTENSIONS_DISPATCH_TOKEN }}
     run: |
       gh api repos/edccorp/edc-software/dispatches \
         -f event_type=product-release
   ```

After that, publishing a release triggers an index rebuild within ~1
minute; customers get the update on their next Blender refresh.

## Note: the product repos were renamed

`PointCloudToolkit → EDCPointCloudToolkit`, `ReconToolkit →
EDCReconToolkit`, `HVEToolkit → EDCHVEToolkit` (CamMatch unchanged).
GitHub redirects the old names, so `tools/build_index.py`'s `PRODUCTS`
list (still the old names) keeps working — but that redirect is only
guaranteed until an old name is reused. When convenient, update
`PRODUCTS` to the canonical names so the index build and the gateway's
download resolution (`packages.json`) don't depend on redirects.
