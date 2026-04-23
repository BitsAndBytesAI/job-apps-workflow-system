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
        .executable(
            name: "JobAppsSecretHelper",
            targets: ["JobAppsSecretHelper"]
        ),
        .executable(
            name: "JobAppsSchedulerAgent",
            targets: ["JobAppsSchedulerAgent"]
        ),
    ],
    targets: [
        .executableTarget(
            name: "JobAppsNative",
            path: "Sources/JobAppsNative"
        ),
        .executableTarget(
            name: "JobAppsSecretHelper",
            path: "Sources/JobAppsSecretHelper"
        ),
        .executableTarget(
            name: "JobAppsSchedulerAgent",
            path: "Sources/JobAppsSchedulerAgent"
        ),
    ]
)
