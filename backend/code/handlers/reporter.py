"""Output saving and run summary.

Pipeline step 3 — called after extract_prices(), final step of a classic run.
"""
from datetime import datetime
import os

import pandas as pd


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
    print('Saved:', output_path)

    inspection_cols = [
        c for c in ['brand', 'market', 'original price', 'new_price',
                     'currency', 'new_currency', 'price_method', 'remarks', 'url']
        if c in df.columns
    ]
    print('Rows processed:', len(df))
    print('Rows with price:', int(df['new_price'].notna().sum()))
    print('Rows without price:', int(df['new_price'].isna().sum()))
    print(
        'Rows matching original price:',
        int(df['is_price_match'].sum()) if 'is_price_match' in df.columns else 0,
    )
    failed_fetch = df[df['new_price'].isna()][inspection_cols]
    print('Failed fetch rows:', len(failed_fetch))
    if 'remarks' in failed_fetch.columns and len(failed_fetch) > 0:
        remarks_lower = failed_fetch['remarks'].astype(str).str.lower()
        n_404     = (remarks_lower == "error404").sum()
        n_blocked = (remarks_lower == "crawler_blocked").sum()
        n_skipped = (remarks_lower == "skipped_error_threshold").sum()
        n_other   = len(failed_fetch) - n_404 - n_blocked - n_skipped
        print(f'  Page not found (404): {int(n_404)}')
        print(f'  Crawler blocked:      {int(n_blocked)}')
        print(f'  Skipped (threshold):  {int(n_skipped)}')
        print(f'  Other failures:       {int(n_other)}')

    return output_path
