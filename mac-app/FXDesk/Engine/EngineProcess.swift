import Foundation

/// Starts and stops the Python engine (Kronos + Day Trader + live prices) in the background.
/// It runs `forex-app/scripts/mac-engine.sh`, which sets everything up on the first start.
/// Its output goes to ~/Library/Logs/FXDesk/engine.log and lines starting with "FXDESK:" are shown in the app.
final class EngineProcess {
    private(set) var process: Process?
    private var logHandle: FileHandle?
    private var buffer = ""

    var isRunning: Bool { process?.isRunning ?? false }

    static var logURL: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/FXDesk", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("engine.log")
    }

    /// - Parameters:
    ///   - folder: the forex-app folder (it holds scripts/mac-engine.sh)
    ///   - onLine: every output line, on the main thread
    ///   - onExit: the exit code, on the main thread
    func start(folder: URL, port: Int, onLine: @escaping (String) -> Void, onExit: @escaping (Int32) -> Void) throws {
        if isRunning { return }
        let script = folder.appendingPathComponent("scripts/mac-engine.sh")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw NSError(domain: "FXDesk", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "No engine in \(folder.path). In Settings, choose the forex-app folder (it contains scripts/mac-engine.sh)."])
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script.path]
        p.currentDirectoryURL = folder
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["FOREX_PORT"] = String(port)
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        FileManager.default.createFile(atPath: EngineProcess.logURL.path, contents: nil)
        logHandle = try? FileHandle(forWritingTo: EngineProcess.logURL)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let self else { return }
            self.logHandle?.write(data)
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                self.buffer += text
                while let nl = self.buffer.firstIndex(of: "\n") {
                    let line = String(self.buffer[..<nl])
                    self.buffer.removeSubrange(...nl)
                    onLine(line)
                }
            }
        }
        p.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async { onExit(proc.terminationStatus) }
        }
        try p.run()
        process = p
    }

    func stop() {
        guard let p = process, p.isRunning else { return }
        p.terminate()                      // SIGTERM: uvicorn and the services shut down
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
    }
}
