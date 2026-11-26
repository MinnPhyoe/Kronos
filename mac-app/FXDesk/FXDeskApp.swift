import AppKit
import SwiftData
import SwiftUI
import UserNotifications

/// FX Desk: a private Mac app for forex analysis. Live chart, Kronos forecasts, the Day Trader
/// opening-range setups, two risk engines, the best result, and your trade records.
/// It runs everything on this Mac and never places a trade.
@main
struct FXDeskApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        WindowGroup("FX Desk", id: "main") {
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

        // Closing the window keeps FX Desk (and its alerts) running; this menu brings it back or quits.
        MenuBarExtra("FX Desk", systemImage: "chart.line.uptrend.xyaxis") {
            MenuBarContent().environmentObject(model)
        }
    }
}

struct MenuBarContent: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(model.engineState == .running ? (model.dataIsStale ? "Prices are not updating" : "Live: watching for setups")
             : model.engineState.label)
        Divider()
        if model.alerts.isEmpty {
            Text("No alerts yet")
        }
        ForEach(model.alerts.prefix(5)) { a in
            Text(a.title)
        }
        Divider()
        Button("Open FX Desk") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("Quit FX Desk") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = NotificationDelegate.shared
        }
    }

    /// Keep running with no window, so the engine keeps watching and alerts keep coming.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            AppModel.shared.stopEngine()
        }
    }
}

/// Shows alert banners even while FX Desk is the front app, and brings FX Desk forward when one is clicked.
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationDelegate()

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        Task { @MainActor in
            NSApp.activate(ignoringOtherApps: true)
            for w in NSApp.windows where w.canBecomeMain {
                w.makeKeyAndOrderFront(nil)
            }
        }
        completionHandler()
    }
}
