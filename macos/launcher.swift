// Amazing Grok.app: gives the menu bar helper its own app identity, so macOS
// asks for Input Monitoring / Accessibility / Microphone for "Amazing Grok"
// rather than for a terminal. build.sh records where menubar.py lives in
// Info.plist (AGMenubarScript). Log: ~/Library/Logs/amazing-grok.log
import Foundation

let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser.path
guard let script = Bundle.main.object(forInfoDictionaryKey: "AGMenubarScript") as? String else {
    fatalError("AGMenubarScript missing from Info.plist; rebuild with build.sh")
}
let path = ["/opt/homebrew/bin", "/usr/local/bin", home + "/.local/bin", home + "/.cargo/bin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
guard let uv = path.map({ $0 + "/uv" }).first(where: { fm.isExecutableFile(atPath: $0) }) else {
    fatalError("uv not found; install it with: brew install uv")
}

let logPath = home + "/Library/Logs/amazing-grok.log"
fm.createFile(atPath: logPath, contents: nil)
let log = FileHandle(forWritingAtPath: logPath)!

let p = Process()
p.executableURL = URL(fileURLWithPath: uv)
p.arguments = ["run", "--quiet", "--script", script] + CommandLine.arguments.dropFirst()
var env = ProcessInfo.processInfo.environment
env["PATH"] = path.joined(separator: ":")  // sox's rec/play live in Homebrew
env["PYTHONUNBUFFERED"] = "1"
p.environment = env
p.standardOutput = log
p.standardError = log
signal(SIGTERM) { _ in exit(0) }
try! p.run()
p.waitUntilExit()
exit(p.terminationStatus)
