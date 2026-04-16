import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppRuntime.shared.shutdown()
    }
}

@main
struct JobAppsNativeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var runtime = AppRuntime.shared

    var body: some Scene {
        WindowGroup("AI Job Agents") {
            ContentView()
                .environmentObject(runtime)
                .frame(minWidth: 1220, minHeight: 860)
                .onAppear {
                    runtime.startIfNeeded()
                }
        }
        .windowStyle(.titleBar)
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("App") {
                Button("Open Dashboard") {
                    runtime.openDashboard()
                }
                .keyboardShortcut("d", modifiers: [.command, .shift])

                Button("Open Logs Folder") {
                    runtime.openLogsFolder()
                }

                Divider()

                Button("Restart Backend") {
                    runtime.restart()
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var runtime: AppRuntime

    var body: some View {
        Group {
            switch runtime.phase {
            case .idle, .launching:
                LaunchingView(
                    statusMessage: runtime.statusMessage,
                    detailMessage: runtime.detailMessage,
                    runtimeModeDescription: runtime.runtimeModeDescription
                )
            case .ready:
                if let url = runtime.uiURL {
                    WebView(url: url)
                } else {
                    LaunchingView(
                        statusMessage: runtime.statusMessage,
                        detailMessage: runtime.detailMessage,
                        runtimeModeDescription: runtime.runtimeModeDescription
                    )
                }
            case .failed:
                FailureView(
                    statusMessage: runtime.statusMessage,
                    detailMessage: runtime.detailMessage,
                    runtimeModeDescription: runtime.runtimeModeDescription,
                    onRetry: runtime.restart,
                    onOpenLogs: runtime.openLogsFolder,
                    onQuit: {
                        NSApplication.shared.terminate(nil)
                    }
                )
            }
        }
    }
}

struct LaunchingView: View {
    let statusMessage: String
    let detailMessage: String
    let runtimeModeDescription: String

    var body: some View {
        VStack(spacing: 18) {
            ProgressView()
                .controlSize(.large)
            Text(statusMessage)
                .font(.title3.weight(.semibold))
            if !runtimeModeDescription.isEmpty {
                Text(runtimeModeDescription)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            if !detailMessage.isEmpty {
                Text(detailMessage)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 640)
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

struct FailureView: View {
    let statusMessage: String
    let detailMessage: String
    let runtimeModeDescription: String
    let onRetry: () -> Void
    let onOpenLogs: () -> Void
    let onQuit: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 40))
                .foregroundStyle(.orange)
            Text(statusMessage)
                .font(.title2.weight(.semibold))
            if !runtimeModeDescription.isEmpty {
                Text(runtimeModeDescription)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Text(detailMessage)
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: 760, alignment: .leading)
                .textSelection(.enabled)
            HStack(spacing: 12) {
                Button("Retry", action: onRetry)
                    .keyboardShortcut(.defaultAction)
                Button("Open Logs Folder", action: onOpenLogs)
                Button("Quit", role: .cancel, action: onQuit)
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
