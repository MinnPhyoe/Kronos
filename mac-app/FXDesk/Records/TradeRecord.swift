import Foundation
import SwiftData

/// One trade in your records. Stored only on this Mac (SwiftData).
@Model
final class TradeRecord {
    var uid: UUID = UUID()
    var openedAt: Date = Date()
    var pair: String = ""
    var side: String = "buy"
    var setup: String = ""
    var source: String = "FX Desk"
    var entry: Double = 0
    var stop: Double = 0
    var target: Double = 0
    var target2: Double?
    var lots: Double = 0
    var riskAmount: Double = 0
    var status: String = "open"          // open | closed
    var exitPrice: Double?
    var closedAt: Date?
    var pnl: Double?                      // in your account currency, from your broker if you entered it
    var resultR: Double?
    var notes: String = ""
    var signalID: String?
    var kronosConfidence: Double?
    var timeMachineOdds: Double?
    var cautiousR: Double?

    init(pair: String, side: String, setup: String, source: String, entry: Double, stop: Double, target: Double,
         target2: Double?, lots: Double, riskAmount: Double, signalID: String?, kronosConfidence: Double?,
         timeMachineOdds: Double?, cautiousR: Double?, notes: String = "", openedAt: Date = Date()) {
        self.uid = UUID()
        self.openedAt = openedAt
        self.pair = pair
        self.side = side
        self.setup = setup
        self.source = source
        self.entry = entry
        self.stop = stop
        self.target = target
        self.target2 = target2
        self.lots = lots
        self.riskAmount = riskAmount
        self.status = "open"
        self.signalID = signalID
        self.kronosConfidence = kronosConfidence
        self.timeMachineOdds = timeMachineOdds
        self.cautiousR = cautiousR
        self.notes = notes
    }

    var isOpen: Bool { status == "open" }

    /// R from the prices: +1 means the amount you risked was won.
    func rMultiple(exit: Double) -> Double? {
        let risk = side == "buy" ? entry - stop : stop - entry
        guard risk > 0 else { return nil }
        return (side == "buy" ? exit - entry : entry - exit) / risk
    }

    /// Close the trade. A result from your broker wins over the one worked out from prices.
    func close(exit: Double, brokerResult: Double?, at date: Date = Date()) {
        exitPrice = exit
        closedAt = date
        status = "closed"
        let r = rMultiple(exit: exit)
        if let b = brokerResult {
            pnl = b
            resultR = riskAmount > 0 ? b / riskAmount : r
        } else {
            resultR = r
            pnl = r.map { $0 * riskAmount }
        }
    }

    var snapshot: RecordSnapshot {
        RecordSnapshot(pair: pair, side: side, openedAt: openedAt, closedAt: closedAt, riskAmount: riskAmount,
                       pnl: pnl, isOpen: isOpen)
    }
}
