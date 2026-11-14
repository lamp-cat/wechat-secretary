// 消息助手 — native macOS shell for the local, receive-only WeChat helper.
// - Ensures the launchd-managed backend (wechat-reader serve + FastAPI gateway) is running.
// - Logs the embedded WebView in with the local gateway token (read from the 0600 file, never logged).
// - Shows the companion panel (/sidebar, the only UI) and the in-app Settings (/settings).
// Receive-only: this app has no way to send WeChat messages.
import Cocoa
import WebKit

let HOME = FileManager.default.homeDirectoryForCurrentUser.path
let PLATFORM_ROOT = UserDefaults.standard.string(forKey: "PlatformRoot") ?? "\(HOME)/wechat-platform"
let READER_ROOT = "\(PLATFORM_ROOT)/wechat-realtime-reader"
let FRONTEND_ROOT = "\(PLATFORM_ROOT)/wechat-frontend"
let TOKEN_FILE = "\(HOME)/.config/wechat-frontend/gateway-token"
let BASE = "http://127.0.0.1:8088"
let READER_URL = "http://127.0.0.1:5678/"
let LA_DIR = "\(HOME)/Library/LaunchAgents"
let READER_LABEL = "com.gly.wechatreader.serve"
let FRONT_LABEL = "com.gly.wechatfrontend"
let KEEP_LABEL = "com.gly.wechatkeepalive"
let UID_STR = String(getuid())

func readToken() -> String {
    (try? String(contentsOfFile: TOKEN_FILE, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

@discardableResult
func run(_ exe: String, _ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

// MARK: - Backend (launchd) management
enum Backend {
    static func loaded(_ label: String) -> Bool { run("/bin/launchctl", ["print", "gui/\(UID_STR)/\(label)"]) == 0 }
    static func plistPath(_ label: String) -> String { "\(LA_DIR)/\(label).plist" }

    /// Generate a LaunchAgent if this machine doesn't have one yet (first run on a fresh install).
    static func writePlistIfMissing(_ label: String) {
        let path = plistPath(label)
        if FileManager.default.fileExists(atPath: path) { return }
        let env: [String: String] = ["PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": HOME]
        var d: [String: Any] = ["Label": label, "RunAtLoad": true, "KeepAlive": true, "EnvironmentVariables": env]
        switch label {
        case READER_LABEL:
            d["ProgramArguments"] = ["\(READER_ROOT)/bin/wechat-reader", "serve", "-addr", "127.0.0.1:5678"]
            d["WorkingDirectory"] = READER_ROOT
            d["StandardOutPath"] = "/tmp/wechat_reader_serve.log"; d["StandardErrorPath"] = "/tmp/wechat_reader_serve.log"
        case FRONT_LABEL:
            d["ProgramArguments"] = ["\(FRONTEND_ROOT)/scripts/start.sh"]
            d["WorkingDirectory"] = FRONTEND_ROOT
            d["StandardOutPath"] = "/tmp/wechat_frontend.log"; d["StandardErrorPath"] = "/tmp/wechat_frontend.log"
        default: return
        }
        try? FileManager.default.createDirectory(atPath: LA_DIR, withIntermediateDirectories: true)
        if let data = try? PropertyListSerialization.data(fromPropertyList: d, format: .xml, options: 0) {
            FileManager.default.createFile(atPath: path, contents: data)
        }
    }

    /// Make sure a service is loaded in the user's GUI domain (bootstrap if needed).
    static func ensure(_ label: String, create: Bool) -> Bool {
        if loaded(label) { return true }
        if create { writePlistIfMissing(label) }
        guard FileManager.default.fileExists(atPath: plistPath(label)) else { return false }
        run("/bin/launchctl", ["bootstrap", "gui/\(UID_STR)", plistPath(label)])
        return loaded(label)
    }
    static func restart(_ label: String) { run("/bin/launchctl", ["kickstart", "-k", "gui/\(UID_STR)/\(label)"]) }
    static func stop(_ label: String) { run("/bin/launchctl", ["bootout", "gui/\(UID_STR)/\(label)"]) }
}

let session: URLSession = {
    let c = URLSessionConfiguration.ephemeral
    c.connectionProxyDictionary = [:]          // loopback only: never via the system proxy
    c.timeoutIntervalForRequest = 4
    return URLSession(configuration: c)
}()

func httpStatus(_ url: String, token: String? = nil) async -> Int {
    var req = URLRequest(url: URL(string: url)!)
    if let t = token, !t.isEmpty { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
    do {
        let (_, resp) = try await session.data(for: req)
        return (resp as? HTTPURLResponse)?.statusCode ?? 0
    } catch { return 0 }
}

// MARK: - App
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, NSWindowDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var panel: NSPanel?
    var panelWeb: WKWebView?
    var statusItem: NSStatusItem!
    var ready = false
    var pendingPath = "/sidebar"

    func makeConfig() -> WKWebViewConfiguration {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.userContentController.add(self, name: "wfCopy")
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        return cfg
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        buildStatusItem()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "消息助手 · 伴侣浮窗"
        window.minSize = NSSize(width: 360, height: 480)
        window.setFrameAutosaveName("SidebarMainWindow")
        if window.frame.origin == .zero { window.center() }
        window.delegate = self
        web = WKWebView(frame: window.contentView!.bounds, configuration: makeConfig())
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        window.contentView!.addSubview(web)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showLoading(["正在启动后台服务…"])
        Task { await self.boot() }
    }

    // MARK: boot sequence
    func showLoading(_ lines: [String], error: Bool = false) {
        let items = lines.map { "<li>\($0)</li>" }.joined()
        let html = """
        <!doctype html><meta charset="utf-8"><body style="font:15px -apple-system,'PingFang SC';display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f6f8;color:#1d2129">
        <div style="max-width:560px"><h2 style="color:\(error ? "#e5484d" : "#07c160")">消息助手</h2>
        <ul style="line-height:1.9;padding-left:18px">\(items)</ul>
        <p style="color:#6b7280;font-size:12px">只读：只读取本机微信数据并展示，没有发送功能。</p></div></body>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func boot() async {
        var log: [String] = []
        let fm = FileManager.default
        if !fm.fileExists(atPath: "\(READER_ROOT)/bin/wechat-reader") || !fm.fileExists(atPath: "\(FRONTEND_ROOT)/scripts/start.sh") {
            await MainActor.run { self.showLoading(["找不到后台程序：\(PLATFORM_ROOT)", "请确认 wechat-realtime-reader 与 wechat-frontend 已安装在该目录。"], error: true) }
            return
        }
        if !fm.fileExists(atPath: "\(HOME)/.config/wechat-realtime-reader/source-config.json") {
            log.append("⚠️ 未找到微信数据源配置（~/.config/wechat-realtime-reader/source-config.json），需要先做一次密钥抓取。")
        } else {
            log.append("✓ 已找到本机微信数据源配置（自动连接，无需手动选目录）")
        }
        let r = Backend.ensure(READER_LABEL, create: true)
        let f = Backend.ensure(FRONT_LABEL, create: true)
        _ = Backend.ensure(KEEP_LABEL, create: false)   // optional WeChat watchdog, only if installed
        log.append(r ? "✓ 读取服务（wechat-reader）已在 launchd 中运行" : "✗ 读取服务启动失败")
        log.append(f ? "✓ 网关（FastAPI :8088）已在 launchd 中运行" : "✗ 网关启动失败")
        let snapshot = log
        await MainActor.run { self.showLoading(snapshot + ["⏳ 等待服务就绪…"]) }

        var readerUp = false, health = 0
        for i in 0..<60 {
            if !readerUp { readerUp = await httpStatus(READER_URL) > 0 }
            health = await httpStatus("\(BASE)/api/health", token: readToken())
            if readerUp && health == 200 { break }
            if i == 20 && health == 0 { Backend.restart(FRONT_LABEL) }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        if health == 200 {
            await MainActor.run { self.ready = true; self.load(self.pendingPath) }
        } else {
            var lines = snapshot
            lines.append(readerUp ? "✓ 读取服务已响应" : "✗ 读取服务无响应（看 /tmp/wechat_reader_serve.log；首次可能需要在弹窗里允许访问其他 App 的数据）")
            lines.append(health == 401 ? "✗ 网关令牌不匹配" : "✗ 网关健康检查失败（HTTP \(health)，看 /tmp/wechat_frontend.log）")
            lines.append("可在菜单栏图标里选「重启后台服务」后重试。")
            await MainActor.run { self.showLoading(lines, error: true) }
        }
    }

    /// Install the gateway cookie (HttpOnly, SameSite=Strict) so the WebView is logged in, then navigate.
    func load(_ path: String, in view: WKWebView? = nil) {
        let target = view ?? web!
        guard ready else { pendingPath = path; return }
        let token = readToken()
        var props: [HTTPCookiePropertyKey: Any] = [
            .name: "wf_gateway", .value: token, .domain: "127.0.0.1", .path: "/",
            .expires: Date().addingTimeInterval(86400 * 365), HTTPCookiePropertyKey("HttpOnly"): "TRUE",
        ]
        props[.sameSitePolicy] = HTTPCookieStringPolicy.sameSiteStrict
        let url = URL(string: BASE + path)!
        if let cookie = HTTPCookie(properties: props), !token.isEmpty {
            WKWebsiteDataStore.default().httpCookieStore.setCookie(cookie) { target.load(URLRequest(url: url)) }
        } else {
            target.load(URLRequest(url: url))
        }
    }

    // MARK: menus
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 消息助手", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 消息助手", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出并停止后台服务", action: #selector(quitAndStop), keyEquivalent: "")
        appMenu.addItem(withTitle: "退出 消息助手", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "视图")
        view.addItem(withTitle: "伴侣浮窗", action: #selector(openSidebarInMain), keyEquivalent: "2")
        view.addItem(withTitle: "伴侣浮窗（置顶小窗）", action: #selector(togglePanel), keyEquivalent: "3")
        view.addItem(withTitle: "设置", action: #selector(openSettings), keyEquivalent: "")
        view.addItem(.separator())
        view.addItem(withTitle: "重新加载", action: #selector(reloadPage), keyEquivalent: "r")
        view.addItem(withTitle: "重启后台服务", action: #selector(restartBackend), keyEquivalent: "")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "关闭", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            if let img = NSImage(systemSymbolName: "bubble.left.and.text.bubble.right", accessibilityDescription: "消息助手") {
                img.isTemplate = true; b.image = img
            } else { b.title = "微" }
        }
        let m = NSMenu()
        m.addItem(withTitle: "显示/隐藏主窗口", action: #selector(toggleWindow), keyEquivalent: "")
        m.addItem(withTitle: "伴侣浮窗", action: #selector(openSidebarInMain), keyEquivalent: "")
        m.addItem(withTitle: "伴侣浮窗（置顶小窗）", action: #selector(togglePanel), keyEquivalent: "")
        m.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(withTitle: "重启后台服务", action: #selector(restartBackend), keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(withTitle: "退出（后台服务保持运行）", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        m.addItem(withTitle: "退出并停止后台服务", action: #selector(quitAndStop), keyEquivalent: "")
        statusItem.menu = m
    }

    func showMain() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func toggleWindow() { if window.isVisible && NSApp.isActive { window.orderOut(nil) } else { showMain() } }
    @objc func openSidebarInMain() { showMain(); load("/sidebar") }
    @objc func openSettings() { showMain(); load("/settings") }
    @objc func reloadPage() { if ready { web.reload() } else { Task { await boot() } } }
    @objc func restartBackend() {
        ready = false
        showMain(); showLoading(["正在重启后台服务…"])
        Backend.restart(READER_LABEL); Backend.restart(FRONT_LABEL)
        Task { try? await Task.sleep(nanoseconds: 1_500_000_000); await self.boot() }
    }
    @objc func quitAndStop() {
        let a = NSAlert()
        a.messageText = "退出并停止后台服务？"
        a.informativeText = "会停止读取服务和网关（launchd）。停止后浏览器/飞书秒搭也无法访问，直到下次打开本应用。微信本身不受影响。"
        a.addButton(withTitle: "停止并退出"); a.addButton(withTitle: "取消")
        if a.runModal() == .alertFirstButtonReturn {
            Backend.stop(FRONT_LABEL); Backend.stop(READER_LABEL)
            NSApp.terminate(nil)
        }
    }

    @objc func togglePanel() {
        if let p = panel, p.isVisible { p.orderOut(nil); return }
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 400, height: 780),
                            styleMask: [.titled, .closable, .resizable, .utilityWindow], backing: .buffered, defer: false)
            p.title = "伴侣浮窗"
            p.level = .floating
            p.isFloatingPanel = true
            p.hidesOnDeactivate = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.setFrameAutosaveName("CompanionPanel")
            if p.frame.origin == .zero, let s = NSScreen.main {
                p.setFrameTopLeftPoint(NSPoint(x: s.visibleFrame.maxX - 420, y: s.visibleFrame.maxY - 20))
            }
            let w = WKWebView(frame: p.contentView!.bounds, configuration: makeConfig())
            w.autoresizingMask = [.width, .height]
            w.navigationDelegate = self; w.uiDelegate = self
            p.contentView!.addSubview(w)
            panel = p; panelWeb = w
        }
        panel!.orderFront(nil)
        if let w = panelWeb { load("/sidebar", in: w) }
    }

    // MARK: window lifecycle — closing the window keeps the app (menubar) alive
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { showMain(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    // MARK: WebView delegates
    func isLocal(_ url: URL?) -> Bool {
        guard let u = url else { return true }
        if u.scheme == "about" || u.scheme == "data" || u.scheme == "blob" { return true }
        return u.host == "127.0.0.1" || u.host == "localhost"
    }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if !isLocal(action.request.url), action.navigationType == .linkActivated, let u = action.request.url {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }
    func webView(_ webView: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url {
            if isLocal(u) { webView.load(action.request) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "好"); a.runModal(); completionHandler()
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = prompt
        let f = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24)); f.stringValue = defaultText ?? ""
        a.accessoryView = f; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn ? f.stringValue : nil)
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { handleLoadError(webView, error) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { handleLoadError(webView, error) }
    func handleLoadError(_ v: WKWebView, _ e: Error) {
        if (e as NSError).code == NSURLErrorCancelled { return }
        if v === web { showLoading(["页面加载失败：\(e.localizedDescription)", "可在菜单栏图标里选「重启后台服务」。"], error: true) }
    }
    // Clipboard bridge for the Settings page "复制" button.
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "wfCopy", let s = message.body as? String else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
