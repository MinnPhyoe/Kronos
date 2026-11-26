import Foundation

// What the engine sends. Field names arrive in snake_case and are decoded with
// .convertFromSnakeCase, so `kronos_confidence` becomes `kronosConfidence`.

struct Candle: Equatable, Identifiable {
    var time: Int
    var open: Double
    var high: Double
    var low: Double
    var close: Double
    var id: Int { time }
}

enum Timeframe: String, CaseIterable, Identifiable {
    case m1 = "1m", m5 = "5m", m15 = "15m", h1 = "1h", h4 = "4h", d1 = "1d"
    var id: String { rawValue }
    var seconds: Int {
        switch self {
        case .m1: return 60
        case .m5: return 300
        case .m15: return 900
        case .h1: return 3600
        case .h4: return 14400
        case .d1: return 86400
        }
    }
    var label: String {
        switch self {
        case .h1: return "1H"
        case .h4: return "4H"
        case .d1: return "1D"
        default: return rawValue
        }
    }
    var engineName: String {
        switch self {
        case .m1: return "M1"
        case .m5: return "M5"
        case .m15: return "M15"
        case .h1: return "H1"
        case .h4: return "H4"
        case .d1: return "D1"
        }
    }
}

struct CandlesResponse: Decodable {
    let pair: String
    let tf: String
    let candles: [[Double]]

    var parsed: [Candle] {
        candles.compactMap { r in
            r.count >= 5 ? Candle(time: Int(r[0]), open: r[1], high: r[2], low: r[3], close: r[4]) : nil
        }
    }
}

struct BandPoint: Codable, Equatable {
    let t: Int
    let p10: Double
    let p50: Double
    let p90: Double
}

struct Forecast: Decodable {
    let pair: String?
    let tf: String?
    let model: String?
    let baseTime: Int?
    let lastClose: Double?
    let directionProb: Double?
    let band: [BandPoint]?
    let available: Bool?
    let stale: Bool?
    let reason: String?
}

struct SignalRange: Decodable {
    let high: Double
    let low: Double
    let start: Int?
    let end: Int?
    let expiry: Int?
}

struct SignalTrigger: Decodable {
    let time: String?
    let price: Double?
}

struct FilterItem: Decodable, Identifiable {
    let name: String
    let ok: Bool
    let detail: String?
    let advisory: Bool?
    var id: String { name }
}

/// One timeframe in the top-down view: "up", "down", "neutral" or "unknown", scored -3..+3.
struct TFView: Decodable {
    let bias: String
    let score: Int?
    let why: String?
}

struct TopDownPlan: Decodable {
    let entry: Double
    let stop: Double
    let target: Double
    let targetNote: String?
    let riskPips: Double?
    let rewardRisk: Double?
    let stopNote: String?
}

/// One entry plan from one strategy on one pair (the top-down pullback, the day-trader setups,
/// JeaFx-style supply and demand, CRT, or a live opening-range breakout).
struct TopDown: Decodable, Identifiable {
    let pair: String
    let strategy: String?
    let entryTf: String?
    let side: String?
    let status: String?
    let summary: String?
    let alignment: Int?
    let price: Double?
    let plan: TopDownPlan?
    let trigger: String?
    let timeframes: [String: TFView]?
    let timemachine: TimeMachineBlock?
    let grade: GradeBlock?
    let riskCheck: String?
    let reasons: [String]?
    let passed: Bool?
    let expectancyR: Double?
    var id: String { pair + ":" + (strategy ?? "") + ":" + (side ?? "") }
    var enterNow: Bool { status == "enter now" && passed == true }
}

/// Each pair's trend on every timeframe, whether or not a strategy found an entry.
struct TopDownPairView: Decodable, Identifiable {
    let pair: String
    let timeframes: [String: TFView]?
    let side: String?
    let status: String?
    let summary: String?
    var id: String { pair }
}

struct TopDownResponse: Decodable {
    let at: String?
    let best: TopDown?
    let results: [TopDown]
    let pairs: [TopDownPairView]?
    let note: String?
}

/// A+ / A / B for a signal that passed every required check: how strong the evidence is, not a promise.
struct GradeBlock: Decodable {
    let grade: String
    let why: [String]?
    let missing: [String]?
    let note: String?
}

struct RiskBlock: Decodable {
    let verdict: String?
    let reasons: [String]?
    let warnings: [String]?
}

struct SizingBlock: Decodable {
    let lots: Double?
    let maxLoss: Double?
}

struct TimeMachineBlock: Decodable {
    let odds: Double?
    let breakEven: Double?
    let avgR: Double?
    let pTarget: Double?
    let pStop: Double?
    let verdict: String?
    let replays: Int?
    let seed: Int?
}

struct KronosBlock: Decodable {
    let paths: Int?
    let entered: Int?
    let tpFirst: Int?
    let slFirst: Int?
    let directionProb: Double?
}

struct ModelBlock: Decodable {
    let available: Bool?
    let name: String?
    let error: String?
}

struct Signal: Decodable, Identifiable {
    let id: String
    let pair: String
    let side: String?
    let setup: String?
    let session: String?
    let status: String
    let entry: Double?
    let sl: Double?
    let tp1: Double?
    let tp2: Double?
    let kronosConfidence: Double?
    let timemachineOdds: Double?
    let expectancyR: Double?
    let riskCheck: String?
    let lots: Double?
    let createdAt: String?
    let createdTs: Int?
    let expiresAt: String?
    let range: SignalRange?
    let trigger: SignalTrigger?
    let filters: [FilterItem]?
    let reasons: [String]?
    let passed: Bool?
    let shown: Bool?
    let risk: RiskBlock?
    let sizing: SizingBlock?
    let timemachine: TimeMachineBlock?
    let kronos: KronosBlock?
    let model: ModelBlock?
    let slPips: Double?
    let tp1Pips: Double?
    let tp2Pips: Double?
    let spreadPips: Double?
    let outcome: String?
    let outcomeR: Double?
    let userAction: String?
    let explanation: String?
    let digits: Int?
    let grade: GradeBlock?
    let kronosGate: Bool?
    let advice: [String]?

    var isActive: Bool { status == "waiting" || status == "triggered" }
    var isAPlus: Bool { grade?.grade == "A+" }
    var isBuy: Bool { side == "buy" }
}

struct SignalsResponse: Decodable {
    let signals: [Signal]
}

struct HealthConfig: Decodable {
    let provider: String?
    let modelBackend: String?
    let pairs: [String]?
}

struct Health: Decodable {
    let feedUp: Bool?
    let modelUp: Bool?
    let marketTime: String?
    let config: HealthConfig?
}

struct ScanRange: Decodable {
    let high: Double
    let low: Double
    let heightPips: Double?
    let start: Int?
    let end: Int?
    let expiry: Int?
}

struct ScanEntry: Decodable {
    let status: String
    let side: String?
    let price: Double?
    let triggerTimeIso: String?
    let distancePips: Double?
}

struct ScanItem: Decodable, Identifiable {
    let pair: String
    let session: String
    let label: String
    let usable: Bool
    let reason: String?
    let range: ScanRange?
    let entry: ScanEntry?
    var id: String { pair + ":" + session }
}

struct ScanResponse: Decodable {
    let results: [ScanItem]
}

struct SessionWindow: Decodable {
    let session: String
    let label: String
    let rangeStart: Int
    let rangeEnd: Int
    let expiry: Int
    let range: ScanRange?
}

struct SessionsResponse: Decodable {
    let sessions: [SessionWindow]
}

struct TradingViewScript: Decodable {
    let script: String
    let tradingviewUrl: String
    let how: String
}

/// One message from the engine's live stream (/stream). Only the fields of its `type` are set.
struct StreamMessage: Decodable {
    struct StreamCandle: Decodable {
        let openTime: Int
        let open: Double
        let high: Double
        let low: Double
        let close: Double
    }

    let type: String
    let pair: String?
    let time: Double?
    let bid: Double?
    let tf: String?
    let closed: Bool?
    let candle: StreamCandle?
    let forecast: Forecast?
    let signal: Signal?
    let title: String?
    let body: String?
    let kind: String?
    let service: String?
    let ok: Bool?
}

enum EngineJSON {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}

enum Instrument {
    static func digits(_ pair: String) -> Int {
        if pair.hasSuffix("JPY") { return 3 }
        if pair == "XAUUSD" { return 2 }
        return 5
    }

    static func pip(_ pair: String) -> Double {
        if pair.hasSuffix("JPY") { return 0.01 }
        if pair == "XAUUSD" { return 0.1 }
        return 0.0001
    }

    static func display(_ pair: String) -> String {
        pair.count == 6 ? String(pair.prefix(3)) + "/" + String(pair.suffix(3)) : pair
    }

    static func price(_ value: Double?, _ pair: String) -> String {
        guard let v = value else { return "–" }
        return String(format: "%.\(digits(pair))f", v)
    }
}
