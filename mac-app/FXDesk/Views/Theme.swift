import SwiftUI

/// The look: a black canvas, heavy rounded type, playful colour cards with slowly morphing blobs,
/// cartoon art with thick black outlines, and white capsule buttons.
enum Theme {
    // Monochrome: near-black surfaces, hairline dividers, grey labels, white numbers.
    // Colour only where it carries meaning: green up / good, red down / bad, amber for warnings.
    static let bg = Color(hex: 0x0E0E0F)
    static let card = Color(hex: 0x151516)
    static let cardHigh = Color(hex: 0x1D1D1F)
    static let ink = Color(hex: 0x0E0E0F)
    static let text = Color(hex: 0xF2F2F3)
    static let dim = Color.white.opacity(0.55)
    static let faint = Color.white.opacity(0.32)
    static let line = Color.white.opacity(0.08)

    static let pink = Color(hex: 0xF2F2F3)        // kept for older call sites: plain white text, no pink anywhere
    static let accent = Color(hex: 0x8E8E93)      // system controls (switches, selection): neutral grey
    static let coral = Color(hex: 0xE8A13A)       // warnings
    static let purple = Color.white.opacity(0.7)
    static let blue = Color(hex: 0x8AB4FF)        // entry / buy
    static let lime = Color.white.opacity(0.85)
    static let gold = Color(hex: 0xE8A13A)
    static let good = Color(hex: 0x3DD68C)
    static let bad = Color(hex: 0xF0616D)

    static func display(_ size: CGFloat) -> Font { .system(size: size * 0.72, weight: .semibold) }
    static func title(_ size: CGFloat) -> Font { .system(size: size * 0.92, weight: .medium) }
    static func body(_ size: CGFloat = 14) -> Font { .system(size: size * 0.95, weight: .regular) }
    static func number(_ size: CGFloat = 14) -> Font { .system(size: size, weight: .medium).monospacedDigit() }
    static func label(_ size: CGFloat = 12) -> Font { .system(size: size, weight: .regular) }

    static let radius: CGFloat = 14
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
        // monochrome: a dark surface with one soft, slowly drifting highlight
        ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Theme.card))
        ctx.fill(blobPath(index: 0, time: t, size: size), with: .color(Color.white.opacity(0.035)))
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
            ctx.stroke(p, with: .color(Color.white.opacity(main ? 0.9 : 0.25)), style: StrokeStyle(lineWidth: main ? 1.8 : 1, lineCap: .round))
        }
        let dot = Path(ellipseIn: CGRect(x: start.x - 3.5, y: start.y - 3.5, width: 7, height: 7))
        ctx.fill(dot, with: .color(.white))
    }
}

// MARK: - building blocks

/// White capsule buttons, like stickers you can press.
struct CapsuleButtonStyle: ButtonStyle {
    enum Kind { case white, ghost, pink }      // pink is kept as an alias of white (primary)
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
            .font(.system(size: 13, weight: .medium))
            .lineLimit(1)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .foregroundStyle(foreground)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Theme.line, lineWidth: kind == .ghost ? 1 : 0))
            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.spring(response: 0.22, dampingFraction: 0.6), value: configuration.isPressed)
            .opacity(isEnabled ? 1 : 0.4)
    }

    private var foreground: Color {
        switch kind {
        case .white: return Theme.ink
        case .ghost: return Theme.text
        case .pink: return Theme.ink
        }
    }

    private var fill: Color {
        switch kind {
        case .white: return Theme.text
        case .ghost: return Theme.cardHigh
        case .pink: return Theme.text
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
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .foregroundStyle(selected ? Theme.ink : Theme.dim)
                .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(selected ? Theme.text : Color.clear))
                .contentShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
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
            Circle().fill(Theme.cardHigh)
            Circle().stroke(Theme.bg, lineWidth: 2)
            Text(CoinBadge.symbol(currency))
                .font(.system(size: size * 0.42, weight: .semibold))
                .foregroundStyle(Theme.text)
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
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(hover || selected ? Theme.cardHigh : Theme.card))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.line, lineWidth: 1))
            .animation(.easeOut(duration: 0.15), value: hover)
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
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
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).stroke(Theme.line, lineWidth: 1))
    }
}


// MARK: - dashboard parts

/// A small icon in a quiet circle: used where an empty state needs a picture.
struct StatusIcon: View {
    let systemName: String
    var size: CGFloat = 34

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: size * 0.42, weight: .regular))
            .foregroundStyle(Theme.dim)
            .frame(width: size, height: size)
            .background(Circle().fill(Theme.cardHigh))
            .accessibilityHidden(true)
    }
}

/// "▲ 2.7% vs …": green up, red down, never colour alone (the arrow says it too).
struct DeltaChip: View {
    let value: Double?
    let suffix: String
    var format: String = "%.2f%%"

    var body: some View {
        HStack(spacing: 4) {
            if let v = value {
                Image(systemName: v >= 0 ? "arrow.up" : "arrow.down")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Theme.bg)
                    .frame(width: 13, height: 13)
                    .background(Circle().fill(v >= 0 ? Theme.good : Theme.bad))
                Text(String(format: format, abs(v))).foregroundStyle(v >= 0 ? Theme.good : Theme.bad)
            }
            Text(suffix).foregroundStyle(Theme.dim)
        }
        .font(Theme.label(11))
    }
}

/// One figure in the top row: grey label, big number, a line under it.
struct StatCell<Footer: View>: View {
    let label: String
    let value: String
    let footer: Footer

    init(_ label: String, value: String, @ViewBuilder footer: () -> Footer) {
        self.label = label
        self.value = value
        self.footer = footer()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(Theme.label(12)).foregroundStyle(Theme.dim)
            Text(value).font(.system(size: 24, weight: .medium).monospacedDigit()).foregroundStyle(Theme.text)
                .lineLimit(1).minimumScaleFactor(0.6)
                .contentTransition(.numericText())                 // digits roll when the value changes
                .animation(.snappy(duration: 0.35), value: value)
            footer
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }
}

/// Bars around an arc, the used share bright: like a dial made of ticks.
struct RadialBarGauge: View, Animatable {
    var fraction: Double
    var ticks = 44

    var animatableData: Double {                               // the ticks light up one by one
        get { fraction }
        set { fraction = newValue }
    }

    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            let r = min(size.width, size.height) / 2
            let start = 140.0, sweep = 260.0
            let lit = Int((min(max(fraction, 0), 1) * Double(ticks)).rounded())
            for i in 0..<ticks {
                let a = (start + sweep * Double(i) / Double(ticks - 1)) * Double.pi / 180
                var p = Path()
                p.move(to: CGPoint(x: c.x + cos(a) * (r * 0.72), y: c.y + sin(a) * (r * 0.72)))
                p.addLine(to: CGPoint(x: c.x + cos(a) * (r * 0.98), y: c.y + sin(a) * (r * 0.98)))
                let on = i < lit
                ctx.stroke(p, with: .color(Color.white.opacity(on ? 0.85 : 0.14)),
                           style: StrokeStyle(lineWidth: 4.5, lineCap: .round))
            }
        }
        .accessibilityHidden(true)
    }
}

/// Thin vertical stripes, the first share bright: a meter that reads at a glance.
struct StripeMeter: View, Animatable {
    var fraction: Double
    var stripes = 64

    var animatableData: Double {
        get { fraction }
        set { fraction = newValue }
    }

    var body: some View {
        Canvas { ctx, size in
            let w = size.width / CGFloat(stripes)
            let lit = Int((min(max(fraction, 0), 1) * Double(stripes)).rounded())
            for i in 0..<stripes {
                let rect = CGRect(x: CGFloat(i) * w, y: 0, width: max(1, w * 0.45), height: size.height)
                ctx.fill(Path(roundedRect: rect, cornerRadius: 1), with: .color(Color.white.opacity(i < lit ? 0.8 : 0.14)))
            }
        }
        .accessibilityHidden(true)
    }
}

/// A bar split into labelled shares (each 0...1), with a legend under it.
struct SegmentBar: View {
    let parts: [(label: String, share: Double, opacity: Double)]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            GeometryReader { geo in
                HStack(spacing: 3) {
                    ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(Color.white.opacity(part.opacity))
                            .frame(width: max(4, (geo.size.width - 3 * CGFloat(parts.count - 1)) * CGFloat(part.share)))
                            .animation(.easeOut(duration: 0.6), value: part.share)
                    }
                }
            }
            .frame(height: 10)
            HStack(spacing: 14) {
                ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                    HStack(spacing: 5) {
                        Circle().fill(Color.white.opacity(part.opacity)).frame(width: 6, height: 6)
                        Text(part.label).font(Theme.label(11)).foregroundStyle(Theme.dim)
                    }
                }
            }
        }
    }
}

/// "Label ........ value" with a hairline under it.
struct KeyValueRow: View {
    let key: String
    let value: String

    var body: some View {
        HStack {
            Text(key).font(Theme.label(13)).foregroundStyle(Theme.dim)
            Spacer()
            Text(value).font(Theme.number(13)).foregroundStyle(Theme.text).lineLimit(1)
        }
        .padding(.vertical, 8)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.line).frame(height: 1) }
    }
}
