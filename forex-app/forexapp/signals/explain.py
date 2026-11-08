"""Claude explanations (Phase 7): a 2-3 sentence plain-English reason for a signal.

Kept out of the hot path: signals come from fast, tested code, and Claude only
writes the explanation after a signal exists, in a background task. It never
slows or changes the numbers. Needs ``ANTHROPIC_API_KEY`` (or another
credential the Anthropic SDK picks up) and ``FOREX_EXPLAIN=1``.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

SYSTEM = (
    "You explain forex trade signals to retail traders in plain English. You get one signal as JSON, "
    "produced by tested code from an opening-range breakout rule, a Kronos price forecast and a "
    "Monte Carlo replay. Write 2 or 3 short sentences: why the signal appeared (the setup and the "
    "breakout), what the forecast and replay numbers say, and the main risk. Use only numbers that are "
    "in the JSON, never invent any, never recommend taking the trade, and never promise an outcome. "
    "No headings, no lists, no markdown."
)

FIELDS = ("pair", "side", "setup", "status", "entry", "sl", "tp1", "tp2", "sl_pips", "tp1_pips", "tp2_pips",
          "kronos_confidence", "timemachine_odds", "expectancy_r", "risk_check", "lots", "range", "trigger",
          "spread_pips", "atr_pips")


def compact(signal: dict) -> dict:
    d = {k: signal.get(k) for k in FIELDS}
    tmc = signal.get("timemachine") or {}
    d["timemachine_verdict"] = tmc.get("verdict")
    d["kronos_paths"] = {k: (signal.get("kronos") or {}).get(k) for k in ("paths", "entered", "tp_first", "sl_first",
                                                                          "neither", "direction_prob")}
    d["filters"] = signal.get("filters")
    return d


class Explainer:
    def __init__(self, model: str = "claude-opus-5-5", timeout: float = 60.0):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(timeout=timeout, max_retries=2)
        self.model = model

    async def explain(self, signal: dict) -> str | None:
        a = self.anthropic
        try:
            # Server-side fallbacks: if a safeguard declines, the API retries on a fallback model.
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": "Signal:\n" + json.dumps(compact(signal), default=str)}],
            )
        except a.RateLimitError:
            log.warning("explanation skipped: rate limited")
            return None
        except a.APIStatusError as e:
            log.warning("explanation failed: HTTP %s %s", e.status_code, e.message)
            return None
        except a.APIConnectionError as e:
            log.warning("explanation failed: %s", e)
            return None
        if resp.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None


def make_explainer(cfg):
    if not cfg.explain:
        return None
    try:
        return Explainer(cfg.explain_model)
    except Exception as e:  # SDK missing or no credentials
        log.warning("Claude explanations off: %s", e)
        return None
