"""LLM-based URL refresh, validation, and save helpers.

This module owns the URL-side load → update → save flow. It loads the scoped
Excel rows, refreshes URLs with Google Search, validates the result, and writes
the final CSV with `new_url`, `url_status`, `reason`, and `last_url_update_dt`.
"""

from __future__ import annotations

import json
import os
import importlib
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, TypedDict

import time
from venv import logger

import pandas as pd
from dotenv import load_dotenv
import logging

try:
    from handlers.loader import load_and_scope
except Exception:  # pragma: no cover - fallback for direct module use
    from loader import load_and_scope

from price_scrape_helpers import _brand_market_key, load_market_map


__all__ = ["update_urls", "save_updated_urls", "run_url_update_pipeline"]

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MARKET_FILE = str(ROOT_DIR / "market_cd.json")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-flash")

market_file = DEFAULT_MARKET_FILE
market_map: dict[str, dict[str, str]] = load_market_map(market_file)
client = None
search_tool = None
types = None
StateGraph = None
START = None
END = None
url_graph = None


def _ensure_runtime() -> None:
    global client, search_tool, types, StateGraph, START, END, url_graph
    if client is not None and search_tool is not None and url_graph is not None:
        return
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")
    genai_module = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
    langgraph_graph = importlib.import_module("langgraph.graph")
    StateGraph = langgraph_graph.StateGraph
    START = langgraph_graph.START
    END = langgraph_graph.END
    client = genai_module.Client(api_key=api_key)
    search_tool = types.Tool(google_search=types.GoogleSearch())
    url_graph_builder = StateGraph(UrlGraphState)
    url_graph_builder.add_node('refresh', _refresh_url)
    url_graph_builder.add_node('validation', _validation_url)
    url_graph_builder.add_edge(START, 'refresh')
    url_graph_builder.add_edge('refresh', 'validation')
    url_graph_builder.add_edge('validation', END)
    url_graph = url_graph_builder.compile()


def _chunk_rows(frame, size):
    for start in range(0, len(frame), size):
        yield frame.iloc[start:start + size]


def _extract_json_block(text):
    cleaned = str(text).strip()
    if cleaned.startswith('```json') and cleaned.endswith('```'):
        cleaned = cleaned.replace('```json', '', 1).rsplit('```', 1)[0].strip()
    elif cleaned.startswith('```') and cleaned.endswith('```'):
        cleaned = cleaned.replace('```', '', 1).rsplit('```', 1)[0].strip()

    first_arr = cleaned.find('[')
    last_arr = cleaned.rfind(']')
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return cleaned[first_arr:last_arr + 1]

    first_obj = cleaned.find('{')
    last_obj = cleaned.rfind('}')
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return cleaned[first_obj:last_obj + 1]

    return cleaned


class UrlGraphState(TypedDict, total=False):
    row: dict[str, Any]
    refresh_item: dict[str, Any]
    validated_item: dict[str, Any]
    error: str


def _refresh_url(state: UrlGraphState) -> UrlGraphState:
    row = state.get('row', {})
    brand = row.get('brand')
    key = _brand_market_key(brand, market_map)
    locale_market_map = market_map.get(key, {})

    prompt = f"""
        # Task: find the web page url for the input content
        Input fields are: brand, name, skus, market.
        If unresolved, set new_url to null and explain the reason.
        do the following steps:
        1. find the default url given brand, name, and skus(march one if multiple).
        2. modify default url to certain market following the market_code_map of this brand.
        3. save the modified url in new_url.
        4. mark url_status as "resolved" if the url is correct and working, otherwise "failed".

        # market_code_map: {locale_market_map}
        # Input: {row}

        # Output Example(Return ONLY a valid JSON object with this format,no explanation, no extra text):
        {{'brand': 'Van Cleef',
        'new_name': 'Vintage Alhambra bracelet, 5 motifs',
        'sku': 'VCARA41300',
        'market': 'VNM',
        'new_url': 'https://www.wancleefarpels.com/vn/en/collections/jewelry/bracelets/vintage-alhambra/V-CARA41300',
        'url_status': 'resolved',
        'reason':  (null if resolved else a string explaining why)}}
        """
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[search_tool],
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        ),
    )
    text = response.text or ''
    payload = json.loads(_extract_json_block(text))
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise ValueError('Refresh response must be a JSON object or list with one object.')
    return {'refresh_item': payload}


def _validation_url(state: UrlGraphState) -> UrlGraphState:
    row = state.get('row', {})
    refresh_item = state.get('refresh_item', {})
    prompt_input = {
        'brand': row.get('brand'),
        'market': row.get('market'),
        'skus': row.get('skus'),
        'name': row.get('name'),
        'new_url': refresh_item.get('new_url'),
        'url_status': refresh_item.get('url_status', refresh_item.get('status')),
        'reason': refresh_item.get('reason'),
    }

    prompt = (f"""
        open the url from the input and check if it is the corresponding product page (right sku, market)
        if not, update the url to a right one. 
        # Input:
        {json.dumps(prompt_input, ensure_ascii=False)}
        # Return ONLY valid JSON with keys: 
        brand, market, sku, new_url, url_status(resolved/failed), reason. 
        no explanation, no extra text:
        """
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[search_tool],
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        ),
    )
    text = response.text or ''
    payload = json.loads(_extract_json_block(text))
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise ValueError('Validation response must be a JSON object or list with one object.')
    return {'validated_item': payload}


def _run_url_graph(batch_frame):
    _ensure_runtime()
    state = {'row': batch_frame.to_dict()}
    return url_graph.invoke(state)


def update_urls(
    df: pd.DataFrame,
    *,
    progress_callback: Callable | None = None
) -> pd.DataFrame:
    """Refresh URLs and return the input frame with ``new_url`` and ``url_status`` columns."""

    _ensure_runtime()

    frame = df.copy()
    if 'new_url' not in frame.columns:
        frame['new_url'] = pd.NA
    if 'url_status' not in frame.columns:
        frame['url_status'] = pd.NA

    if 'new_price' in frame.columns:
        frame = frame[frame['new_price'].isna()]

    refresh_rows = []
    t0 = time.time()
    done = 0
    error_count = 0

    def _report(phase: str, brand: str, total: int) -> None:
        if progress_callback is not None:
            try:
                progress_callback(phase, brand, done, total, time.time() - t0)
            except Exception:
                pass

    try:
        batch_brand = str(frame.iloc[0].get('brand') or 'all')
        for row_idx, row in frame.iterrows():
            if error_count >= 10:
                logger.warning("Too many errors processing brand %s, skipping remaining rows.", batch_brand)
                break
            try:    
                done += 1
                _report('URL refresh', batch_brand, len(frame))

                out = {
                    '__src_idx': row_idx,
                    'temp_url': pd.NA,
                    'new_url': pd.NA,
                    'url_status': pd.NA,
                    'reason': pd.NA,
                }
                row_frame = pd.Series(
                    {
                        'brand': row.get('brand'),
                        'name': row.get('name'),
                        'sku': row.get('skus'),
                        'market': row.get('market'),
                    }
                )
                if done >= 10:
                    break
                graph_state = _run_url_graph(row_frame)
                refresh_item = graph_state.get('refresh_item', {})
                validated_item = graph_state.get('validated_item', {})

                out['temp_url'] = refresh_item.get('new_url', pd.NA)
                out['new_url'] = validated_item.get('new_url', refresh_item.get('new_url', pd.NA))
                out['url_status'] = validated_item.get('url_status', refresh_item.get('url_status', refresh_item.get('status', pd.NA)))
                out['reason'] = validated_item.get('reason', refresh_item.get('reason', pd.NA))
                refresh_rows.append(out)
            except Exception as exc:
                error_count += 1
                logger.exception("Error processing row %s: %s", row_idx, row)

        _report('URL refresh', batch_brand, len(frame))
    
    except Exception as fatal_exc:
        logger.exception("Fatal error in update_urls: %s", fatal_exc)


    payload_df = pd.DataFrame(refresh_rows)
    frame_refreshed = frame.copy()
    if not payload_df.empty:
        payload_df = payload_df.set_index('__src_idx')
        common_idx = frame_refreshed.index.intersection(payload_df.index)

        for col in ['temp_url', 'new_url', 'url_status', 'reason']:
            if col not in frame_refreshed.columns:
                frame_refreshed[col] = pd.NA
            frame_refreshed.loc[common_idx, col] = payload_df.loc[common_idx, col]
    
    frame.loc[frame_refreshed.index, 'temp_url'] = frame_refreshed['temp_url']
    frame.loc[frame_refreshed.index, 'new_url'] = frame_refreshed['new_url']
    frame.loc[frame_refreshed.index, 'url_status'] = frame_refreshed['url_status']
    frame.loc[frame_refreshed.index, 'reason'] = frame_refreshed['reason']
    return frame


def save_updated_urls(df: pd.DataFrame, output_path: str) -> str:
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    df = df.copy()
    df["last_url_update_dt"] = datetime.now().strftime("%Y-%m-%d")
    df.to_csv(output_path, index=False)
    logger = logging.getLogger(__name__)
    logger.info('Saved: %s', output_path)
    return output_path


def run_url_update_pipeline(
    input_path: str,
    sheet_name: str,
    output_path: str,
    target_scope: dict,
    progress_callback: Callable | None = None,
) -> str:
    df = load_and_scope(input_path, sheet_name, target_scope)
    df = update_urls(df, progress_callback=progress_callback)
    return save_updated_urls(df, output_path)