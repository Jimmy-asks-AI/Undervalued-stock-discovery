# Financial Sector Value Factor Framework

Version: 0.8.0

Date: 2026-06-12

## Purpose

Financial stocks often look cheap on PE and PB because the market is pricing balance-sheet risk, credit losses, capital constraints, spread compression, market-risk exposure, or insurance liability risk. The financial-sector value agent therefore cannot reuse the generic quality-value gate as a final conclusion.

This framework separates:

1. Tradable valuation cheapness.
2. Evidence that the low valuation is not only risk pricing.
3. Missing sector-critical data that blocks confirmation.

## Common Financial-Sector Factors

| Factor | Direction | Use | Failure Mode |
|---|---:|---|---|
| PB | lower with context | Primary balance-sheet valuation anchor. | Low PB can reflect expected asset write-downs or capital shortfall. |
| PE TTM | lower with context | Earnings valuation check. | Cyclical or provision-light earnings can make PE falsely low. |
| ROE / PB | higher | Cheapness adjusted for capital return; a high ROE at low PB is more interesting than low PB alone. | ROE can be inflated by leverage or under-provisioning. |
| Dividend yield | higher if sustainable | Shareholder return support. | High yield from price collapse or payout stress is a value-trap sign. |
| Profit and revenue growth stability | higher | Confirms earnings are not collapsing. | Financial earnings are policy, rate, market, and credit-cycle sensitive. |
| Equity / assets and asset leverage | higher equity ratio, lower leverage | Proxy for loss absorption when true regulatory capital is missing. | Accounting equity is not a substitute for risk-weighted capital. |

## Bank-Specific Factors

Required for confirmation:

- Non-performing loan ratio: lower is better.
- Special mention loan ratio and overdue loan ratio: lower is better.
- Provision coverage ratio and loan-loss reserve ratio: higher is better when not driven only by low NPL recognition.
- Core tier-1 capital adequacy, tier-1 capital adequacy, capital adequacy: higher is better.
- Net interest margin and deposit cost: stable or improving is better.
- Loan-to-deposit ratio: should be in a reasonable range; too high can indicate funding pressure.
- Loan growth and deposit growth: moderate and deposit-supported growth is better.

Current V0.8 proxy fields:

- `loan_to_deposit`
- `loan_growth_yoy`
- `deposit_growth_yoy`
- `equity_to_assets`
- `asset_leverage`

Blocked confirmation fields:

- `npl_ratio`
- `provision_coverage_ratio`
- `core_tier1_capital_adequacy_ratio`
- `capital_adequacy_ratio`
- `net_interest_margin`

## Securities-Specific Factors

Required for confirmation:

- Risk coverage ratio.
- Capital leverage ratio.
- Liquidity coverage ratio.
- Net stable funding ratio.
- Net capital and net capital trend.
- Proprietary trading and derivatives exposure relative to equity or net capital.
- Financing and securities-lending exposure relative to net capital.
- Fee income versus trading/investment income mix.

Current V0.8 proxy fields:

- `asset_leverage`
- `equity_to_assets`
- `financial_market_risk_assets_to_equity`
- `repo_funding_to_equity`
- `customer_deposit_to_liabilities`

Blocked confirmation fields:

- `risk_coverage_ratio`
- `capital_leverage_ratio`
- `liquidity_coverage_ratio`
- `net_stable_funding_ratio`
- `net_capital`

## Insurance-Specific Factors

Required for confirmation:

- Price to embedded value.
- Embedded value growth.
- New business value growth and margin.
- Core solvency adequacy ratio.
- Comprehensive solvency adequacy ratio.
- Combined ratio or loss ratio for P&C insurers.
- Investment yield and asset impairment sensitivity.

Current V0.8 proxy fields:

- `asset_leverage`
- `equity_to_assets`
- `profitability_quality`
- `growth_stability`

Blocked confirmation fields:

- `price_to_embedded_value`
- `embedded_value_growth`
- `new_business_value_growth`
- `core_solvency_ratio`
- `comprehensive_solvency_ratio`
- `combined_ratio`

## V0.8 Decision Rule

The financial-sector agent may produce a ranked proxy list, but it must not mark a financial stock as confirmed undervalued unless the sector-critical fields above are available and pass risk gates.

Current output statuses:

- `proxy_pass_regulatory_data_required`: valuation and balance-sheet proxies are acceptable, but critical regulatory metrics are missing.
- `cheap_but_proxy_risk_not_cleared`: generic value quality exists, but the financial-specific proxy gate fails.
- `data_fetch_failed`: current balance-sheet data could not be fetched.

All V0.8 financial outputs remain `research_only`.
