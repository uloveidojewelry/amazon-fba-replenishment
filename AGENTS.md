# Codex development instructions
- Read-only planning system. Never submit or create shipments, listings or purchase orders.
- Never commit credentials, raw customer PII, or tokens.
- A manually stopped SKU stops its entire mapped family; manual approval is required to resume.
- Unknown family mapping must block replenishment.
- Missing inventory must block replenishment; never treat missing values as zero.
- Keep sales and inventory snapshots and change logs; no duplicate sales import.
- Run `pytest -q` after modifications.
- Do not modify unrelated repositories or production data.
