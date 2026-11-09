import SwiftUI
import WebKit

/// The chart (TradingView's Lightweight Charts inside the app).
struct ChartView: NSViewRepresentable {
    let bridge: ChartBridge

    func makeNSView(context: Context) -> WKWebView { bridge.makeWebView() }

    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

struct Pill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text.uppercased())
            .font(Theme.title(10))
            .tracking(0.6)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
            .foregroundStyle(color)
            .background(Capsule().fill(color.opacity(0.16)))
    }
}

/// One check with a tick or a cross: never colour alone.
struct CheckRow: View {
    let title: String
    let ok: Bool?
    let detail: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: ok == nil ? "questionmark.circle.fill" : (ok == true ? "checkmark.circle.fill" : "xmark.circle.fill"))
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(ok == nil ? Theme.faint : (ok == true ? Theme.good : Theme.bad))
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(Theme.title(13)).foregroundStyle(Theme.text)
                if !detail.isEmpty {
                    Text(detail).font(Theme.body(12)).foregroundStyle(Theme.dim).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

struct LevelBox: View {
    let title: String
    let value: String
    let note: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                Circle().fill(color).frame(width: 7, height: 7)
                Text(title).font(Theme.title(10)).tracking(0.5).foregroundStyle(Theme.dim)
            }
            Text(value).font(Theme.number(14)).foregroundStyle(Theme.text).lineLimit(1).minimumScaleFactor(0.7)
            if !note.isEmpty {
                Text(note).font(Theme.body(11)).foregroundStyle(Theme.faint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.cardHigh))
    }
}

struct StatTile: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased()).font(Theme.title(10)).tracking(0.8).foregroundStyle(Theme.dim)
            Text(value).font(Theme.display(20)).foregroundStyle(Theme.text).monospacedDigit().lineLimit(1).minimumScaleFactor(0.6)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.card))
    }
}

func pct(_ x: Double?) -> String { x.map { String(format: "%.0f%%", $0 * 100) } ?? "–" }
func rText(_ x: Double?) -> String { x.map { String(format: "%+.2fR", $0) } ?? "–" }

func statusColor(_ status: String) -> Color {
    switch status {
    case "triggered": return Theme.blue
    case "waiting": return Theme.gold
    case "closed": return Theme.dim
    case "filtered", "expired": return Theme.faint
    default: return Theme.dim
    }
}

/// A signal with everything both risk engines, Kronos and the Time Machine said about it.
struct SignalCard: View {
    @EnvironmentObject var model: AppModel
    let signal: Signal
    let appRisk: AppRiskVerdict
    var highlight = false
    var onRecord: (() -> Void)?

    var body: some View {
        let s = signal
        let pair = s.pair
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 10) {
                PairBadge(pair: pair, size: 26)
                VStack(alignment: .leading, spacing: 4) {
                    Text(Instrument.display(pair))
                        .font(Theme.display(22))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                        .fixedSize()
                    HStack(spacing: 6) {
                        if let side = s.side {
                            Pill(text: side, color: side == "buy" ? Theme.blue : Theme.bad)
                        }
                        Text(s.setup ?? "").font(Theme.body(13)).foregroundStyle(Theme.dim).lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                Pill(text: s.status, color: statusColor(s.status))
            }
            if s.entry != nil {
                HStack(spacing: 8) {
                    LevelBox(title: s.status == "waiting" ? "ENTRY (PLANNED)" : "ENTRY", value: Instrument.price(s.entry, pair), note: "", color: Theme.blue)
                    LevelBox(title: "STOP", value: Instrument.price(s.sl, pair), note: s.slPips.map { v in String(format: "%.1f pips", v) } ?? "", color: Theme.bad)
                    LevelBox(title: "TP1", value: Instrument.price(s.tp1, pair), note: s.tp1Pips.map { v in String(format: "%.1f pips", v) } ?? "", color: Theme.good)
                    LevelBox(title: "TP2", value: Instrument.price(s.tp2, pair), note: s.tp2Pips.map { v in String(format: "%.1f pips", v) } ?? "", color: Theme.good)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                CheckRow(title: "Day Trader risk check: \(s.riskCheck ?? "not run")", ok: s.riskCheck.map { v in v == "PASS" },
                         detail: (s.risk?.reasons ?? []).joined(separator: "; "))
                CheckRow(title: "Your records: \(appRisk.pass ? "PASS" : "BLOCK")", ok: appRisk.pass,
                         detail: (appRisk.pass ? appRisk.notes : appRisk.reasons).joined(separator: "; "))
                CheckRow(title: "Kronos: \(pct(s.kronosConfidence)) of paths reach TP1 first", ok: filterOK("confidence"),
                         detail: s.model?.name.map { v in "model: " + v } ?? "model unavailable")
                CheckRow(title: "Time Machine: \(pct(s.timemachineOdds)) target first", ok: filterOK("timemachine"),
                         detail: s.timemachine?.verdict ?? "")
                CheckRow(title: "Cautious estimate: \(rText(BestResult.cautiousEstimate(s)))", ok: BestResult.cautiousEstimate(s).map { v in v > 0 },
                         detail: "the lower of Kronos \(rText(s.expectancyR)) and Time Machine \(rText(s.timemachine?.avgR)). Estimates, not measured win rates.")
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.bg.opacity(0.6)))
            if let lots = s.sizing?.lots ?? s.lots, lots > 0 {
                Text(String(format: "Size %.2f lots, risk %.2f %@ at the stop", lots, s.sizing?.maxLoss ?? 0, model.settings.limits.currency))
                    .font(Theme.body(13)).foregroundStyle(Theme.dim)
            }
            if let e = s.explanation {
                Text(e).font(Theme.body(13)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                if s.isActive, s.entry != nil, let onRecord {
                    Button("Record trade…", action: onRecord).buttonStyle(CapsuleButtonStyle())
                    Button("Skip") { model.markSignal(s, action: "skipped") }.buttonStyle(CapsuleButtonStyle(kind: .ghost))
                }
                if s.entry != nil {
                    Button { Task { await model.copyToTradingView(s) } } label: {
                        Label("Copy to TradingView", systemImage: "doc.on.clipboard")
                    }
                    .buttonStyle(CapsuleButtonStyle(kind: .ghost))
                    .help("Copies a Pine Script that draws this entry, stop and targets, and opens the pair on TradingView")
                }
            }
            Text("Analysis only. The app never places a trade.").font(Theme.body(11)).foregroundStyle(Theme.faint)
        }
        .padding(18)
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
            .stroke(highlight ? Theme.pink : Theme.line, lineWidth: highlight ? 2.5 : 1))
    }

    private func filterOK(_ name: String) -> Bool? {
        signal.filters?.first(where: { $0.name == name })?.ok
    }
}
