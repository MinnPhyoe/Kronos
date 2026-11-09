import XCTest
@testable import FXDesk

final class RiskEngineTests: XCTestCase {
    let limits = RiskLimits(balance: 10_000, currency: "GBP", riskPerTradePct: 0.5, dailyLossPct: 2, weeklyLossPct: 4,
                            maxTradesPerDay: 3, minRewardRisk: 1)
    let now = ISO8601DateFormatter().date(from: "2026-11-10T10:00:00Z")!

    func testPassesWithCleanRecords() {
        let v = AppRiskEngine.check(pair: "EURUSD", side: "buy", entry: 1.0852, stop: 1.0838, target: 1.0866,
                                    riskAmount: 49, records: [], limits: limits, now: now)
        XCTAssertTrue(v.pass, v.reasons.joined(separator: "; "))
    }

    func testDailyLimitCountsLossesAndOpenRisk() {
        let lost = RecordSnapshot(pair: "GBPUSD", side: "buy", openedAt: now.addingTimeInterval(-7200),
                                  closedAt: now.addingTimeInterval(-3600), riskAmount: 50, pnl: -120, isOpen: false)
        let open = RecordSnapshot(pair: "USDJPY", side: "sell", openedAt: now.addingTimeInterval(-600), closedAt: nil,
                                  riskAmount: 50, pnl: nil, isOpen: true)
        let v = AppRiskEngine.check(pair: "EURUSD", side: "buy", entry: 1.0852, stop: 1.0838, target: 1.0866,
                                    riskAmount: 49, records: [lost, open], limits: limits, now: now)
        XCTAssertFalse(v.pass)
        XCTAssertTrue(v.reasons.contains { $0.hasPrefix("daily loss") })      // 120 + 50 + 49 > 200
    }

    func testOpenTradeOnSamePairAndUnverifiableSizeBlock() {
        let open = RecordSnapshot(pair: "EURUSD", side: "buy", openedAt: now, closedAt: nil, riskAmount: 10, pnl: nil, isOpen: true)
        let v = AppRiskEngine.check(pair: "EURUSD", side: "buy", entry: 1.0852, stop: 1.0838, target: 1.0866,
                                    riskAmount: 20, records: [open], limits: limits, now: now)
        XCTAssertTrue(v.reasons.contains { $0.contains("already have an open EURUSD") })
        let u = AppRiskEngine.check(pair: "EURUSD", side: "buy", entry: 1.0852, stop: 1.0838, target: 1.0866,
                                    riskAmount: nil, records: [], limits: limits, now: now)
        XCTAssertFalse(u.pass)
    }

    func testTradingDayRollsAt5pmNewYork() {
        let sundayEvening = ISO8601DateFormatter().date(from: "2026-11-15T22:30:00Z")!
        XCTAssertEqual(AppRiskEngine.tradingDay(sundayEvening), "2026-11-16")
    }
}

final class BestResultTests: XCTestCase {
    func signal(_ id: String, passed: Bool, exp: Double, tm: Double) throws -> Signal {
        let json = """
        {"id": "\(id)", "pair": "EURUSD", "side": "buy", "setup": "London ORB", "status": "triggered", "entry": 1.0852,
         "sl": 1.0838, "tp1": 1.0866, "tp2": 1.0881, "kronos_confidence": 0.67, "timemachine_odds": 0.61,
         "expectancy_r": \(exp), "risk_check": "PASS", "passed": \(passed), "shown": \(passed),
         "sizing": {"lots": 0.35, "max_loss": 49.0}, "timemachine": {"avg_R": \(tm), "odds": 0.61},
         "reasons": \(passed ? "[]" : "[\"3 of 30 paths\"]"), "filters": [{"name": "confidence", "ok": true, "detail": "x"}]}
        """
        return try EngineJSON.decoder.decode(Signal.self, from: Data(json.utf8))
    }

    func testRanksByTheCautiousEstimate() throws {
        let a = try signal("a", passed: true, exp: 0.40, tm: 0.10)     // cautious 0.10
        let b = try signal("b", passed: true, exp: 0.25, tm: 0.20)     // cautious 0.20: best
        let c = try signal("c", passed: false, exp: 0.90, tm: 0.90)    // engine blocked
        let ranked = BestResult.rank([a, b, c]) { _ in AppRiskVerdict(pass: true, reasons: [], notes: []) }
        XCTAssertEqual(ranked.map { $0.signal.id }, ["b", "a", "c"])
        XCTAssertFalse(ranked[2].eligible)
        XCTAssertTrue(BestResult.message(ranked).hasPrefix("Best: EUR/USD BUY"))
    }

    func testRecordsCanBlockTheBest() throws {
        let a = try signal("a", passed: true, exp: 0.40, tm: 0.30)
        let ranked = BestResult.rank([a]) { _ in AppRiskVerdict(pass: false, reasons: ["daily loss"], notes: []) }
        XCTAssertFalse(ranked[0].eligible)
        XCTAssertTrue(BestResult.message(ranked).contains("Waiting is a result too"))
    }
}

final class EnvAndStreamTests: XCTestCase {
    func testEnvMergeKeepsCommentsAndReplacesKeys() {
        let old = "# private\nFOREX_PROVIDER=yahoo\nOANDA_TOKEN=secret\nOTHER=1\n"
        let new = EnvFile.merge(existing: old, updates: ["FOREX_PROVIDER": "oanda", "FOREX_PAIRS": "EURUSD", "OTHER": nil])
        XCTAssertEqual(new, "# private\nFOREX_PROVIDER=oanda\nOANDA_TOKEN=secret\nFOREX_PAIRS=EURUSD\n")
        XCTAssertEqual(EnvFile.value("OANDA_TOKEN", in: new), "secret")
    }

    func testDecodesStreamMessages() throws {
        let tick = try EngineJSON.decoder.decode(StreamMessage.self, from: Data(#"{"type":"tick","pair":"EURUSD","time":1794211200.5,"bid":1.0852,"ask":1.0853}"#.utf8))
        XCTAssertEqual(tick.bid, 1.0852)
        let candle = try EngineJSON.decoder.decode(StreamMessage.self, from: Data(#"{"type":"candle","pair":"EURUSD","tf":"5m","closed":true,"candle":{"pair":"EURUSD","tf":"5m","open_time":1794211200,"open":1.1,"high":1.2,"low":1.0,"close":1.15,"tick_volume":3,"spread":0.0001,"time":"2026-11-09T08:00:00Z"}}"#.utf8))
        XCTAssertEqual(candle.candle?.openTime, 1794211200)
    }
}
