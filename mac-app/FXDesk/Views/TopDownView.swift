import SwiftUI

/// Top-down analysis of every pair: the higher timeframes give the direction, the lower ones the entry.
/// The best plan is on top; "Show on chart" draws its entry, stop and target on the live chart.
struct TopDownView: View {
    @EnvironmentObject var model: AppModel
    let go: (Screen) -> Void
    @State private var appeared = false

    var body: some View {
        let results = model.topdown?.results ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Top-down analysis").font(.system(size: 20, weight: .medium)).foregroundStyle(Theme.text)
                        Text("Daily, 4-hour and 1-hour give the direction · 15-minute the entry · 5-minute the trigger")
                            .font(Theme.label(12)).foregroundStyle(Theme.dim)
                    }
                    Spacer()
                    if let at = model.topdown?.at {
                        Text("Updated " + at.suffix(9).prefix(5) + " UTC").font(Theme.label(11)).foregroundStyle(Theme.faint)
                    }
                    Button {
                        Task { await model.refreshTopDown() }
                    } label: {
                        Label(model.topdownLoading ? "Analysing…" : "Analyse all", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(CapsuleButtonStyle())
                    .disabled(model.topdownLoading || model.engineState != .running)
                }

                if let best = model.topdown?.best {
                    BestEntryCard(t: best) {
                        model.showPlan(best)
                        go(.live)
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                } else {
                    Panel("") {
                        HStack(spacing: 12) {
                            StatusIcon(systemName: model.topdownLoading ? "hourglass" : "scope")
                            Text(model.topdown == nil
                                 ? (model.engineState == .running ? "Analysing every pair…" : "Start the engine to analyse.")
                                 : "No plan passes every check right now. Waiting is a result too: the list below says what each pair needs.")
                                .font(Theme.body(14)).foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }

                Text("All pairs").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text).padding(.top, 6)
                ForEach(Array(results.enumerated()), id: \.element.id) { i, t in
                    TopDownRow(t: t) {
                        model.showPlan(t)
                        go(.live)
                    }
                    .opacity(appeared ? 1 : 0)
                    .offset(y: appeared ? 0 : 10)
                    .animation(.easeOut(duration: 0.35).delay(Double(i) * 0.06), value: appeared)
                }
                Text(model.topdown?.note ?? "Rules, not predictions. Nothing is ever placed.")
                    .font(Theme.label(11)).foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(24)
            .frame(maxWidth: 1000, alignment: .leading)
            .frame(maxWidth: .infinity)
            .animation(.easeInOut(duration: 0.3), value: model.topdown?.best?.pair)
        }
        .background(Theme.bg)
        .navigationTitle("Top-down")
        .task {
            if model.topdown == nil { await model.refreshTopDown() }
            appeared = true
        }
    }
}

/// The best plan, large: levels, grade, trigger and why.
struct BestEntryCard: View {
    let t: TopDown
    let onShow: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                PairBadge(pair: t.pair, size: 24)
                Text("\(Instrument.display(t.pair)) \((t.side ?? "").uppercased())")
                    .font(.system(size: 18, weight: .semibold)).foregroundStyle(Theme.text)
                if let g = t.grade?.grade { Pill(text: g, color: g == "A+" ? Theme.good : Theme.text) }
                Spacer()
                LiveStatusPill(text: t.status ?? "", hot: t.enterNow)
            }
            Text("Best entry").font(Theme.label(12)).foregroundStyle(Theme.dim)
            if let p = t.plan {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 8)], spacing: 8) {
                    LevelBox(title: "ENTRY", value: Instrument.price(p.entry, t.pair), note: "15m 20 EMA pullback", color: Theme.blue)
                    LevelBox(title: "STOP", value: Instrument.price(p.stop, t.pair),
                             note: p.riskPips.map { v in String(format: "%.1f pips", v) } ?? "", color: Theme.bad)
                    LevelBox(title: "TARGET", value: Instrument.price(p.target, t.pair),
                             note: p.rewardRisk.map { v in String(format: "%.1fR", v) } ?? "", color: Theme.good)
                }
            }
            if let trig = t.trigger {
                Label(trig, systemImage: t.enterNow ? "bolt.fill" : "clock")
                    .font(Theme.body(13)).foregroundStyle(t.enterNow ? Theme.good : Theme.dim)
            }
            TimeframeStrip(t: t)
            HStack(spacing: 18) {
                fact("Time Machine", t.timemachine?.odds.map { v in String(format: "%.0f%% target first", v * 100) } ?? "–")
                fact("Expected", t.expectancyR.map { v in String(format: "%+.2fR", v) } ?? "–")
                fact("Risk check", t.riskCheck ?? "–")
                fact("Alignment", t.alignment.map { v in "\(v) of 9" } ?? "–")
            }
            if let why = t.grade?.why, !why.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array((why + (t.grade?.missing ?? []).map { m in "Not A+: " + m }).enumerated()), id: \.offset) { _, line in
                        Text("• " + line).font(Theme.label(12)).foregroundStyle(Theme.dim).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            HStack {
                Button("Show on chart", action: onShow).buttonStyle(CapsuleButtonStyle())
                Spacer()
                Text("Analysis only. The app never places a trade.").font(Theme.label(11)).foregroundStyle(Theme.faint)
            }
        }
        .padding(18)
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
            .stroke(t.enterNow ? Theme.good.opacity(0.6) : Theme.line, lineWidth: 1))
    }

    private func fact(_ k: String, _ v: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(k).font(Theme.label(11)).foregroundStyle(Theme.dim)
            Text(v).font(Theme.number(13)).foregroundStyle(Theme.text).contentTransition(.numericText())
        }
    }
}

/// One pair: its five timeframes, status, grade.
struct TopDownRow: View {
    let t: TopDown
    let onOpen: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    PairBadge(pair: t.pair, size: 22)
                    Text(Instrument.display(t.pair)).font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.text)
                    if let s = t.side { Pill(text: s, color: s == "buy" ? Theme.blue : Theme.bad) }
                    if let g = t.grade?.grade { Pill(text: g, color: g == "A+" ? Theme.good : Theme.text) }
                    Spacer()
                    LiveStatusPill(text: t.status ?? "–", hot: t.enterNow)
                }
                TimeframeStrip(t: t)
                Text(t.summary ?? "").font(Theme.label(12)).foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                if let rs = t.reasons, !rs.isEmpty {
                    Text("Not taken: " + rs.joined(separator: "; ")).font(Theme.label(11)).foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(14)
            .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).fill(hover ? Theme.cardHigh : Theme.card))
            .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).stroke(Theme.line, lineWidth: 1))
            .scaleEffect(hover ? 1.005 : 1)
            .animation(.easeOut(duration: 0.15), value: hover)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { h in hover = h }
        .help(t.plan == nil ? "No plan for this pair" : "Show this plan on the live chart")
    }
}

/// D1 H4 H1 M15 M5 with an arrow each: up green, down red, flat grey.
struct TimeframeStrip: View {
    let t: TopDown
    private let order = [("1d", "D1"), ("4h", "H4"), ("1h", "H1"), ("15m", "M15"), ("5m", "M5")]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(order, id: \.0) { item in
                let label = item.1
                let v = t.timeframes?[item.0]
                let bias = v?.bias ?? "unknown"
                HStack(spacing: 5) {
                    Text(label).font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.dim)
                    Image(systemName: bias == "up" ? "arrow.up.right" : (bias == "down" ? "arrow.down.right" : "minus"))
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(bias == "up" ? Theme.good : (bias == "down" ? Theme.bad : Theme.faint))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(Theme.cardHigh))
                .help(v.map { x in "\(label): \(x.bias), score \(x.score ?? 0). \(x.why ?? "")" } ?? "\(label): no data")
            }
        }
    }
}

/// A status with a softly pulsing dot when it is time to act.
struct LiveStatusPill: View {
    let text: String
    let hot: Bool
    @State private var pulse = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(hot ? Theme.good : Theme.faint)
                .frame(width: 7, height: 7)
                .scaleEffect(hot && pulse ? 1.6 : 1)
                .opacity(hot && pulse ? 0.35 : 1)
            Text(text.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(0.6)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .foregroundStyle(hot ? Theme.good : Theme.dim)
        .background(Capsule().fill(Theme.cardHigh))
        .onAppear {
            guard hot, !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) { pulse = true }
        }
    }
}
