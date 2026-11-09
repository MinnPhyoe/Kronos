import Foundation

/// A trade as the risk engine sees it (taken from your records).
struct RecordSnapshot {
    var pair: String
    var side: String
    var openedAt: Date
    var closedAt: Date?
    var riskAmount: Double
    var pnl: Double?
    var isOpen: Bool
}

struct RiskLimits: Codable, Equatable {
    var balance: Double = 10_000
    var currency: String = "GBP"
    var riskPerTradePct: Double = 0.5
    var dailyLossPct: Double = 2.0
    var weeklyLossPct: Double = 4.0
    var maxTradesPerDay: Int = 3
    var minRewardRisk: Double = 1.0

    var maxLossPerTrade: Double { balance * riskPerTradePct / 100 }
    var dailyLimit: Double { balance * dailyLossPct / 100 }
    var weeklyLimit: Double { balance * weeklyLossPct / 100 }
}

struct AppRiskVerdict: Equatable {
    var pass: Bool
    var reasons: [String]
    var notes: [String]
}

/// The app's own risk engine (the second one). The engine's Day Trader risk check looks at the
/// rules; this one looks at YOUR records: what you have lost today and this week, what is still
/// open, and how many trades you have taken. Anything it cannot verify counts as a block.
enum AppRiskEngine {
    /// The forex trading day rolls over at 17:00 New York.
    static func tradingDay(_ date: Date) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/New_York") ?? TimeZone(secondsFromGMT: -5 * 3600)!
        let shifted = date.addingTimeInterval(7 * 3600)
        let c = cal.dateComponents([.year, .month, .day], from: shifted)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    static func tradingWeek(_ date: Date) -> String {
        var cal = Calendar(identifier: .iso8601)
        cal.timeZone = TimeZone(identifier: "America/New_York") ?? TimeZone(secondsFromGMT: -5 * 3600)!
        let shifted = date.addingTimeInterval(7 * 3600)
        let c = cal.dateComponents([.yearForWeekOfYear, .weekOfYear], from: shifted)
        return String(format: "%04d-W%02d", c.yearForWeekOfYear ?? 0, c.weekOfYear ?? 0)
    }

    static func money(_ x: Double, _ currency: String) -> String {
        String(format: "%.2f %@", x, currency)
    }

    static func check(pair: String, side: String, entry: Double?, stop: Double?, target: Double?, riskAmount: Double?,
                      records: [RecordSnapshot], limits: RiskLimits, now: Date = Date()) -> AppRiskVerdict {
        var reasons: [String] = []
        var notes: [String] = []
        guard let e = entry, let s = stop, let t = target else {
            return AppRiskVerdict(pass: false, reasons: ["no entry, stop and target to check"], notes: [])
        }
        let isBuy = side == "buy"
        let risk = isBuy ? e - s : s - e
        let reward = isBuy ? t - e : e - t
        if risk <= 0 || reward <= 0 {
            reasons.append("the stop or the target is on the wrong side of the entry")
        } else if reward / risk + 1e-9 < limits.minRewardRisk {
            reasons.append(String(format: "reward:risk %.2f is under your minimum %.2f", reward / risk, limits.minRewardRisk))
        }
        guard let amount = riskAmount, amount > 0 else {
            reasons.append("cannot verify the size: the engine gave no risk amount")
            return AppRiskVerdict(pass: false, reasons: reasons, notes: notes)
        }
        if amount > limits.maxLossPerTrade + 0.005 {
            reasons.append("risk \(money(amount, limits.currency)) is over your \(money(limits.maxLossPerTrade, limits.currency)) per trade")
        }
        let today = tradingDay(now)
        let week = tradingWeek(now)
        let openRisk = records.filter { r in r.isOpen }.reduce(0.0) { sum, r in sum + r.riskAmount }
        let closedToday = records.filter { r in
            guard !r.isOpen, let d = r.closedAt else { return false }
            return AppRiskEngine.tradingDay(d) == today
        }
        let closedWeek = records.filter { r in
            guard !r.isOpen, let d = r.closedAt else { return false }
            return AppRiskEngine.tradingWeek(d) == week
        }
        let lostToday = max(0.0, -closedToday.reduce(0.0) { sum, r in sum + (r.pnl ?? 0) })
        let lostWeek = max(0.0, -closedWeek.reduce(0.0) { sum, r in sum + (r.pnl ?? 0) })
        let takenToday = records.filter { r in AppRiskEngine.tradingDay(r.openedAt) == today }.count

        if lostToday + openRisk + amount > limits.dailyLimit + 0.005 {
            reasons.append("daily loss: \(money(lostToday, limits.currency)) lost today + \(money(openRisk, limits.currency)) open risk + \(money(amount, limits.currency)) is over your \(money(limits.dailyLimit, limits.currency)) daily limit")
        }
        if lostWeek + openRisk + amount > limits.weeklyLimit + 0.005 {
            reasons.append("weekly loss: \(money(lostWeek, limits.currency)) lost this week + \(money(openRisk, limits.currency)) open risk + \(money(amount, limits.currency)) is over your \(money(limits.weeklyLimit, limits.currency)) weekly limit")
        }
        if takenToday >= limits.maxTradesPerDay {
            reasons.append("trades: \(takenToday) of \(limits.maxTradesPerDay) already taken today")
        }
        if records.contains(where: { r in r.isOpen && r.pair == pair }) {
            reasons.append("you already have an open \(pair) trade")
        }
        notes.append("today: \(money(lostToday, limits.currency)) lost, \(takenToday) of \(limits.maxTradesPerDay) trades")
        return AppRiskVerdict(pass: reasons.isEmpty, reasons: reasons, notes: notes)
    }
}
