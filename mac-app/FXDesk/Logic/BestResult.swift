import Foundation

struct RankedSignal: Identifiable {
    let signal: Signal
    let appRisk: AppRiskVerdict
    let eligible: Bool
    let score: Double?
    let whyNot: [String]
    var id: String { signal.id }
}

/// Picks the best result. A signal counts only when:
/// 1. the engine passed it: Day Trader risk check, Kronos confidence and expectancy, Time Machine;
/// 2. the app's own risk engine passes it (your records);
/// 3. its cautious estimate, the lower of Kronos' and the Time Machine's expected R, is above zero.
/// The best is the eligible signal with the highest cautious estimate.
enum BestResult {
    /// With "Kronos must agree" on: the lower of Kronos' and the Time Machine's expected result.
    /// Otherwise Kronos is advice only and the Time Machine's expectancy (after costs) decides.
    static func cautiousEstimate(_ s: Signal) -> Double? {
        if s.kronosGate == false { return s.expectancyR ?? s.timemachine?.avgR }
        guard let k = s.expectancyR, let t = s.timemachine?.avgR else { return nil }
        return min(k, t)
    }

    static func rank(_ signals: [Signal], appRisk: (Signal) -> AppRiskVerdict) -> [RankedSignal] {
        let ranked = signals.filter { s in s.isActive }.map { s -> RankedSignal in
            let verdict = appRisk(s)
            var why: [String] = []
            if s.passed != true {
                let engine = (s.reasons ?? []).filter { !$0.isEmpty }
                why += engine.isEmpty ? ["the engine did not pass it"] : engine.map { "Engine: " + $0 }
            }
            why += verdict.reasons.map { "Your records: " + $0 }
            let score = cautiousEstimate(s)
            if why.isEmpty, let sc = score, sc <= 0 {
                why.append(String(format: "the cautious estimate (%+.2fR) is not above zero", sc))
            }
            if why.isEmpty && score == nil {
                why.append("no expected-R estimate from the Time Machine")
            }
            return RankedSignal(signal: s, appRisk: verdict, eligible: why.isEmpty, score: score, whyNot: why)
        }
        return ranked.sorted { a, b in
            if a.eligible != b.eligible { return a.eligible }
            if a.signal.isAPlus != b.signal.isAPlus { return a.signal.isAPlus }      // A+ first
            return (a.score ?? -99) > (b.score ?? -99)
        }
    }

    static func message(_ ranked: [RankedSignal]) -> String {
        guard let best = ranked.first(where: { $0.eligible }), let sc = best.score else {
            return "No setup passed both risk checks and the Time Machine. Waiting is a result too."
        }
        let grade = best.signal.grade.map { g in " Grade " + g.grade + "." } ?? ""
        return String(format: "Best: %@ %@, %@.%@ It passed both risk checks and the Time Machine; expected %+.2fR a trade.",
                      Instrument.display(best.signal.pair), (best.signal.side ?? "").uppercased(), best.signal.setup ?? "setup",
                      grade, sc)
    }
}
