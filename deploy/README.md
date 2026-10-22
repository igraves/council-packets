# Deploying the subdivision map

The map (`map/`) and the images of the documents it refers to, as static files in a private S3
bucket served by CloudFront over HTTPS at a subdomain of an existing Route 53 zone.

The document images name the parties to each instrument. They are public county records, but
they stay out of git: `pvc map-bundle` renders every page of the documents the map links (281
documents, 1,280 pages) as grey AVIF at two sizes, full (1600 px, the scans' own resolution,
~48 KB a page) and thumbnail (360 px, ~3 KB), into `deploy/bundle/site/`, which is gitignored:
about 70 MB in all, against 258 MB for the PDFs. Pages are cached in
`deeds/cache/pages/map-avif/`, so a rebuild renders only new pages.

`deploy/bundle/site/` is laid out as it is served: `index.html`, `config.js`, the GeoJSON,
`highlights.json`, and `docs/index.json` with `docs/<slug>/<page>-<thumb|full>.avif`.

## Build

```sh
uv run pvc subdivision-map          # rebuild map/*.geojson if the data changed
uv run pvc map-bundle               # → deploy/bundle/site/ (needs MAPBOX_TOKEN in .env)
```

The Mapbox token is sent to every browser that opens the map. In the Mapbox account, restrict it
to the site's URL.

## Deploy (CDK, TypeScript)

```sh
cd deploy/cdk
npm ci
npx cdk bootstrap aws://<account>/us-east-1   # once per account
npx cdk deploy -c zoneName=example.com -c zoneId=Z0123456789ABC -c subdomain=pv-map
```

- `zoneName` / `zoneId`: the existing Route 53 hosted zone the subdomain goes in.
- `subdomain`: the record's first label; the site is `https://<subdomain>.<zoneName>/`.

The stack lives in us-east-1, because CloudFront only uses certificates from there. It makes the
certificate (validated by DNS in the zone), the bucket (readable only by the distribution), the
distribution and the A/AAAA alias records. `cdk deploy` uploads `deploy/bundle/site/` in three
parts: the page images as `image/avif` cached for a week; the GeoJSON as `application/json`;
and the rest. All but the images are rechecked on each visit, and each deploy invalidates the
distribution's cache, so changes show at once. Run `pvc map-bundle` before each deploy. To remove
everything: `npx cdk destroy` with the same `-c` values (the bucket is emptied and deleted).

Cost: well under a dollar a month at this site's traffic. Storage is ~70 MB, and CloudFront's
free allowance (1 TB out and 10 million requests a month) covers the rest. There is no server,
load balancer or public IPv4 address to pay for.

## Running it in a container instead

`deploy/Dockerfile` (copied into the bundle) serves the same files with
`src/pvcouncil/mapserve.py`, which needs only the Python standard library:

```sh
docker build --platform linux/arm64 -t pv-map deploy/bundle
docker run --rm -p 127.0.0.1:8080:8080 pv-map   # http://127.0.0.1:8080/
```
