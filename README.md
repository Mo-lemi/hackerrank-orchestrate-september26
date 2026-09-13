# Buy or Wait: Autonomous Financial Decision Agent

An autonomous financial planning engine that determines affordability, optimizes payment structures, and recommends safe execution strategies for incoming purchase requests over a 90-day simulation horizon.

## Architecture

- **`code/domain/`**: Strict Pydantic schemas enforcing data validation, currency alignment, and output formatting contracts.
- **`code/engine/`**:
  - `recurrence_detector.py`: Detects periodic income/expenses and materializes conservative future projections.
  - `cashflow_simulator.py`: Day-by-day balance walk incorporating running balances, credit card cycles, pending transactions, and overrides.
  - `safety_check.py`: Evaluates minimum balance buffers and calculates `amount_safe_to_pay` bound to the worst future day.
- **`code/optimizer/`**:
  - `spending_changes.py`: Double-gated override selector (flexibility ∩ eligible categories) restricted to <= 3 greedy interventions.
  - `plan_generator.py`: Generates candidate payment methods (`full_payment`, `partial_payment`, `installments`, `wait`).
  - `ranking.py`: Strict multi-tier deterministic tie-breaking hierarchy reconciling status tiers, override count, total payable, and timing.
- **`code/pipeline/`**: Orchestrates event reconciliation, currency exchange (FX), recurrence materialization, candidate evaluation, and deterministic decision explanations.
- **`code/io_/`**: Robust CSV loaders, incremental flushing writers, and format parsers.

## Key Design Principles

1. **Deterministic & Network-Independent**: Full execution runs with `--skip-vision` locally in under 25 seconds with zero external network or API dependencies.
2. **Conservative Cashflow Bounds**: Debits bind on the worst future balance day across the entire 90-day horizon rather than day zero.
3. **Double-Gated Spending Optimization**: Overrides require both non-protected category status and explicit profile flexibility flags, capped strictly at 3 changes.
4. **Resilient Streaming Output**: The incremental writer flushes row-by-row, ensuring graceful degradation with conservative fallback rows if an isolated request encounters anomalies.

## Setup & Running

### Installation
```bash
pip install -r requirements.txt

```
### Running Tests
```bash
pytest code/tests/ -v -p no:debugging

```
