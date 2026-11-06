# Intelligence Engine modules — synced copies

These four modules are the 103 Sports Intelligence Engine pipeline
(harvest → normalize → aggregates + the nflverse open-dataset importer).
The canonical source lives in the SportsBettingGPT repo under
`services/intelligence_*.py`; these copies exist so the daily GitHub
Actions harvest job in THIS repo can run them without cross-repo access.

Rule: edit the canonical modules first, then re-copy them here in the
same change. The modules are stdlib-only and take `--data-root`, so they
run unchanged from any working directory.

Data lands on this repo's `data` branch under `data/intelligence/`
(raw landing, normalized records, aggregates). SportsBettingGPT's
router reads the published aggregates from that branch over HTTPS
(INTELLIGENCE_AGGREGATES_BASE_URL) — never blended with live data.
