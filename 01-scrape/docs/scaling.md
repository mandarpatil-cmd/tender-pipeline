# Scaling

The layout is split so you can grow one layer without rewriting the others.

## Add another GePNIC portal

State sites share the same Tapestry patterns. Add:

- `stage1_scrape.config` values (or a `Portals` table: base URL, form id, AOC code)
- keep `domain` and `persist` unchanged
- swap or subclass `scraping.client` only if cookies/headers differ

Do not copy `app/pipeline.py`. Parameterize `SEARCH_PAGE_URL` and `TENDER_STATUS_AOC`.

## Higher volume

`scrape_one_tender` is already one tender = one job.

1. Run `probe` + search once; persist cookies + listing HTML (already in `data/debug`).
2. Enqueue each `ListingRow`.
3. Workers call `scrape_one_tender` with a shared cookie jar **or** fresh sessions if `sp=` is row-scoped.

Watch `--delay`. The portal is a public government site.

## API or UI

Keep `cli.py` thin. Import `scrape_aoc` / `probe_search_form` from `stage1_scrape.app`. A FastAPI/Flask app should not scrape inside a request; use a worker.

`main.py` is the same idea already: a root-level caller that imports `app` and `export` and
adds only sequencing and progress output. Keep it that way — orchestration that other callers
would want belongs in `app/`, not in `main.py`.

## Storage

The database holds a tender catalog plus **vendor** and **award** rows, and it is shared with
stages 2 and 3 — the schema lives in `../core` (`pipeline_core`), not here. JSON remains the
full record. When you outgrow SQLite:

- keep writing JSON as the source of truth, and
- point `pipeline_core.db.engine` at Postgres. The models are already SQLAlchemy, so
  `persist.store.Store` keeps the same `save_tender` / `known_ids` surface.

## Parsers

If markup drifts, change only `parse/listing.py`, `parse/status.py`, or `parse/summary.py`. Fixtures live in `tests/fixtures/`. Save live HTML under `data/debug/` and promote a slice into a fixture.
