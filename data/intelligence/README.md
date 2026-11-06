# Intelligence Engine data branch

Written by the daily harvest job (see the `feat/intelligence-harvest`
branch / intelligence/SYNC.md on main). Layout:

- `raw/<sport>/<dataset>/<date>/…` — append-only provider payloads
- `normalized/<sport>/` — team-season + player-season records
- `aggregates/<sport>.json` — what SportsBettingGPT's router reads

Never edit by hand; the job owns this branch.
