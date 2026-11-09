import AppKit
import SwiftData
import SwiftUI

/// FX Desk: a private Mac app for forex analysis. Live chart, Kronos forecasts, the Day Trader
/// opening-range setups, two risk engines, the best result, and your trade records.
/// It runs everything on this Mac and never places a trade.
@main
struct FXDeskApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        WindowGroup("FX Desk") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 980, minHeight: 640)
                .preferredColorScheme(.dark)
        }
        .defaultSize(width: 1280, height: 820)
        .modelContainer(for: TradeRecord.self)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            AppModel.shared.stopEngine()
        }
    }
}
