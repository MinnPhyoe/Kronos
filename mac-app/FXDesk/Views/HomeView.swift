import Charts
import SwiftData
import SwiftUI

/// The front page, a quiet dashboard: today's key figures, the price, your loss limit, Kronos' view
/// (advice only), the opening ranges and the markets.
struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @Query(sort: \TradeRecord.openedAt, order: .reverse) private var records: [TradeRecord]
    let go: (Screen) -> Void

    var body: some View {
        let snaps = records.map { r in r.snapshot }
        let limits = model.settings.limits
        let lostToday = JournalView.lost(snaps, inSamePeriodAs: Date(), by: AppRiskEngine.tradingDay)
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                HStack(alignment: .top, spacing: 0) {
                    VStack(spacing: 0) {
                        statsRow(lostToday: lostToday, limits: limits)
                        hairline
                        priceSection
                        hairline
                        HStack(alignment: .top, spacing: 0) {
                            insight(snaps: snaps, limits: limits)
                            vline
                            rangesSection
                        }
                    }
                    vline
                    VStack(spacing: 0) {
                        limitGauge(lostToday: lostToday, limits: limits)
                        hairline
                        outlookMeter
                        hairline
                        sessionDetails
                    }
                    .frame(width: 290)
                }
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.card))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(Theme.line, lineWidth: 1))
                markets
                Text("Analysis only. FX Desk never places a trade. Time Machine and Kronos numbers are estimates, not win rates.")
                    .font(Theme.label(11))
                    .foregroundStyle(Theme.faint)
            }
            .padding(24)
            .frame(maxWidth: 1180, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.bg)
        .navigationTitle("Home")
    }

    private var hairline: some View { Rectangle().fill(Theme.line).frame(height: 1) }
    private var vline: some View { Rectangle().fill(Theme.line).frame(width: 1) }

    // MARK: header

    private var header: some View {
        HStack(alignment: .center) {
            Text(greeting).font(.system(size: 20, weight: .medium)).foregroundStyle(Theme.text)
            Spacer()
            Picker("Pair", selection: Binding(get: { model.pair }, set: { p in model.select(pair: p) })) {
                ForEach(model.settings.pairs, id: \.self) { p in Text(Instrument.display(p)).tag(p) }
            }
            .labelsHidden()
            .frame(width: 130)
            Button("Open the live chart") { go(.live) }
                .buttonStyle(CapsuleButtonStyle(kind: .ghost))
        }
    }

    private var greeting: String {
        let h = Calendar.current.component(.hour, from: Date())
        return h < 12 ? "Good morning" : (h < 18 ? "Good afternoon" : "Good evening")
    }

    // MARK: the top row

    private func statsRow(lostToday: Double, limits: RiskLimits) -> some View {
        let today = signalsToday
        let aPlus = today.filter { s in s.isAPlus }.count
        let passed = today.filter { s in s.passed == true }.count
        return HStack(spacing: 0) {
            StatCell(Instrument.display(model.pair), value: Instrument.price(model.lastPrice, model.pair)) {
                DeltaChip(value: dayChange, suffix: "since 00:00 UTC")
            }
            vline
            StatCell("Signals today", value: "\(today.count)") {
                Text("\(aPlus) A+ · \(passed) passed every check").font(Theme.label(11)).foregroundStyle(Theme.dim)
            }
            vline
            StatCell("Lost today", value: money(lostToday, limits.currency)) {
                Text("of \(money(limits.dailyLimit, limits.currency)) daily limit").font(Theme.label(11)).foregroundStyle(Theme.dim)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private var signalsToday: [Signal] {
        let day = AppRiskEngine.tradingDay(Date())
        return model.signals.values.filter { s in
            guard let t = s.createdTs else { return false }
            return AppRiskEngine.tradingDay(Date(timeIntervalSince1970: TimeInterval(t))) == day
        }
    }

    /// Change since the first candle of today (UTC), in percent.
    private var dayChange: Double? {
        let start = Int(Date().timeIntervalSince1970) / 86400 * 86400
        guard let open = model.candles.first(where: { c in c.time >= start })?.open ?? model.candles.first?.open,
              let now = model.lastPrice ?? model.candles.last?.close, open > 0 else { return nil }
        return (now - open) / open * 100
    }

    private func money(_ v: Double, _ ccy: String) -> String {
        let sym = ["GBP": "£", "USD": "$", "EUR": "€"][ccy] ?? ""
        return sym + String(format: "%.0f", v)
    }

    // MARK: price

    private var priceSection: some View {
        let shown = Array(model.candles.suffix(96))
        let first = shown.first?.close
        let last = model.lastPrice ?? shown.last?.close
        let change: Double? = (first != nil && last != nil && first! > 0) ? (last! - first!) / first! * 100 : nil
        let span = shown.count * model.timeframe.seconds
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(Instrument.price(last, model.pair)).font(.system(size: 26, weight: .medium).monospacedDigit())
                        .foregroundStyle(Theme.text)
                    Text("\(Instrument.display(model.pair)) · last \(durationText(span)) of \(model.timeframe.label) candles")
                        .font(Theme.label(12)).foregroundStyle(Theme.dim)
                }
                Spacer()
                DeltaChip(value: change, suffix: "over this view")
            }
            if shown.count > 1 {
                PriceLineChart(candles: shown, pair: model.pair)
                    .frame(height: 230)
                    .padding(.top, 10)
            } else {
                Text(model.engineState == .running ? "Waiting for prices…" : "The engine is not running.")
                    .font(Theme.label(13)).foregroundStyle(Theme.dim)
                    .frame(maxWidth: .infinity, minHeight: 230)
            }
        }
        .padding(16)
    }

    private func durationText(_ seconds: Int) -> String {
        seconds >= 2 * 86400 ? "\(seconds / 86400) days" : (seconds >= 2 * 3600 ? "\(seconds / 3600) hours" : "\(max(1, seconds / 60)) minutes")
    }

    // MARK: insight and ranges

    private func insight(snaps: [RecordSnapshot], limits: RiskLimits) -> some View {
        let ranked = BestResult.rank(model.activeSignals) { s in
            AppRiskEngine.check(pair: s.pair, side: s.side ?? "buy", entry: s.entry, stop: s.sl, target: s.tp1,
                                riskAmount: s.sizing?.maxLoss, records: snaps, limits: limits)
        }
        return Button { go(.best) } label: {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 6) {
                    Image(systemName: "sparkle").font(.system(size: 12))
                    Text("Best result").font(Theme.label(13))
                    Spacer()
                    Text("Open").font(Theme.label(12)).foregroundStyle(Theme.dim)
                }
                .foregroundStyle(Theme.text)
                Text(BestResult.message(ranked))
                    .font(.system(size: 17, weight: .regular))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var rangesSection: some View {
        let items = model.scan
        let triggered = items.filter { i in i.entry?.status == "triggered" }.count
        let waiting = items.filter { i in i.entry?.status == "waiting" }.count
        let other = max(0, items.count - triggered - waiting)
        let total = max(1, items.count)
        return VStack(alignment: .leading, spacing: 10) {
            Text("Opening ranges today").font(Theme.label(12)).foregroundStyle(Theme.dim)
            Text("\(items.count)").font(.system(size: 24, weight: .medium).monospacedDigit()).foregroundStyle(Theme.text)
            SegmentBar(parts: [
                ("Triggered \(triggered)", Double(triggered) / Double(total), 0.85),
                ("Waiting \(waiting)", Double(waiting) / Double(total), 0.45),
                ("Other \(other)", Double(other) / Double(total), 0.18),
            ])
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: right column

    private func limitGauge(lostToday: Double, limits: RiskLimits) -> some View {
        let used = limits.dailyLimit > 0 ? lostToday / limits.dailyLimit : 0
        return VStack(spacing: 12) {
            ZStack {
                RadialBarGauge(fraction: used)
                VStack(spacing: 4) {
                    Image(systemName: "shield.lefthalf.filled").font(.system(size: 14)).foregroundStyle(Theme.dim)
                        .frame(width: 30, height: 30).background(Circle().fill(Theme.cardHigh))
                    Text("Daily limit used").font(Theme.label(11)).foregroundStyle(Theme.dim)
                    Text(String(format: "%.0f%%", min(used, 9.99) * 100)).font(.system(size: 20, weight: .medium).monospacedDigit())
                        .foregroundStyle(used >= 0.8 ? Theme.coral : Theme.text)
                }
            }
            .frame(width: 190, height: 190)
            HStack(spacing: 14) {
                legendDot("Lost \(money(lostToday, limits.currency))", 0.85)
                legendDot("Left \(money(max(0, limits.dailyLimit - lostToday), limits.currency))", 0.18)
            }
            Button("View records") { go(.records) }
                .buttonStyle(CapsuleButtonStyle(kind: .ghost))
                .frame(maxWidth: .infinity)
        }
        .padding(16)
    }

    private func legendDot(_ text: String, _ opacity: Double) -> some View {
        HStack(spacing: 5) {
            Circle().fill(Color.white.opacity(opacity)).frame(width: 6, height: 6)
            Text(text).font(Theme.label(11)).foregroundStyle(Theme.dim)
        }
    }

    private var outlookMeter: some View {
        let f = model.analysis ?? model.forecast
        let up: Double? = (f?.available != false && f?.stale != true) ? f?.directionProb : nil
        return VStack(alignment: .leading, spacing: 8) {
            Text("Kronos outlook (advice only)").font(Theme.label(12)).foregroundStyle(Theme.dim)
            HStack(alignment: .firstTextBaseline) {
                Text(up.map { p in String(format: "%.0f%% up", p * 100) } ?? "–")
                    .font(.system(size: 22, weight: .medium).monospacedDigit()).foregroundStyle(Theme.text)
                Spacer()
                Text(up == nil ? "press Analyse" : "of forecast paths").font(Theme.label(11)).foregroundStyle(Theme.faint)
            }
            StripeMeter(fraction: up ?? 0).frame(height: 30)
            HStack(spacing: 14) {
                legendDot("Up", 0.8)
                legendDot("Down", 0.14)
            }
        }
        .padding(16)
    }

    private var sessionDetails: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Sessions · \(Instrument.display(model.pair))").font(Theme.label(12)).foregroundStyle(Theme.dim)
                .padding(.bottom, 6)
            if model.scanForPair.isEmpty {
                KeyValueRow(key: "Today", value: "no range yet")
            }
            ForEach(model.scanForPair) { item in
                KeyValueRow(key: item.label.replacingOccurrences(of: " ORB", with: ""),
                            value: (item.entry?.status ?? (item.usable ? "–" : "unusable")).capitalized)
            }
            KeyValueRow(key: "Prices", value: model.health?.config?.provider?.uppercased() ?? "–")
            KeyValueRow(key: "Status", value: model.dataIsStale ? "Not updating" : (model.engineState == .running ? "Live" : "Off"))
        }
        .padding(16)
    }

    // MARK: markets

    private var markets: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Markets").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
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

/// A quiet line chart of closes with a soft fill and a hover readout.
struct PriceLineChart: View {
    let candles: [Candle]
    let pair: String
    @State private var hover: Candle?

    private func date(_ c: Candle) -> Date { Date(timeIntervalSince1970: TimeInterval(c.time)) }

    var body: some View {
        let lows = candles.map { c in c.close }
        let lo = lows.min() ?? 0
        let hi = lows.max() ?? 1
        let pad = max((hi - lo) * 0.12, Instrument.pip(pair) * 2)
        let floor = lo - pad
        Chart {
            ForEach(candles, id: \.time) { c in
                AreaMark(x: .value("Time", date(c)), yStart: .value("Floor", floor), yEnd: .value("Close", c.close))
                    .foregroundStyle(LinearGradient(colors: [Color.white.opacity(0.14), Color.white.opacity(0.0)],
                                                    startPoint: .top, endPoint: .bottom))
                LineMark(x: .value("Time", date(c)), y: .value("Close", c.close))
                    .foregroundStyle(Color.white.opacity(0.85))
                    .lineStyle(StrokeStyle(lineWidth: 1.5))
            }
            if let h = hover {
                RuleMark(x: .value("Time", date(h)))
                    .foregroundStyle(Color.white.opacity(0.3))
                PointMark(x: .value("Time", date(h)), y: .value("Close", h.close))
                    .foregroundStyle(Color.white)
                    .symbolSize(36)
            }
        }
        .chartYScale(domain: floor...(hi + pad))
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 6)) { _ in
                AxisValueLabel(format: .dateTime.hour().minute())
                    .foregroundStyle(Theme.faint)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 5)) { _ in
                AxisGridLine().foregroundStyle(Theme.line)
                AxisValueLabel().foregroundStyle(Theme.faint)
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geo in
                Rectangle().fill(Color.clear).contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let loc):
                            guard let frame = proxy.plotFrame else { return }
                            let x = loc.x - geo[frame].origin.x
                            if let d = proxy.value(atX: x, as: Date.self) {
                                hover = candles.min(by: { a, b in
                                    abs(date(a).timeIntervalSince(d)) < abs(date(b).timeIntervalSince(d))
                                })
                            }
                        case .ended:
                            hover = nil
                        }
                    }
            }
        }
        .overlay(alignment: .topTrailing) {
            if let h = hover {
                VStack(alignment: .leading, spacing: 3) {
                    Text(date(h), format: .dateTime.weekday(.abbreviated).hour().minute())
                        .font(Theme.label(11)).foregroundStyle(Theme.dim)
                    Text(Instrument.price(h.close, pair)).font(Theme.number(13)).foregroundStyle(Theme.text)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Theme.cardHigh))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Theme.line, lineWidth: 1))
            }
        }
    }
}
