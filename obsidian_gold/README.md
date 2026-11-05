# OBSIDIAN Gold

A private one-page XAU/USD desk built from two repos. It has no strategy of
its own:

| From | What it does here |
| --- | --- |
| **day-trader-skill** `trader-entry` | Opening-range breakout. The first 15 minutes after 9:30 ET set the range; a 5-minute close above it is a buy, below it a sell. |
| **day-trader-skill** `trader-exit` | Stop at the range midpoint (or the far side), target at 1R. |
| **day-trader-skill** `trader-timemachine` | 10,000 replays of the rest of the session, plus the same rule on every past session in the data. |
| **day-trader-skill** `trader-budget` | Size from your max loss per trade, in ounces, shown as lots (1 lot = 100 oz). |
| **day-trader-skill** `trader-alerts` | Stop, target and flat-by-close alerts. |
| **Kronos** (this repo) | Forecast of the next hour of 5-minute candles. |

New events go to Telegram (range set, entry, stop, target, flat by close).
It never connects to a broker and never places a trade.

The day-trader tools are written for long trades only. Sells run as buys on a
mirror image of the chart (price p becomes M − p), and every number is
mirrored back. The tests check that a mirrored chart gives exactly the
mirrored trade.

## Run it

Clone both repos side by side:

```bash
git clone -b claude/analyze-two-repos-nfmvcr https://github.com/MinnPhyoe/Kronos.git
git clone https://github.com/MinnPhyoe/day-trader-skill.git
cd Kronos
python3 obsidian_gold/server.py           # http://127.0.0.1:8750
```

The page and the day-trader tools need only Python 3.8+. The Kronos forecast
needs `pip install -r requirements.txt` (torch); the first run downloads
`NeoQuasar/Kronos-small`. Without it, the page works and the Kronos card says
it is off.

If day-trader-skill is somewhere else, set `DAY_TRADER_PATH=/path/to/day-trader-skill`.

- **Look around offline:**
  `python3 obsidian_gold/server.py --demo-at "2026-11-04 17:40"` uses
  made-up candles and pretends it is that time (UTC).
- **Open it from your phone:** run it on an always-on machine with a PIN:
  `OBSIDIAN_PIN=482913 python3 obsidian_gold/server.py --host 0.0.0.0`.
  Put HTTPS in front if it faces the internet, or use Tailscale.
- **Turn on Telegram:**
  1. Create a bot with @BotFather and copy its token.
  2. Send your bot any message.
  3. Get your chat id from @userinfobot.
  4. Paste the token and chat id in Settings, then press "Send a test".

## Data

The page reads Yahoo 5-minute bars, 60 days of them, through trader-entry's
own fetcher. Choose the source in Settings:

- `GC=F` (COMEX gold futures, the default) trades a few dollars above spot.
- `XAUUSD=X` is spot.

Check the levels against your broker before you act.

## Tests

```bash
python3 -m unittest discover -s obsidian_gold/tests -v
```

The tests run the day-trader README's TSLA trade through this app and get the
same numbers:

- entry 364.18, stop 358.32, target 370.04
- size: 8 (shares in the README, 0.08 lots here)
- history: 33 breakouts in 59 sessions, 13 to the target, 15 to the stop

The Time Machine percentages differ a little from the README's, because this
page stops the replays at the 15:55 flat-by time and the README replays to
the 16:00 close.

## Fine print

**Not financial advice.** This is arithmetic on past prices. The rule was
written for US stocks at the 9:30 ET open; gold also moves hard on 8:30 ET
data, in London, and overnight, and none of that is in this rule. The odds
and history include no spread or commission. A stop can fill worse than its
price. Paper trade it for a few weeks first.
