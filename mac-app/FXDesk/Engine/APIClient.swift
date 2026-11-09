import Foundation

enum APIError: LocalizedError {
    case http(Int, String)
    case notRunning

    var errorDescription: String? {
        switch self {
        case .http(let code, let detail): return "Engine answered \(code): \(detail)"
        case .notRunning: return "The engine is not running."
        }
    }
}

/// Talks to the engine on this Mac (http://127.0.0.1). Nothing goes over the internet.
final class APIClient {
    var baseURL: URL
    var token: String?
    private let session: URLSession

    init(baseURL: URL, token: String? = nil) {
        self.baseURL = baseURL
        self.token = token
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 180
        cfg.waitsForConnectivity = false
        session = URLSession(configuration: cfg)
    }

    private func url(_ path: String, _ query: [String: String] = [:]) -> URL {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        c.percentEncodedPath = path
        if !query.isEmpty {
            c.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        return c.url!
    }

    private func send(_ req: URLRequest) async throws -> Data {
        var r = req
        if let t = token, !t.isEmpty { r.setValue("Bearer " + t, forHTTPHeaderField: "Authorization") }
        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: r)
        } catch {
            throw APIError.notRunning
        }
        let data = result.0
        let response = result.1
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            var detail = String(data: data, encoding: .utf8) ?? ""
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let d = obj["detail"] {
                detail = "\(d)"
            }
            throw APIError.http(code, detail)
        }
        return data
    }

    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:]) async throws -> T {
        let data = try await send(URLRequest(url: url(path, query)))
        return try EngineJSON.decoder.decode(T.self, from: data)
    }

    func send<T: Decodable>(_ method: String, _ path: String, json: Any) async throws -> T {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: json)
        let data = try await send(req)
        return try EngineJSON.decoder.decode(T.self, from: data)
    }

    // MARK: engine calls

    func health() async throws -> Health { try await get("/health") }

    func candles(pair: String, tf: Timeframe, limit: Int = 500) async throws -> [Candle] {
        let r: CandlesResponse = try await get("/candles", ["pair": pair, "tf": tf.rawValue, "limit": String(limit)])
        return r.parsed
    }

    func forecast(pair: String, tf: Timeframe) async throws -> Forecast {
        try await get("/forecast", ["pair": pair, "tf": tf.rawValue])
    }

    func signals() async throws -> [Signal] {
        let r: SignalsResponse = try await get("/signals", ["status": "all", "include_filtered": "true", "limit": "300"])
        return r.signals
    }

    func scan() async throws -> [ScanItem] {
        let r: ScanResponse = try await get("/scan")
        return r.results
    }

    func sessions(pair: String) async throws -> [SessionWindow] {
        let r: SessionsResponse = try await get("/sessions", ["pair": pair])
        return r.sessions
    }

    func tradingView(signalID: String) async throws -> TradingViewScript {
        let id = signalID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? signalID
        return try await get("/signals/\(id)/tradingview")
    }

    func record(signalID: String, action: String) async throws {
        let id = signalID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? signalID
        let _: Signal = try await send("POST", "/signals/\(id)/action", json: ["action": action])
    }

    /// Kronos on demand for any timeframe (the Analyse button).
    func analyse(pair: String, tf: Timeframe, candles: [Candle]) async throws -> Forecast {
        let rows: [[String: Any]] = candles.suffix(480).map {
            ["time": $0.time, "open": $0.open, "high": $0.high, "low": $0.low, "close": $0.close]
        }
        return try await send("POST", "/engine/forecast", json: ["pair": pair, "timeframe": tf.engineName, "candles": rows])
    }

    func putSettings(_ settings: [String: Any]) async throws {
        struct Ignored: Decodable {}
        let _: Ignored = try await send("PUT", "/settings", json: settings)
    }

    var streamURL: URL {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        c.scheme = "ws"
        c.path = "/stream"
        if let t = token, !t.isEmpty { c.queryItems = [URLQueryItem(name: "token", value: t)] }
        return c.url!
    }
}
