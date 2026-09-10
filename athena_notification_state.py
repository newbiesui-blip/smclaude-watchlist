"""Compatibility entrypoint for ATHENA notification state.

Keeps the production import name expected by the GitHub Actions launcher while
using the existing central notification implementation. Also prevents the
legacy lifecycle gate from suppressing qualifying WAIT/NEAR opportunity alerts.
"""
from __future__ import annotations

import athena_notification as _state

claim_opportunity_alert = _state.claim_opportunity_alert
record_opportunity_alert = _state.record_opportunity_alert
event_key = _state.event_key

try:
    import smc_scanner as scanner
    _original_lifecycle = getattr(scanner, "update_setup_lifecycle", None)
    if callable(_original_lifecycle) and not getattr(scanner, "_ATHENA_LIFECYCLE_ALERT_PATCHED", False):
        def _lifecycle_with_opportunity_alert(symbol, plan, scan_cycle):
            result = _original_lifecycle(symbol, plan, scan_cycle)
            if not isinstance(result, dict):
                return result
            status = plan.get("status")
            qualifies = (
                status in {"READY_MARKET", "READY_LIMIT", "NEAR_READY", "WAIT_PULLBACK", "WAIT_BREAKOUT", "WAIT_RETEST"}
                and float(plan.get("setup_quality", 0) or 0) >= float(getattr(scanner, "MIN_SETUP_QUALITY", 70))
                and float(plan.get("actionable_rr", 0) or 0) >= float(getattr(scanner, "MIN_STRUCTURAL_RR", 2.0))
                and float(plan.get("setup_score", plan.get("score", 0)) or 0) >= float(getattr(scanner, "MIN_SETUP_SCORE", 60))
            )
            if qualifies:
                fingerprint = result.get("fingerprint") or plan.get("setup_fingerprint")
                should_alert, reason = claim_opportunity_alert(
                    symbol, plan.get("direction"), fingerprint, status, scan_cycle=scan_cycle
                )
                result["send_alert"] = bool(should_alert)
                result["reason"] = reason
            return result
        scanner.update_setup_lifecycle = _lifecycle_with_opportunity_alert
        scanner._ATHENA_LIFECYCLE_ALERT_PATCHED = True
except Exception as exc:
    print(f"! ATHENA lifecycle compatibility patch unavailable: {type(exc).__name__}: {exc}")
