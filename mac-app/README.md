# FX Desk for Mac

A private Mac app for forex analysis. It shows a live chart, Kronos forecasts and the Day Trader
opening-range setups. Every setup is checked by two risk engines, and the app picks the best result.
Your trades are kept as records on this Mac.

- **Everything runs on your Mac.** The app starts the engine from `../forex-app` on `127.0.0.1` only.
- **It never places a trade.** It is analysis only: you place orders yourself with your broker.
- **Kronos and Time Machine numbers are estimates, not win rates.**

## What you need

- A Mac with macOS 14 (Sonoma) or later.
- Xcode 16 or later, for `FXDesk.xcodeproj`. With Xcode 15, open `Package.swift` instead and run the **FXDesk** scheme.
- Python 3.10 or later. Get it from Homebrew (`brew install python@3.12`) or from python.org.
- This repository on your Mac, for example in `~/Applications/Kronos`.

## Build and run

1. Open `mac-app/FXDesk.xcodeproj` in Xcode.
2. Choose the **FXDesk** scheme and **My Mac**, then press **Run** (⌘R).
   The app is signed to run locally, so you don't need an Apple developer account.
3. The first time, open **Settings** and choose the `forex-app` folder as the engine folder.
   It is the folder that holds `scripts/mac-engine.sh`. The default is `~/Applications/Kronos/forex-app`.
4. Press **Save**. The engine starts.
   - The first start creates a Python environment and installs packages.
   - If **Use Kronos** is on, the first start also downloads the Kronos model, about 1 GB.
   - The first start can take several minutes. The sidebar shows each step.
   - Later starts take a few seconds.

If the build fails, copy the first error from Xcode's Issue navigator and paste it back to Claude.
The app could not be compiled where it was written, so expect a small fix or two on the first build.

## The screens

| Screen | What it shows |
| --- | --- |
| **Home** | Candle Buddy, the best result, cards for Kronos, the opening ranges, today's loss limit and your records, and live prices. |
| **Live chart** | The candles, the Kronos forecast band, the session ranges, and the signal's entry, stop and targets. Press **Analyse** to run Kronos now. |
| **Best result** | Every active setup, ranked. A setup counts only if both risk engines pass it, at least 55% of Kronos' paths reach the target first, and the Time Machine agrees. |
| **Records** | Your trades and your results. Your daily and weekly loss limits are worked out from these records. |
| **Alerts** | New setups, triggers, TP and SL hits, and news. They also show as Mac notifications. |
| **Settings** | The engine folder, the price source, pairs, sessions, Kronos, your account size and limits, and alerts. |

Candle Buddy, the mascot, shows how things are going:

- **Happy, waving:** a setup passed every check.
- **Waiting, looking around:** the engine is live and nothing has passed yet.
- **Sleepy:** the engine is stopped or starting.
- **Worried:** the engine failed, or there are no live prices.

The animations pause when **Reduce motion** is on in System Settings → Accessibility → Display.

## The two risk engines

1. **Day Trader risk check (in the engine).** It checks size, stop, reward to risk and the session rules. It returns PASS or BLOCK.
2. **Your records (in the app).** It checks your trades this day and this week. It blocks a setup if any of these is true:
   - it would break your daily or weekly loss limit;
   - you have reached your trade count for the day;
   - you already have an open trade on that pair;
   - the reward to risk is too low;
   - the size can't be checked.

The best result has the highest **cautious estimate**: the lower of Kronos' expected result and the Time Machine's.

## Entry, stop and targets in TradingView

The app can't draw on your TradingView account directly. Instead, every signal card has
**Copy to TradingView**:

1. Press **Copy to TradingView**. The app copies a small Pine Script and opens the pair on TradingView.
2. In TradingView, open the **Pine Editor** at the bottom of the chart.
3. Select everything in the editor, paste, and press **Add to chart**.

The script draws:

- the entry, stop, TP1 and TP2 lines with labels;
- the profit zone in green and the loss zone in red.

If the chart shows a different symbol, the script says so. To remove it, delete the indicator from the chart.

## Tests

The logic (risk engine, best result, settings file, stream decoding) has unit tests:

```bash
cd mac-app
swift test
```

Or, in Xcode, open `Package.swift` and press ⌘U.

## Privacy

- **Tokens and passwords.** Your OANDA token and any password are kept only in `forex-app/.env`. Its permissions are set so only you can read it, and git ignores it. Never share or upload that file.
- **Records.** Your records are kept only in the app's own storage on this Mac.
- **The chart.** The chart library (TradingView Lightweight Charts, Apache 2.0) is bundled in the app. The chart loads nothing from the internet.
- **The engine.** The engine listens on `127.0.0.1`, so other devices can't reach it.
