import AppKit
import Foundation
import UserNotifications

enum EngineState: Equatable {
    case stopped
    case starting(String)
    case running
    case failed(String)

    var label: String {
        switch self {
        case .stopped: return "Engine stopped"
        case .starting(let s): return s
        case .running: return "Live"
        case .failed(let s): return s
        }
    }
}

struct AlertItem: Identifiable {
    let id = UUID()
    let title: String
    let body: String
    let time: Date
}

/// Everything the windows show, kept up to date from the engine.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var settings = AppSettings.load()
    @Published var engineState: EngineState = .stopped
    @Published var engineLog: [String] = []
    @Published var health: Health?
    @Published var streamConnected = false

    @Published var pair = "EURUSD"
    @Published var timeframe: Timeframe = .m5
    @Published var candles: [Candle] = []
    @Published var forecast: Forecast?
    @Published var analysis: Forecast?
    @Published var analysing = false
    @Published var lastPrice: Double?
    @Published var prices: [String: Double] = [:]
    @Published var lastData: Date?                 // the last tick or candle from the engine
    @Published var topdown: TopDownResponse?
    @Published var topdownLoading = false
    private var notifiedEntries = Set<String>()
    @Published var signals: [String: Signal] = [:]
    @Published var scan: [ScanItem] = []
    @Published var alerts: [AlertItem] = []
    @Published var message: String?

    let chart = ChartBridge()
    private let engine = EngineProcess()
    private let stream = LiveStream()
    private(set) var api: APIClient
    private var booted = false
    private var sessionWindows: [SessionWindow] = []
    private var priceStamps: [String: Date] = [:]
    private var streamWasUp = false
    private var awakeToken: NSObjectProtocol?

    private init() {
        api = APIClient(baseURL: AppSettings.load().baseURL)
        api.token = AppModel.readEnvPassword(AppSettings.load())
    }

    var activeSignals: [Signal] { signals.values.filter { $0.isActive }.sorted { ($0.createdTs ?? 0) > ($1.createdTs ?? 0) } }
    var signalForPair: Signal? {
        let mine = signals.values.filter { $0.pair == pair }
        return mine.first(where: { $0.isActive && $0.shown == true })
            ?? mine.sorted { ($0.createdTs ?? 0) > ($1.createdTs ?? 0) }.first
    }
    var scanForPair: [ScanItem] { scan.filter { $0.pair == pair } }

    // MARK: start up

    func boot() {
        guard !booted else { return }
        booted = true
        if settings.notifications, Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        Task { await connectOrStart() }
    }

    func connectOrStart() async {
        if let h = try? await api.health() {
            if AppModel.isFXDeskEngine(h) {
                await engineIsUp()
            } else {
                engineState = .failed("Another program is using port \(settings.port), so FX Desk can't start its engine. "
                                      + "Quit it (in Terminal: kill $(lsof -t -iTCP:\(settings.port) -sTCP:LISTEN)), then press Start.")
            }
            return
        }
        startEngine()
    }

    /// FX Desk's engine reports its price source in /health; another program on the same port does not.
    nonisolated static func isFXDeskEngine(_ h: Health) -> Bool {
        h.config?.provider != nil
    }

    func startEngine() {
        guard !engine.isRunning else { return }
        engineState = .starting("Starting the engine…")
        engineLog.removeAll()
        let folder = URL(fileURLWithPath: settings.engineFolder, isDirectory: true)
        // the engine reads its settings from .env: write them first, or a first start runs on its defaults
        if FileManager.default.fileExists(atPath: folder.appendingPathComponent("scripts/mac-engine.sh").path) {
            writeEngineEnv(settings, oandaToken: nil, phonePassword: nil)
        }
        do {
            try engine.start(folder: folder, port: settings.port, onLine: { [weak self] line in
                self?.engineLine(line)
            }, onExit: { [weak self] code in
                guard let self else { return }
                self.streamConnected = false
                if case .running = self.engineState {
                    self.engineState = .failed("The engine stopped (code \(code)). See Settings for its log.")
                } else if case .starting = self.engineState {
                    self.engineState = .failed(self.engineLog.last(where: { $0.hasPrefix("FXDESK:ERROR") })?
                        .replacingOccurrences(of: "FXDESK:ERROR ", with: "") ?? "The engine could not start (code \(code)).")
                }
            })
        } catch {
            engineState = .failed(error.localizedDescription)
            return
        }
        Task { await waitForEngine() }
    }

    func stopEngine() {
        stream.stop()
        engine.stop()
        streamConnected = false
        streamWasUp = false
        engineState = .stopped
        updateKeepAwake()
    }

    func restartEngine() {
        stopEngine()
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            startEngine()
        }
    }

    private func engineLine(_ line: String) {
        engineLog.append(line)
        if engineLog.count > 400 { engineLog.removeFirst(engineLog.count - 400) }
        if line.hasPrefix("FXDESK:STAGE ") {
            engineState = .starting(String(line.dropFirst("FXDESK:STAGE ".count)))
        }
    }

    private func waitForEngine() async {
        // the first start installs packages and Kronos: allow up to 20 minutes
        for _ in 0..<600 {
            if case .failed = engineState { return }
            if (try? await api.health()) != nil {
                await engineIsUp()
                return
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        engineState = .failed("The engine did not answer. See Settings for its log.")
    }

    private func engineIsUp() async {
        engineState = .running
        try? await api.putSettings(settings.engineAccount)
        await refreshAll()
        Task { await refreshTopDown() }
        stream.start(url: api.streamURL, onMessage: { [weak self] m in
            self?.handle(m)
        }, onState: { [weak self] up in
            guard let self else { return }
            self.streamConnected = up
            // back after a dropped connection: fetch what was sent while it was down
            if up && self.streamWasUp { Task { await self.refreshAll() } }
            if up { self.streamWasUp = true }
        })
        updateKeepAwake()
        Task { await healthLoop() }
    }

    private func healthLoop() async {
        var round = 0
        while engineState == .running {
            health = try? await api.health()
            round += 1
            if round % 4 == 0 {                    // every minute, in case a stream message was missed
                await refreshSignals()
                scan = (try? await api.scan()) ?? scan
                await refreshTopDown()
            }
            try? await Task.sleep(nanoseconds: 15_000_000_000)
        }
        updateKeepAwake()                          // the engine stopped or failed: let the Mac sleep again
    }

    /// The top-down analysis of every pair; alerts (Mac and phone) when a plan turns "enter now".
    func refreshTopDown() async {
        guard engineState == .running else { return }
        topdownLoading = true
        defer { topdownLoading = false }
        guard let r = try? await api.topdown() else { return }
        topdown = r
        for t in r.results where t.enterNow {
            guard let g = t.grade?.grade, AppModel.gradeAtLeast(g, settings.alertMinGrade), let p = t.plan else { continue }
            let key = "\(t.pair):\(p.entry):\(t.side ?? "")"
            guard notifiedEntries.insert(key).inserted else { continue }
            let title = "\(g) · \(Instrument.display(t.pair)) \((t.side ?? "").uppercased()): enter now"
            let body = "Top-down: entry \(Instrument.price(p.entry, t.pair)), stop \(Instrument.price(p.stop, t.pair)), "
                + "target \(Instrument.price(p.target, t.pair)). Your call: nothing was placed."
            let a = AlertItem(title: title, body: body, time: Date())
            alerts.insert(a, at: 0)
            notify(a)
            Task { await pushToPhone(title: title, body: body) }
        }
    }

    nonisolated static func gradeAtLeast(_ g: String, _ minimum: String) -> Bool {
        let order = ["A+": 3, "A": 2, "B": 1]
        return (order[g] ?? 0) >= (order[minimum] ?? 1)
    }

    /// Draw a top-down plan on the live chart of its pair.
    func showPlan(_ t: TopDown) {
        guard let p = t.plan else { return }
        if t.pair != pair { select(pair: t.pair) }
        Task {
            try? await Task.sleep(nanoseconds: 900_000_000)      // after the pair's chart has loaded
            chart.setPlan(side: t.side ?? "buy", entry: p.entry, stop: p.stop, target: p.target)
        }
    }

    private func pushToPhone(title: String, body: String) async {
        let topic = settings.ntfyTopic.trimmingCharacters(in: .whitespaces)
        guard !topic.isEmpty, let url = URL(string: "https://ntfy.sh/" + topic) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(title, forHTTPHeaderField: "Title")
        req.setValue("high", forHTTPHeaderField: "Priority")
        req.httpBody = Data(body.utf8)
        _ = try? await URLSession.shared.data(for: req)
    }

    /// True when the market is open but no price has arrived for 3 minutes.
    var dataIsStale: Bool {
        guard engineState == .running, AppModel.marketOpen(Date()) else { return false }
        guard let d = lastData else { return true }
        return Date().timeIntervalSince(d) > 180
    }

    /// Forex trades from Sunday 22:00 to Friday 22:00 UTC (it shifts an hour with US summer time).
    nonisolated static func marketOpen(_ now: Date) -> Bool {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let wd = cal.component(.weekday, from: now)      // 1 = Sunday, 7 = Saturday
        let h = cal.component(.hour, from: now)
        return !(wd == 7 || (wd == 1 && h < 22) || (wd == 6 && h >= 22))
    }

    /// Stop the Mac from sleeping while the engine runs, so alerts keep coming (the screen can still sleep).
    func updateKeepAwake() {
        let want = settings.keepAwake && engineState == .running
        if want && awakeToken == nil {
            awakeToken = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled],
                                                               reason: "FX Desk is watching the markets for alerts")
        } else if !want, let t = awakeToken {
            ProcessInfo.processInfo.endActivity(t)
            awakeToken = nil
        }
    }

    /// Send one test notification to the phone through ntfy.
    func sendTestPush(topic: String) async {
        let t = topic.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, let url = URL(string: "https://ntfy.sh/" + t) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("FX Desk", forHTTPHeaderField: "Title")
        req.httpBody = Data("Test from FX Desk: phone notifications work.".utf8)
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            message = code == 200 ? "Test sent. It should appear on your phone in a few seconds."
                : "ntfy answered \(code). Check the topic name."
        } catch {
            message = "Could not reach ntfy: \(error.localizedDescription)"
        }
    }

    // MARK: data

    func refreshAll() async {
        health = try? await api.health()
        await loadChart()
        await refreshSignals()
        scan = (try? await api.scan()) ?? scan
        await loadPrices()
    }

    /// The latest close for every pair, so the Home screen has prices before the first tick.
    func loadPrices() async {
        for p in settings.pairs where prices[p] == nil {
            if let last = try? await api.candles(pair: p, tf: .m1, limit: 2).last {
                prices[p] = last.close
            }
        }
    }

    func refreshSignals() async {
        if let list = try? await api.signals() {
            var d: [String: Signal] = [:]
            for s in list { d[s.id] = s }
            signals = d
            chart.setLevels(signalForPair)
        }
    }

    func loadChart() async {
        let p = pair, tf = timeframe
        chart.setInstrument(digits: Instrument.digits(p))
        do {
            let cs = try await api.candles(pair: p, tf: tf)
            guard p == pair, tf == timeframe else { return }
            candles = cs
            chart.setCandles(cs)
            lastPrice = cs.last?.close
            if let c = cs.last?.close { prices[p] = c }
        } catch {
            message = error.localizedDescription
        }
        forecast = try? await api.forecast(pair: p, tf: tf)
        analysis = nil
        chart.setForecast(forecast?.available == true ? forecast : nil)
        sessionWindows = (try? await api.sessions(pair: p)) ?? []
        chart.setRanges(tf == .h1 ? [] : sessionWindows, until: candles.last.map { $0.time + tf.seconds } ?? 0)
        chart.setLevels(signalForPair)
    }

    func select(pair p: String) {
        pair = p
        Task { await loadChart() }
    }

    func select(timeframe tf: Timeframe) {
        timeframe = tf
        Task { await loadChart() }
    }

    /// Kronos on demand for the pair and timeframe on screen, plus the Day Trader setups.
    func analyseNow() async {
        analysing = true
        defer { analysing = false }
        do {
            let f = try await api.analyse(pair: pair, tf: timeframe, candles: candles)
            analysis = f
            chart.setForecast(f)
        } catch {
            message = "Analysis failed: \(error.localizedDescription)"
        }
        scan = (try? await api.scan()) ?? scan
    }

    func copyToTradingView(_ s: Signal) async {
        do {
            let tv = try await api.tradingView(signalID: s.id)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(tv.script, forType: .string)
            message = "Copied. In TradingView: Pine Editor, paste, Add to chart."
            if let url = URL(string: tv.tradingviewUrl) { NSWorkspace.shared.open(url) }
        } catch {
            message = "Could not make the TradingView script: \(error.localizedDescription)"
        }
    }

    func markSignal(_ s: Signal, action: String) {
        Task { try? await api.record(signalID: s.id, action: action) }
    }

    // MARK: live stream

    private func handle(_ m: StreamMessage) {
        switch m.type {
        case "tick":
            if let p = m.pair, let bid = m.bid {
                let now = Date()
                if now.timeIntervalSince(lastData ?? .distantPast) > 1 { lastData = now }
                if now.timeIntervalSince(priceStamps[p] ?? .distantPast) > 0.5 {
                    priceStamps[p] = now
                    prices[p] = bid
                }
            }
            guard m.pair == pair, let t = m.time, let bid = m.bid else { return }
            lastPrice = bid
            chart.tick(time: t, price: bid, tfSeconds: timeframe.seconds)
        case "candle":
            lastData = Date()
            guard m.pair == pair, m.tf == timeframe.rawValue, m.closed == true, let c = m.candle else { return }
            let candle = Candle(time: c.openTime, open: c.open, high: c.high, low: c.low, close: c.close)
            if let i = candles.lastIndex(where: { $0.time == candle.time }) { candles[i] = candle } else { candles.append(candle) }
            chart.updateCandle(candle)
        case "forecast":
            guard let f = m.forecast, f.pair == pair, f.tf == timeframe.rawValue, analysis == nil else { return }
            forecast = f
            chart.setForecast(f)
        case "signal":
            guard let s = m.signal else { return }
            signals[s.id] = s
            if s.pair == pair { chart.setLevels(signalForPair) }
        case "alert":
            let a = AlertItem(title: m.title ?? "Alert", body: m.body ?? "", time: Date())
            alerts.insert(a, at: 0)
            if alerts.count > 100 { alerts.removeLast() }
            notify(a)
        default:
            break
        }
    }

    private func notify(_ a: AlertItem) {
        guard settings.notifications, Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = a.title
        content.body = a.body
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: a.id.uuidString, content: content, trigger: nil))
    }

    // MARK: settings

    /// Save settings, write them into the engine's private .env, and restart the engine when needed.
    func apply(_ new: AppSettings, oandaToken: String?, phonePassword: String? = nil) {
        let engineChanged = new.provider != settings.provider || new.pairs != settings.pairs || new.sessions != settings.sessions
            || new.useKronos != settings.useKronos || new.port != settings.port || new.engineFolder != settings.engineFolder
            || new.oandaAccount != settings.oandaAccount || !(oandaToken ?? "").isEmpty
            || new.phoneAccess != settings.phoneAccess || !(phonePassword ?? "").isEmpty
            || new.kronosMustAgree != settings.kronosMustAgree
            || new.alertMinGrade != settings.alertMinGrade || new.ntfyTopic != settings.ntfyTopic
        settings = new
        new.save()
        writeEngineEnv(new, oandaToken: oandaToken, phonePassword: phonePassword)
        api = APIClient(baseURL: new.baseURL, token: AppModel.readEnvPassword(new))
        if !new.pairs.contains(pair), let first = new.pairs.first { pair = first }
        if engineChanged {
            restartEngine()
        } else {
            Task { try? await api.putSettings(new.engineAccount) }
        }
        message = engineChanged ? "Saved. Restarting the engine with the new settings." : "Saved."
        updateKeepAwake()
    }

    /// Merge the settings into the engine's private .env (only you can read it); other keys, like tokens, are kept.
    private func writeEngineEnv(_ s: AppSettings, oandaToken: String?, phonePassword: String?) {
        let envURL = URL(fileURLWithPath: s.engineFolder).appendingPathComponent(".env")
        let existing = (try? String(contentsOf: envURL, encoding: .utf8)) ?? "# Written by FX Desk. Private: never share or upload this file.\n"
        let merged = EnvFile.merge(existing: existing, updates: s.envUpdates(oandaToken: oandaToken, phonePassword: phonePassword))
        guard merged != existing else { return }
        do {
            try merged.write(to: envURL, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envURL.path)
        } catch {
            message = "Could not write the engine settings: \(error.localizedDescription)"
        }
    }

    var hasPhonePassword: Bool {
        let envURL = URL(fileURLWithPath: settings.engineFolder).appendingPathComponent(".env")
        let text = (try? String(contentsOf: envURL, encoding: .utf8)) ?? ""
        return (EnvFile.value("FOREX_PASSWORD", in: text) ?? "").count >= 8
    }

    /// The web addresses your phone can open, when "Use on my phone" is on and the engine is live.
    var phoneAddresses: [String] {
        NetworkInfo.addresses().map { a in "http://\(a.host):\(settings.port)" }
    }

    var hasOandaToken: Bool {
        let envURL = URL(fileURLWithPath: settings.engineFolder).appendingPathComponent(".env")
        let text = (try? String(contentsOf: envURL, encoding: .utf8)) ?? ""
        return !(EnvFile.value("OANDA_TOKEN", in: text) ?? "").isEmpty
    }

    static func readEnvPassword(_ s: AppSettings) -> String? {
        let envURL = URL(fileURLWithPath: s.engineFolder).appendingPathComponent(".env")
        guard let text = try? String(contentsOf: envURL, encoding: .utf8) else { return nil }
        return EnvFile.value("FOREX_PASSWORD", in: text) ?? EnvFile.value("FOREX_API_TOKEN", in: text)
    }
}
