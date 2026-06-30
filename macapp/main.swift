// Hermes PR Auto-Review — 일반 Mac 앱(창 우선) + 메뉴바 보조.
// 실행하면 대시보드 창이 바로 열리고, 상단 메뉴바에도 triage 개수가 뜬다.
// 백엔드(파이썬)는 launchd가 별개로 돌리며, 실행 시 죽어있으면 살린다.
import Cocoa
import WebKit

let DASH_URL = "http://127.0.0.1:8788"
let API_URL = "http://127.0.0.1:8788/api/board"
let AGENTS = ["io.hermes.receiver", "io.hermes.dashboard", "io.hermes.tick"]

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var statusItem: NSStatusItem!
    var headerItem: NSMenuItem!
    var window: NSWindow?
    var webView: WKWebView?
    var timer: Timer?

    var appVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "?"
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        ensureBackend()
        buildMainMenu()
        buildStatusItem()
        openDashboard()                 // 일반 앱처럼 실행 시 창을 바로 띄움
        refreshCount()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.refreshCount()
        }
    }

    // 마지막 창을 닫아도 앱은 살아있음(메뉴바 유지). 종료는 Cmd+Q.
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    // Dock 아이콘 다시 클릭 시 창 복귀
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { openDashboard() }
        return true
    }

    // MARK: - Menus
    func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Lookout v\(appVersion)", action: nil, keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "백엔드 재시작", action: #selector(restartBackend), keyEquivalent: "")
        appMenu.addItem(withTitle: "업데이트 확인…", action: #selector(checkForUpdates), keyEquivalent: "u")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Lookout 가리기", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Lookout 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editItem = NSMenuItem(); main.addItem(editItem)
        let editMenu = NSMenu(title: "편집"); editItem.submenu = editMenu
        editMenu.addItem(withTitle: "실행 취소", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "다시 실행", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "잘라내기", action: Selector(("cut:")), keyEquivalent: "x")
        editMenu.addItem(withTitle: "복사", action: Selector(("copy:")), keyEquivalent: "c")
        editMenu.addItem(withTitle: "붙여넣기", action: Selector(("paste:")), keyEquivalent: "v")
        editMenu.addItem(withTitle: "전체 선택", action: Selector(("selectAll:")), keyEquivalent: "a")

        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "윈도우"); winItem.submenu = winMenu
        winMenu.addItem(withTitle: "최소화", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "닫기", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winMenu.addItem(.separator())
        winMenu.addItem(withTitle: "대시보드 열기", action: #selector(openDashboard), keyEquivalent: "0")

        NSApp.mainMenu = main
        NSApp.windowsMenu = winMenu
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "👁 …"
        let menu = NSMenu()
        headerItem = NSMenuItem(title: "Lookout", action: nil, keyEquivalent: "")
        headerItem.isEnabled = false
        menu.addItem(headerItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "대시보드 열기", action: #selector(openDashboard), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "백엔드 재시작", action: #selector(restartBackend), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "업데이트 확인…", action: #selector(checkForUpdates), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: ""))
        statusItem.menu = menu
    }

    // MARK: - Window
    @objc func openDashboard() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable],
                             backing: .buffered, defer: false)
            w.title = "Lookout"
            w.center()
            w.isReleasedWhenClosed = false
            w.minSize = NSSize(width: 720, height: 480)
            let wv = WKWebView(frame: w.contentView!.bounds)
            wv.autoresizingMask = [.width, .height]
            wv.navigationDelegate = self
            wv.uiDelegate = self
            w.contentView?.addSubview(wv)
            wv.load(URLRequest(url: URL(string: DASH_URL)!))
            self.webView = wv
            self.window = w
        } else {
            webView?.reload()
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    @objc func restartBackend() {
        AGENTS.forEach { kickstart($0, force: true) }
    }

    // MARK: - Backend
    func ensureBackend() {
        if !probe() { AGENTS.forEach { kickstart($0, force: false) } }
    }

    func probe() -> Bool {
        guard let url = URL(string: API_URL) else { return false }
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        var req = URLRequest(url: url); req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let h = resp as? HTTPURLResponse, h.statusCode == 200 { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok
    }

    func kickstart(_ label: String, force: Bool) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        let target = "gui/\(getuid())/\(label)"
        p.arguments = force ? ["kickstart", "-k", target] : ["kickstart", target]
        try? p.run()
    }

    // MARK: - WebView delegates
    // 외부 링크(github.com 등)는 기본 브라우저로, 로컬 대시보드는 webview 안에서
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, let host = url.host,
           host != "127.0.0.1", host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // JS alert()/confirm() — WKWebView는 기본으론 안 띄움 → 직접 NSAlert로 연결
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "확인")
        a.runModal(); completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "확인"); a.addButton(withTitle: "취소")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    // MARK: - Menu-bar count
    func refreshCount() {
        guard let url = URL(string: API_URL) else { return }
        var req = URLRequest(url: url); req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var triage = 0, total = 0, reachable = false
            if let data = data,
               let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
                reachable = true
                total = arr.count
                triage = arr.filter { ($0["status"] as? String) == "triage" }.count
            }
            DispatchQueue.main.async {
                if !reachable {
                    self.statusItem.button?.title = "👁 ⚠️"
                    self.headerItem.title = "백엔드 응답 없음 — 재시작 해보세요"
                } else {
                    self.statusItem.button?.title = triage > 0 ? "👁 \(triage)" : "👁"
                    self.headerItem.title = "Triage \(triage) · 전체 \(total)"
                }
            }
        }.resume()
    }

    // MARK: - Updates (메뉴 → 업데이트 확인 → 팝업 승인 → 한방 업데이트)
    var progressWin: NSWindow?

    /// update.sh가 있는 lookout repo 경로. 빌드 시 Info.plist에 기록(LookoutRepoDir),
    /// 못 찾으면 흔한 위치로 폴백.
    func repoDir() -> String? {
        if let p = Bundle.main.object(forInfoDictionaryKey: "LookoutRepoDir") as? String,
           FileManager.default.fileExists(atPath: p + "/update.sh") { return p }
        for c in ["\(NSHomeDirectory())/lookout", "\(NSHomeDirectory())/hermes-pr"] {
            if FileManager.default.fileExists(atPath: c + "/update.sh") { return c }
        }
        return nil
    }

    @objc func checkForUpdates() {
        guard let dir = repoDir() else {
            showAlert("업데이트 위치를 찾을 수 없음",
                      "lookout repo를 찾지 못했습니다. build_app.sh로 앱을 다시 빌드하면 경로가 기록됩니다.")
            return
        }
        runScript(dir: dir, args: ["--check"], timeout: 60) { code, out in
            if code != 0 {
                self.showAlert("업데이트 확인 실패",
                               out.isEmpty ? "git fetch 실패 — 네트워크/인증을 확인하세요." : self.tail(out))
                return
            }
            if out.contains("이미 최신") {
                self.showAlert("최신 버전입니다 ✓", "추가 업데이트가 없습니다.")
                return
            }
            let a = NSAlert()
            a.messageText = "업데이트가 있습니다"
            a.informativeText = self.commitSummary(out)
                + "\n\n지금 업데이트할까요?\n(코드 받기 · 데몬 재시작 · 필요 시 앱 재빌드 포함)"
            a.addButton(withTitle: "업데이트")
            a.addButton(withTitle: "나중에")
            if a.runModal() == .alertFirstButtonReturn { self.applyUpdate(dir: dir) }
        }
    }

    func applyUpdate(dir: String) {
        showProgress("업데이트 중…", "코드 받기 · 데몬 재시작 · 앱 재빌드")
        runScript(dir: dir, args: [], timeout: 300) { code, out in
            self.hideProgress()
            if code != 0 { self.showAlert("업데이트 실패", self.tail(out)); return }
            self.webView?.reload()                       // 새 대시보드 HTML/JS 반영
            if out.contains("재빌드") {                    // 앱 자체가 갱신됨 → 재실행 권유
                let a = NSAlert()
                a.messageText = "업데이트 완료 — 재실행 필요"
                a.informativeText = "앱 자체가 갱신되었습니다. 지금 재실행할까요?"
                a.addButton(withTitle: "재실행")
                a.addButton(withTitle: "나중에")
                if a.runModal() == .alertFirstButtonReturn { self.relaunch() }
            } else {
                self.showAlert("업데이트 완료 ✓", "최신 버전으로 갱신되었습니다.")
            }
        }
    }

    /// update.sh를 백그라운드에서 실행, stdout+stderr를 합쳐 완료 시 메인스레드로 콜백.
    func runScript(dir: String, args: [String], timeout: TimeInterval,
                   completion: @escaping (Int32, String) -> Void) {
        DispatchQueue.global().async {
            let p = Process()
            p.currentDirectoryURL = URL(fileURLWithPath: dir)
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = ["update.sh"] + args
            var env = ProcessInfo.processInfo.environment   // git/python3/swiftc/launchctl PATH 보강
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            p.environment = env
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            do { try p.run() } catch {
                DispatchQueue.main.async { completion(-1, "실행 실패: \(error.localizedDescription)") }
                return
            }
            let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
            let data = pipe.fileHandleForReading.readDataToEndOfFile()  // waitUntilExit 전에 읽어 데드락 방지
            p.waitUntilExit()
            killer.cancel()
            let out = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async { completion(p.terminationStatus, out) }
        }
    }

    func relaunch() {
        let candidates = ["/Applications/Lookout.app",
                          "\(NSHomeDirectory())/Applications/Lookout.app",
                          Bundle.main.bundlePath]
        let appPath = candidates.first { FileManager.default.fileExists(atPath: $0) } ?? Bundle.main.bundlePath
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 0.6; open \"\(appPath)\""]
        try? p.run()
        NSApp.terminate(nil)
    }

    func showProgress(_ title: String, _ sub: String) {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 380, height: 110),
                         styleMask: [.titled], backing: .buffered, defer: false)
        w.title = "Lookout"
        w.center()
        let v = w.contentView!
        let spin = NSProgressIndicator(frame: NSRect(x: 22, y: 50, width: 26, height: 26))
        spin.style = .spinning
        spin.startAnimation(nil)
        v.addSubview(spin)
        let lbl = NSTextField(labelWithString: title)
        lbl.frame = NSRect(x: 60, y: 58, width: 300, height: 20)
        lbl.font = .boldSystemFont(ofSize: 14)
        v.addSubview(lbl)
        let sl = NSTextField(labelWithString: sub)
        sl.frame = NSRect(x: 60, y: 34, width: 300, height: 18)
        sl.textColor = .secondaryLabelColor
        sl.font = .systemFont(ofSize: 11)
        v.addSubview(sl)
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        progressWin = w
    }

    func hideProgress() { progressWin?.orderOut(nil); progressWin = nil }

    func showAlert(_ title: String, _ info: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = info
        a.addButton(withTitle: "확인")
        a.runModal()
    }

    /// --check 출력에서 "N개 커밋 앞섬" 줄 + 들여쓴 커밋 목록만 추려 보여줌.
    func commitSummary(_ out: String) -> String {
        let lines = out.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let picked = lines.filter { $0.contains("앞섬") || $0.hasPrefix("    ") }
        let joined = picked.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return joined.isEmpty ? "새 버전이 있습니다." : joined
    }

    func tail(_ s: String, _ n: Int = 14) -> String {
        s.split(separator: "\n").map(String.init).suffix(n).joined(separator: "\n")
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)  // 일반 앱 (Dock + 메뉴바)
app.run()
