"""Data loading and scope filtering.

Pipeline step 1 — called at the very start of a run, before any extraction.
"""
import os

import pandas as pd

REQUIRED_COLUMNS = {"brand", "url"}


import os
import pandas as pd

def load_and_scope(input_path: str, sheet_name: str, target_scope: dict) -> pd.DataFrame:
    if not os.path.isfile(input_path):
        raise ValueError(f"Input file not found: {input_path}")

    ext = os.path.splitext(input_path)[-1].lower()

    # ── 1. Load Data ─────────────────────────────────────────────────────────
    if ext == ".csv":
        df = pd.read_csv(input_path)
    elif ext in {".xlsx", ".xls"}:
        sheets = pd.ExcelFile(input_path).sheet_names
        if sheet_name not in sheets:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {', '.join(sheets)}")
        df = pd.read_excel(input_path, sheet_name=sheet_name)
    else:
        raise ValueError(f"Unsupported format '{ext}'. Must be .csv, .xlsx, or .xls.")

    # ── 2. Clean & Validate ──────────────────────────────────────────────────
    # Vectorized column cleaning (faster and cleaner than a list comprehension)
    df.columns = df.columns.astype(str).str.strip().str.lower()

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    if df.empty:
        raise ValueError("The loaded file/sheet is empty.")

    # ── 3. Filter Scope ──────────────────────────────────────────────────────
    if not target_scope:
        return df

    frames = []
    for brand, markets in target_scope.items():
        # Create a single boolean mask for the brand
        mask = df['brand'] == brand
        
        # If markets are specified, chain the market condition onto the mask
        if markets:
            valid_markets = {str(m).upper() for m in markets}
            mask &= df['market'].astype(str).str.upper().isin(valid_markets)
            
        frames.append(df[mask])

    return pd.concat(frames, ignore_index=True) if frames else df.iloc[0:0]
