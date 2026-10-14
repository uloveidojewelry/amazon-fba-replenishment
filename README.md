# Amazon FBA Replenishment — read-only planning

Python 3.10+ / SQLite / requests / openpyxl. No shipment, listing, purchase-order or other Amazon business write operation exists. Only LWA token exchange uses POST; business collection is allowlisted GET. Live requests require explicit `--confirm-read-only`; the default diagnostic and mock mode are offline.

Auto SKU Discovery V1.0 persists marketplace/SellerSKU identity and history, separates
candidate and approved mappings, inherits family stops and produces review workbooks.
See [自动 SKU 发现与每日运行](docs/AUTO_SKU_DISCOVERY.zh-CN.md).

Finite-set batch review adds evidence based priority queues, explicit family approval
or rejection, confirmed stop-list imports and immutable before/after audits. See
[SKU 系列批量审核与导入](docs/SKU_BATCH_REVIEW.zh-CN.md). Preparing a queue is offline;
exact reference matches and naming candidates remain unapproved.

The first exact-reference cohort has a separate `APPROVE / REJECT / NEEDS_REVIEW`
workbook and SELECT-only simulation. See [第一批 SKU 审核说明](docs/FIRST_BATCH_REVIEW.zh-CN.md).
Preparing or simulating this workbook never imports mappings; a later explicit user
confirmation is required for any formal configuration import.

Independent SKU acceptance is an opt-in mode via `sku-sync-plan` and `sku-plan`.
It reuses the resumable 90-day collectors, keeps the original family configuration
in its source database, and calculates SKU scenarios without family approval or
family stop inheritance. Explicit SKU stops remain effective. See
[单 SKU 独立统计、恢复与试算参数](docs/INDEPENDENT_SKU.zh-CN.md).

## Install and test

```bash
python -m venv /workspace/amazon-fba-venv
/workspace/amazon-fba-venv/bin/python -m pip install -e '.[dev]'
/workspace/amazon-fba-venv/bin/pytest -q
```

Run commands from the project root. Activate the virtual environment or use `/workspace/amazon-fba-venv/bin/fba-plan`. This CLI requires no background service. Never store credentials or customer PII in Git.

## CSV planning

```bash
fba-plan import sku data/sku.csv
fba-plan import sales data/sales.csv
fba-plan import stock data/stock.csv
fba-plan import inbound data/inbound.csv
fba-plan stop 'J001-size 5' --reason 'Discontinued family'
fba-plan plan --asof 2026-10-14 --lead-days 35 --target-days 75 --output output/replenishment.xlsx
```

The uploaded example contains just **one sales day**, not 30. Its workbook correctly sets J001 children to `STOPPED` and J002 to `INCOMPLETE_SALES`, all with quantity zero. Do not fabricate the other 29 days. Each CSV sales row must be a complete daily total. Explicit zero-sales rows are needed for days with no sales. Reimporting the same daily total replaces it, never adds it again.

`fba-plan resume 'J001-size 5' --approved-by 'Manager'` records a manual approval. This records the operator-supplied approver; it does not implement user authentication or enforce manager identity. Other active stops still block the family. Stop records survive reopening the DB, new children, and mapping changes. Remapping a stopped trigger conservatively blocks both the original and newly assigned family until that trigger is explicitly resumed.

Explicit `family` mapping is preferred. Conservative inference only accepts `J001-size 5` form. Unknown family, missing/stale stock, incomplete sales, unknown active-inbound ETA, and the latest failed collection all block replenishment. Marketplace namespaces are isolated. A disabled child (`enabled=0`) also blocks its mapped family; `resume --approved-by` is required to re-enable it. CSV mapping imports do not clear disabled flags.

## Read-only SP-API synchronization

Configure environment bindings securely in environment settings; `.env.example` is documentation, not automatically loaded. Required:

- `SP_API_LWA_CLIENT_ID`, `SP_API_LWA_CLIENT_SECRET`, `SP_API_REFRESH_TOKEN`: authorized seller application LWA credentials.
- `SP_API_MARKETPLACE_ID`: actual Amazon marketplace ID (default US `ATVPDKIKX0DER`).
- `SP_API_ENDPOINT`: correct regional HTTPS endpoint, default `https://sellingpartnerapi-na.amazon.com`.
- `SP_API_ORDERS_VERSION`: default `2026-01-01`; `v0` is an explicit deprecated compatibility option.
- `SP_API_TIMEZONE`: IANA timezone for daily sales, default UTC. Choose before first sales sync. For US business-day accounting, e.g. `America/Los_Angeles`; do not silently mix timezones.

```bash
fba-plan --marketplace US sync --kind all --confirm-read-only
# Optional individual refreshes:
fba-plan --marketplace US sync --kind sales --asof 2026-10-13 --timezone UTC --confirm-read-only
fba-plan --marketplace US sync --kind stock --confirm-read-only
fba-plan --marketplace US sync --kind inbound --confirm-read-only
fba-plan --marketplace US plan --output output/replenishment.xlsx
```

`--marketplace` is the local namespace, not the API ID. Its first successful sync binds it to `SP_API_MARKETPLACE_ID`; subsequent syncs reject another API marketplace. Daily sales timezone is also persisted and cannot silently change. Use a separate DB/namespace to migrate. Plan defaults to the previous completed day in the persisted sales timezone (UTC for CSV). Stock freshness defaults to two days relative to current date/asof, not two days after an arbitrary historical asof.

Implemented collectors:

| Data | Official read operation | Local behavior |
|---|---|---|
| Orders | Orders 2026-01-01 `searchOrders` (v0 explicit compatibility) | AFN only, full creation-date window, all pages, canceled excluded; unresolved pending statuses reject the sales snapshot |
| Order items / sales | Embedded `orderItems` in 2026 search responses (v0 `getOrderItems` compatibility) | All search pages, item identity deduplication, executed item cancellations excluded, gross ordered units by purchase day; fill verified absent dates with zero for mapped SKUs |
| FBA stock | Inventory v1 `getInventorySummaries` | No incremental date/SKU filter; top-level pagination; aggregate distinct condition/FNSKU identities, require quantity details; full marketplace replacement |
| Inbound shipments | Fulfillment Inbound v0 `getShipments`, `getShipmentItems` | All statuses/pages; continuation items use `getShipmentItemsByNextToken`; remaining units = max(0, shipped-received); reconcile disappeared/terminal shipments |

Collectors strip customer fields. Only order ID, item ID, SKU, day, status, and units are persisted. New Orders requests use `FULFILLMENT,CANCELLATION`, never BUYER/RECIPIENT datasets. Redirects are rejected; TLS/proxy failures have safe error codes without response-body logging. Missing required fields, repeated pagination tokens, ambiguous identities and API errors fail closed. Rate limits/transient failures have bounded retries; 401 refreshes once. Error messages and sync logs omit response bodies/tokens. TLS verification remains enabled.

`sync --kind all` fetches and validates **all** collections before a single SQLite transaction. Failure retains previous snapshots and records `FAILED`; plans block until the affected sources have successful refreshes or verified CSV replacements. Inventory absent from a successful full response is removed, not treated as zero. Sales re-sync replaces the 30-day window so subsequent cancellations are reconciled. SQLite `snapshots`, `sync_log`, and `audit` retain input versions and actions. Do not mix sellers inside one marketplace namespace/database.

Inbound V0 has **no reliable ETA**. `ConfirmedNeedByDate` is not an ETA. Import confirmed ETAs through inbound CSV; sync retains ETA by SKU/shipment ID. Active shipments without ETA block that SKU. WORKING/terminal shipments do not reduce planned demand. Fulfillable is already separate from reserved/unfulfillable; inventory summary inbound totals are never deducted a second time.

## Calculation and Excel

Demand window: 30 calendar days, inclusive of `asof`. Horizon = lead + target + safety days.

`raw = max(0, ceil(daily_sales * horizon - fulfillable - eligible_inbound))`

If raw > 0, round `max(raw, MOQ)` up to the next case-pack multiple. Eligible inbound is SHIPPED/IN_TRANSIT/DELIVERED/CHECKED_IN/RECEIVING with known ETA <= asof + lead days. Zero demand produces zero replenishment. This is a simplified horizon model, not a time-phased forecast.

Workbook sheets: Replenishment, Alerts, Sales, Inventory, Inbound, Orders, Sync Log, Audit, Parameters. They contain actual SQLite data and calculation settings, filters and frozen headers. External strings are literal text to prevent Excel formula injection. Snapshots remain in SQLite rather than duplicating every historical version in Excel.

## Permissions and remaining work

Enable the seller-authorized SP-API app with **Inventory and Order Tracking** (Orders/FBA Inventory) and **Amazon Fulfillment** (Fulfillment Inbound) roles as applicable, then authorize the application for the target seller/marketplace. These role names are preparation requirements, not proof of current approval: the live documentation website was proxy-blocked during this phase. Latest official models were retrieved; roles remain subject to current Amazon approval and a separately confirmed endpoint smoke test. These collectors need no buyer/address restricted-data access or RDT; they never call buyerInfo/address APIs.

Allow HTTPS to `api.amazon.com` and the selected regional host (`sellingpartnerapi-na.amazon.com`, `sellingpartnerapi-eu.amazon.com`, or `sellingpartnerapi-fe.amazon.com`). Keep existing package/GitHub network presets. No inbound service port is needed.

**Not yet implemented / verified:** Reports API request/poll/download adapters (report creation uses POST and requires separately agreed read-data scope), Sales v1 metrics, Fulfillment Inbound 2024 plan/delivery-window adapters, carrier ETA, refunds/net-sales reports, hosted daily scheduling, seasonality, time-phased forecast and production approval identity enforcement. LWA, Orders, FBA Inventory and Inbound v0 real read-only smoke tests passed. Current FBA inventory was fully collected; 90-day sales remain paused pending mapping review. Catalog relationships and Inbound 2024 plan probes returned 403. Review all procurement decisions manually.

Official models checked during development:
- https://github.com/amzn/selling-partner-api-models/blob/main/models/orders-api-model/ordersV0.json
- https://github.com/amzn/selling-partner-api-models/blob/main/models/fba-inventory-api-model/fbaInventory.json
- https://github.com/amzn/selling-partner-api-models/blob/main/models/fulfillment-inbound-api-model/fulfillmentInboundV0.json


## Connection preparation and offline diagnostics

See [Seller Central authorization and Codex Secrets guide](docs/SP_API_SETUP.zh-CN.md) for current-model findings, roles, exact variable names, safe error codes and authorization steps.

```bash
# No network, no SQLite file creation, no credential values:
fba-plan diagnose-spapi
# In-memory synthetic collection, complete 30-day data and family-stop verification:
fba-plan diagnose-spapi --mock
# Isolated synthetic store, no credentials needed; defaults to output/mock-fba.db:
fba-plan sync --mock
fba-plan --db output/mock-fba.db plan --output output/mock-replenishment.xlsx
```

Mock snapshots are labeled `MOCK`, the workbook mode is `SIMULATED_READ_ONLY`, and simulated orders/shipments have SIMULATED IDs. The mock DB starts with the J001 family stopped and J002 active. Mock mode refuses existing real data; live sync refuses mock databases. The uploaded one-day CSVs and their original sample workbook are unchanged.

Only after explicit user confirmation and secure credential configuration:

```bash
fba-plan diagnose-spapi --live --confirm-read-only
```

This authenticates via LWA and checks one response page for each of Inventory, Orders and Inbound. It prints only status/counts/safe errors, does not write a DB, and does not certify a complete snapshot. Requests may retry within the bounded retry budget. A token success alone does not validate all roles. Default offline exit code 0 means syntactically valid configuration, not working credentials or a connected store; check `configuration_complete`, `authentication_verified` and `checks` separately. Failed/blocked diagnostics exit 1; unconfirmed live sync exits 2 without creating a DB.

Official model review found Orders v0 GET operations marked `deprecated: true` and the 2026-01-01 replacement. No retirement date is inferred from that flag. The 2026 adapter uses camelCase parameters, top-level `orders`/`pagination.nextToken`, and embedded item records. It has offline tests only. FBA Inventory remains v1; retained Inbound v0 read operations are in the current model and are not marked deprecated there. Inbound 2024-03-20 is a separate API, not a reason to call its shipment creation/confirmation operations.
