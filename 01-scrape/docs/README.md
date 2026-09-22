# Docs

Stage 1 of the pipeline. For the pipeline as a whole — all three stages and the shared
database — see the [root README](../../README.md).

Start with the project [README](../README.md) for setup, commands, and folder layout.

To run everything in one command, use `python main.py`: it scrapes the portal and writes
`data/exports/vendors.csv` / `.xlsx`. Contact details for those vendors come from stage 2
(`../../02-enrich`), not from here.

- [Architecture](architecture.md) — layers, diagram, module map
- [Data flow](data-flow.md) — the steps against the live portal
- [Scaling](scaling.md) — how to grow without rewriting the CLI
