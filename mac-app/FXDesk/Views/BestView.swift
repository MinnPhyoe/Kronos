import SwiftData
import SwiftUI

/// Every active setup, checked by both risk engines, Kronos and the Time Machine; the best one on top.
struct BestView: View {
    @EnvironmentObject var model: AppModel
    @Query(sort: \TradeRecord.openedAt, order: .reverse) private var records: [TradeRecord]
    @State private var recordFrom: Signal?

    var body: some View {
        let snaps = records.map { $0.snapshot }
        let limits = model.settings.limits
        let ranked = BestResult.rank(model.activeSignals) { s in
            AppRiskEngine.check(pair: s.pair, side: s.side ?? "buy", entry: s.entry, stop: s.sl, target: s.tp1,
                                riskAmount: s.sizing?.maxLoss, records: snaps, limits: limits)
        }
        let best = ranked.first(where: { r in r.eligible })
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                ZStack(alignment: .leading) {
                    BlobField(base: Theme.pink, blob: Theme.coral, seed: 2)
                    HStack(alignment: .center, spacing: 18) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("BEST RESULT").font(Theme.title(12)).tracking(1.5).foregroundStyle(Theme.ink.opacity(0.7))
                            Text(BestResult.message(ranked))
                                .font(Theme.display(26))
                                .foregroundStyle(Theme.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        CandleBuddy(mood: best == nil ? .waiting : .happy, color: Theme.lime).frame(width: 96, height: 134)
                    }
                    .padding(22)
                }
                .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
                Text("A setup counts only if the engine's Day Trader risk check AND your records' risk check pass it, at least 55% of Kronos' paths reach the target first, and the Time Machine agrees. The best is the one with the highest cautious estimate: the lower of the two expected results.")
                    .font(Theme.body(13))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                if let b = best {
                    SignalCard(signal: b.signal, appRisk: b.appRisk, highlight: true, onRecord: { recordFrom = b.signal })
                }
                let others = ranked.filter { r in !r.eligible }
                if !others.isEmpty {
                    Text("Not chosen").font(Theme.display(22)).foregroundStyle(Theme.text).padding(.top, 8)
                    ForEach(others) { r in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 10) {
                                PairBadge(pair: r.signal.pair, size: 22)
                                Text("\(Instrument.display(r.signal.pair)) \((r.signal.side ?? "").uppercased())")
                                    .font(Theme.title(15)).foregroundStyle(Theme.text)
                                Text(r.signal.setup ?? "").font(Theme.body(12)).foregroundStyle(Theme.dim)
                                Spacer()
                                Pill(text: r.signal.status, color: statusColor(r.signal.status))
                            }
                            ForEach(Array(r.whyNot.enumerated()), id: \.offset) { _, w in
                                Label(w, systemImage: "xmark.circle.fill")
                                    .font(Theme.body(12))
                                    .foregroundStyle(Theme.dim)
                            }
                        }
                        .padding(14)
                        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.card))
                    }
                }
                if ranked.isEmpty {
                    Panel("") {
                        HStack(spacing: 14) {
                            CandleBuddy(mood: .sleepy).frame(width: 50)
                            Text("No active setups right now. They appear after the London and New York opening ranges.")
                                .font(Theme.body(14))
                                .foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: 860, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .navigationTitle("Best result")
        .toolbar {
            Button { Task { await model.refreshSignals() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                .help("Ask the engine for the latest setups")
        }
        .sheet(item: $recordFrom) { s in RecordEditor(signal: s, record: nil).environmentObject(model) }
    }
}
