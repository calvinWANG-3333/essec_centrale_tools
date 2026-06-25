from handlers.extractor import run_price_pipeline
from handlers.updator import run_url_update_pipeline as _run_url_update_pipeline
import logging

logging.basicConfig(
level=logging.INFO,
format="%(asctime)s [%(levelname)s] %(message)s",
handlers=[
    logging.FileHandler("scraper_debug.log"),
    logging.StreamHandler()
])

def run_classic_pipeline(
    input_path: str,
    sheet_name: str,
    output_path: str,
    target_scope: dict,
    max_errors_threshold: int | None = None,
    progress_callback: "callable | None" = None,
) -> str:
    return run_price_pipeline(
        input_path=input_path,
        sheet_name=sheet_name,
        output_path=output_path,
        target_scope=target_scope,
        max_errors_threshold=max_errors_threshold,
        progress_callback=progress_callback,
    )


def run_url_pipeline(
    input_path: str,
    sheet_name: str,
    output_path: str,
    target_scope: dict,
    progress_callback: "callable | None" = None
) -> str:
    return _run_url_update_pipeline(
        input_path=input_path,
        sheet_name=sheet_name,
        output_path=output_path,
        target_scope=target_scope,
        progress_callback=progress_callback
    )
