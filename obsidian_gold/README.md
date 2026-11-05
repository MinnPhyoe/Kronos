# OBSIDIAN Gold

A private XAU/USD desk. It reads gold candles, shows the trend, and tells you
when an entry qualifies. Each signal comes with its stop loss, two take
profits, your lot size, the odds from 2,000 replays of the next 4 hours, and
how the same setup did on past prices. New signals, targets, stops and
session opens are sent to Telegram, and the open page also shows a browser
notification.

The look matches the earlier OBSIDIAN app. The strategy is new: "Gold Pulse".
The **Rules** page in the app explains it in full.

It does analysis only. It never connects to a broker and never places a trade.

## Run it

You need Python 3.8 or newer. Nothing to install.

```bash
cd Kronos
python3 obsidian_gold/server.py            # open http://127.0.0.1:8750
python3 obsidian_gold/server.py --demo     # made-up candles, to look around offline
```

### Reach it from your phone

Run it on an always-on computer or a small VPS, protected by a PIN:

```bash
OBSIDIAN_PIN=482913 python3 obsidian_gold/server.py --host 0.0.0.0
```

The server refuses to listen beyond this computer unless a PIN is set. For
anything on the public internet, put it behind HTTPS too, for example with
Caddy or a Cloudflare Tunnel. On a home network, Tailscale also works.

### Notifications on your phone (Telegram)

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token.
2. Send your new bot any message.
3. Get your chat id from **@userinfobot**.
4. In the app, open **Settings → Notifications**, paste both, save, then press
   **Alerts → Send a test**.

The server sends alerts while it is running, even with the page closed.

## Price data

| Source | What | Notes |
| --- | --- | --- |
| `yahoo` (default) | `GC=F` COMEX gold futures, or `XAUUSD=X` spot | Free and unofficial; can lag a little. Futures trade a few dollars above spot. |
| `binance` | `PAXGUSDT`, a token backed by one ounce of gold | 24/7, tracks spot closely but not exactly. |
| `csv` | Your MT4/MT5 or TradingView export | Use M1, M5 or M15. Set "Broker time vs UTC" (often +2 or +3). |
| `demo` | Made-up candles | For trying the app only. |

For entries that match your broker exactly, upload your broker's own XAUUSD
M5 export (in MT5: View → Symbols → Bars → Export).

## Gold Pulse, in short

1. **Trend:** H1 EMA50 against EMA200, and the H4 close against its EMA50.
   Both up means buys only, both down means sells only. If they disagree,
   there are no trades.
2. **When:** London 07:00–11:00 UTC and New York 12:00–16:00 UTC. You can
   change both windows in Settings.
3. **Entry on M15:**
   - **Pullback:** the candle dips to the EMA20 and closes back in the trend
     direction with a rejection.
   - **Breakout:** the candle closes beyond the last 20 candles' range, with
     a body of at least 0.6 ATR.
4. **Stop:** beyond the swing plus 0.3 ATR, kept between 0.8 and 3 ATR.
   **Targets:** TP1 at 1R (then move the stop to entry) and TP2 at 2R. A
   trade still open after 8 hours is closed.
5. **Size:** lots = balance × risk % ÷ (stop distance × 100 oz).

**Forecast:** 2,000 replays of the next 16 M15 candles. Each candle is drawn
from gold's real candles at the same time of day over the last 60 days.

**Kronos (optional):** a second opinion from the Kronos model in this repo.
Run `pip install -r requirements.txt` first; the first run downloads
`NeoQuasar/Kronos-small`.

## Tests

```bash
python3 -m unittest discover -s obsidian_gold/tests -v
```

## Files

```
obsidian_gold/
  server.py       web server, price polling, alerts, Telegram, optional Kronos
  engine.py       indicators, the Gold Pulse rules, backtest, forecast, lot sizing
  feeds.py        Yahoo, Binance, CSV (MT4/MT5/TradingView) and demo candles
  static/index.html  the app (one file, no build step)
  data/           your settings, alerts and candle cache (not committed)
  tests/          offline tests
```

## Fine print

**Not financial advice.** This is arithmetic on past prices. The win rates it
shows come from the candles it has loaded. They include no spread or
commission, and they don't promise future results. News such as CPI, NFP and
FOMC moves gold more than any pattern: stand aside around it. A stop can fill
worse than its price in a fast market. Paper trade it for a few weeks before
you trust it with money.
