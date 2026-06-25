"""Price extraction, final save, and summary helpers.

This module owns the classic load → extract → save flow. `extract_prices()`
returns the processed DataFrame even if some rows or phases fail, and
`run_price_pipeline()` writes the final CSV.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import time
import traceback
from typing import Any, Callable, Optional
import pandas as pd
import logging

try:
    from handlers.loader import load_and_scope
except Exception:
    from loader import load_and_scope

import price_scrape_helpers as ps

_build_uc_driver = ps.build_uc_driver
_should_use_uc = ps._should_use_uc
get_product_price = ps.get_product_price

_ROOT_DIR = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

_EXTRACTION_DEFAULTS = dict(
    market_file=str(_ROOT_DIR / "market_cd.json"),
    auto_uc_brands={'dior', 'louis vuitton', 'lv', 'rolex', 'fendi', 'audemars piguet'},
    use_uc=False,
    long_wait_brands={'dior', 'louis vuitton', 'lv'},
    fetch_wait_range_seconds=(3.5, 7.5),
    block_wait_range_seconds=(4.0, 8.0),
    other_fetch_wait_range_seconds=(1.0, 3.0),
    other_block_wait_range_seconds=(2.0, 4.0),
)

    # These remarks are treated as infrastructure failures rather than content
    # extraction failures, so they do not consume the per-brand error budget.
_INFRA_REMARKS = frozenset({"error404", "crawler_blocked"})


def _safe_write(df: pd.DataFrame, idx: Any, price_val, method_val, remark_val, currency_val) -> None:
    """Write extraction results into `df` safely."""
    try:
        df.at[idx, "new_price"] = price_val
        df.at[idx, "price_method"] = method_val
        df.at[idx, "remarks"] = remark_val
        df.at[idx, "new_currency"] = currency_val
    except Exception:
        # avoid failing the whole run just because one write failed
        logger.warning("Failed to write results for index %s", idx)


def _counts_as_error(remark_val) -> bool:
    return remark_val not in _INFRA_REMARKS


def _run_get_price_with_handling(idx: Any, row: pd.Series, shared: dict, driver=None):
    """Call `get_product_price` and convert exceptions to a remark tuple.

    Returns: (price, method, remark, currency)
    """
    try:
        return get_product_price(
            brand=row.get("brand"),
            url=row.get("url"),
            country_code=row.get("market"),
            driver=driver,
            input_skus=row.get("skus_list"),
            **shared,
        )
    except Exception as exc:
        remark = f"ProcessingError: {type(exc).__name__}: {str(exc)}"
        logger.exception("Error processing row %s: %s", idx, remark)
        return pd.NA, "Exception", remark, pd.NA


def extract_prices(
    df: pd.DataFrame,
    *,
    market_file: str,
    auto_uc_brands: set,
    use_uc: bool,
    long_wait_brands: set,
    fetch_wait_range_seconds: tuple,
    block_wait_range_seconds: tuple,
    other_fetch_wait_range_seconds: tuple,
    other_block_wait_range_seconds: tuple,
    browser_pause_seconds: float = 0,
    keep_uc_open_until_enter: bool = False,
    pause_every_n_urls: int = 30,
    pause_seconds_every_n_urls: float = 10,
    max_errors_threshold: Optional[int] = None,
    progress_callback: Callable | None = None,
) -> pd.DataFrame:
    """Run the 3-phase price extraction and always return `df`.

    The function is defensive: per-row errors are logged and saved to the
    `remarks` column; phase-level errors are printed but processing continues
    for other brands/rows. This keeps the DataFrame in a usable state even if
    some network or parsing errors occur.
    """

    # Ensure required columns exist
    for col in ("new_price", "price_method", "remarks", "new_currency"):
        if col not in df.columns:
            df[col] = pd.NA

    # Normalize SKUs into lists
    if "skus" in df.columns:
        df["skus_list"] = df["skus"].apply(
            lambda v: [s.strip() for s in str(v).split(",") if s.strip()] if pd.notna(v) else []
        )
    else:
        df["skus_list"] = [[] for _ in range(len(df))]

    pending_mask = df["url"].notna() & df["new_price"].isna()
    pending = df[pending_mask]
    logger.info("Pending URLs: %d", len(pending))

    shared = dict(
        market_file=market_file,
        auto_uc_brands=auto_uc_brands,
        use_uc=use_uc,
        long_wait_brands=long_wait_brands,
        fetch_wait_range_seconds=fetch_wait_range_seconds,
        block_wait_range_seconds=block_wait_range_seconds,
        other_fetch_wait_range_seconds=other_fetch_wait_range_seconds,
        other_block_wait_range_seconds=other_block_wait_range_seconds,
    )

    def _report(phase: str, brand: str, done: int, total: int, t0: float) -> None:
        if progress_callback is not None:
            try:
                progress_callback(phase, brand, done, total, time.time() - t0)
            except Exception:
                pass

    # Helper to process a group (brand) optionally using a UC driver
    def _process_group(brand_name: str, brand_df: pd.DataFrame, phase_label: str, use_driver: bool):
        driver = None
        error_count = 0
        blocked_count = 0
        url_counter = 0
        t0 = time.time()
        total = len(brand_df)

        try:
            if use_driver:
                try:
                    driver = _build_uc_driver(timeout=25)
                except Exception as e:
                    logger.warning("UC driver init failed for %s: %s", brand_name, e)
                    driver = None

            for idx, row in brand_df.iterrows():
                # skip if we've exceeded the error budget
                if max_errors_threshold is not None and error_count > max_errors_threshold:
                    df.at[idx, "remarks"] = "Skipped_Error_Threshold"
                    url_counter += 1
                    _report(phase_label, brand_name, url_counter, total, t0)
                    continue

                price_val, method_val, remark_val, currency_val = _run_get_price_with_handling(idx, row, shared, driver=driver)
                _safe_write(df, idx, price_val, method_val, remark_val, currency_val)

                url_counter += 1
                _report(phase_label, brand_name, url_counter, total, t0)

                if pd.isna(price_val) and _counts_as_error(remark_val):
                    error_count += 1

                remark_text = "" if pd.isna(remark_val) else str(remark_val).lower()
                blocked_count = blocked_count + 1 if "blocked" in remark_text else 0
                if blocked_count >= 3 and driver is not None:
                    logger.warning("3 consecutive blocks for %s; stopping driver for this brand.", brand_name)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
                    break

                if pause_every_n_urls and url_counter % pause_every_n_urls == 0:
                    logger.info("Pausing %ss after %d URLs (%s).", pause_seconds_every_n_urls, url_counter, brand_name)
                    time.sleep(pause_seconds_every_n_urls)

                if browser_pause_seconds > 0:
                    time.sleep(browser_pause_seconds)

            if keep_uc_open_until_enter and blocked_count >= 3:
                input(f"Blocked-stop for {brand_name}. Press Enter to close the UC window...")
        except Exception as exc:
            logger.exception("Phase-level error for %s in %s", brand_name, phase_label)
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    # ── Phase 1: UC brands
    try:
        uc_mask = pending["brand"].apply(lambda b: _should_use_uc(b, auto_uc_brands=auto_uc_brands, use_uc=use_uc))
        uc_pending = pending[uc_mask]
        if len(uc_pending) > 0:
            logger.info("Phase 1 (UC brands): %d URLs", len(uc_pending))
            for brand_name, brand_df in uc_pending.groupby("brand"):
                _process_group(brand_name, brand_df, "Phase 1 · UC", use_driver=True)
    except Exception as exc:
        logger.exception("Unexpected error in Phase 1")

    # ── Phase 1: HTTP
    http_failed_idx: list = []
    phase_label = "Phase 1 · HTTP"

    try:
        non_uc_pending = pending[~uc_mask]
        if len(non_uc_pending) > 0:
            logger.info("Phase 1 (HTTP brands): %d URLs", len(non_uc_pending))
            for brand_name, brand_df in non_uc_pending.groupby("brand"):
                total = len(brand_df)  
                url_counter = 0
                t0 = time.time()
                for idx, row in brand_df.iterrows():
                    try:
                        price_val, method_val, remark_val, currency_val = _run_get_price_with_handling(idx, row, shared, driver=None)
                        _safe_write(df, idx, price_val, method_val, remark_val, currency_val)
                        if pd.isna(price_val):
                            http_failed_idx.append(idx)
                        url_counter += 1
                        _report(phase_label, brand_name, url_counter, total, t0)
                    except Exception as exc:
                        logger.exception("Row-level error in Phase 1 for index %s", idx)
    except Exception as exc:
        logger.exception("Unexpected error in Phase 1")

    # ── Phase 2: UC retry for HTTP failures
    try:
        if http_failed_idx:
            retry_df = df.loc[http_failed_idx]
            retry_df = retry_df[retry_df["remarks"].astype(str) != "Skipped_Error_Threshold"]
            retry_df = retry_df[~retry_df["brand"].apply(lambda b: _should_use_uc(b, auto_uc_brands=auto_uc_brands, use_uc=use_uc))]
            if len(retry_df) > 0:
                logger.info("Phase 2 (UC retry): %d URLs", len(retry_df))
                for brand_name, brand_df in retry_df.groupby("brand"):
                    _process_group(brand_name, brand_df, "Phase 2 · UC retry", use_driver=True)
    except Exception as exc:
        logger.exception("Unexpected error in Phase 2")

    # Finalize numeric columns but do not fail if that errors
    try:
        df["original price"] = pd.to_numeric(df.get("original price"), errors="coerce")
        df["new_price"] = pd.to_numeric(df["new_price"], errors="coerce")
        df["is_price_match"] = df["original price"] == df["new_price"]
    except Exception as exc:
        logger.exception("Error finalizing DataFrame numeric columns")

    df["last_price_update_dt"] = datetime.now().strftime("%Y-%m-%d")

    return df


def save_and_summarize(df: pd.DataFrame, output_path: str) -> str:
    """Save df to CSV at output_path and print a brief extraction summary.

    Prints: rows processed, rows with/without price, rows matching original price.
    Returns output_path so callers can chain: return save_and_summarize(df, path).
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    df = df.copy()
    if "last_price_update_dt" not in df.columns:
        df["last_price_update_dt"] = datetime.now().strftime("%Y-%m-%d")
    df.to_csv(output_path, index=False)
    logger.info("Saved: %s", output_path)

    inspection_cols = [
        c for c in ['brand', 'market', 'original price', 'new_price',
                    'currency', 'new_currency', 'price_method', 'remarks', 'url']
        if c in df.columns
    ]
    logger.info('Rows processed: %d', len(df))
    logger.info('Rows with price: %d', int(df['new_price'].notna().sum()))
    logger.info('Rows without price: %d', int(df['new_price'].isna().sum()))
    logger.info('Rows matching original price: %d', int(df['is_price_match'].sum()) if 'is_price_match' in df.columns else 0)
    failed_fetch = df[df['new_price'].isna()][inspection_cols]
    logger.info('Failed fetch rows: %d', len(failed_fetch))
    if 'remarks' in failed_fetch.columns and len(failed_fetch) > 0:
        remarks_lower = failed_fetch['remarks'].astype(str).str.lower()
        n_404     = (remarks_lower == "error404").sum()
        n_blocked = (remarks_lower == "crawler_blocked").sum()
        n_skipped = (remarks_lower == "skipped_error_threshold").sum()
        n_other   = len(failed_fetch) - n_404 - n_blocked - n_skipped
        logger.info('  Page not found (404): %d', int(n_404))
        logger.info('  Crawler blocked:      %d', int(n_blocked))
        logger.info('  Skipped (threshold):  %d', int(n_skipped))
        logger.info('  Other failures:       %d', int(n_other))

    return output_path


def run_price_pipeline(
    input_path: str,
    sheet_name: str,
    output_path: str,
    target_scope: dict,
    max_errors_threshold: Optional[int] = None,
    progress_callback: Callable | None = None,
) -> str:
    """Load, extract, summarize, and save the classic price results."""
    df = load_and_scope(input_path, sheet_name, target_scope)
    df = extract_prices(
        df, **_EXTRACTION_DEFAULTS,
        max_errors_threshold=max_errors_threshold,
        progress_callback=progress_callback,
    )
    return save_and_summarize(df, output_path)
