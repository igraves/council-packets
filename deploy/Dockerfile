# The subdivision map and its documents' page images in a container (linux/arm64). The site itself
# is deployed as static files to S3 + CloudFront (deploy/cdk); this image runs the same files, for
# trying the site locally or hosting it on a container service instead.
# Build context: deploy/bundle/, made by `uv run pvc map-bundle` (gitignored: the pages name the
# parties). The pages are pre-rendered AVIF, so the server needs only the standard library.
# Build by hand with
#   docker build --platform linux/arm64 -t pv-map deploy/bundle
FROM python:3.12-slim

RUN useradd --system --uid 10001 --home-dir /srv app

WORKDIR /srv
COPY mapserve.py ./
COPY site/ site/

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "mapserve.py", "--map-dir", "site", "--pages-dir", "site/docs", "--host", "0.0.0.0", "--port", "8080"]
