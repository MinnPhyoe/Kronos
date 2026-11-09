import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = AppSettings()
    @State private var oandaToken = ""
    @State private var loaded = false

    var body: some View {
        Form {
            Section("Engine (runs on this Mac only)") {
                HStack {
                    TextField("Engine folder (forex-app)", text: $draft.engineFolder)
                    Button("Choose…", action: chooseFolder)
                }
                Text("The forex-app folder from the Kronos download. It holds scripts/mac-engine.sh.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Text("Status: \(model.engineState.label)")
                    Spacer()
                    Button("Start") { model.startEngine() }.disabled(model.engineState == .running)
                    Button("Restart") { model.restartEngine() }
                    Button("Stop") { model.stopEngine() }
                }
                DisclosureGroup("Engine log") {
                    ScrollView {
                        Text(model.engineLog.suffix(200).joined(separator: "\n"))
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 160)
                    Button("Open the full log") { NSWorkspace.shared.open(EngineProcess.logURL) }
                }
            }
            Section("Live prices") {
                Picker("Price source", selection: $draft.provider) {
                    Text("Yahoo Finance (free, no account; can lag a few seconds)").tag("yahoo")
                    Text("OANDA practice account (real broker prices)").tag("oanda")
                }
                if draft.provider == "oanda" {
                    TextField("OANDA account ID", text: $draft.oandaAccount, prompt: Text("101-001-1234567-001"))
                    SecureField("OANDA token", text: $oandaToken, prompt: Text(model.hasOandaToken ? "saved (type to replace)" : "paste your token"))
                    Text("The token goes only into the engine's private .env file on this Mac.").font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Pairs") {
                ForEach(AppSettings.allPairs, id: \.self) { p in
                    Toggle(Instrument.display(p), isOn: Binding(
                        get: { draft.pairs.contains(p) },
                        set: { on in
                            if on { if !draft.pairs.contains(p) { draft.pairs.append(p) } } else if draft.pairs.count > 1 { draft.pairs.removeAll { $0 == p } }
                        }))
                }
            }
            Section("Day Trader sessions") {
                ForEach(AppSettings.allSessions, id: \.0) { key, label in
                    Toggle(label, isOn: Binding(
                        get: { draft.sessions.contains(key) },
                        set: { on in
                            if on { if !draft.sessions.contains(key) { draft.sessions.append(key) } } else { draft.sessions.removeAll { $0 == key } }
                        }))
                }
            }
            Section("Kronos") {
                Toggle("Use Kronos (first start downloads about 1 GB)", isOn: $draft.useKronos)
                Text("Off: a simple statistical stand-in is used, and the app says so.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Account and limits (both risk engines use these)") {
                TextField("Account size", value: $draft.limits.balance, format: .number)
                Picker("Account currency", selection: $draft.limits.currency) {
                    ForEach(["GBP", "USD", "EUR"], id: \.self) { Text($0).tag($0) }
                }
                TextField("Risk per trade (%)", value: $draft.limits.riskPerTradePct, format: .number)
                TextField("Daily loss limit (%)", value: $draft.limits.dailyLossPct, format: .number)
                TextField("Weekly loss limit (%)", value: $draft.limits.weeklyLossPct, format: .number)
                Stepper("Max trades per day: \(draft.limits.maxTradesPerDay)", value: $draft.limits.maxTradesPerDay, in: 1...20)
                TextField("Minimum reward:risk", value: $draft.limits.minRewardRisk, format: .number)
                Text(String(format: "That is %.2f %@ a trade, %.2f a day, %.2f a week.", draft.limits.maxLossPerTrade,
                            draft.limits.currency, draft.limits.dailyLimit, draft.limits.weeklyLimit))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Alerts") {
                Toggle("Mac notifications for setups, triggers, TP and SL, and news", isOn: $draft.notifications)
            }
            HStack {
                Spacer()
                Button("Save") {
                    model.apply(draft, oandaToken: oandaToken.isEmpty ? nil : oandaToken)
                    oandaToken = ""
                }
                .buttonStyle(CapsuleButtonStyle())
                .keyboardShortcut("s", modifiers: .command)
                .disabled(draft == model.settings && oandaToken.isEmpty)
            }
        }
        .formStyle(.grouped)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .tint(Theme.pink)
        .navigationTitle("Settings")
        .onAppear {
            if !loaded { draft = model.settings; loaded = true }
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use this folder"
        panel.message = "Choose the forex-app folder (it holds scripts/mac-engine.sh)"
        if panel.runModal() == .OK, let url = panel.url { draft.engineFolder = url.path }
    }
}
