Third-party browser files served by the dashboard, so the site never loads scripts from a CDN.

The Docker image downloads them itself (see Dockerfile). For local development
without Docker, download them once (see README, "Frontend files"):

  htmx.min.js   https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
