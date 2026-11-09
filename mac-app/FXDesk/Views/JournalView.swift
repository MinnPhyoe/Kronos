import SwiftData
import SwiftUI

/// Your trades, kept only on this Mac. Your loss limits are worked out from these records,
/// so they are only as accurate as what you enter.
struct JournalView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.modelContext) private var context
    @Query(sort: \TradeRecord.openedAt, order: .reverse) private var records: [TradeRecord]
    @State private var selection: TradeRecord.ID?
    @State private var editing: TradeRecord?
    @State private var addingManual = false

    var body: some View {
        let limits = model.settings.limits
        let closed = records.filter { r in !r.isOpen }
        let wins = closed.filter { r in (r.resultR ?? 0) > 0 }.count
        let totalR = closed.reduce(0.0) { sum, r in sum + (r.resultR ?? 0) }
        let totalPnl = closed.reduce(0.0) { sum, r in sum + (r.pnl ?? 0) }
        let snaps = records.map { r in r.snapshot }
        let lostToday = JournalView.lost(snaps, inSamePeriodAs: Date(), by: AppRiskEngine.tradingDay)
        let lostWeek = JournalView.lost(snaps, inSamePeriodAs: Date(), by: AppRiskEngine.tradingWeek)
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 14) {
                Text("Records").font(Theme.display(34)).foregroundStyle(Theme.pink)
                Spacer()
                Text("Kept only on this Mac. Your loss limits are worked out from these.")
                    .font(Theme.body(12))
                    .foregroundStyle(Theme.dim)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 10)], spacing: 10) {
                StatTile(title: "Closed trades", value: "\(closed.count)")
                StatTile(title: "Win rate", value: closed.isEmpty ? "–" : String(format: "%.0f%%", Double(wins) / Double(closed.count) * 100))
                StatTile(title: "Average", value: closed.isEmpty ? "–" : String(format: "%+.2fR", totalR / Double(closed.count)))
                StatTile(title: "Total", value: String(format: "%+.2f %@", totalPnl, limits.currency))
                StatTile(title: "Lost today", value: String(format: "%.0f of %.0f", lostToday, limits.dailyLimit))
                StatTile(title: "Lost this week", value: String(format: "%.0f of %.0f", lostWeek, limits.weeklyLimit))
            }
            Table(records, selection: $selection) {
                TableColumn("Opened") { r in Text(r.openedAt, format: .dateTime.day().month(.abbreviated).hour().minute()) }
                TableColumn("Pair") { r in Text(Instrument.display(r.pair)) }
                TableColumn("Side") { r in Text(r.side) }
                TableColumn("Setup") { r in Text(r.setup) }
                TableColumn("Entry") { r in Text(Instrument.price(r.entry, r.pair)).monospacedDigit() }
                TableColumn("Stop") { r in Text(Instrument.price(r.stop, r.pair)).monospacedDigit() }
                TableColumn("Lots") { r in Text(String(format: "%.2f", r.lots)).monospacedDigit() }
                TableColumn("Status") { r in Text(r.isOpen ? "open" : (r.exitPrice.map { "closed at " + Instrument.price($0, r.pair) } ?? "closed")) }
                TableColumn("Result") { r in
                    Text(r.resultR.map { String(format: "%+.2fR", $0) } ?? "–")
                        .foregroundStyle((r.resultR ?? 0) > 0 ? Theme.good : ((r.resultR ?? 0) < 0 ? Theme.bad : Theme.text))
                        .monospacedDigit()
                }
            }
            .contextMenu(forSelectionType: TradeRecord.ID.self) { ids in
                Button("Edit or close…") { open(ids.first) }
                Button("Delete", role: .destructive) {
                    for id in ids { if let r = records.first(where: { $0.id == id }) { context.delete(r) } }
                }
            } primaryAction: { ids in
                open(ids.first)
            }
            .scrollContentBackground(.hidden)
            .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.card))
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            if records.isEmpty {
                HStack(spacing: 14) {
                    CandleBuddy(mood: .waiting).frame(width: 46)
                    Text("No trades recorded yet. Press “Record trade…” on a signal, or add one by hand.")
                        .font(Theme.body(13))
                        .foregroundStyle(Theme.dim)
                }
            }
        }
        .padding(20)
        .background(Theme.bg)
        .navigationTitle("Records")
        .toolbar {
            Button { addingManual = true } label: { Label("Add trade", systemImage: "plus") }
            Button { open(selection) } label: { Label("Edit or close", systemImage: "square.and.pencil") }
                .disabled(selection == nil)
        }
        .sheet(item: $editing) { r in RecordEditor(signal: nil, record: r).environmentObject(model) }
        .sheet(isPresented: $addingManual) { RecordEditor(signal: nil, record: nil).environmentObject(model) }
    }

    static func lost(_ records: [RecordSnapshot], inSamePeriodAs now: Date, by period: (Date) -> String) -> Double {
        let key = period(now)
        var net = 0.0
        for r in records where !r.isOpen {
            if let d = r.closedAt, period(d) == key { net += r.pnl ?? 0 }
        }
        return max(0.0, -net)
    }

    private func open(_ id: TradeRecord.ID?) {
        guard let id, let r = records.first(where: { $0.id == id }) else { return }
        editing = r
    }
}

/// Record a new trade (from a signal or by hand), or close and annotate an existing one.
struct RecordEditor: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.modelContext) private var context
    @Environment(\.dismiss) private var dismiss

    let signal: Signal?
    let record: TradeRecord?

    @State private var pair = "EURUSD"
    @State private var side = "buy"
    @State private var setup = ""
    @State private var entry = 0.0
    @State private var stop = 0.0
    @State private var target = 0.0
    @State private var lots = 0.0
    @State private var riskAmount = 0.0
    @State private var notes = ""
    @State private var exitPrice = 0.0
    @State private var brokerResult = ""
    @State private var loaded = false

    var body: some View {
        let digits = Instrument.digits(pair)
        let fmt = FloatingPointFormatStyle<Double>(locale: .current).precision(.fractionLength(digits))
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                PairBadge(pair: pair, size: 28)
                Text(record == nil ? "Record a trade" : "Trade \(Instrument.display(pair)) \(side)")
                    .font(Theme.display(26))
                    .foregroundStyle(Theme.pink)
            }
            Text("This records the trade in your journal. It does not place an order: place it yourself with your broker.")
                .font(Theme.body(13)).foregroundStyle(Theme.dim)
            Form {
                if record == nil {
                    Picker("Pair", selection: $pair) { ForEach(AppSettings.allPairs, id: \.self) { Text(Instrument.display($0)).tag($0) } }
                    Picker("Side", selection: $side) {
                        Text("Buy").tag("buy")
                        Text("Sell").tag("sell")
                    }
                    .pickerStyle(.segmented)
                    TextField("Setup", text: $setup)
                }
                TextField("Entry", value: $entry, format: fmt)
                TextField("Stop loss", value: $stop, format: fmt)
                TextField("Take profit", value: $target, format: fmt)
                TextField("Lots", value: $lots, format: .number.precision(.fractionLength(2)))
                TextField("Risk at the stop (\(model.settings.limits.currency))", value: $riskAmount, format: .number.precision(.fractionLength(2)))
                if let r = record, r.isOpen {
                    Section("Close the trade") {
                        TextField("Exit price", value: $exitPrice, format: fmt)
                        TextField("Result from your broker (\(model.settings.limits.currency), optional)", text: $brokerResult)
                    }
                }
                TextField("Notes", text: $notes, axis: .vertical).lineLimit(3...6)
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            HStack(spacing: 8) {
                Button("Cancel", role: .cancel) { dismiss() }
                    .buttonStyle(CapsuleButtonStyle(kind: .ghost))
                    .keyboardShortcut(.cancelAction)
                Spacer()
                if let r = record, r.isOpen {
                    Button("Close trade") { closeTrade(r) }
                        .buttonStyle(CapsuleButtonStyle(kind: .pink))
                        .disabled(exitPrice <= 0)
                }
                Button(record == nil ? "Record trade" : "Save") { save() }
                    .buttonStyle(CapsuleButtonStyle())
                    .keyboardShortcut(.defaultAction)
                    .disabled(entry <= 0 || stop <= 0 || target <= 0)
            }
        }
        .padding(22)
        .frame(width: 540)
        .background(Theme.bg)
        .tint(Theme.pink)
        .preferredColorScheme(.dark)
        .onAppear(perform: load)
    }

    private func load() {
        guard !loaded else { return }
        loaded = true
        if let r = record {
            pair = r.pair; side = r.side; setup = r.setup; entry = r.entry; stop = r.stop; target = r.target
            lots = r.lots; riskAmount = r.riskAmount; notes = r.notes; exitPrice = r.exitPrice ?? 0
        } else if let s = signal {
            pair = s.pair; side = s.side ?? "buy"; setup = s.setup ?? ""; entry = s.entry ?? 0; stop = s.sl ?? 0
            target = s.tp1 ?? 0; lots = s.sizing?.lots ?? s.lots ?? 0; riskAmount = s.sizing?.maxLoss ?? 0
        } else {
            pair = model.pair
        }
    }

    private func save() {
        if let r = record {
            r.entry = entry; r.stop = stop; r.target = target; r.lots = lots; r.riskAmount = riskAmount; r.notes = notes
        } else {
            let r = TradeRecord(pair: pair, side: side, setup: setup.isEmpty ? "manual" : setup, source: signal == nil ? "manual" : "FX Desk",
                                entry: entry, stop: stop, target: target, target2: signal?.tp2, lots: lots, riskAmount: riskAmount,
                                signalID: signal?.id, kronosConfidence: signal?.kronosConfidence,
                                timeMachineOdds: signal?.timemachineOdds, cautiousR: signal.flatMap { BestResult.cautiousEstimate($0) },
                                notes: notes)
            context.insert(r)
            if let s = signal { model.markSignal(s, action: "took") }
        }
        try? context.save()
        dismiss()
    }

    private func closeTrade(_ r: TradeRecord) {
        r.notes = notes
        let broker = Double(brokerResult.replacingOccurrences(of: ",", with: "."))
        r.close(exit: exitPrice, brokerResult: broker)
        try? context.save()
        dismiss()
    }
}
