import SwiftData
import SwiftUI

struct LiveView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 14) {
                    PairBadge(pair: model.pair, size: 30)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(Instrument.display(model.pair)).font(Theme.display(26)).foregroundStyle(Theme.text)
                        Text(Instrument.name(model.pair)).font(Theme.body(12)).foregroundStyle(Theme.dim)
                    }
                    Spacer()
                    if let p = model.lastPrice {
                        Text(Instrument.price(p, model.pair))
                            .font(Theme.number(24))
                            .foregroundStyle(Theme.pink)
                            .monospacedDigit()
                            .contentTransition(.numericText())
                            .animation(.easeOut(duration: 0.2), value: p)
                    }
                    Button {
                        Task { await model.analyseNow() }
                    } label: {
                        Label(model.analysing ? "Analysing…" : "Analyse", systemImage: "sparkles")
                    }
                    .buttonStyle(CapsuleButtonStyle(kind: .pink))
                    .disabled(model.analysing || model.engineState != .running)
                    .help("Run Kronos now on this pair and timeframe, and refresh the Day Trader setups")
                }
                HStack(spacing: 12) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(model.settings.pairs, id: \.self) { p in
                                Chip(text: Instrument.display(p), selected: p == model.pair) { model.select(pair: p) }
                            }
                        }
                    }
                    HStack(spacing: 4) {
                        ForEach(Timeframe.allCases) { tf in
                            Chip(text: tf.label, selected: tf == model.timeframe) { model.select(timeframe: tf) }
                        }
                    }
                    .padding(3)
                    .background(Capsule().fill(Theme.card))
                }
                ChartView(bridge: model.chart)
                    .frame(minWidth: 460, minHeight: 360)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).stroke(Theme.line, lineWidth: 1))
            }
            .padding(16)
            .background(Theme.bg)
            AnalysisPanel()
                .frame(minWidth: 300, idealWidth: 370, maxWidth: 480)
                .background(Theme.bg)
        }
        .background(Theme.bg)
        .navigationTitle("Live chart")
    }
}

struct AnalysisPanel: View {
    @EnvironmentObject var model: AppModel
    @Query(sort: \TradeRecord.openedAt, order: .reverse) private var records: [TradeRecord]
    @State private var recordFrom: Signal?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                outlook
                dayTrader
                if let s = model.signalForPair {
                    SignalCard(signal: s, appRisk: appRisk(s), onRecord: { recordFrom = s })
                } else {
                    Panel("Signal") {
                        HStack(spacing: 14) {
                            CandleBuddy(mood: .waiting).frame(width: 46)
                            Text("No setup on \(Instrument.display(model.pair)) yet. Signals appear after a session's opening range, when the checks pass.")
                                .font(Theme.body(13))
                                .foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(.vertical, 16)
            .padding(.trailing, 16)
            .padding(.leading, 4)
        }
        .sheet(item: $recordFrom) { s in
            RecordEditor(signal: s, record: nil).environmentObject(model)
        }
    }

    private func appRisk(_ s: Signal) -> AppRiskVerdict {
        AppRiskEngine.check(pair: s.pair, side: s.side ?? "buy", entry: s.entry, stop: s.sl, target: s.tp1,
                            riskAmount: s.sizing?.maxLoss, records: records.map { $0.snapshot }, limits: model.settings.limits)
    }

    @ViewBuilder private var outlook: some View {
        let f = model.analysis ?? model.forecast
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("KRONOS OUTLOOK").font(Theme.title(11)).tracking(1.2).foregroundStyle(Theme.dim)
                Spacer()
                if model.analysis != nil { Pill(text: "just analysed", color: Theme.lime) }
            }
            .padding(14)
            .background(Theme.card)
            ZStack(alignment: .bottomLeading) {
                BlobField(base: Theme.purple, blob: Theme.blue, seed: 3)
                if let f, f.available != false, f.stale != true, let dp = f.directionProb, let band = f.band, let last = band.last {
                    HStack(alignment: .bottom) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(String(format: "%.0f%% up", dp * 100)).font(Theme.display(34)).foregroundStyle(Color.white)
                            Text(String(format: "%.0f%% of the forecast paths end higher, %.0f%% lower", dp * 100, (1 - dp) * 100))
                                .font(Theme.title(12)).foregroundStyle(Color.white.opacity(0.9))
                            Text("In \(band.count) candles: \(Instrument.price(last.p10, model.pair)) to \(Instrument.price(last.p90, model.pair)) (10–90%), middle \(Instrument.price(last.p50, model.pair))")
                                .font(Theme.body(12)).foregroundStyle(Color.white.opacity(0.85))
                                .fixedSize(horizontal: false, vertical: true)
                            if let name = f.model {
                                Text(name.contains("bootstrap") ? "Model: a statistical stand-in, not Kronos" : "Model: \(name)")
                                    .font(Theme.title(11))
                                    .foregroundStyle(name.contains("bootstrap") ? Theme.gold : Color.white.opacity(0.75))
                            }
                        }
                        Spacer(minLength: 4)
                        FanArt(upProbability: dp).frame(width: 96, height: 80)
                    }
                    .padding(14)
                } else {
                    HStack(spacing: 12) {
                        CandleBuddy(mood: .sleepy, color: Theme.lime).frame(width: 44)
                        Text(f?.stale == true ? "The last forecast is older than the last candle. Press Analyse." : "No forecast yet. Press Analyse.")
                            .font(Theme.title(13)).foregroundStyle(Color.white)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(14)
                }
            }
            .frame(minHeight: 130)
        }
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    @ViewBuilder private var dayTrader: some View {
        Panel("Day Trader opening ranges") {
            if model.scanForPair.isEmpty {
                Text("No session range has formed today for this pair.").font(Theme.body(13)).foregroundStyle(Theme.dim)
            }
            ForEach(model.scanForPair) { item in
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(item.label.replacingOccurrences(of: " ORB", with: "")).font(Theme.title(14)).foregroundStyle(Theme.text)
                        Spacer()
                        Pill(text: item.entry?.status ?? (item.usable ? "–" : "unusable"), color: statusColor(item.entry?.status ?? ""))
                    }
                    if let r = item.range {
                        Text("Range \(Instrument.price(r.low, item.pair)) – \(Instrument.price(r.high, item.pair))\(r.heightPips.map { v in String(format: " (%.1f pips)", v) } ?? "")")
                            .font(Theme.number(12)).foregroundStyle(Theme.dim)
                    }
                    if let e = item.entry {
                        if e.status == "triggered", let side = e.side, let p = e.price {
                            Text("A 5-minute candle closed \(side == "buy" ? "above" : "below") at \(Instrument.price(p, item.pair))")
                                .font(Theme.body(12)).foregroundStyle(Theme.text)
                        } else if e.status == "waiting", let d = e.distancePips {
                            Text(String(format: "%.1f pips from a breakout", d)).font(Theme.body(12)).foregroundStyle(Theme.text)
                        }
                    } else if let reason = item.reason {
                        Text(reason).font(Theme.body(12)).foregroundStyle(Theme.dim)
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.cardHigh))
            }
        }
    }
}
