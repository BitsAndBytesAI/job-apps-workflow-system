// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JobAppsNative",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .executable(
            name: "JobAppsNative",
            targets: ["JobAppsNative"]
        ),
    ],
    targets: [
        .executableTarget(
            name: "JobAppsNative",
            path: "Sources/JobAppsNative"
        ),
    ]
)
