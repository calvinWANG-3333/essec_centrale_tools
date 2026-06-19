# CRP_LuxuryInsight

Luxury price-check workflow for CRP with two pipelines:
- classic price extraction,
- LLM + Google Search URL refresh.

## Repository Structure
- `frontend/code/app.py`: Streamlit UI for running and reviewing results.
- `backend/code/pipeline.py`: shared programmatic entry points for the two pipelines.
- `backend/code/cli_run_pipeline.py`: command-line wrapper around the same pipeline entry points.
- `backend/code/handlers/extractor.py`: price extraction, final save, and run summary.
- `backend/code/handlers/updator.py`: URL refresh, validation, and save.
- `backend/code/handlers/loader.py`: Excel loading and scope filtering.
- `backend/code/price_scrape_helpers.py`: reusable scraping and parsing helpers.
- `market_cd.json`: brand-specific market/locale mapping.
- `output/`: generated CSV results.

## Quick Start
1. Create a virtual environment.
2. Install dependencies from `requirements.txt`.
3. Set `GOOGLE_API_KEY` in `.env` if you plan to use the URL refresh flow.

Windows example:
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`.env`:
```env
GOOGLE_API_KEY=your_key_here
```

## Streamlit App
Run the UI from the project root:
```bash
./run_app.sh
```

The app loads an Excel file, lets you scope brands and markets, and runs either:
- `Extract Price` for the classic scraping pipeline,
- `Update Url` for the LLM refresh pipeline.

## Command Line
Use the CLI entry point for direct runs:
```bash
python backend/code/cli_run_pipeline.py <input.xlsx> <output.csv> --sheet <SheetName>
```

Examples:
```bash
python backend/code/cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Sheet1
python backend/code/cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Leather_Goods --brands "Dior, Celine"
python backend/code/cli_run_pipeline.py data/input.xlsx output/results.csv --sheet Leather_Goods --brands "Dior" --markets "USA, JPN"
python backend/code/cli_run_pipeline.py data/input.xlsx output/results.csv --mode url --sheet Leather_Goods
```

Help:
```bash
python backend/code/cli_run_pipeline.py --help
```

## Output Columns
- Price runs add `new_price`, `new_currency`, `price_method`, `remarks`, `is_price_match`, and `last_price_update_dt`.
- URL runs add `new_url`, `url_status`, `reason`, and `last_url_update_dt`.

## Notes
- `backend/code/pipeline.py` is the shared API layer used by both the UI and the CLI.
- `backend/code/cli_run_pipeline.py` is only a command-line wrapper.
- Keep prompt/schema changes aligned with downstream columns and charts.
