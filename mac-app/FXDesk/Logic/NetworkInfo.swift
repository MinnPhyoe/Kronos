import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import SystemConfiguration

/// An address your phone can use to reach this Mac.
struct LocalAddress: Identifiable, Equatable {
    let host: String
    let kind: String
    var id: String { host }
}

/// This Mac's addresses on your home network and on Tailscale.
enum NetworkInfo {
    static func addresses() -> [LocalAddress] {
        var result: [LocalAddress] = []
        if let name = SCDynamicStoreCopyLocalHostName(nil) {
            result.append(LocalAddress(host: (name as String) + ".local", kind: "home network"))
        }
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0 else { return result }
        defer { freeifaddrs(list) }
        var cursor = list
        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(bitPattern: entry.pointee.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0, let sa = entry.pointee.ifa_addr,
                  sa.pointee.sa_family == sa_family_t(AF_INET) else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0
            else { continue }
            let ip = host.withUnsafeBufferPointer { b in String(cString: b.baseAddress!) }
            if let kind = kind(of: ip), !result.contains(where: { a in a.host == ip }) {
                result.append(LocalAddress(host: ip, kind: kind))
            }
        }
        return result
    }

    /// Private home-network ranges, and Tailscale's 100.64.0.0/10. Anything else is not offered.
    static func kind(of ip: String) -> String? {
        let p = ip.split(separator: ".").compactMap { part in Int(part) }
        guard p.count == 4 else { return nil }
        if p[0] == 10 || (p[0] == 172 && (16...31).contains(p[1])) || (p[0] == 192 && p[1] == 168) { return "home network" }
        if p[0] == 100 && (64...127).contains(p[1]) { return "Tailscale" }
        return nil
    }
}

enum QRCode {
    /// A sharp QR code for a web address (never for a password).
    static func image(for text: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
        guard let cg = CIContext().createCGImage(scaled, from: scaled.extent) else { return nil }
        return NSImage(cgImage: cg, size: NSSize(width: scaled.extent.width, height: scaled.extent.height))
    }
}
