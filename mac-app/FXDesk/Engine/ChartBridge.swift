import Foundation
import WebKit

/// Drives the chart page (TradingView's open-source Lightweight Charts, bundled with the app).
/// Calls made before the page is ready are queued and sent once it is.
final class ChartBridge: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    private(set) var webView: WKWebView?
    private var ready = false
    private var queue: [String] = []
    /// The page's drawings changed (JSON text): the app keeps them per pair.
    var onDrawings: ((String) -> Void)?

    func makeWebView() -> WKWebView {
        if let w = webView { return w }
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "fxReady")
        config.userContentController.add(self, name: "fxDrawings")
        let w = WKWebView(frame: .zero, configuration: config)
        w.navigationDelegate = self
        w.setValue(false, forKey: "drawsBackground")
        w.wantsLayer = true
        w.layer?.cornerRadius = 24
        w.layer?.masksToBounds = true
        webView = w
        if let url = ChartBridge.pageURL() {
            w.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        } else {
            w.loadHTMLString("<p style='font:14px -apple-system;padding:20px'>chart.html is missing from the app.</p>", baseURL: nil)
        }
        return w
    }

    static func pageURL() -> URL? {
        #if SWIFT_PACKAGE
        let bundle = Bundle.module
        #else
        let bundle = Bundle.main
        #endif
        return bundle.url(forResource: "chart", withExtension: "html")
            ?? bundle.url(forResource: "chart", withExtension: "html", subdirectory: "Chart")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        markReady()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "fxReady" { markReady() }
        if message.name == "fxDrawings", let json = message.body as? String { onDrawings?(json) }
    }

    private func markReady() {
        guard !ready else { return }
        ready = true
        let pending = queue
        queue.removeAll()
        for js in pending { webView?.evaluateJavaScript(js, completionHandler: nil) }
    }

    private func call(_ function: String, _ argument: Any?) {
        var arg = "null"
        if let a = argument, let data = try? JSONSerialization.data(withJSONObject: a, options: [.fragmentsAllowed]),
           let s = String(data: data, encoding: .utf8) {
            arg = s
        }
        let js = "window.fx && window.fx.\(function)(\(arg));"
        if ready { webView?.evaluateJavaScript(js, completionHandler: nil) } else { queue.append(js) }
    }

    // MARK: what the app shows

    func setInstrument(digits: Int) { call("setInstrument", digits) }

    func setCandles(_ candles: [Candle]) {
        call("setCandles", candles.map { [Double($0.time), $0.open, $0.high, $0.low, $0.close] })
    }

    func updateCandle(_ c: Candle) { call("updateCandle", [Double(c.time), c.open, c.high, c.low, c.close]) }

    func tick(time: Double, price: Double, tfSeconds: Int) {
        let js = "window.fx && window.fx.tick(\(time), \(price), \(tfSeconds));"
        if ready { webView?.evaluateJavaScript(js, completionHandler: nil) }
    }

    func setForecast(_ f: Forecast?) {
        guard let f, let band = f.band, !band.isEmpty, let base = f.baseTime, let last = f.lastClose, f.stale != true else {
            call("setForecast", nil)
            return
        }
        call("setForecast", ["baseTime": base, "lastClose": last,
                             "band": band.map { ["t": $0.t, "p10": $0.p10, "p50": $0.p50, "p90": $0.p90] }])
    }

    func setRanges(_ ranges: [SessionWindow], until: Int) {
        let list: [[String: Any]] = ranges.compactMap { w in
            guard let r = w.range else { return nil }
            return ["start": w.rangeStart, "end": min(w.expiry, until), "high": r.high, "low": r.low,
                    "label": w.label.replacingOccurrences(of: " ORB", with: "") + " range"]
        }
        call("setRanges", list)
    }

    /// A plan on the chart: entry, stop and target with their labels, dashed until it is entered.
    func setPlan(side: String, entry: Double, stop: Double, target: Double, pair: String, strategy: String, waiting: Bool = true) {
        let now = Int(Date().timeIntervalSince1970)
        var levels = ChartBridge.labels(side: side, entry: entry, stop: stop, target: target, pair: pair, name: strategy)
        levels.merge(["side": side, "entry": entry, "stop": stop, "tp1": target, "tp2": target,
                      "start": now - 3600, "end": now + 7200, "waiting": waiting]) { _, new in new }
        call("setLevels", levels)
    }

    /// "Buy · CRT H4→M15", "SL −16.0 pips", "TP +32.0 pips · 2.0R".
    static func labels(side: String, entry: Double, stop: Double, target: Double, pair: String, name: String) -> [String: Any] {
        let pip = Instrument.pip(pair)
        let risk = abs(entry - stop)
        let reward = abs(target - entry)
        let r = risk > 0 ? reward / risk : 0
        return ["entryLabel": (side == "sell" ? "Sell" : "Buy") + (name.isEmpty ? "" : " · " + name),
                "slLabel": String(format: "SL −%.1f pips", risk / pip),
                "tpLabel": String(format: "TP +%.1f pips · %.1fR", reward / pip, r)]
    }

    /// ▲ / ▼ markers on the candles where signals fired: [(time, side, text)].
    func setMarkers(_ markers: [(time: Int, side: String, text: String)]) {
        call("setMarkers", markers.map { m in ["time": m.time, "side": m.side, "text": m.text] as [String: Any] })
    }

    /// The drawings saved for this pair (JSON text from the page), or none.
    func setDrawings(_ json: String?) {
        let obj = json.flatMap { j in try? JSONSerialization.jsonObject(with: Data(j.utf8)) } ?? []
        call("setDrawings", obj)
    }

    func setLevels(_ s: Signal?, holdSeconds: Int = 7200) {
        guard let s, s.isActive || s.status == "closed", let e = s.entry, let sl = s.sl, let t1 = s.tp1 else {
            call("setLevels", nil)
            return
        }
        let start = s.range?.end ?? s.createdTs ?? Int(Date().timeIntervalSince1970)
        var levels = ChartBridge.labels(side: s.side ?? "buy", entry: e, stop: sl, target: t1, pair: s.pair, name: s.setup ?? "")
        levels.merge(["side": s.side ?? "buy", "entry": e, "stop": sl, "tp1": t1, "tp2": s.tp2 ?? t1,
                      "start": start, "end": start + holdSeconds, "waiting": s.status == "waiting"]) { _, new in new }
        if let t2 = s.tp2, t2 != t1 { levels["tp2Label"] = String(format: "TP2 +%.1f pips", abs(t2 - e) / Instrument.pip(s.pair)) }
        call("setLevels", levels)
    }
}
