#!/bin/bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=${CAMPUS_TEST_PYTHON:-$root/.venv/bin/python}
cd "$root/server"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root/server:$root/scripts"
export CAMPUS_SCIHUB_FALLBACK=0
"$python" -B -m unittest test_deployment_guard test_scihub test_retrieval test_ai_search \
 test_publisher_downloads test_aip_downloads test_springer_pdf test_wiley_downloads \
 test_crossref_cambridge test_springer_bundle test_research_sources test_article_bundle \
 test_native_file_output test_article_downloads
CAMPUS_SERVER_PATH="$root/server/server.py" "$python" -B local_test.py
