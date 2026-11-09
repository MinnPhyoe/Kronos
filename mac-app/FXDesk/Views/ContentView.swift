import AppKit
import SwiftUI

enum Screen: String, CaseIterable, Identifiable {
    case home, live, best, records, alerts, settings
    var id: Screen { self }

    var title: String {
        switch self {
        case .home: return "Home"
        case .live: return "Live chart"
        case .best: return "Best result"
        case .records: return "Records"
        case .alerts: return "Alerts"
        case .settings: return "Settings"
        }
    }

    var icon: String {
        switch self {
        case .home: return "house"
        case .live: return "chart.xyaxis.line"
        case .best: return "star"
        case .records: return "book.closed"
        case .alerts: return "bell"
        case .settings: return "gearshape"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var screen: Screen? = .home

    var body: some View {
        NavigationSplitView {
            List(Screen.allCases, id: \.self, selection: $screen) { s in
                Label(s.title, systemImage: s.icon)
                    .font(Theme.title(14))
                    .padding(.vertical, 3)
            }
            .scrollContentBackground(.hidden)
            .background(Theme.bg)
            .navigationSplitViewColumnWidth(min: 180, ideal: 200)
            .safeAreaInset(edge: .top) { SidebarHeader().padding(.horizontal, 14).padding(.top, 6) }
            .safeAreaInset(edge: .bottom) { EngineBadge().padding(10) }
        } detail: {
            detail
                .background(Theme.bg)
        }
        .tint(Theme.pink)
        .background(Theme.bg)
        .onAppear { model.boot() }
        .task { await tourIfAsked() }
        .overlay(alignment: .bottom) {
            if let m = model.message {
                Text(m)
                    .font(Theme.title(13))
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Capsule().fill(Color.white))
                    .overlay(Capsule().stroke(Theme.ink, lineWidth: 2))
                    .shadow(color: Color.black.opacity(0.4), radius: 12, y: 4)
                    .padding(.bottom, 18)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .onTapGesture { model.message = nil }
                    .task(id: m) {
                        try? await Task.sleep(nanoseconds: 5_000_000_000)
                        if model.message == m { model.message = nil }
                    }
            }
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.8), value: model.message)
    }

    /// For automated runs only (never in normal use): with FXDESK_TOUR_DIR set, wait for the engine,
    /// open each screen in turn, write "<screen>.ready" there, wait for "<screen>.done", then quit.
    @MainActor private func tourIfAsked() async {
        guard let dir = ProcessInfo.processInfo.environment["FXDESK_TOUR_DIR"], !dir.isEmpty else { return }
        let files = FileManager.default
        var waited = 0
        while waited < 1800 {
            if case .failed = model.engineState { break }
            if model.engineState == .running { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            waited += 1
        }
        let settle = UInt64(ProcessInfo.processInfo.environment["FXDESK_TOUR_WAIT"].flatMap { v in Int(v) } ?? 20)
        try? await Task.sleep(nanoseconds: settle * 1_000_000_000)
        await model.refreshAll()
        if let s = model.activeSignals.first, s.pair != model.pair {
            model.select(pair: s.pair)
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
        for s in Screen.allCases {
            screen = s
            if s == .live, model.engineState == .running {
                await model.analyseNow()
            }
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            files.createFile(atPath: dir + "/" + s.rawValue + ".ready", contents: nil)
            var tries = 0
            while tries < 120, !files.fileExists(atPath: dir + "/" + s.rawValue + ".done") {
                try? await Task.sleep(nanoseconds: 500_000_000)
                tries += 1
            }
        }
        NSApp.terminate(nil)
    }

    @ViewBuilder private var detail: some View {
        switch screen ?? .home {
        case .home: HomeView(go: { s in screen = s })
        case .live: LiveView()
        case .best: BestView()
        case .records: JournalView()
        case .alerts: AlertsView()
        case .settings: SettingsView()
        }
    }
}

struct SidebarHeader: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            CandleBuddy(mood: model.engineState == .running ? .happy : .sleepy).frame(width: 26, height: 36)
            Text("FX Desk").font(Theme.display(20)).foregroundStyle(Theme.pink)
            Spacer(minLength: 0)
        }
        .padding(.bottom, 6)
    }
}

struct EngineBadge: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let color: Color = {
            switch model.engineState {
            case .running: return model.health?.feedUp == false ? Theme.coral : Theme.good
            case .starting: return Theme.gold
            case .failed: return Theme.bad
            case .stopped: return Theme.faint
            }
        }()
        HStack(spacing: 8) {
            Circle().fill(color).frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 1) {
                Text(model.engineState == .running && model.health?.feedUp == false ? "No live prices" : model.engineState.label)
                    .font(Theme.title(12))
                    .foregroundStyle(Theme.text)
                    .lineLimit(2)
                if model.engineState == .running, let p = model.health?.config?.provider {
                    Text("Prices: \(p)\(model.streamConnected ? "" : ", reconnecting")")
                        .font(Theme.body(11))
                        .foregroundStyle(Theme.dim)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.card))
    }
}

struct AlertsView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("Alerts").font(Theme.display(34)).foregroundStyle(Theme.pink)
                if model.alerts.isEmpty {
                    HStack(spacing: 18) {
                        CandleBuddy(mood: .sleepy).frame(width: 70)
                        Text("Alerts appear here when a setup forms, triggers, hits TP or SL, or news is coming. They also show as Mac notifications.")
                            .font(Theme.body(14))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(18)
                    .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.card))
                }
                ForEach(model.alerts) { a in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: "bell.fill")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(Theme.ink)
                            .frame(width: 30, height: 30)
                            .background(Circle().fill(Theme.lime))
                            .overlay(Circle().stroke(Theme.ink, lineWidth: 2))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(a.title).font(Theme.title(15)).foregroundStyle(Theme.text)
                            Text(a.body).font(Theme.body(13)).foregroundStyle(Theme.dim).fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Text(a.time, format: .dateTime.hour().minute().second())
                            .font(Theme.number(12))
                            .foregroundStyle(Theme.faint)
                    }
                    .padding(14)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.card))
                }
            }
            .padding(24)
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .navigationTitle("Alerts")
    }
}
