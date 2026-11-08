"""Alerts (trader-alerts, sent to the app and phones instead of the terminal).

Every alert goes on the bus (the app shows it and, if allowed, raises a
browser notification). Optional sinks: a webhook (JSON POST, e.g. to a
Slack or Discord relay) and ntfy.sh (free phone push: install the ntfy app
and subscribe to your topic). Each alert key is sent once.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, bus, webhook_url: str | None = None, ntfy_topic: str | None = None):
        self.bus = bus
        self.webhook_url = webhook_url
        self.ntfy_topic = ntfy_topic
        self.sent: dict[str, float] = {}
        self.enabled = True

    async def send(self, key: str, kind: str, title: str, body: str, data: dict | None = None,
                   priority: str = "default"):
        if not self.enabled or key in self.sent:
            return
        self.sent[key] = time.time()
        if len(self.sent) > 5000:
            for k in sorted(self.sent, key=self.sent.get)[:2500]:
                self.sent.pop(k, None)
        msg = {"type": "alert", "kind": kind, "key": key, "title": title, "body": body, "data": data or {},
               "ts": time.time()}
        await self.bus.publish("alerts", msg)
        if self.webhook_url or self.ntfy_topic:
            asyncio.create_task(self._external(msg, priority))

    async def _external(self, msg: dict, priority: str):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                if self.webhook_url:
                    await c.post(self.webhook_url, json=msg)
                if self.ntfy_topic:
                    topic = self.ntfy_topic if self.ntfy_topic.startswith("http") else "https://ntfy.sh/" + self.ntfy_topic
                    await c.post(topic, content=msg["body"].encode(),
                                 headers={"Title": msg["title"], "Priority": priority, "Tags": msg["kind"]})
        except Exception as e:
            log.warning("alert delivery failed: %s", e)
