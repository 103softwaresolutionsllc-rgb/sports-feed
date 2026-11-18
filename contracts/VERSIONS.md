# Contract Versions Registry

Quant Apparatus spec §4 — the registry of seam contracts. A contract's
version lives in its schema file (`v` const + `payloadRequired`); this
registry records status and who consumes what. Producers write ONE
version; consumers declare the versions they accept (in their tests).

| Contract | File | Version | Status | Producers | Consumers (wired in Phase A) |
|---|---|---|---|---|---|
| Game | `game.schema.json` | 1 | Active — foundation | sports-feed collector (slate/board), Intelligence harvest (API-Sports/ESPN payloads) | sports-feed tests (payload level), SportsBettingGPT tests (payload level, Historical) |
| LineSet | `lineset.schema.json` | 1 | Active — foundation | sports-feed collector (`extractBoardLineSet`) | sports-feed tests (payload level) |
| Pick | `pick.schema.json` | 1 | Active — foundation | BetTracker app (locked legs), sports-feed collector (pick cards, partial — see README gap G4) | sports-feed tests, simulation-lab tests (as parlay legs) |
| ParlaySnapshot | `parlay-snapshot.schema.json` | 1 | Active — foundation | BetTracker app (parlay store) | simulation-lab tests (payload level) |
| FitArtifact | `fit-artifact.schema.json` | 1 (native fits are v0 instances) | Active — foundation | simulation-lab fit scripts (manual at v1; pipeline in Phase C) | simulation-lab tests (payload level, all four sports) |
| GradeRecord | `grade-record.schema.json` | 1 | Active — foundation | BetTracker app (unified record, on-device), grading ledger (Phase B) | sports-feed tests (envelope example), simulation-lab tests (payload level) |

**Pairing note:** `parlay-snapshot.schema.json` embeds the Pick PAYLOAD
form (pick v1 minus the envelope) as its `legs` items. Any change to
pick's payload fields must be mirrored there in the same commit, and
both files' versions move together.

**Versioning rule (spec §4):** additive changes are minor (new optional
field — same major, noted here); removing or re-meaning a field is a
major version and requires every consumer's test suite to pass against
the new major before any producer ships it. No silent coercion, ever.
