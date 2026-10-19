# Vendored frontend dependencies

These files are served by the application from `/static/vendor/` through an
explicit allowlist in `src/main.py`. They are pinned copies of upstream
releases, used so the UI does not depend on a CDN being reachable (offline LAN,
Opera/ad-blocker environments).

| File | Version | Upstream | License |
|---|---|---|---|
| `leaflet.js`, `leaflet.css`, `images/*` | 1.9.4 | https://leafletjs.com/ | BSD-2-Clause |
| `dom-to-image-more.min.js` | 3.4.5 | https://github.com/1904labs/dom-to-image-more | MIT |

Leaflet attribution prefix is disabled in the UI, but the required
OpenStreetMap attribution remains visible. Do not remove OSM attribution.
