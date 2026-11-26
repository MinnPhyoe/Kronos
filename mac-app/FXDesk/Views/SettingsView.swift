import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = AppSettings()
    @State private var oandaToken = ""
    @State private var phonePassword = ""
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
            Section("Use on my phone") {
                Toggle("Let my phone open FX Desk on my home Wi‑Fi", isOn: $draft.phoneAccess)
                if draft.phoneAccess {
                    SecureField("Password for the phone", text: $phonePassword,
                                prompt: Text(model.hasPhonePassword ? "saved (type to replace)" : "8 characters or more"))
                    if let problem = phoneProblem {
                        Text(problem).font(.caption).foregroundStyle(Theme.coral)
                    }
                    if model.settings.phoneAccess && model.hasPhonePassword && model.engineState == .running {
                        PhoneAccessCard(addresses: model.phoneAddresses)
                    }
                    Text("On your iPhone: open the address in Safari, log in with this password, then tap Share → Add to Home Screen. Your Mac must be on with FX Desk open. If macOS asks whether Python may accept incoming connections, choose Allow.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Away from home: install the free Tailscale app on this Mac and on your phone and sign both in. A Tailscale address (100.x.x.x) then appears here. Don't use this on public Wi‑Fi without Tailscale: the address is not encrypted.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("Off: only this Mac can open the engine.").font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Live prices") {
                Picker("Price source", selection: $draft.provider) {
                    Text("OANDA practice account (recommended: real broker prices, free)").tag("oanda")
                    Text("Yahoo Finance (no account, but often blocked and delayed)").tag("yahoo")
                }
                if draft.provider == "oanda" {
                    TextField("OANDA account ID", text: $draft.oandaAccount, prompt: Text("101-001-1234567-001"))
                    SecureField("OANDA token", text: $oandaToken, prompt: Text(model.hasOandaToken ? "saved (type to replace)" : "paste your token"))
                    Text("The token goes only into the engine's private .env file on this Mac.").font(.caption).foregroundStyle(.secondary)
                    Text("Get both from a free OANDA demo account: log in, open Manage API Access, and press Generate. The account ID looks like 101-004-1234567-001.")
                        .font(.caption).foregroundStyle(.secondary)
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
                Toggle("Kronos must agree before a signal shows", isOn: $draft.kronosMustAgree)
                Text("Off (recommended for now): Kronos is advice only. Replayed against recent EUR/USD, it was less accurate than assuming no change, and it pulled its forecasts toward the recent average price. Signals are decided by the Time Machine and both risk checks.")
                    .font(.caption).foregroundStyle(.secondary)
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
                Picker("Alert me for", selection: $draft.alertMinGrade) {
                    Text("A+ setups only").tag("A+")
                    Text("A and A+").tag("A")
                    Text("Every signal that passes (B and up)").tag("B")
                }
                Text("A+ setups are rare by design: on many days there are none. A grade is how strong the evidence is, not a promise.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("Keep this Mac awake while FX Desk runs", isOn: $draft.keepAwake)
                Text("Alerts need the Mac on and FX Desk running. Closing the window keeps it running in the menu bar; the screen can still turn off.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Notifications on my phone") {
                HStack {
                    TextField("ntfy topic", text: $draft.ntfyTopic, prompt: Text("off"))
                    Button("Make a private topic") { draft.ntfyTopic = "fxdesk-" + randomTopic() }
                    Button("Send a test") { Task { await model.sendTestPush(topic: draft.ntfyTopic) } }
                        .disabled(draft.ntfyTopic.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                Text("1. Install the free ntfy app on your iPhone. 2. Press Make a private topic, then Save. 3. In ntfy, tap + and subscribe to the same topic. 4. Press Send a test. Alerts then reach your phone even away from home, as long as this Mac is on.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Alerts pass through ntfy.sh's free server. Anyone who knows the topic name can read them, so keep it long and private. They hold the pair, side and levels, never your password or account.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("Save") {
                    model.apply(draft, oandaToken: oandaToken.isEmpty ? nil : oandaToken,
                                phonePassword: phonePassword.isEmpty ? nil : phonePassword)
                    oandaToken = ""
                    phonePassword = ""
                }
                .buttonStyle(CapsuleButtonStyle())
                .keyboardShortcut("s", modifiers: .command)
                .disabled((draft == model.settings && oandaToken.isEmpty && phonePassword.isEmpty) || phoneProblem != nil)
            }
        }
        .formStyle(.grouped)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .tint(Theme.accent)
        .navigationTitle("Settings")
        .onAppear {
            if !loaded { draft = model.settings; loaded = true }
        }
    }

    /// What stops "Use on my phone" from being saved, if anything.
    private var phoneProblem: String? {
        guard draft.phoneAccess else { return nil }
        if phonePassword.isEmpty {
            return model.hasPhonePassword ? nil : "Choose a password for the phone, then press Save."
        }
        return AppSettings.phonePasswordProblem(phonePassword)
    }

    private func randomTopic() -> String {
        (0..<16).map { _ in String("abcdefghijkmnpqrstuvwxyz23456789".randomElement()!) }.joined()
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

/// The address to open on the phone, as text and as a QR code to scan with the camera.
struct PhoneAccessCard: View {
    let addresses: [String]

    var body: some View {
        if let first = addresses.first {
            HStack(alignment: .top, spacing: 16) {
                if let qr = QRCode.image(for: first) {
                    Image(nsImage: qr)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: 132, height: 132)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white))
                        .accessibilityLabel("QR code for \(first)")
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Scan with your iPhone camera, or type one of these in Safari:")
                        .font(Theme.title(13))
                    ForEach(addresses, id: \.self) { a in
                        Text(a).font(Theme.number(13)).textSelection(.enabled)
                    }
                    Button("Copy the first address") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(first, forType: .string)
                    }
                    .buttonStyle(CapsuleButtonStyle(kind: .ghost))
                }
            }
            .padding(.vertical, 4)
        } else {
            Text("This Mac isn't on a network right now, so your phone can't reach it.")
                .font(.caption).foregroundStyle(Theme.coral)
        }
    }
}
