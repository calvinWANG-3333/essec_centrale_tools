"""Reusable price scraping helpers for notebooks and scripts.

These helpers are intentionally notebook-agnostic so other notebooks can
import them without copying the scraping cell block.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import time
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import pandas as pd
from bs4 import BeautifulSoup
import logging

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None

try:
    import curl_cffi
except Exception:  # pragma: no cover - optional dependency
    curl_cffi = None

try:
    import undetected_chromedriver as uc
except Exception:  # pragma: no cover - optional dependency
    uc = None


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

DEFAULT_AUTO_UC_BRANDS = {"dior", "louis vuitton", "lv", "rolex", "fendi"}
DEFAULT_LONG_WAIT_BRANDS = {"dior", "louis vuitton", "lv"}
DEFAULT_FETCH_WAIT_RANGE_SECONDS = (3.5, 7.5)
DEFAULT_BLOCK_WAIT_RANGE_SECONDS = (6.0, 12.0)
DEFAULT_OTHER_FETCH_WAIT_RANGE_SECONDS = (1.8, 2.8)
DEFAULT_OTHER_BLOCK_WAIT_RANGE_SECONDS = (2.5, 4.5)

_MARKET_TO_PROXY_SUFFIX = {
    "ARE": "ae", "AED": "ae", "AUS": "au", "BHR": "bh", "BRA": "br",
    "CAN": "ca", "CHE": "ch", "CHN": "cn", "CZE": "cz", "DNK": "dk",
    "DEU": "de", "GBR": "gb", "HKG": "hk", "IND": "in", "ITA": "it",
    "JPN": "jp", "KOR": "kr", "KWT": "kw", "MEX": "mx", "MYS": "my",
    "NZL": "nz", "QAT": "qa", "SAU": "sa", "SGP": "sg", "THA": "th",
    "TUR": "tr", "TWN": "tw", "USA": "us", "VNM": "vn", "PHL": "ph",
}

__all__ = [
    "build_market_url",
    "build_uc_driver",
    "clean_price",
    "fetch_soup",
    "get_product_price",
    "load_market_map",
    "tag_method_with_fetch",
]

logger = logging.getLogger(__name__)



# ============================================================================
# UTILITY FUNCTIONS - Normalization & Helpers
# ============================================================================

def _normalize_brand_name(value: Any) -> str:
    """Normalize brand name for consistent matching."""
    text = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower())
    return re.sub(r"\s+", " ", text).strip()


def _normalize_locale_token(value: Any) -> str:
    """Normalize locale token by removing non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _split_locale_parts(value: Any) -> list[str]:
    """Split locale string by '/' separator, filtering empty parts."""
    return [part for part in str(value).split("/") if part]


def _sleep_with_jitter(wait_range: Tuple[float, float]) -> None:
    """Sleep for a random duration within the specified range."""
    low, high = wait_range
    low, high = float(low), float(high)
    if high < low:
        low, high = high, low
    time.sleep(random.uniform(low, high))


def _sku_normalize(value: Any) -> str:
    """Normalize SKU by removing whitespace and converting to lowercase."""
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _parse_input_skus(value: Any) -> set[str]:
    """Parse input SKUs from various formats into a normalized set."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    if isinstance(value, (list, tuple, set)):
        return {_sku_normalize(v) for v in value if str(v).strip()}
    return {_sku_normalize(v) for v in str(value).split(",") if v.strip()}


# ============================================================================
# HTML & PRICE CLEANING
# ============================================================================

def _clean_soup(soup):
    """Remove noise elements (recommendations, footer, etc.) from page."""
    noise_selectors = [
        '.recommendations', '.related-products', '.upsell', 
        '.cross-sell', '.suggested-items', '#footer', 'nav',
        '.vca-sb-container', '.vca-sb-suggestions', '.vca-cc-product',
        '.vca-footer', '.vca-hide',
    ]
    clean_soup = soup
    for selector in noise_selectors:
        for element in clean_soup.select(selector):
            element.decompose() 
    return clean_soup

def clean_price(price):
    """Parse a price string to an int.
    """
    if price is None:
        return None
    text = str(price).strip()
    if text == "":
        return None
    try:
        if pd.isna(price):
            return None
    except Exception:
        pass
    if text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return int(float(text))
    except Exception:
        pass

    cleaned = text.replace(",", "")
    cleaned = re.sub(r"[^\d\.\-eE]", "", cleaned)
    cleaned = cleaned.rstrip(".")

    try:
        return int(float(cleaned))
    except Exception as exc:
        return None


def _has_price_value(price: Any) -> bool:
    """Check if price value is valid (non-None, non-zero, numeric)."""
    value = clean_price(price)
    return value is not None and value > 0


# ============================================================================
# MARKET & PROXY MANAGEMENT
# ============================================================================

def _load_proxy_credentials(env_file: str = ".env") -> tuple[str, str, str, str]:
    """Load proxy credentials (Username, Password, Domain, Port) from .env file."""
    if load_dotenv is not None:
        load_dotenv(env_file, override=False)
    username = (os.environ.get("User") or "").strip()
    password = (os.environ.get("Password") or "").strip()
    domain = (os.environ.get("Domain") or "").strip()
    port = (os.environ.get("Port") or "").strip()
    return username, password, domain, port


def _build_uc_proxy_extension(proxy_uri: str) -> Optional[str]:
    """Build temporary Chrome extension for authenticated proxy support in UC driver."""
    parsed = urlparse(proxy_uri)
    if not parsed.hostname or not parsed.port:
        return None
    extension_dir = tempfile.mkdtemp(prefix="uc_proxy_auth_")
    manifest = {
        "manifest_version": 3,
        "name": "Proxy Auth Helper",
        "version": "1.0.0",
        "permissions": ["proxy", "storage", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "88",
    }
    background_js = f"""
            const proxyConfig = {{
                mode: "fixed_servers",
                rules: {{
                    singleProxy: {{ scheme: "{parsed.scheme or 'http'}", host: "{parsed.hostname}", port: {parsed.port} }},
                    bypassList: ["localhost", "127.0.0.1"]
                }}
            }};
            const applyProxyConfig = () => chrome.proxy.settings.set({{ value: proxyConfig, scope: "regular" }});
            applyProxyConfig();
            chrome.runtime.onInstalled.addListener(applyProxyConfig);
            chrome.runtime.onStartup.addListener(applyProxyConfig);
            chrome.webRequest.onAuthRequired.addListener(
                () => ({{
                    authCredentials: {{
                        username: "{parsed.username or ''}",
                        password: "{parsed.password or ''}"
                    }}
                }}),
                {{ urls: ["<all_urls>"] }},
                ["asyncBlocking"]
            );
            """.strip()
    with open(os.path.join(extension_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(extension_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)
    return extension_dir


@lru_cache(maxsize=16)
def load_market_map(market_file: str = "market_cd.json") -> Dict[str, Dict[str, str]]:
    """Load market mapping from JSON file (cached)."""
    with open(market_file, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=16)
def load_proxy_map(country_code: Any = None, env_file: str = ".env") -> Dict[str, str]:
    """Load proxy map from .env credentials. Format: http://User:Pass_country-XX@Domain:Port"""
    username, password, domain, port = _load_proxy_credentials(env_file)
    market_filter = str(country_code).upper() if country_code is not None and str(country_code).strip() else None
    if username and password and domain and port:
        proxy_map: Dict[str, str] = {}
        market_items = _MARKET_TO_PROXY_SUFFIX.items()
        if market_filter is not None:
            suffix = _MARKET_TO_PROXY_SUFFIX.get(market_filter)
            market_items = ((market_filter, suffix),) if suffix else ()
        for market_code, country_suffix in market_items:
            if not country_suffix:
                continue
            proxy_map[market_code] = f"http://{username}:{password}_country-{country_suffix}@{domain}:{port}"
        return proxy_map
    proxy_map = {}
    for key, value in os.environ.items():
        if key.startswith("PROXY_") and value:
            market_code = key[6:]
            proxy_map[market_code] = value.strip()
    if market_filter is not None and market_filter in proxy_map:
        return {market_filter: proxy_map[market_filter]}
    return proxy_map


def get_proxy_for_market(country_code: Any, proxy_map: Optional[Dict[str, str]] = None, env_file: str = ".env") -> Optional[str]:
    """Get proxy URL for a specific market code."""
    pm = proxy_map if proxy_map is not None else load_proxy_map(country_code, env_file)
    if country_code is None:
        return None
    return pm.get(str(country_code).upper())


def _brand_market_key(brand: Any, market_map: Dict[str, Dict[str, str]]) -> Optional[str]:
    """Find market map key for a brand (with alias resolution)."""
    brand_norm = _normalize_brand_name(brand)
    alias_map = {
        "lv": "louis vuitton",
        "ap": "audemars piguet",
        "vc": "vacheron constantin",
        "van cleef arpels": "van cleef",
    }
    brand_norm = alias_map.get(brand_norm, brand_norm)
    for key in market_map.keys():
        if not str(key).startswith("MARKET_CD_"):
            continue
        key_name = str(key).replace("MARKET_CD_", "").replace("_", " ")
        key_norm = _normalize_brand_name(key_name)
        if brand_norm == key_norm:
            return key
    return None


def build_market_url(
    url: str,
    brand: Any,
    country_code: Any = None,
    market_file: str = "market_cd.json",
) -> str:
    """Rewrite URL according to market locale mapping. Returns original URL if no match found."""
    if not country_code:
        return url
    try:
        market_map = load_market_map(market_file)
    except Exception:
        return url
    key = _brand_market_key(brand, market_map)
    if key is None:
        return url
    locale = market_map.get(key, {}).get(str(country_code).upper())
    if not locale:
        return url
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    locale_parts = _split_locale_parts(locale)
    if not locale_parts:
        return url
    candidate_variants: list[list[str]] = []
    for value in market_map.get(key, {}).values():
        parts = _split_locale_parts(value)
        if not parts:
            continue
        norm_parts = [_normalize_locale_token(part) for part in parts]
        candidate_variants.append(norm_parts)
        if len(norm_parts) > 1:
            candidate_variants.append(["".join(norm_parts)])
    path_norm = [_normalize_locale_token(part) for part in path_parts]
    matched_len = 0
    for candidate in candidate_variants:
        candidate_len = len(candidate)
        if candidate_len <= len(path_norm) and path_norm[:candidate_len] == candidate:
            matched_len = max(matched_len, candidate_len)
    tail_parts = path_parts[matched_len:] if matched_len else path_parts
    new_parts = locale_parts + tail_parts
    new_path = "/" + "/".join(new_parts)
    return urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))




# ============================================================================
# BROWSER & DRIVER OPERATIONS
# ============================================================================

def _should_use_uc(
    brand: Any,
    auto_uc_brands: Optional[Iterable[str]] = None,
    use_uc: bool = False,
) -> bool:
    """Determine if undetected_chromedriver should be used for this brand."""
    auto_uc = {str(item).strip().lower() for item in (auto_uc_brands or DEFAULT_AUTO_UC_BRANDS)}
    brand_norm = str(brand).strip().lower()
    if brand_norm in auto_uc:
        return True
    return bool(use_uc)


def build_uc_driver(timeout: int = 12, proxy: Optional[str] = None):
    """Build undetected_chromedriver with optional proxy support."""
    if uc is None:
        raise RuntimeError("undetected_chromedriver is not installed.")
    options = uc.ChromeOptions()
    if proxy:
        parsed = urlparse(proxy)
        if parsed.username or parsed.password:
            extension_dir = _build_uc_proxy_extension(proxy)
            if extension_dir:
                options.add_argument(f"--disable-extensions-except={extension_dir}")
                options.add_argument(f"--load-extension={extension_dir}")
        else:
            options.add_argument(f"--proxy-server={proxy}")
    driver = uc.Chrome(options=options, version_main=147, use_subprocess=True)
    driver.set_page_load_timeout(timeout + 8)
    return driver


def _wait_for_page_ready(driver, timeout: int = 12) -> None:
    """Poll document.readyState until 'complete' or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.3)


def _wait_for_url_stable(driver, stable_for: float = 0.8, max_wait: float = 6.0) -> None:
    """Wait until current_url stops changing for stable_for seconds (JS redirects)."""
    last_url = getattr(driver, "current_url", "")
    stable_since = time.time()
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(0.25)
        current = getattr(driver, "current_url", "")
        if current != last_url:
            last_url = current
            stable_since = time.time()
        elif time.time() - stable_since >= stable_for:
            return


def _click_consent_button(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
                // 1. First, attempt a highly specific selector that targets common banner elements
                // including known OneTrust/TrustArc button IDs which Cartier often uses.
                const directSelectors = [
                    '#onetrust-accept-btn-handler',
                    'button[id*="accept"]',
                    '.cookie-banner button'
                ];
                
                for (const selector of directSelectors) {
                    const el = document.querySelector(selector);
                    if (el && el.offsetParent !== null && (el.innerText.includes('同意する') || el.innerText.includes('Accept'))) {
                        el.click();
                        return true;
                    }
                }

                // 2. Shadow DOM Deep-Search fallback
                function findElementInShadows(root, targetText) {
                    const nodes = root.querySelectorAll('button, [role="button"], input[type="button"], a');
                    for (const el of nodes) {
                        if (el.innerText && el.innerText.trim() === targetText) {
                            return el;
                        }
                    }
                    
                    // Recursively check all elements with shadow roots
                    const allElements = root.querySelectorAll('*');
                    for (const el of allElements) {
                        if (el.shadowRoot) {
                            const found = findElementInShadows(el.shadowRoot, targetText);
                            if (found) return found;
                        }
                    }
                    return null;
                }

                // Look specifically for an exact match of the Japanese "Agree" button
                const targetButton = findElementInShadows(document, "同意する");
                if (targetButton) {
                    targetButton.scrollIntoView({block: 'center'});
                    targetButton.click();
                    return true;
                }

                return false;
                """
            )
        )
    except Exception:
        return False


# ============================================================================
# PAGE DETECTION & DIAGNOSTICS
# ============================================================================

def _is_blocked_page(soup: Optional[BeautifulSoup]) -> bool:
    """Check if page content indicates blocked/restricted access."""
    if soup is None:
        return False
    page_text = soup.get_text(" ", strip=True).lower()
    blocked_signals = [
        "access denied", "forbidden", "captcha", "are you human",
        "verify you are human", "blocked", "bot", "request blocked",
        "temporarily unavailable", "security check",
    ]
    return any(signal in page_text for signal in blocked_signals)


def _is_not_found_page(soup: Optional[BeautifulSoup]) -> bool:
    """Check if page is 404 or not found."""
    if soup is None:
        return False
    page_text = soup.get_text(" ", strip=True).lower()
    not_found_signals = ["404", "page not found", "error 404", "http 404", "requested page could not be found"]
    return any(signal in page_text for signal in not_found_signals)


def build_failure_remark(soup: Optional[BeautifulSoup], fetch_error: Exception | None = None) -> str:
    """Generate descriptive failure remark based on page detection and error."""
    if _is_not_found_page(soup):
        return "error404"
    if _is_blocked_page(soup):
        return "crawler_blocked"
    if fetch_error is not None:
        msg = str(fetch_error).lower()
        if "404" in msg or "not found" in msg or "no longer exists" in msg:
            return "error404"
        if any(token in msg for token in ["blocked", "access denied", "forbidden", "captcha", "403"]):
            return "crawler_blocked"
        return f"Request failed: {type(fetch_error).__name__}"
    return "Price not found in HTML"


def tag_method_with_fetch(method_name: str, fetch_channel: str) -> str:
    """Format extraction method name with fetch channel (UC or HTTP)."""
    channel = str(fetch_channel).upper() if fetch_channel else "HTTP"
    if channel not in {"UC", "HTTP"}:
        channel = "HTTP"
    return f"{method_name} [{channel}]"


def get_brand_wait_ranges(
    brand: Any,
    long_wait_brands: Optional[Iterable[str]] = None,
    fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_FETCH_WAIT_RANGE_SECONDS,
    block_wait_range_seconds: Tuple[float, float] = DEFAULT_BLOCK_WAIT_RANGE_SECONDS,
    other_fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_FETCH_WAIT_RANGE_SECONDS,
    other_block_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_BLOCK_WAIT_RANGE_SECONDS,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Get appropriate wait ranges (fetch & block) for a brand."""
    long_wait = {str(item).strip().lower() for item in (long_wait_brands or DEFAULT_LONG_WAIT_BRANDS)}
    brand_norm = str(brand).strip().lower()
    if brand_norm in long_wait:
        return fetch_wait_range_seconds, block_wait_range_seconds
    return other_fetch_wait_range_seconds, other_block_wait_range_seconds




# ============================================================================
# MAIN API - FETCH & EXTRACT
# ============================================================================

def fetch_soup(
    brand: Any,
    url: str,
    country_code: Any = None,
    apply_market_url: bool = True,
    proxy_country_code: Any = None,
    timeout: int = 25,
    max_retries: int = 3,
    backoff_seconds: int = 1,
    driver=None,
    market_file: str = "market_cd.json",
    auto_uc_brands: Optional[Iterable[str]] = None,
    use_uc: bool = False,
    long_wait_brands: Optional[Iterable[str]] = None,
    fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_FETCH_WAIT_RANGE_SECONDS,
    block_wait_range_seconds: Tuple[float, float] = DEFAULT_BLOCK_WAIT_RANGE_SECONDS,
    other_fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_FETCH_WAIT_RANGE_SECONDS,
    other_block_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_BLOCK_WAIT_RANGE_SECONDS,
    proxy: Optional[str] = None,
    env_file: str = ".env",
) -> Tuple[Optional[BeautifulSoup], Optional[Exception], str]:
    """Fetch and parse page HTML using UC (browser) or HTTP.
    
    Returns (soup, error, fetch_channel) where:
    - soup: BeautifulSoup object or None if fetch failed
    - error: Exception object if failed, None if successful
    - fetch_channel: "UC" for browser, "HTTP" for curl_cffi
    """
    last_error: Optional[Exception] = None
    fetch_channel = "HTTP"
    fetch_wait, block_wait = get_brand_wait_ranges(
        brand,
        long_wait_brands=long_wait_brands,
        fetch_wait_range_seconds=fetch_wait_range_seconds,
        block_wait_range_seconds=block_wait_range_seconds,
        other_fetch_wait_range_seconds=other_fetch_wait_range_seconds,
        other_block_wait_range_seconds=other_block_wait_range_seconds,
    )
    effective_url = build_market_url(url, brand, country_code=country_code, market_file=market_file) if apply_market_url else url
    proxy_market_code = proxy_country_code if proxy_country_code is not None else country_code
    if proxy is None:
        proxy = get_proxy_for_market(proxy_market_code, env_file=env_file)
    candidate_urls = [effective_url]
    if effective_url != url:
        candidate_urls.append(url)
    # print("TARGET URL:", effective_url) # TESTING
    if driver is not None:
        fetch_channel = "UC"
        try:
            fallback_soup = None
            for target_url in candidate_urls:
                driver.get(target_url)
                _wait_for_page_ready(driver, timeout=timeout)
                time.sleep(0.8)  # allow consent dialogs to render after DOM ready
                _click_consent_button(driver)
                _wait_for_url_stable(driver)  # wait out post-consent client-side redirects
                _sleep_with_jitter(fetch_wait)
                final_url = getattr(driver, "current_url", "")
                if final_url and final_url.rstrip("/").lower().endswith("/404"):
                    continue
                soup = BeautifulSoup(driver.page_source, "html.parser")
                if len(candidate_urls) > 1 and (_is_blocked_page(soup) or _is_not_found_page(soup)):
                    fallback_soup = soup
                    continue
                return soup, None, fetch_channel
            if fallback_soup is not None:
                return fallback_soup, None, fetch_channel
            return None, RuntimeError(f"404 redirect: {effective_url}"), fetch_channel
        except Exception as exc:
            last_error = exc

    if _should_use_uc(brand, auto_uc_brands=auto_uc_brands, use_uc=use_uc) and driver is None:
        timeout = 25
        temp_driver = None
        fetch_channel = "UC"
        try:
            temp_driver = build_uc_driver(timeout=timeout, proxy=proxy)
            fallback_soup = None
            for target_url in candidate_urls:
                temp_driver.get(target_url)
                _wait_for_page_ready(temp_driver, timeout=timeout)
                time.sleep(0.8)
                _click_consent_button(temp_driver)
                _wait_for_url_stable(temp_driver)
                _sleep_with_jitter(fetch_wait)
                final_url = getattr(temp_driver, "current_url", "")
                if final_url and final_url.rstrip("/").lower().endswith("/404"):
                    continue
                soup = BeautifulSoup(temp_driver.page_source, "html.parser")
                if len(candidate_urls) > 1 and (_is_blocked_page(soup) or _is_not_found_page(soup)):
                    fallback_soup = soup
                    continue
                return soup, None, fetch_channel
            if fallback_soup is not None:
                return fallback_soup, None, fetch_channel
            return None, RuntimeError(f"404 redirect: {effective_url}"), fetch_channel
        except Exception as exc:
            last_error = exc
            logger.warning("UC failed for %s, fallback to curl_cffi: %s: %s", brand, type(exc).__name__, exc)
            _sleep_with_jitter(block_wait)
        finally:
            if temp_driver is not None:
                try:
                    temp_driver.quit()
                except Exception:
                    pass

    if curl_cffi is None:
        return None, RuntimeError("curl_cffi is not installed."), fetch_channel

    for attempt in range(1, max_retries + 1):
        fetch_channel = "HTTP"
        fallback_soup = None
        for target_url in candidate_urls:
            try:
                proxies = {"http": proxy, "https": proxy} if proxy else None
                response = curl_cffi.get(target_url, impersonate="chrome", timeout=timeout, proxies=proxies)
                response.raise_for_status()
                response_url = str(getattr(response, "url", ""))
                if response_url and response_url.rstrip("/").lower().endswith("/404"):
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                if len(candidate_urls) > 1 and (_is_blocked_page(soup) or _is_not_found_page(soup)):
                    fallback_soup = soup
                    continue
                return soup, None, fetch_channel
            except Exception as exc:
                last_error = exc
                continue  # try next candidate before giving up on this attempt
        if fallback_soup is not None:
            return fallback_soup, None, fetch_channel
        if attempt < max_retries:
            msg = str(last_error).lower() if last_error else ""
            if any(token in msg for token in ["blocked", "access denied", "forbidden", "captcha", "403"]):
                _sleep_with_jitter(block_wait)
            else:
                _sleep_with_jitter((backoff_seconds * attempt, backoff_seconds * attempt + 1.5))

    return None, last_error, fetch_channel




# ============================================================================
# PRICE EXTRACTION METHODS
# ============================================================================

def _iter_json_ld_nodes(node: Any):
    """Recursively iterate through JSON-LD nodes and @graph structures."""
    if isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if graph is not None:
            yield from _iter_json_ld_nodes(graph)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_ld_nodes(item)


def _extract_json_ld_price(brand: Any, soup: BeautifulSoup, input_skus: Any = None) -> Tuple[Any, Optional[str]]:
    """Extract price from JSON-LD structured data (returns price and currency)."""
    target_skus = _parse_input_skus(input_skus)
    if str(brand).lower() == "van cleef":
        main_area = soup.find("main", class_="vca-main")
        scripts = main_area.find_all("script", type="application/ld+json") if main_area else soup.find_all("script", type="application/ld+json")
    else:
        scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            for item in _iter_json_ld_nodes(data):
                if not isinstance(item, dict):
                    continue
                item_type = item.get("@type")
                is_product = ("Product" in item_type) if isinstance(item_type, list) else (item_type == "Product")
                if not is_product or "offers" not in item:
                    continue
                offers = item.get("offers")
                offers_list = offers if isinstance(offers, list) else [offers]
                for offer in offers_list:
                    if not isinstance(offer, dict):
                        continue
                    sku_val = offer.get("sku", item.get("sku"))
                    if target_skus and _sku_normalize(sku_val) not in target_skus:
                        continue
                    price = offer.get("price")
                    if _has_price_value(price):
                        currency = offer.get("priceCurrency")
                        return price, currency
        except Exception:
            continue
    return None, None


def _extract_meta_price(soup: BeautifulSoup) -> Any:
    """Extract price from meta tags."""
    meta_price = soup.find("meta", property=re.compile(r"(og:price:amount|product:price:amount)"))
    if meta_price and meta_price.get("content"):
        return meta_price["content"]
    return None


def _extract_microdata_price(soup: BeautifulSoup) -> Any:
    """Extract price from microdata schema."""
    microdata_price = soup.find(attrs={"itemprop": "price"})
    if microdata_price:
        return microdata_price.get("content") or microdata_price.text.strip()
    return None


def _extract_css_price(soup: BeautifulSoup, brand: Optional[str] = None) -> Any:
    """Extract price using CSS selectors (brand-specific, then generic)."""
    brand = str(brand).lower() if brand else None
    brand_map = {
        "rolex": [".rolex-price", "[class*='Price-StyledPrice'] .price"],
        "vca": [".vca-pdp-price-info[data-amount]", ".vca-pdp-price-info", ".vca-pdp-price [data-amount]"],
        "dior": [".dior-price",'span[data-testid="price-line"]'],
        "louis vuitton": [".lv-price","lv-price lv-product__price body-s"],
        "audemars piguet": [".ap-price", ".ap-productinfo__price"],
        "valentino": [".pdpProductInformation"],
        "cartier": [".car_pdp_right",".car-pdp__price"],
    }
    data_selectors = ["[itemprop='price']", "meta[itemprop='price']", "[data-price]", "[data-amount]"]
    generic_selectors = [".product-price", ".current-price", "#price", ".price"]
    
    selectors_to_try = []
    if brand and brand in brand_map:
        selectors_to_try.extend(brand_map[brand])
    selectors_to_try.extend(data_selectors)
    selectors_to_try.extend(generic_selectors)
    
    for selector in selectors_to_try:
        elements = soup.select(selector)
        for element in elements:
            price = (
                element.get("content") or 
                element.get("data-price") or 
                element.get("data-amount") or 
                element.get_text(strip=True)
            )
            if _has_price_value(price):
                return price
    return None


def _extract_regex_price(soup: BeautifulSoup) -> Any:
    """Extract price using regex pattern (fallback method)."""
    if not soup.body:
        return None
    pattern = re.compile(
        r"(?:[\$\£\€\¥\￥\₹\₩\₫]|AED|SAR|KWD|QAR|BHD|THB|RM|S\$|AU\$|C\$|CHF|TL|Kč|kr\.?)\s*\d+(?:[.,]\d+)?"
    )
    match = pattern.search(soup.body.text)
    return match.group(0).strip() if match else None


def extract_currency_from_text(text: Any):
    """Extract currency code from price text."""
    if not text:
        return pd.NA
    t = str(text).upper()
    symbol_map = [
        ("AU$", "AUD"), ("US$", "USD"), ("CA$", "CAD"), ("C$", "CAD"),
        ("HK$", "HKD"), ("SG$", "SGD"), ("NZ$", "NZD"), ("A$", "AUD"),
        ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("฿", "THB"),
        ("₺", "TRY"), ("₫", "VND"), ("₹", "INR"), ("₽", "RUB"),
        ("₱", "PHP"), ("₪", "ILS"), ("$", "USD")
    ]
    for token, code in symbol_map:
        if token in t:
            return code
    code_match = re.search(r"\b(AED|AUD|BRL|CAD|CHF|CNY|EUR|GBP|HKD|JPY|KRW|KWD|MYR|NZL|QAR|SAR|SGD|THB|TRY|TWD|USD|VND)\b", t)
    if code_match:
        return code_match.group(1)
    return pd.NA


def get_product_price(
    brand: Any,
    url: str,
    country_code: Any = None,
    apply_market_url: bool = True,
    proxy_country_code: Any = None,
    timeout: int = 12,
    max_retries: int = 3,
    backoff_seconds: int = 1,
    driver=None,
    market_file: str = "market_cd.json",
    auto_uc_brands: Optional[Iterable[str]] = None,
    use_uc: bool = False,
    long_wait_brands: Optional[Iterable[str]] = None,
    fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_FETCH_WAIT_RANGE_SECONDS,
    block_wait_range_seconds: Tuple[float, float] = DEFAULT_BLOCK_WAIT_RANGE_SECONDS,
    other_fetch_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_FETCH_WAIT_RANGE_SECONDS,
    other_block_wait_range_seconds: Tuple[float, float] = DEFAULT_OTHER_BLOCK_WAIT_RANGE_SECONDS,
    input_skus: Any = None,
    proxy: Optional[str] = None,
    env_file: str = ".env",
):
    """Fetch page and extract product price using multiple extraction methods.
    
    Returns (price, method, remark, currency) tuple with:
    - price: Extracted price value (int or pd.NA)
    - method: Extraction method used (e.g., "JSON-LD [UC]")
    - remark: Error message if price not found, else pd.NA
    - currency: Currency code (e.g., "USD", "THB"), else pd.NA
    """
    soup, fetch_error, fetch_channel = fetch_soup(
        brand=brand,
        url=url,
        country_code=country_code,
        apply_market_url=apply_market_url,
        proxy_country_code=proxy_country_code,
        timeout=timeout,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        driver=driver,
        market_file=market_file,
        auto_uc_brands=auto_uc_brands,
        use_uc=use_uc,
        long_wait_brands=long_wait_brands,
        fetch_wait_range_seconds=fetch_wait_range_seconds,
        block_wait_range_seconds=block_wait_range_seconds,
        other_fetch_wait_range_seconds=other_fetch_wait_range_seconds,
        other_block_wait_range_seconds=other_block_wait_range_seconds,
        proxy=proxy,
        env_file=env_file,
    )
    # print(len(soup.text) if soup else "No content") # TESTING
    if soup is None:
        return pd.NA, tag_method_with_fetch("Request Failed", fetch_channel), build_failure_remark(soup, fetch_error), pd.NA

    soup = _clean_soup(soup)  # Remove noise elements before extraction

    price, json_ld_currency = _extract_json_ld_price(brand, soup, input_skus=input_skus)
    if _has_price_value(price):
        currency = json_ld_currency or extract_currency_from_text(price)
        return clean_price(price), tag_method_with_fetch("JSON-LD", fetch_channel), pd.NA, currency

    price = _extract_meta_price(soup)
    if _has_price_value(price):
        return clean_price(price), tag_method_with_fetch("Meta Tags", fetch_channel), pd.NA, extract_currency_from_text(price)

    price = _extract_microdata_price(soup)
    if _has_price_value(price):
        return clean_price(price), tag_method_with_fetch("Microdata", fetch_channel), pd.NA, extract_currency_from_text(price)

    price = _extract_css_price(soup, brand=brand)
    if _has_price_value(price):
        return clean_price(price), tag_method_with_fetch("CSS Selectors", fetch_channel), pd.NA, extract_currency_from_text(price)

    price = _extract_regex_price(soup)
    if _has_price_value(price):
        return clean_price(price), tag_method_with_fetch("Regex Fallback", fetch_channel), pd.NA, extract_currency_from_text(price)

    return pd.NA, tag_method_with_fetch("Not Found", fetch_channel), build_failure_remark(soup, None), pd.NA
