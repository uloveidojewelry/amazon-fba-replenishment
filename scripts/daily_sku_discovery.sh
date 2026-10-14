#!/usr/bin/env bash
set -euo pipefail
project_dir="${FBA_PROJECT_DIR:-/workspace/amazon-fba-replenishment}"
fba_bin="${FBA_PLAN_BIN:-/workspace/amazon-fba-venv/bin/fba-plan}"
cd "$project_dir"
exec "$fba_bin" --db output/sku-review/review.db --marketplace US discover-skus \
  --daily --confirm-read-only --catalog-sample-size 0 --output-dir output/sku-review "$@"
