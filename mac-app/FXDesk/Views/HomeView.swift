import SwiftData
import SwiftUI

/// The front page: the mascot, the best result, four colour cards and the markets.
struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @Query(sort: \TradeRecord.openedAt, order: .reverse) private var records: [TradeRecord]
    let go: (Screen) -> Void

    var body: some View {
        let snaps = records.map { r in r.snapshot }
        let limits = model.settings.limits
        let ranked = BestResult.rank(model.activeSignals) { s in
            AppRiskEngine.check(pair: s.pair, side: s.side ?? "buy", entry: s.entry, stop: s.sl, target: s.tp1,
                                riskAmount: s.sizing?.maxLoss, records: snaps, limits: limits)
        }
        let best = ranked.first(where: { r in r.eligible })
        let mood = buddyMood(hasBest: best != nil)
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                hero(mood: mood, ranked: ranked)
                cards(best: best, snaps: snaps, limits: limits)
                markets
                Text("Analysis only. FX Desk never places a trade. Kronos and Time Machine numbers are estimates, not win rates.")
                    .font(Theme.body(12))
                    .foregroundStyle(Theme.faint)
            }
            .padding(28)
            .frame(maxWidth: 1120, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.bg)
        .navigationTitle("Home")
    }

    // MARK: hero

    private func hero(mood: BuddyMood, ranked: [RankedSignal]) -> some View {
        HStack(alignment: .center, spacing: 28) {
            VStack(alignment: .leading, spacing: 16) {
                Text("FX DESK · PRIVATE")
                    .font(Theme.title(12))
                    .tracking(1.5)
                    .foregroundStyle(Theme.dim)
                Text("Setups,\nchecked twice.")
                    .font(Theme.display(58))
                    .foregroundStyle(Theme.pink)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Kronos forecasts and Day Trader opening ranges, checked by two risk engines. Everything runs on this Mac.")
                    .font(Theme.body(16))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 520, alignment: .leading)
                HStack(spacing: 10) {
                    Button("Open the live chart") { go(.live) }
                        .buttonStyle(CapsuleButtonStyle())
                    Button("See the best result") { go(.best) }
                        .buttonStyle(CapsuleButtonStyle(kind: .ghost))
                }
                .padding(.top, 4)
            }
            Spacer(minLength: 0)
            VStack(alignment: .trailing, spacing: 10) {
                SpeechBubble(text: buddyLine(mood: mood, ranked: ranked))
                    .frame(maxWidth: 240, alignment: .trailing)
                ZStack {
                    BlobField(base: Theme.purple, blob: Theme.pink, seed: 1)
                    CandleBuddy(mood: mood).padding(22)
                }
                .frame(width: 240, height: 240)
                .clipShape(RoundedRectangle(cornerRadius: 36, style: .continuous))
            }
        }
    }

    private func buddyMood(hasBest: Bool) -> BuddyMood {
        switch model.engineState {
        case .failed: return .worried
        case .starting, .stopped: return .sleepy
        case .running: return model.health?.feedUp == false ? .worried : (hasBest ? .happy : .waiting)
        }
    }

    private func buddyLine(mood: BuddyMood, ranked: [RankedSignal]) -> String {
        switch mood {
        case .happy: return BestResult.message(ranked)
        case .waiting: return ranked.isEmpty ? "Watching the opening ranges. Waiting is a result too." : BestResult.message(ranked)
        case .sleepy: return model.engineState == .stopped ? "The engine is stopped. Start it in Settings." : model.engineState.label
        case .worried: return model.engineState == .running ? "No live prices right now. Check the price source in Settings." : model.engineState.label
        }
    }

    // MARK: cards

    private func cards(best: RankedSignal?, snaps: [RecordSnapshot], limits: RiskLimits) -> some View {
        let columns = [GridItem(.adaptive(minimum: 250, maximum: 400), spacing: 16)]
        let f = model.analysis ?? model.forecast
        let up: Double? = (f?.available != false && f?.stale != true) ? f?.directionProb : nil
        let formed = model.scan.filter { item in item.range != nil }.count
        let triggered = model.scan.filter { item in item.entry?.status == "triggered" }.count
        let lostToday = JournalView.lost(snaps, inSamePeriodAs: Date(), by: AppRiskEngine.tradingDay)
        let used: Double = limits.dailyLimit > 0 ? lostToday / limits.dailyLimit : 0
        let closed = snaps.filter { r in !r.isOpen }
        let wins = closed.filter { r in (r.pnl ?? 0) > 0 }.count
        return LazyVGrid(columns: columns, alignment: .leading, spacing: 16) {
            Button { go(.best) } label: {
                FeatureCard(caption: "Best result", title: bestTitle(best), base: Theme.pink, blob: Theme.coral, seed: 2) {
                    HStack(spacing: 6) {
                        CandleBuddy(mood: best == nil ? .waiting : .happy, color: Theme.lime).frame(width: 82)
                        Sticker(big: rText(best.flatMap { b in BestResult.cautiousEstimate(b.signal) }),
                                small: best == nil ? "nothing passed yet" : "cautious estimate", tilt: -5)
                    }
                }
            }
            .buttonStyle(.plain)

            Button { go(.live) } label: {
                FeatureCard(caption: "Kronos outlook · \(Instrument.display(model.pair))",
                            title: up.map { p in String(format: "%.0f%% of paths end higher", p * 100) } ?? "Press Analyse for a forecast",
                            base: Theme.purple, blob: Theme.blue, seed: 3) {
                    FanArt(upProbability: up ?? 0.5)
                }
            }
            .buttonStyle(.plain)

            Button { go(.live) } label: {
                FeatureCard(caption: "Day Trader opening ranges",
                            title: formed == 0 ? "No range has formed yet today" : "\(formed) formed, \(triggered) triggered",
                            base: Theme.coral, blob: Theme.pink, seed: 4) {
                    RangeArt()
                }
            }
            .buttonStyle(.plain)

            Button { go(.records) } label: {
                FeatureCard(caption: "Your limit today",
                            title: String(format: "%.0f of %.0f %@ lost", lostToday, limits.dailyLimit, limits.currency),
                            base: Theme.blue, blob: Theme.purple, seed: 5) {
                    GaugeArt(fraction: used, label: String(format: "%.0f%%", min(used, 9.99) * 100))
                }
            }
            .buttonStyle(.plain)

            Button { go(.records) } label: {
                FeatureCard(caption: "Records",
                            title: closed.isEmpty ? "No closed trades yet" : "\(closed.count) closed, \(wins) won",
                            base: Theme.lime, blob: Theme.good, seed: 6) {
                    Sticker(big: closed.isEmpty ? "0" : String(format: "%.0f%%", Double(wins) / Double(closed.count) * 100),
                            small: closed.isEmpty ? "record a trade from a signal" : "of closed trades won", tilt: 4)
                }
            }
            .buttonStyle(.plain)
        }
    }

    private func bestTitle(_ best: RankedSignal?) -> String {
        guard let b = best else { return "No setup passed every check" }
        return "\(Instrument.display(b.signal.pair)) \((b.signal.side ?? "").uppercased()) · \(b.signal.setup ?? "setup")"
    }

    // MARK: markets

    private var markets: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text("Markets").font(Theme.display(26)).foregroundStyle(Theme.text)
                Spacer()
                Text(model.streamConnected ? "live" : "not live")
                    .font(Theme.title(12))
                    .foregroundStyle(model.streamConnected ? Theme.good : Theme.faint)
            }
            ForEach(model.settings.pairs, id: \.self) { p in
                PairRow(pair: p, price: model.prices[p], status: activeStatus(p), selected: p == model.pair) {
                    model.select(pair: p)
                    go(.live)
                }
            }
        }
    }

    private func activeStatus(_ pair: String) -> String? {
        model.signals.values.first(where: { s in s.pair == pair && s.isActive })?.status
    }
}
