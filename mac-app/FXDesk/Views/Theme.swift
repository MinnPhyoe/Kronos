import SwiftUI

/// The look: a black canvas, heavy rounded type, playful colour cards with slowly morphing blobs,
/// cartoon art with thick black outlines, and white capsule buttons.
enum Theme {
    static let bg = Color(hex: 0x0B0B0C)
    static let card = Color(hex: 0x1C1C1F)
    static let cardHigh = Color(hex: 0x26262B)
    static let ink = Color(hex: 0x111111)
    static let text = Color.white
    static let dim = Color.white.opacity(0.62)
    static let faint = Color.white.opacity(0.38)
    static let line = Color.white.opacity(0.10)

    static let pink = Color(hex: 0xFF2E8A)
    static let coral = Color(hex: 0xFF7A59)
    static let purple = Color(hex: 0x6B3BFF)
    static let blue = Color(hex: 0x1EA7FF)
    static let lime = Color(hex: 0xC7F561)
    static let gold = Color(hex: 0xFFC83D)
    static let good = Color(hex: 0x30D158)
    static let bad = Color(hex: 0xFF453A)

    static func display(_ size: CGFloat) -> Font { .system(size: size, weight: .heavy, design: .rounded) }
    static func title(_ size: CGFloat) -> Font { .system(size: size, weight: .bold, design: .rounded) }
    static func body(_ size: CGFloat = 14) -> Font { .system(size: size, weight: .medium, design: .rounded) }
    static func number(_ size: CGFloat = 14) -> Font { .system(size: size, weight: .semibold, design: .monospaced) }

    static let radius: CGFloat = 24
}

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
}

extension Instrument {
    static func name(_ pair: String) -> String {
        let names: [String: String] = [
            "EUR": "Euro", "USD": "US dollar", "GBP": "Pound", "JPY": "Yen", "XAU": "Gold",
            "AUD": "Aussie dollar", "CAD": "Canadian dollar", "CHF": "Swiss franc", "NZD": "Kiwi dollar",
        ]
        guard pair.count == 6 else { return pair }
        let base = names[String(pair.prefix(3))] ?? String(pair.prefix(3))
        let quote = names[String(pair.suffix(3))] ?? String(pair.suffix(3))
        return base + " in " + quote
    }
}

// MARK: - animated backgrounds

/// A colour field with a few soft blobs that slowly change shape. Still when Reduce Motion is on.
struct BlobField: View {
    let base: Color
    let blob: Color
    var seed: Double = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate + seed * 37.0
            Canvas { context, size in
                BlobField.draw(&context, size: size, time: t, base: base, blob: blob)
            }
        }
        .accessibilityHidden(true)
    }

    static func draw(_ ctx: inout GraphicsContext, size: CGSize, time t: Double, base: Color, blob: Color) {
        ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(base))
        for i in 0..<4 {
            let opacity: Double = i % 2 == 0 ? 0.85 : 0.5
            ctx.fill(blobPath(index: i, time: t, size: size), with: .color(blob.opacity(opacity)))
        }
    }

    static func blobPath(index: Int, time t: Double, size: CGSize) -> Path {
        let k = Double(index)
        let w = Double(size.width)
        let h = Double(size.height)
        let cx: Double = w * (0.12 + 0.76 * fract(0.37 * k + 0.13)) + sin(t * 0.31 + k * 1.7) * w * 0.06
        let cy: Double = h * (0.15 + 0.7 * fract(0.61 * k + 0.29)) + cos(t * 0.27 + k * 2.3) * h * 0.08
        let r: Double = min(w, h) * (index % 2 == 0 ? 0.30 : 0.22)
        var path = Path()
        let n = 48
        for j in 0...n {
            let a: Double = Double(j) / Double(n) * 2.0 * Double.pi
            let wobble: Double = 0.16 * sin(3.0 * a + t * 0.9 + k) + 0.08 * sin(5.0 * a - t * 0.7 + k * 2.0)
            let rr: Double = r * (1.0 + wobble)
            let p = CGPoint(x: cx + cos(a) * rr, y: cy + sin(a) * rr)
            if j == 0 {
                path.move(to: p)
            } else {
                path.addLine(to: p)
            }
        }
        path.closeSubpath()
        return path
    }

    private static func fract(_ x: Double) -> Double { x - x.rounded(.down) }
}

// MARK: - the mascot

enum BuddyMood {
    case happy, waiting, sleepy, worried

    var label: String {
        switch self {
        case .happy: return "happy"
        case .waiting: return "waiting"
        case .sleepy: return "sleepy"
        case .worried: return "worried"
        }
    }
}

/// Candle Buddy: an original cartoon candlestick with a thick black outline.
/// It bobs, blinks, looks around, waves when a setup passes, and dozes while the engine starts.
struct CandleBuddy: View {
    var mood: BuddyMood = .happy
    var color: Color = Theme.pink
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            Canvas { context, size in
                CandleBuddy.draw(&context, size: size, time: t, mood: mood, color: color)
            }
        }
        .aspectRatio(100.0 / 140.0, contentMode: .fit)
        .accessibilityElement()
        .accessibilityLabel("Candle Buddy, \(mood.label)")
    }

    /// Drawn in a 100 × 140 space, scaled to fit.
    static func draw(_ ctx: inout GraphicsContext, size: CGSize, time t: Double, mood: BuddyMood, color: Color) {
        let s: CGFloat = min(size.width / 100.0, size.height / 140.0)
        var base = ctx
        base.translateBy(x: (size.width - 100.0 * s) / 2.0, y: (size.height - 140.0 * s) / 2.0)
        base.scaleBy(x: s, y: s)

        let bobSize: Double = mood == .sleepy ? 1.5 : 3.0
        let bobSpeed: Double = mood == .sleepy ? 1.1 : (mood == .worried ? 5.0 : 2.2)
        let bob = CGFloat(sin(t * bobSpeed) * bobSize)

        // shadow on the floor (it does not bob)
        let shadowWidth: CGFloat = 46.0 - bob * 2.0
        base.fill(Path(ellipseIn: CGRect(x: 50.0 - shadowWidth / 2.0, y: 132.0, width: shadowWidth, height: 6.0)),
                  with: .color(Color.black.opacity(0.28)))

        var g = base
        g.translateBy(x: 0, y: bob - 2.0)
        let ink = GraphicsContext.Shading.color(Theme.ink)
        let thick = StrokeStyle(lineWidth: 5, lineCap: .round, lineJoin: .round)
        let thin = StrokeStyle(lineWidth: 3.5, lineCap: .round, lineJoin: .round)

        // wicks
        var wick = Path()
        wick.move(to: CGPoint(x: 50, y: 10))
        wick.addLine(to: CGPoint(x: 50, y: 30))
        wick.move(to: CGPoint(x: 50, y: 110))
        wick.addLine(to: CGPoint(x: 50, y: 128))
        g.stroke(wick, with: ink, style: thick)

        // a little spark on the top wick when happy
        if mood == .happy {
            let pulse = CGFloat(0.75 + 0.25 * sin(t * 6.0))
            let spark = Path(ellipseIn: CGRect(x: 50.0 - 5.0 * pulse, y: 4.0 - 5.0 * pulse, width: 10.0 * pulse, height: 10.0 * pulse))
            g.fill(spark, with: .color(Theme.gold))
            g.stroke(spark, with: ink, style: thin)
        }

        // arms
        var arms = Path()
        arms.move(to: CGPoint(x: 23, y: 78))
        arms.addLine(to: CGPoint(x: 11, y: mood == .worried ? 66 : 92))
        let waveAngle: Double = mood == .happy ? -1.0 + 0.55 * sin(t * 7.0) : (mood == .worried ? -0.6 : 0.9)
        let armEnd = CGPoint(x: 77.0 + cos(waveAngle) * 17.0, y: 78.0 + sin(waveAngle) * 17.0)
        arms.move(to: CGPoint(x: 77, y: 78))
        arms.addLine(to: armEnd)
        g.stroke(arms, with: ink, style: thick)
        let hand1 = Path(ellipseIn: CGRect(x: 11.0 - 4.0, y: (mood == .worried ? 66.0 : 92.0) - 4.0, width: 8, height: 8))
        let hand2 = Path(ellipseIn: CGRect(x: armEnd.x - 4.0, y: armEnd.y - 4.0, width: 8, height: 8))
        g.fill(hand1, with: .color(.white))
        g.stroke(hand1, with: ink, style: thin)
        g.fill(hand2, with: .color(.white))
        g.stroke(hand2, with: ink, style: thin)

        // body
        let shape = Path(roundedRect: CGRect(x: 23, y: 28, width: 54, height: 84), cornerRadius: 16)
        g.fill(shape, with: .color(color))
        g.stroke(shape, with: ink, style: thick)
        g.fill(Path(roundedRect: CGRect(x: 30, y: 36, width: 7, height: 24), cornerRadius: 3.5), with: .color(Color.white.opacity(0.5)))

        // cheeks
        g.fill(Path(ellipseIn: CGRect(x: 28, y: 76, width: 10, height: 6)), with: .color(Theme.coral.opacity(0.9)))
        g.fill(Path(ellipseIn: CGRect(x: 62, y: 76, width: 10, height: 6)), with: .color(Theme.coral.opacity(0.9)))

        drawEyes(&g, time: t, mood: mood, ink: ink, thin: thin)
        drawMouth(&g, time: t, mood: mood, ink: ink, thin: thin)

        if mood == .worried {
            var drop = Path()
            drop.move(to: CGPoint(x: 80, y: 34))
            drop.addQuadCurve(to: CGPoint(x: 80, y: 48), control: CGPoint(x: 88, y: 46))
            drop.addQuadCurve(to: CGPoint(x: 80, y: 34), control: CGPoint(x: 72, y: 46))
            g.fill(drop, with: .color(Theme.blue))
            g.stroke(drop, with: ink, style: thin)
        }
        if mood == .sleepy {
            for i in 0..<3 {
                let phase: Double = (t * 0.5 + Double(i) / 3.0).truncatingRemainder(dividingBy: 1.0)
                let z = Text("z").font(.system(size: CGFloat(9.0 + phase * 8.0), weight: .heavy, design: .rounded))
                    .foregroundColor(Color.white.opacity(1.0 - phase))
                g.draw(z, at: CGPoint(x: 78.0 + phase * 14.0, y: 30.0 - phase * 26.0))
            }
        }
    }

    private static func drawEyes(_ g: inout GraphicsContext, time t: Double, mood: BuddyMood, ink: GraphicsContext.Shading, thin: StrokeStyle) {
        let blinking = mood != .sleepy && t.truncatingRemainder(dividingBy: 4.2) < 0.14
        let lookX: CGFloat = mood == .waiting ? CGFloat(sin(t * 0.8) * 2.2) : 0
        let lookY: CGFloat = mood == .happy ? -1.5 : (mood == .worried ? 1.5 : 0)
        for cx in [CGFloat(40), CGFloat(60)] {
            if blinking || mood == .sleepy {
                var lid = Path()
                lid.move(to: CGPoint(x: cx - 6, y: 62))
                lid.addQuadCurve(to: CGPoint(x: cx + 6, y: 62), control: CGPoint(x: cx, y: mood == .sleepy ? 67 : 64))
                g.stroke(lid, with: ink, style: thin)
            } else {
                let eye = Path(ellipseIn: CGRect(x: cx - 7.5, y: 54.5, width: 15, height: 15))
                g.fill(eye, with: .color(.white))
                g.stroke(eye, with: ink, style: thin)
                g.fill(Path(ellipseIn: CGRect(x: cx - 3.5 + lookX, y: 58.5 + lookY, width: 7, height: 7)), with: ink)
                g.fill(Path(ellipseIn: CGRect(x: cx - 1.5 + lookX, y: 59.5 + lookY, width: 2.5, height: 2.5)), with: .color(.white))
            }
            if mood == .worried {
                var brow = Path()
                let inner: CGFloat = cx < 50 ? cx + 6 : cx - 6
                let outer: CGFloat = cx < 50 ? cx - 6 : cx + 6
                brow.move(to: CGPoint(x: outer, y: 50))
                brow.addLine(to: CGPoint(x: inner, y: 46))
                g.stroke(brow, with: ink, style: thin)
            }
        }
    }

    private static func drawMouth(_ g: inout GraphicsContext, time t: Double, mood: BuddyMood, ink: GraphicsContext.Shading, thin: StrokeStyle) {
        var mouth = Path()
        switch mood {
        case .happy:
            mouth.move(to: CGPoint(x: 41, y: 82))
            mouth.addQuadCurve(to: CGPoint(x: 59, y: 82), control: CGPoint(x: 50, y: 97))
            mouth.closeSubpath()
            g.fill(mouth, with: ink)
            g.fill(Path(ellipseIn: CGRect(x: 45, y: 86, width: 10, height: 5)), with: .color(Theme.coral))
        case .waiting:
            mouth.move(to: CGPoint(x: 44, y: 87))
            mouth.addLine(to: CGPoint(x: 56, y: 87))
            g.stroke(mouth, with: ink, style: thin)
        case .sleepy:
            let gap = CGFloat(3.0 + 1.5 * sin(t * 1.1))
            g.fill(Path(ellipseIn: CGRect(x: 50 - gap, y: 86 - gap, width: gap * 2, height: gap * 2)), with: ink)
        case .worried:
            mouth.move(to: CGPoint(x: 42, y: 91))
            mouth.addQuadCurve(to: CGPoint(x: 58, y: 91), control: CGPoint(x: 50, y: 82))
            g.stroke(mouth, with: ink, style: thin)
        }
    }
}

// MARK: - card art

/// Kronos' forecast paths fanning out: tilted up when most paths end higher.
struct FanArt: View {
    var upProbability: Double = 0.5
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            Canvas { context, size in
                FanArt.draw(&context, size: size, time: t, up: upProbability)
            }
        }
        .accessibilityHidden(true)
    }

    static func draw(_ ctx: inout GraphicsContext, size: CGSize, time t: Double, up: Double) {
        let w = Double(size.width)
        let h = Double(size.height)
        let mid: Double = h * 0.55
        let start = CGPoint(x: w * 0.12, y: mid)
        let tilt: Double = (up - 0.5) * 2.0
        for i in 0..<7 {
            let k = Double(i) - 3.0
            let endY: Double = mid - tilt * h * 0.2 + k * h * 0.075 + sin(t * 1.3 + Double(i)) * h * 0.02
            let c1 = CGPoint(x: w * 0.4, y: mid + sin(t * 0.9 + Double(i) * 0.8) * h * 0.06)
            let c2 = CGPoint(x: w * 0.65, y: (mid + endY) / 2.0 + cos(t * 1.1 + Double(i)) * h * 0.05)
            var p = Path()
            p.move(to: start)
            p.addCurve(to: CGPoint(x: w * 0.88, y: endY), control1: c1, control2: c2)
            let main = i == 3
            ctx.stroke(p, with: .color(Theme.ink), style: StrokeStyle(lineWidth: main ? 8 : 5, lineCap: .round))
            ctx.stroke(p, with: .color(main ? Color.white : Theme.lime), style: StrokeStyle(lineWidth: main ? 4 : 2, lineCap: .round))
        }
        let dot = Path(ellipseIn: CGRect(x: start.x - 8, y: start.y - 8, width: 16, height: 16))
        ctx.fill(dot, with: .color(.white))
        ctx.stroke(dot, with: .color(Theme.ink), lineWidth: 3.5)
    }
}

/// An opening range with one candle breaking out of it.
struct RangeArt: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            Canvas { context, size in
                RangeArt.draw(&context, size: size, time: t)
            }
        }
        .accessibilityHidden(true)
    }

    static func draw(_ ctx: inout GraphicsContext, size: CGSize, time t: Double) {
        let w = Double(size.width)
        let h = Double(size.height)
        let box = CGRect(x: w * 0.1, y: h * 0.42, width: w * 0.52, height: h * 0.3)
        let boxPath = Path(roundedRect: box, cornerRadius: 12)
        ctx.fill(boxPath, with: .color(Color.white.opacity(0.22)))
        ctx.stroke(boxPath, with: .color(Theme.ink), style: StrokeStyle(lineWidth: 3, lineCap: .round, dash: [8, 6]))
        let heights: [Double] = [0.55, 0.75, 0.45, 0.65]
        for i in 0..<heights.count {
            let cx: Double = Double(box.minX) + (Double(i) + 0.5) * Double(box.width) / 4.0
            let ch: Double = Double(box.height) * 0.62 * heights[i]
            let cy: Double = Double(box.midY) - ch / 2.0 + (i % 2 == 0 ? 3.0 : -3.0)
            candle(&ctx, x: cx, top: cy, height: ch, fill: i % 2 == 0 ? Color.white : Theme.pink)
        }
        let grow: Double = 0.75 + 0.25 * sin(t * 2.0)
        let bh: Double = h * 0.42 * grow
        let bx: Double = w * 0.76
        let bottom: Double = Double(box.maxY) - 8.0
        candle(&ctx, x: bx, top: bottom - bh, height: bh, fill: Theme.lime)
        var arrow = Path()
        let tip = CGPoint(x: bx + 22.0, y: bottom - bh - 4.0)
        arrow.move(to: CGPoint(x: tip.x, y: tip.y + 22))
        arrow.addLine(to: tip)
        arrow.move(to: CGPoint(x: tip.x - 7, y: tip.y + 7))
        arrow.addLine(to: tip)
        arrow.addLine(to: CGPoint(x: tip.x + 7, y: tip.y + 7))
        ctx.stroke(arrow, with: .color(Theme.ink), style: StrokeStyle(lineWidth: 4, lineCap: .round, lineJoin: .round))
    }

    private static func candle(_ ctx: inout GraphicsContext, x: Double, top: Double, height: Double, fill: Color) {
        var wick = Path()
        wick.move(to: CGPoint(x: x, y: top - 7.0))
        wick.addLine(to: CGPoint(x: x, y: top + height + 7.0))
        ctx.stroke(wick, with: .color(Theme.ink), style: StrokeStyle(lineWidth: 3, lineCap: .round))
        let rect = Path(roundedRect: CGRect(x: x - 7.0, y: top, width: 14.0, height: max(height, 6.0)), cornerRadius: 4)
        ctx.fill(rect, with: .color(fill))
        ctx.stroke(rect, with: .color(Theme.ink), lineWidth: 3)
    }
}

/// How much of today's loss limit is used.
struct GaugeArt: View {
    let fraction: Double
    let label: String

    var body: some View {
        let used = CGFloat(min(max(fraction, 0.0), 1.0)) * 0.75
        ZStack {
            Circle().trim(from: 0.0, to: 0.75)
                .stroke(Theme.ink, style: StrokeStyle(lineWidth: 20, lineCap: .round))
                .rotationEffect(.degrees(135))
            Circle().trim(from: 0.0, to: max(used, 0.001))
                .stroke(fraction >= 0.8 ? Theme.coral : Color.white, style: StrokeStyle(lineWidth: 11, lineCap: .round))
                .rotationEffect(.degrees(135))
                .animation(.spring(response: 0.6, dampingFraction: 0.7), value: used)
            Text(label)
                .font(Theme.display(22))
                .foregroundStyle(Theme.ink)
        }
        .frame(width: 112, height: 112)
        .accessibilityElement()
        .accessibilityLabel("Today's loss limit used: \(label)")
    }
}

/// A white sticker with a thick outline, slightly tilted.
struct Sticker: View {
    let big: String
    let small: String
    var tilt: Double = -4

    var body: some View {
        VStack(spacing: 2) {
            Text(big).font(Theme.display(28)).foregroundStyle(Theme.ink).minimumScaleFactor(0.6).lineLimit(1)
            Text(small).font(Theme.title(12)).foregroundStyle(Theme.ink.opacity(0.7)).multilineTextAlignment(.center)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.white))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Theme.ink, lineWidth: 3.5))
        .rotationEffect(.degrees(tilt))
    }
}

/// A short line from the mascot.
struct SpeechBubble: View {
    let text: String

    var body: some View {
        Text(text)
            .font(Theme.title(13))
            .foregroundStyle(Theme.ink)
            .multilineTextAlignment(.leading)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.white))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.ink, lineWidth: 3))
    }
}

// MARK: - building blocks

/// A dark header band with a caption and a bold title, over a colour area with art.
struct FeatureCard<Art: View>: View {
    let caption: String
    let title: String
    let base: Color
    let blob: Color
    var seed: Double
    let art: Art
    @State private var hover = false

    init(caption: String, title: String, base: Color, blob: Color, seed: Double = 0, @ViewBuilder art: () -> Art) {
        self.caption = caption
        self.title = title
        self.base = base
        self.blob = blob
        self.seed = seed
        self.art = art()
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(caption.uppercased())
                    .font(Theme.title(11))
                    .tracking(1.2)
                    .foregroundStyle(Theme.dim)
                Text(title)
                    .font(Theme.title(19))
                    .foregroundStyle(Theme.text)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, minHeight: 64, alignment: .topLeading)
            .padding(16)
            .background(Theme.card)
            ZStack {
                BlobField(base: base, blob: blob, seed: seed)
                art.padding(14)
            }
            .frame(height: 170)
        }
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous))
        .scaleEffect(hover ? 1.015 : 1)
        .animation(.spring(response: 0.3, dampingFraction: 0.7), value: hover)
        .onHover { h in hover = h }
        .contentShape(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous))
    }
}

/// White capsule buttons, like stickers you can press.
struct CapsuleButtonStyle: ButtonStyle {
    enum Kind { case white, ghost, pink }
    var kind: Kind = .white

    func makeBody(configuration: Configuration) -> some View {
        CapsuleButtonBody(configuration: configuration, kind: kind)
    }
}

struct CapsuleButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let kind: CapsuleButtonStyle.Kind
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        configuration.label
            .font(Theme.title(14))
            .lineLimit(1)
            .padding(.horizontal, 18)
            .padding(.vertical, 9)
            .foregroundStyle(foreground)
            .background(Capsule().fill(fill))
            .overlay(Capsule().stroke(Color.white.opacity(kind == .ghost ? 0.28 : 0), lineWidth: 1.5))
            .contentShape(Capsule())
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.spring(response: 0.22, dampingFraction: 0.6), value: configuration.isPressed)
            .opacity(isEnabled ? 1 : 0.4)
    }

    private var foreground: Color {
        switch kind {
        case .white: return Theme.ink
        case .ghost: return Theme.text
        case .pink: return Color.white
        }
    }

    private var fill: Color {
        switch kind {
        case .white: return Color.white
        case .ghost: return Color.white.opacity(0.06)
        case .pink: return Theme.pink
        }
    }
}

/// A rounded choice, white when picked.
struct Chip: View {
    let text: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(text)
                .font(Theme.title(13))
                .lineLimit(1)
                .padding(.horizontal, 13)
                .padding(.vertical, 7)
                .foregroundStyle(selected ? Theme.ink : Theme.text)
                .background(Capsule().fill(selected ? Color.white : Theme.card))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .animation(.easeOut(duration: 0.15), value: selected)
    }
}

/// A coin with the currency's symbol, outlined like the rest of the art.
struct CoinBadge: View {
    let currency: String
    var size: CGFloat = 32

    var body: some View {
        ZStack {
            Circle().fill(CoinBadge.color(currency))
            Circle().stroke(Theme.ink, lineWidth: 2.5)
            Text(CoinBadge.symbol(currency))
                .font(.system(size: size * 0.46, weight: .heavy, design: .rounded))
                .foregroundStyle(Theme.ink)
        }
        .frame(width: size, height: size)
    }

    static func symbol(_ c: String) -> String {
        switch c {
        case "EUR": return "€"
        case "GBP": return "£"
        case "JPY": return "¥"
        case "CHF": return "Fr"
        case "XAU": return "Au"
        case "AUD": return "A$"
        case "CAD": return "C$"
        case "NZD": return "N$"
        default: return "$"
        }
    }

    static func color(_ c: String) -> Color {
        switch c {
        case "EUR": return Theme.blue
        case "GBP": return Theme.purple
        case "JPY": return Theme.coral
        case "XAU": return Theme.gold
        case "USD": return Theme.lime
        case "CHF": return Theme.bad
        default: return Theme.pink
        }
    }
}

/// Two overlapping coins for a pair.
struct PairBadge: View {
    let pair: String
    var size: CGFloat = 32

    var body: some View {
        ZStack(alignment: .topLeading) {
            CoinBadge(currency: String(pair.suffix(3)), size: size).offset(x: size * 0.55, y: size * 0.25)
            CoinBadge(currency: String(pair.prefix(3)), size: size)
        }
        .frame(width: size * 1.55, height: size * 1.25, alignment: .topLeading)
        .accessibilityHidden(true)
    }
}

/// One market in the list: coins, name, live price.
struct PairRow: View {
    let pair: String
    let price: Double?
    let status: String?
    let selected: Bool
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                PairBadge(pair: pair)
                VStack(alignment: .leading, spacing: 2) {
                    Text(Instrument.display(pair)).font(Theme.title(16)).foregroundStyle(Theme.text)
                    Text(Instrument.name(pair)).font(Theme.body(12)).foregroundStyle(Theme.dim)
                }
                Spacer(minLength: 8)
                if let s = status {
                    Pill(text: s, color: statusColor(s))
                }
                Text(Instrument.price(price, pair))
                    .font(Theme.number(15))
                    .foregroundStyle(Theme.text)
                    .monospacedDigit()
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Theme.faint)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(hover || selected ? Theme.cardHigh : Theme.card))
            .contentShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { h in hover = h }
    }
}

/// A dark rounded panel with a small heading.
struct Panel<Content: View>: View {
    let title: String
    let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !title.isEmpty {
                Text(title).font(Theme.title(16)).foregroundStyle(Theme.text)
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.card))
    }
}
