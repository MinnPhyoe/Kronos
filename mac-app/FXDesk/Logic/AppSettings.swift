import Foundation

/// Your settings. Kept on this Mac (UserDefaults); the OANDA token only ever goes into the
/// engine's private .env file, never into the app's settings.
struct AppSettings: Codable, Equatable {
    var engineFolder: String = NSString(string: "~/Applications/Kronos/forex-app").expandingTildeInPath
    var port: Int = 8000
    var provider: String = "yahoo"             // yahoo | oanda
    var oandaAccount: String = ""
    var pairs: [String] = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    var sessions: [String] = ["london", "newyork"]
    var useKronos: Bool = true
    var notifications: Bool = true
    var limits = RiskLimits()
    var phoneAccess: Bool = false              // let your phone open the engine's web app on your network

    static let allPairs = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"]
    static let allSessions = [("london", "London 08:00"), ("newyork", "New York 08:00"), ("tokyo", "Tokyo 09:00 (JPY)")]
    private static let key = "FXDesk.settings"

    static func load() -> AppSettings {
        guard let data = UserDefaults.standard.data(forKey: key),
              let s = try? JSONDecoder().decode(AppSettings.self, from: data) else { return AppSettings() }
        return s
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: AppSettings.key)
        }
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    /// The engine settings that live in its .env file (nil removes a key).
    func envUpdates(oandaToken: String?, phonePassword: String? = nil) -> [String: String?] {
        var u: [String: String?] = [
            "FOREX_PROVIDER": provider,
            "FOREX_PAIRS": pairs.joined(separator: ","),
            "FOREX_SESSIONS": sessions.joined(separator: ","),
            "FOREX_MODEL_BACKEND": useKronos ? "kronos" : "bootstrap",
            "FOREX_PORT": String(port),
            // the engine script listens on the network only when this says so AND a password is set
            "FOREX_HOST": phoneAccess ? "0.0.0.0" : "127.0.0.1",
        ]
        if let p = phonePassword, !p.isEmpty { u["FOREX_PASSWORD"] = p }
        if provider == "oanda" {
            u["OANDA_ACCOUNT"] = oandaAccount
            u["OANDA_ENV"] = "practice"
            if let t = oandaToken, !t.isEmpty { u["OANDA_TOKEN"] = t }
        }
        return u
    }

    /// The account limits the engine's Day Trader risk check should use (PUT /settings).
    var engineAccount: [String: Any] {
        [
            "account_balance": limits.balance,
            "account_currency": limits.currency,
            "max_loss_per_trade": limits.maxLossPerTrade,
            "max_daily_loss": max(limits.dailyLimit, limits.maxLossPerTrade),
            "max_trades_per_day": limits.maxTradesPerDay,
            "min_reward_risk": limits.minRewardRisk,
            "pairs": pairs,
            "sessions": sessions,
        ]
    }
}

extension AppSettings {
    /// Settings saved by an older version lack newer options: those keep their defaults instead of
    /// the whole file failing to load.
    init(from decoder: Decoder) throws {
        self.init()
        let c = try decoder.container(keyedBy: CodingKeys.self)
        engineFolder = try c.decodeIfPresent(String.self, forKey: .engineFolder) ?? engineFolder
        port = try c.decodeIfPresent(Int.self, forKey: .port) ?? port
        provider = try c.decodeIfPresent(String.self, forKey: .provider) ?? provider
        oandaAccount = try c.decodeIfPresent(String.self, forKey: .oandaAccount) ?? oandaAccount
        pairs = try c.decodeIfPresent([String].self, forKey: .pairs) ?? pairs
        sessions = try c.decodeIfPresent([String].self, forKey: .sessions) ?? sessions
        useKronos = try c.decodeIfPresent(Bool.self, forKey: .useKronos) ?? useKronos
        notifications = try c.decodeIfPresent(Bool.self, forKey: .notifications) ?? notifications
        limits = (try? c.decodeIfPresent(RiskLimits.self, forKey: .limits)) ?? limits
        phoneAccess = try c.decodeIfPresent(Bool.self, forKey: .phoneAccess) ?? phoneAccess
    }

    /// Why a phone password can't be used, or nil when it is fine.
    static func phonePasswordProblem(_ p: String) -> String? {
        if p.count < 8 { return "Use at least 8 characters." }
        if p != p.trimmingCharacters(in: .whitespacesAndNewlines) { return "No spaces at the start or end." }
        if p.contains("\n") || p.contains("\r") { return "Use a single line." }
        if let f = p.first, let l = p.last, f == l, f == "\"" || f == "'" { return "Don't wrap it in quotes." }
        return nil
    }
}

enum EnvFile {
    /// Merge updates into an existing .env text: replace keys in place, append new ones, drop keys set to nil.
    /// Comments and unrelated keys are kept.
    static func merge(existing: String, updates: [String: String?]) -> String {
        var done = Set<String>()
        var out: [String] = []
        for line in existing.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if !trimmed.hasPrefix("#"), let eq = trimmed.firstIndex(of: "=") {
                let key = String(trimmed[..<eq]).trimmingCharacters(in: .whitespaces)
                if let entry = updates[key] {
                    done.insert(key)
                    if let value = entry { out.append("\(key)=\(value)") }
                    continue
                }
            }
            out.append(line)
        }
        while let last = out.last, last.trimmingCharacters(in: .whitespaces).isEmpty { out.removeLast() }
        for key in updates.keys.sorted() where !done.contains(key) {
            if let value = updates[key] ?? nil { out.append("\(key)=\(value)") }
        }
        return out.joined(separator: "\n") + "\n"
    }

    static func value(_ key: String, in text: String) -> String? {
        for line in text.components(separatedBy: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix(key + "=") { return String(t.dropFirst(key.count + 1)) }
        }
        return nil
    }
}
