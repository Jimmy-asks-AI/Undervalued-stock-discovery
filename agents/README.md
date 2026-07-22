# Agent Playbook

This folder defines the human-readable role contract for the Fundamental Value Research OS.

The executable V0 implementation is in `strategy_lab/fundamental_value_os/agents.py`; the machine-readable role graph is in `configs/fundamental_value_agents.json`.

## Shared Rules

Every agent must:

- state its input fields and timestamp assumptions;
- separate current snapshot research from point-in-time historical evidence;
- list red flags and failure cases;
- avoid investment advice wording;
- pass structured output to the next agent instead of only writing prose.

## Agent Contracts

### chief_value_orchestrator

Owns the run. It selects the universe, loads the factor registry, calls downstream agents, enforces data/validation gates, and decides whether a result is a candidate, watchlist item, rejected item, or validation task.

Output:

- run manifest;
- score model version;
- agent status table;
- promotion or rejection status.

### fundamental_data_steward

Audits whether the data can be used. This agent has veto power over historical backtests.

Must reject or quarantine:

- missing `available_date`;
- `available_date > trade_date`;
- current-only industry membership in historical tests;
- missing ST/suspension/lifecycle fields;
- raw unadjusted price labels for long-horizon validation.

### valuation_factor_researcher

Computes absolute cheapness. It must handle negative or invalid denominators explicitly.

Core fields:

- PE, PB, PCF;
- FCF yield;
- dividend yield;
- optional EV/EBITDA or sector-specific valuation fields.

### industry_relative_value_agent

Compares each asset against historical point-in-time industry peers.

Must check:

- peer count;
- industry classification date;
- sector-specific metrics;
- whether the asset is cheap only because the whole industry is distressed.

### profitability_growth_analyst

Scores whether the company can sustain value creation.

Core fields:

- ROE and ROIC;
- revenue growth;
- net profit growth;
- margin stability where available.

### shareholder_return_analyst

Reviews dividend, buyback, and payout sustainability.

Must distinguish:

- sustainable dividends;
- one-off dividends;
- high yield caused by price collapse;
- payouts funded by debt or weak cash flow.

### accounting_quality_auditor

Finds accounting red flags.

Core checks:

- OCF/net income;
- accrual pressure;
- leverage;
- interest coverage;
- receivables, inventory, goodwill, and related-party issues when fields are available.

### value_trap_risk_agent

Decides whether cheapness is likely a trap.

Hard blocks:

- ST or delisting risk;
- suspension/non-tradability;
- invalid data;
- severe cash-flow and solvency failure.

Soft penalties:

- weak liquidity;
- high payout with weak cash conversion;
- high leverage;
- deteriorating growth.

### factor_validation_auditor

Prevents unvalidated factors from being promoted.

Minimum evidence:

- IC/RankIC;
- group returns;
- neutralized incremental return;
- OOS/walk-forward;
- costs, turnover, and capacity;
- stability by year, industry, size, and market regime.

### research_report_synthesizer

Writes the final candidate report.

Must include:

- score decomposition;
- reason for cheapness;
- value-trap review;
- data status;
- validation status;
- expected-return decomposition;
- failure cases and next required tests.
