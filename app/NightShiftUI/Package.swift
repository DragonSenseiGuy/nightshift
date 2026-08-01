// swift-tools-version: 5.9
//
// The SwiftUI client (Phase 17). SwiftPM rather than an Xcode project on purpose: the
// whole app is source, so it builds from a terminal with the toolchain that ships with
// macOS, and `build.sh` wraps the executable in the .app bundle a menu bar agent needs.

import PackageDescription

let package = Package(
    name: "NightShiftUI",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "NightShiftUI",
            path: "Sources/NightShiftUI"
        )
    ]
)
