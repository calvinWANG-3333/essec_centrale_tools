#!/bin/bash
source "$(dirname "$0")/.venv/bin/activate"
streamlit run "$(dirname "$0")/frontend/code/app.py"
