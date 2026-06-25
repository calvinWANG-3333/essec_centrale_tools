#!/usr/bin/env python3
"""Command-line entry point for the CRP LuxuryInsight pipelines.

This module parses CLI arguments and dispatches to the shared pipeline layer.
The Streamlit app imports `backend/code/pipeline.py` directly instead.
"""

import argparse
import sys
import logging

from pipeline import run_classic_pipeline, run_url_pipeline

def main():
    logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper_debug.log"),
        logging.StreamHandler()
    ])
    logger = logging.getLogger(__name__)
    parser = argparse.ArgumentParser(
                description="CRP LuxuryInsight — run the price or URL pipeline from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # all brands, all markets
    python cli_run_pipeline.py data/input.xlsx output/results.csv

  # specific sheet
    python cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Leather_Goods

  # filter by brand
    python cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Leather_Goods --brands "Dior, Celine"

    # filter by brand and market
    python cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Leather_Goods --brands "Dior" --markets "USA, JPN"

    # URL refresh mode
    python cli_run_pipeline.py data/input.xlsx output/results.csv --mode url --sheet Leather_Goods
        """,
    )
    parser.add_argument(
        "input",
        help="path to the input Excel file (.xlsx)",
    )
    parser.add_argument(
        "output",
        help="path for the output CSV file (created if it does not exist)",
    )
    parser.add_argument(
        "--sheet",
        default="Sheet1",
        metavar="NAME",
        help="sheet name to read inside the Excel file (default: Sheet1)",
    )
    parser.add_argument(
        "--brands",
        default="",
        metavar="BRAND,...",
        help="comma-separated list of brands to include (default: all brands)",
    )
    parser.add_argument(
        "--markets",
        default="",
        metavar="MKT,...",
        help="comma-separated list of market codes to include, e.g. USA,JPN (default: all markets)",
    )
    parser.add_argument(
        "--mode",
        choices=("price", "url"),
        default="price",
        help="Which pipeline to run: 'price' (classic price extractor) or 'url' (URL updater).",
    )
    parser.add_argument(
        "--max-errors",
        type=int,
        default=None,
        help="Max errors per brand (only applies to the price pipeline).",
    )

    args = parser.parse_args()

    brands  = [b.strip() for b in args.brands.split(",")  if b.strip()]
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    target_scope = {b: (markets or None) for b in brands} if brands else {}

    logger.info("Input:   %s", args.input)
    logger.info("Output:  %s", args.output)
    logger.info("Sheet:   %s", args.sheet)
    logger.info("Brands:  %s", brands or 'all')
    logger.info("Markets: %s", markets or 'all')
    logger.info("")

    try:
        if args.mode == "price":
            run_classic_pipeline(
                input_path=args.input,
                sheet_name=args.sheet,
                output_path=args.output,
                target_scope=target_scope,
                max_errors_threshold=args.max_errors,
            )
        else:
            run_url_pipeline(
                input_path=args.input,
                sheet_name=args.sheet,
                output_path=args.output,
                target_scope=target_scope,
            )
    except Exception as e:
        logger.exception("Error running pipeline")
        sys.exit(1)


if __name__ == "__main__":
    main()
