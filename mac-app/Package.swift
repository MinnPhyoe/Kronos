// swift-tools-version:5.9
// For `swift test` (the logic tests) and `swift run`. To build the app, open FXDesk.xcodeproj in Xcode.
import PackageDescription

let package = Package(
    name: "FXDesk",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "FXDesk",
            path: "FXDesk",
            exclude: ["Assets.xcassets", "Chart/LIGHTWEIGHT-CHARTS-LICENSE"],
            resources: [
                .copy("Chart/chart.html"),
                .copy("Chart/lightweight-charts.standalone.production.js"),
            ]
        ),
        .testTarget(name: "FXDeskTests", dependencies: ["FXDesk"], path: "Tests/FXDeskTests"),
    ]
)
