import Foundation

/// The engine's live WebSocket (/stream): ticks, closed candles, forecasts, signals, alerts.
/// Reconnects by itself, waiting a little longer each time (up to 30 seconds).
final class LiveStream {
    private var task: URLSessionWebSocketTask?
    private var loop: Task<Void, Never>?
    private let session = URLSession(configuration: .default)

    func start(url: URL, onMessage: @escaping (StreamMessage) -> Void, onState: @escaping (Bool) -> Void) {
        stop()
        loop = Task { [weak self] in
            var delay: UInt64 = 1
            while !Task.isCancelled {
                guard let self else { return }
                let task = self.session.webSocketTask(with: url)
                self.task = task
                task.resume()
                var connected = false
                while !Task.isCancelled {
                    do {
                        let message = try await task.receive()
                        if !connected {
                            connected = true
                            delay = 1
                            await MainActor.run { onState(true) }
                        }
                        var data: Data?
                        switch message {
                        case .string(let text): data = text.data(using: .utf8)
                        case .data(let d): data = d
                        @unknown default: data = nil
                        }
                        if let d = data, let msg = try? EngineJSON.decoder.decode(StreamMessage.self, from: d) {
                            await MainActor.run { onMessage(msg) }
                        }
                    } catch {
                        break
                    }
                }
                task.cancel(with: .goingAway, reason: nil)
                await MainActor.run { onState(false) }
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                delay = min(30, delay * 2)
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }
}
