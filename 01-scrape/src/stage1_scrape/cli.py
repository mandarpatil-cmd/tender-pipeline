from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .app.pipeline import probe_search_form, reindex_saved, scrape_aoc
from .scraping.captcha import parse_captcha_option


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stage1-scrape",
        description="Scrape AOC (Award of Contract) records from eprocure.gov.in",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="Fetch the search form and captcha (Step 0 only)")
    _add_common(probe)

    scrape = sub.add_parser("scrape", help="Search AOC tenders and walk the confirmed chain")
    _add_common(scrape)
    _add_scrape_args(
        scrape,
        captcha_help=(
            "6-character captcha, 'auto' (OpenRouter OCR), or omit to type it. "
            "Default: type the image."
        ),
    )

    run = sub.add_parser(
        "run",
        help="Scrape AOC then write data/exports/vendors.csv (OCR captcha)",
    )
    _add_common(run)
    _add_scrape_args(
        run,
        captcha_default="auto",
        captcha_help=(
            "6-character captcha, 'auto' (OpenRouter OCR, default), or 'manual'."
        ),
        max_tenders_default=1,
    )

    index = sub.add_parser(
        "index",
        help="Re-parse saved summary HTML into JSON and vendor/award SQLite tables",
    )
    _add_common(index)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out = Path(args.out)

    if args.command == "probe":
        info = probe_search_form(out, delay=args.delay)
        print(json.dumps(info, indent=2))
        print(
            "\ntokenSecret is a hidden session field, not the captcha. "
            "Type the 6 characters from captcha.png, or run:"
        )
        print(f"  python -m stage1_scrape run --out {out} --from-probe --max-tenders 1")
        return 0

    if args.command == "index":
        from .persist.store import Store

        n = reindex_saved(out)
        print(f"Indexed {n} tender(s) into {Store(out).db_path}")
        return 0

    extra = {
        "fromDate": getattr(args, "from_date", "") or "",
        "toDate": getattr(args, "to_date", "") or "",
        "KeyWord": getattr(args, "keyword", "") or "",
    }
    default_solver = "openrouter" if args.command == "run" else "manual"
    try:
        captcha_text, captcha_solver = parse_captcha_option(
            getattr(args, "captcha", None),
            default_solver=default_solver,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "run":
        from .app.run import run_scrape

        try:
            info = run_scrape(
                out,
                captcha_text=captcha_text,
                captcha_solver=captcha_solver,
                max_pages=args.max_pages,
                max_tenders=args.max_tenders,
                delay=args.delay,
                download_pdfs=not args.no_pdfs,
                skip_known=not args.refresh,
                captcha_retries=args.captcha_retries,
                extra_fields=extra,
                from_probe=args.from_probe,
            )
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(info, indent=2))
        return 0

    try:
        records = scrape_aoc(
            out_dir=out,
            captcha_text=captcha_text,
            captcha_solver=captcha_solver,
            max_pages=args.max_pages,
            max_tenders=args.max_tenders,
            delay=args.delay,
            download_pdfs=not args.no_pdfs,
            skip_known=not args.refresh,
            captcha_retries=args.captcha_retries,
            extra_fields=extra,
            from_probe=getattr(args, "from_probe", False),
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Saved {len(records)} tender(s) under {out.resolve()}")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", default="data", help="Output directory (json, pdfs, sqlite)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between HTTP calls")
    parser.add_argument("-v", "--verbose", action="store_true")


def _add_scrape_args(
    parser: argparse.ArgumentParser,
    *,
    captcha_default: str | None = None,
    captcha_help: str,
    max_tenders_default: int | None = None,
) -> None:
    parser.add_argument("--captcha", default=captcha_default, help=captcha_help)
    parser.add_argument("--max-pages", type=int, default=1, help="Listing pages to walk (10 rows each)")
    parser.add_argument(
        "--max-tenders",
        type=int,
        default=max_tenders_default,
        help="Stop after N tenders",
    )
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch tenders even if their ID is already in SQLite",
    )
    parser.add_argument("--from-date", dest="from_date", help="Filter: fromDate as dd/MM/yyyy")
    parser.add_argument("--to-date", dest="to_date", help="Filter: toDate as dd/MM/yyyy")
    parser.add_argument("--keyword", help="Filter: KeyWord")
    parser.add_argument(
        "--from-probe",
        action="store_true",
        help="Reuse the last `probe` session and captcha.png (do not fetch a new form)",
    )
    parser.add_argument("--captcha-retries", type=int, default=8)


if __name__ == "__main__":
    sys.exit(main())
