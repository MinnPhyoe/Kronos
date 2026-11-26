"""A grade for each signal that passed every required check: A+, A or B, with the reasons.

The grade rests on the Time Machine (replays of how similar past breakouts played out), the risk
check and the market regime. Kronos is not part of it unless "Kronos must agree" is on: replayed
against recent EUR/USD, Kronos-small was less accurate than "no change" and pulled its forecasts
toward the average price of its input (see scripts/kronos_diagnose.py).

A grade says how strong the evidence is, not that a trade will win.
"""
from __future__ import annotations

APLUS_EDGE = 0.10          # Time Machine: target-first odds at least 10 points above break-even
APLUS_AVG_R = 0.15         # and the replays average at least +0.15 R a trade
APLUS_POOL = 30            # from at least 30 replayed past situations
A_EDGE = 0.05


def grade_signal(sig: dict, kronos_gate: bool) -> dict | None:
    if not sig.get("passed"):
        return None
    tm = sig.get("timemachine") or {}
    odds, be, avg_r, pool = tm.get("odds"), tm.get("break_even"), tm.get("avg_R"), tm.get("pool") or 0
    edge = (odds - be) if odds is not None and be is not None else None
    risk = sig.get("risk") or {}
    regime = ((sig.get("extras") or {}).get("regime") or {}).get("label")
    why, missing = [], []

    if edge is not None:
        why.append("Time Machine: %.0f%% target first vs %.0f%% break-even (%+.0f points), %+.2f R on average, %d replays"
                   % (100 * odds, 100 * be, 100 * edge, avg_r or 0, pool))
    if edge is None or edge < APLUS_EDGE:
        missing.append("Time Machine edge under %d points" % round(100 * APLUS_EDGE))
    if avg_r is None or avg_r < APLUS_AVG_R:
        missing.append("Time Machine average under %+.2f R" % APLUS_AVG_R)
    if pool < APLUS_POOL:
        missing.append("fewer than %d past situations to replay" % APLUS_POOL)
    if risk.get("warnings"):
        missing.append("risk check warnings: %s" % "; ".join(risk["warnings"]))
    if regime == "ranging":
        missing.append("the market is ranging")

    if not missing:
        grade = "A+"
    elif edge is not None and edge >= A_EDGE and (avg_r or 0) > 0:
        grade = "A"
    else:
        grade = "B"

    k = sig.get("kronos") or {}
    if k.get("confidence") is not None:
        why.append("Kronos (%s): %.0f%% of paths reach TP1 first" % ("must agree" if kronos_gate else "advice only",
                                                                   100 * k["confidence"]))
    return {"grade": grade, "why": why, "missing": missing if grade != "A+" else [],
            "kronos_gate": kronos_gate,
            "note": "A grade says how strong the evidence is, not that the trade will win."}
