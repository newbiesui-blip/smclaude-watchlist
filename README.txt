ATHENA market-environment entry gate

Files:
- market_environment.py — deterministic, fail-closed entry-only policy layer.
- test_market_environment.py — regression tests.
- full_scan_market_environment.patch — minimal integration patch for main.

Integration:
1. Import market_environment in full_scan.py.
2. Evaluate the environment once per run.
3. Pass the state into scan_all.
4. Apply it BEFORE update_setup_lifecycle for NEW SMC setups.
5. Prevent auto-add while blocked.
6. Do NOT put the gate around Position Registry reconciliation,
   orphan discovery, health, intelligence, or BUG-2 monitoring.
7. Do NOT modify smc_scanner.py.

Important operational point:
The gate intentionally fails closed until both event data and liquidity data
are supplied. Configure MARKET_ENV_EVENTS_JSON and
MARKET_ENV_LIQUIDITY_JSON in the unattended runtime. Existing BingX OPEN
positions remain monitorable even while new entries are blocked.
