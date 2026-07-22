# Fundamental Value Data Contract

Status: V0.2 draft

This contract defines the minimum data boundary for a fundamental-factor undervalued asset discovery system.

The machine-readable companion schema is `data_catalog/input_asset_panel_schema.csv`.
Use `scripts/validate_asset_panel_schema.py` before running any current snapshot or PIT panel through the scoring pipeline.

## Timestamp Rule

Every historical research row must carry:

- `trade_date`: the decision or rebalance date.
- `available_date`: the earliest date the raw fundamental information could be observed by an investor.
- `report_period`: the financial statement period.
- `source`: data vendor or file origin.
- `data_status`: one of `pit_verified`, `research_only`, `prototype`, `not_pit`, `rejected`.

Rows without `available_date` cannot enter historical factor backtests. They can only be used for current snapshot research and must be tagged `research_only` or `prototype`.

## Minimum Asset Fields

| Field | Required | Notes |
|---|---:|---|
| `asset` | yes | Stable security code. Keep leading zeros. |
| `name` | yes | Security name at `trade_date` if historical. |
| `trade_date` | yes | Decision date. |
| `available_date` | yes for backtest | Must be no later than `trade_date`. |
| `industry` | yes | Must be point-in-time for history. |
| `market_cap` | yes | Unit must be explicit. |
| `avg_amount_20d` | yes | Liquidity gate. |
| `st_flag` | yes | Historical status needed for backtest. |
| `suspend_flag` | yes | Historical tradability needed. |

## Valuation Fields

| Field | Direction | Pit Risk |
|---|---|---|
| `pe_ttm` | lower is cheaper, but invalid if earnings <= 0 | high |
| `pb` | lower is cheaper | medium |
| `pcf_ocf_ttm` | lower is cheaper if OCF positive | high |
| `fcf_yield_ttm` | higher is better | high |
| `dividend_yield_ttm` | higher is better if sustainable | high |
| `ev_ebitda` | lower is cheaper | high |

## Quality And Safety Fields

| Field | Direction | Notes |
|---|---|---|
| `roe_ttm` | higher | Needs denominator sanity check. |
| `roic_ttm` | higher | Prefer for cross-industry capital efficiency. |
| `gross_margin_stability_3y` | higher | Industry-specific. |
| `ocf_to_net_income` | higher | Below 0.6 is warning. |
| `accruals_to_assets` | lower | Higher accruals are accounting risk. |
| `debt_to_assets` | lower | Sector-specific thresholds. |
| `interest_coverage` | higher | Negative/low is risk. |
| `payout_ratio` | moderate | Too high may be unsustainable. |

## Growth Fields

| Field | Direction | Notes |
|---|---|---|
| `revenue_cagr_3y` | higher, but capped | Penalize collapse and extreme one-offs. |
| `net_profit_cagr_3y` | higher, but capped | Negative base years need special handling. |
| `eps_growth_stability_5y` | higher | Prefer stable compounding to one-year spike. |

## Labels For Validation

Historical labels must use adjusted total-return data where possible.

Required label examples:

- forward 60/120/252-day total return;
- benchmark-relative forward return;
- industry-relative forward return;
- tradability-adjusted return after costs and slippage.

Raw unadjusted prices are not acceptable for long-horizon return labels.

## Data Quality Gates

Reject or quarantine rows when:

- `available_date` is missing for historical backtest.
- `available_date > trade_date`.
- `industry` is current-only in a historical panel.
- security lifecycle, delisting, ST, suspension, or limit status is unavailable for the tested period.
- valuation denominator is negative or near zero but not explicitly handled.
- duplicated `asset + trade_date + report_period` rows exist.
- fields mix different vendors or units without source mapping.

## Current Snapshot Rule

Current snapshots are useful for watchlist research. They cannot prove factor performance.

Allowed output label:

```text
research_only_current_snapshot
```

Disallowed output label:

```text
validated_alpha
```

## Source Inventory Rule

The prioritized source queue is `data_catalog/fundamental_value_source_inventory.csv`.
Each data acquisition task must state which downstream capability it unlocks, such as lifecycle control, valuation factors, dividend sustainability, historical industry comparison, adjusted return labels, or validation.

No source should be promoted from `planned_not_acquired` to `pit_verified` until row-level timestamp, lifecycle, and source-vintage rules are documented.
