# Deploying the subdivision map

The map (`map/`) and the images of the documents it refers to, served by
`src/pvcouncil/mapserve.py` from an arm64 container on ECS Fargate behind an HTTPS load balancer.

The document images name the parties to each instrument. They are public county records, but
they stay out of git: `pvc map-bundle` renders every page of the documents the map links (281
documents, 1,280 pages) as grey AVIF at two sizes, full (1600 px, the scans' own resolution,
~48 KB a page) and thumbnail (360 px, ~3 KB), into `deploy/bundle/`, which is gitignored: about
70 MB in all, against 258 MB for the PDFs. No PDFs go into the image, and the server needs only
the Python standard library. Pages are cached in `deeds/cache/pages/map-avif/`, so a rebuild
renders only new pages.

## Build

```sh
uv run pvc subdivision-map          # rebuild map/*.geojson if the data changed
uv run pvc map-bundle               # → deploy/bundle/ (needs MAPBOX_TOKEN in .env)
docker build --platform linux/arm64 -t pv-map deploy/bundle
docker run --rm -p 127.0.0.1:8080:8080 pv-map   # try it at http://127.0.0.1:8080/
```

The Mapbox token is sent to every browser that opens the map. In the Mapbox account, restrict it
to the site's URL.

## Deploy (CDK, TypeScript)

```sh
cd deploy/cdk
npm ci
npx cdk bootstrap                   # once per account and region
npx cdk deploy -c zoneName=example.com -c zoneId=Z0123456789ABC -c subdomain=pv-map
```

- `zoneName` / `zoneId`: the existing Route 53 hosted zone the subdomain goes in.
- `subdomain`: the record's first label; the site is `https://<subdomain>.<zoneName>/`.
- `vpcId` (optional): an existing VPC with public subnets. Without it the stack creates a two-AZ
  VPC with public subnets only (no NAT gateway); tasks get a public IP to pull the image, and
  their security group admits only the load balancer.

`cdk deploy` builds the image from `deploy/bundle/` for linux/arm64, pushes it to the CDK asset
repository in ECR and rolls the service. Run `pvc map-bundle` before each deploy so the image has
the current map. To remove everything: `npx cdk destroy` with the same `-c` values.

Roughly: one 0.5 vCPU / 1 GB ARM Fargate task, one Application Load Balancer, a log group with
one month's retention, and the hosted zone you already have.
