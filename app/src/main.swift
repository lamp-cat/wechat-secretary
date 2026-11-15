// 消息工作台 — native macOS shell for the local, receive-only WeChat helper.
// First launch: auto-locate WeChat data, reuse/acquire keys, start services, register MCP into local agent tools.
// Receive-only: this app has no way to send WeChat messages. Tokens/keys are never logged.
import Cocoa
import WebKit

let HOME = FileManager.default.homeDirectoryForCurrentUser.path
let BUNDLE = Bundle.main.bundleURL.path
let PAYLOAD: String = {
    let candidates = ["\(BUNDLE)/Contents/Resources/payload",
                      (Bundle.main.resourceURL?.appendingPathComponent("payload").path) ?? ""]
    for c in candidates where FileManager.default.fileExists(atPath: "\(c)/setup/bootstrap.sh") { return c }
    // Dev / source-tree fallback: run from the repo that contains this .app's build.
    let fromDist = (Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path)
    if FileManager.default.fileExists(atPath: "\(fromDist)/setup/bootstrap.sh") { return fromDist }
    return candidates[0]
}()
let MANAGED_ROOT = "\(HOME)/.local/share/wechatdesk/runtime"
let TOKEN_FILE = "\(HOME)/.config/wechat-frontend/gateway-token"
let BASE = "http://127.0.0.1:8088"
let READER_URL = "http://127.0.0.1:5678/"
let LA_DIR = "\(HOME)/Library/LaunchAgents"
let READER_LABEL = "com.gly.wechatreader.serve"
let FRONT_LABEL = "com.gly.wechatfrontend"
let KEEP_LABEL = "com.gly.wechatkeepalive"
let UID_STR = String(getuid())

func platformRoot() -> String {
    if let p = UserDefaults.standard.string(forKey: "PlatformRoot"), !p.isEmpty,
       FileManager.default.fileExists(atPath: "\(p)/wechat-realtime-reader/bin/wechat-reader") { return p }
    if FileManager.default.fileExists(atPath: "\(HOME)/wechat-platform/wechat-realtime-reader/bin/wechat-reader") {
        return "\(HOME)/wechat-platform"
    }
    return MANAGED_ROOT
}

func readToken() -> String {
    (try? String(contentsOfFile: TOKEN_FILE, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

@discardableResult
func run(_ exe: String, _ args: [String], env: [String: String]? = nil) -> Int32 {
    let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
    p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
    if let env = env { var e = ProcessInfo.processInfo.environment; e.merge(env) { _, n in n }; p.environment = e }
    do { try p.run() } catch { return -1 }
    p.waitUntilExit(); return p.terminationStatus
}

enum Backend {
    static func loaded(_ label: String) -> Bool { run("/bin/launchctl", ["print", "gui/\(UID_STR)/\(label)"]) == 0 }
    static func restart(_ label: String) { run("/bin/launchctl", ["kickstart", "-k", "gui/\(UID_STR)/\(label)"]) }
    static func stop(_ label: String) { run("/bin/launchctl", ["bootout", "gui/\(UID_STR)/\(label)"]) }
}

let session: URLSession = {
    let c = URLSessionConfiguration.ephemeral
    c.connectionProxyDictionary = [:]
    c.timeoutIntervalForRequest = 4
    return URLSession(configuration: c)
}()

func httpStatus(_ url: String, token: String? = nil) async -> Int {
    var req = URLRequest(url: URL(string: url)! )
    if let t = token, !t.isEmpty { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
    do { let (_, resp) = try await session.data(for: req); return (resp as? HTTPURLResponse)?.statusCode ?? 0 }
    catch { return 0 }
}

/// Streaming runner for bootstrap / capture scripts. Parses @@step / @@need / @@done lines.
final class ScriptRunner {
    private var proc: Process?
    private var pipe: Pipe?
    var onLine: ((String) -> Void)?
    var onDone: ((Int32) -> Void)?

    func start(_ exe: String, _ args: [String], env: [String: String]) {
        let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
        var e = ProcessInfo.processInfo.environment; e.merge(env) { _, n in n }; p.environment = e
        let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
        self.pipe = pipe; self.proc = p
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { return }
            guard let s = String(data: data, encoding: .utf8) else { return }
            for line in s.split(separator: "\n", omittingEmptySubsequences: false) {
                let t = String(line)
                if !t.isEmpty { DispatchQueue.main.async { self?.onLine?(t) } }
            }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.pipe?.fileHandleForReading.readabilityHandler = nil
                self?.onDone?(proc.terminationStatus)
            }
        }
        do { try p.run() } catch {
            DispatchQueue.main.async { self.onDone?(-1) }
        }
    }
    func cancel() { proc?.terminate() }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, NSWindowDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var panel: NSPanel?
    var panelWeb: WKWebView?
    var statusItem: NSStatusItem!
    var ready = false
    var pendingPath = "/sidebar"
    var bootLines: [(id: String, status: String, text: String)] = []
    var bootNeed: (code: String, text: String)?
    var bootExtra: [String] = []
    var runner: ScriptRunner?
    var mode: String = "boot"   // boot | capture

    func makeConfig() -> WKWebViewConfiguration {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.userContentController.add(self, name: "wfCopy")
        cfg.userContentController.add(self, name: "wfSetup")
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        return cfg
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu(); buildStatusItem()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 720),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "消息工作台"
        window.minSize = NSSize(width: 400, height: 520)
        window.setFrameAutosaveName("SidebarMainWindow")
        if window.frame.origin == .zero { window.center() }
        window.delegate = self
        web = WKWebView(frame: window.contentView!.bounds, configuration: makeConfig())
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self; web.uiDelegate = self
        window.contentView!.addSubview(web)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        Task { await self.boot() }
    }

    // MARK: progress UI
    func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
    }
    func showProgress(title: String, error: Bool = false, actions: [(String, String)] = []) {
        let items = bootLines.map { row -> String in
            let icon: String
            switch row.status {
            case "ok": icon = "✓"
            case "warn": icon = "!"
            case "fail": icon = "✗"
            case "skip": icon = "–"
            default: icon = "…"
            }
            let color: String
            switch row.status {
            case "ok": color = "#07c160"
            case "warn": color = "#d48806"
            case "fail": color = "#e5484d"
            default: color = "#6b7280"
            }
            return "<li style=\"color:\(color)\"><b>\(icon)</b> \(esc(row.text))</li>"
        }.joined()
        let extra = bootExtra.map { "<p style=\"color:#6b7280;font-size:12px;margin:2px 0\">\(esc($0))</p>" }.joined()
        var needHTML = ""
        if let n = bootNeed {
            needHTML = "<div style=\"margin:16px 0;padding:12px 14px;background:#fff7e6;border:1px solid #ffd591;border-radius:10px\">" +
                       "<p style=\"margin:0 0 8px;color:#873800\"><b>需要你操作一步</b></p>" +
                       "<p style=\"margin:0;color:#1d2129\">\(esc(n.text))</p></div>"
        }
        let btns = actions.map { a in
            "<button onclick=\"webkit.messageHandlers.wfSetup.postMessage('\(a.1)')\" " +
            "style=\"margin:4px 8px 4px 0;padding:8px 16px;border:0;border-radius:8px;background:#07c160;color:#fff;font:14px -apple-system,'PingFang SC';cursor:pointer\">\(esc(a.0))</button>"
        }.joined()
        let html = """
        <!doctype html><meta charset="utf-8"><body style="font:15px -apple-system,'PingFang SC';margin:0;background:#f5f6f8;color:#1d2129">
        <div style="max-width:640px;margin:40px auto;padding:0 24px">
          <h2 style="color:\(error ? "#e5484d" : "#07c160")">\(esc(title))</h2>
          <ul style="line-height:1.9;padding-left:4px;list-style:none">\(items)</ul>
          \(extra)\(needHTML)
          <div style="margin-top:12px">\(btns)</div>
          <p style="color:#6b7280;font-size:12px;margin-top:24px">只读：只读取本机微信数据并展示，没有发送功能。密钥和令牌不会显示。</p>
        </div></body>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func upsertStep(id: String, status: String, text: String) {
        if let i = bootLines.firstIndex(where: { $0.id == id }) {
            bootLines[i] = (id, status, text)
        } else {
            bootLines.append((id, status, text))
        }
    }

    // MARK: boot
    func boot() async {
        mode = "boot"
        bootLines = []; bootNeed = nil; bootExtra = []
        await MainActor.run { self.showProgress(title: "正在准备…") }
        // Fast path: services already healthy → skip the full bootstrap.
        let tok = readToken()
        if !tok.isEmpty {
            let r = await httpStatus(READER_URL)
            let h = await httpStatus("\(BASE)/api/health", token: tok)
            if r > 0 && h == 200 {
                await MainActor.run {
                    self.upsertStep(id: "ready", status: "ok", text: "后台服务已就绪")
                    self.ready = true; self.load(self.pendingPath)
                }
                // Soft: keep agent registrations up to date in the background.
                DispatchQueue.global(qos: .utility).async { self.runAgentsAuto() }
                return
            }
        }
        await MainActor.run { self.startBootstrap() }
    }

    func startBootstrap() {
        mode = "boot"; bootLines = []; bootNeed = nil; bootExtra = []
        showProgress(title: "首次启动 · 自动配置")
        let script = "\(PAYLOAD)/setup/bootstrap.sh"
        guard FileManager.default.isExecutableFile(atPath: script) else {
            upsertStep(id: "env", status: "fail", text: "安装包不完整：找不到 setup/bootstrap.sh")
            showProgress(title: "无法启动", error: true, actions: [("重试", "retry")])
            return
        }
        var env: [String: String] = [
            "WD_PAYLOAD": PAYLOAD,
            "WD_HOME": HOME,
            "WD_ROOT": platformRoot() == MANAGED_ROOT ? MANAGED_ROOT : platformRoot(),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        ]
        // Reuse keys from a previous install on this Mac when possible (same account).
        let live = "\(HOME)/.config/wechat-realtime-reader"
        if !FileManager.default.fileExists(atPath: "\(live)/keys.json"),
           FileManager.default.fileExists(atPath: "\(HOME)/.config/wechat-realtime-reader/keys.json") {
            // no-op: live IS the default
        }
        // Prefer importing from the already-running production config when testing under a scratch HOME
        // is not in play; on a normal machine the live config is what we want.
        if FileManager.default.fileExists(atPath: "\(live)/keys.json") {
            env["WD_IMPORT_KEYS_FROM"] = live
        }
        let r = ScriptRunner()
        r.onLine = { [weak self] line in self?.handleBootLine(line) }
        r.onDone = { [weak self] code in self?.handleBootDone(code) }
        runner = r
        r.start("/bin/bash", [script], env: env)
    }

    func startCapture() {
        mode = "capture"; bootLines = []; bootNeed = nil; bootExtra = []
        showProgress(title: "抓取微信数据库密钥")
        let script = "\(PAYLOAD)/setup/capture-gui.sh"
        guard FileManager.default.isExecutableFile(atPath: script) else {
            upsertStep(id: "env", status: "fail", text: "安装包不完整：找不到 setup/capture-gui.sh")
            showProgress(title: "无法抓取", error: true, actions: [("返回", "retry")])
            return
        }
        let root = platformRoot()
        let env: [String: String] = [
            "WD_ROOT": root, "WD_HOME": HOME, "WD_READER_LABEL": READER_LABEL,
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        ]
        let r = ScriptRunner(); runner = r
        r.onLine = { [weak self] line in self?.handleBootLine(line) }
        r.onDone = { [weak self] code in
            if code == 0 {
                self?.upsertStep(id: "done", status: "ok", text: "抓取完成，正在继续启动…")
                self?.showProgress(title: "密钥就绪")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self?.startBootstrap() }
            } else if code == 10 {
                // need already shown
            } else {
                self?.showProgress(title: "抓取未完成", error: true,
                                   actions: [("重试", "capture"), ("取消，返回启动", "retry")])
            }
        }
        r.start("/bin/bash", [script], env: env)
    }

    func handleBootLine(_ line: String) {
        if line.hasPrefix("@@step\t") {
            let parts = line.split(separator: "\t", maxSplits: 3, omittingEmptySubsequences: false).map(String.init)
            guard parts.count >= 4 else { return }
            upsertStep(id: parts[1], status: parts[2], text: parts[3])
            showProgress(title: mode == "capture" ? "抓取微信数据库密钥" : "首次启动 · 自动配置")
        } else if line.hasPrefix("@@need\t") {
            let parts = line.split(separator: "\t", maxSplits: 2, omittingEmptySubsequences: false).map(String.init)
            guard parts.count >= 3 else { return }
            bootNeed = (parts[1], parts[2])
            showProgress(title: mode == "capture" ? "抓取微信数据库密钥" : "首次启动 · 自动配置",
                         actions: actionsForNeed(parts[1]))
        } else if line.hasPrefix("@@root\t") {
            let root = String(line.dropFirst(7))
            if !root.isEmpty {
                UserDefaults.standard.set(root, forKey: "PlatformRoot")
            }
        } else if line.hasPrefix("@@done\t") {
            // handled in onDone
        } else {
            let t = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty && !t.contains("token=") && !t.lowercased().contains("bearer ") {
                bootExtra.append(t)
                if bootExtra.count > 8 { bootExtra.removeFirst() }
                showProgress(title: mode == "capture" ? "抓取微信数据库密钥" : "首次启动 · 自动配置")
            }
        }
    }

    func actionsForNeed(_ code: String) -> [(String, String)] {
        switch code {
        case "keys", "keys_failed":
            return [("开始抓取密钥", "capture"), ("稍后重试", "retry")]
        case "wechat_quit":
            return [("我已退出，继续", "capture"), ("取消", "retry")]
        case "data_permission":
            return [("打开系统设置", "open_fda"), ("在访达中显示读取器", "reveal_reader"), ("我已授权，重试", "retry")]
        case "clt":
            return [("安装命令行工具", "install_clt"), ("我已装好，重试", "retry")]
        case "wechat_missing":
            return [("打开 App Store 搜索微信", "open_wechat_store"), ("我已安装，重试", "retry")]
        case "wechat_login":
            return [("打开微信", "open_wechat"), ("我已登录，重试", "retry")]
        default:
            return [("重试", "retry")]
        }
    }

    func handleBootDone(_ code: Int32) {
        if code == 0 {
            upsertStep(id: "done", status: "ok", text: "配置完成，正在打开界面…")
            showProgress(title: "就绪")
            Task { await self.waitReady() }
        } else if code == 10 {
            // need already rendered with actions
        } else {
            showProgress(title: "启动未完成", error: true, actions: [("重试", "retry")])
        }
    }

    func waitReady() async {
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
            await MainActor.run {
                self.upsertStep(id: "ready", status: "fail",
                                text: readerUp ? "网关健康检查失败（HTTP \(health)）" : "读取服务无响应")
                self.showProgress(title: "服务未就绪", error: true,
                                  actions: [("重启后台服务", "restart"), ("重试启动", "retry")])
            }
        }
    }

    func runAgentsAuto() {
        let root = platformRoot()
        let py = "\(root)/wechat-frontend/.venv/bin/python"
        let reg = "\(root)/wechat-frontend/app/agent_register.py"
        let bin = "\(root)/wechat-realtime-reader/bin/wechat-reader"
        guard FileManager.default.isExecutableFile(atPath: py),
              FileManager.default.fileExists(atPath: reg) else { return }
        _ = run(py, [reg, "auto", "--bin", bin, "--config-dir", "\(HOME)/.config/wechat-realtime-reader"])
    }

    // MARK: load UI
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
        appMenu.addItem(withTitle: "关于 消息工作台", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        appMenu.addItem(withTitle: "重新运行首次配置…", action: #selector(rerunSetup), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 消息工作台", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出并停止后台服务", action: #selector(quitAndStop), keyEquivalent: "")
        appMenu.addItem(withTitle: "退出 消息工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
            if let img = NSImage(systemSymbolName: "bubble.left.and.text.bubble.right", accessibilityDescription: "消息工作台") {
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
        m.addItem(withTitle: "重新运行首次配置…", action: #selector(rerunSetup), keyEquivalent: "")
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
    @objc func rerunSetup() {
        ready = false; showMain(); startBootstrap()
    }
    @objc func restartBackend() {
        ready = false; showMain()
        bootLines = [(id: "restart", status: "run", text: "正在重启后台服务…")]
        showProgress(title: "重启中")
        Backend.restart(READER_LABEL); Backend.restart(FRONT_LABEL)
        Task { try? await Task.sleep(nanoseconds: 1_500_000_000); await self.waitReady() }
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
            p.title = "伴侣浮窗"; p.level = .floating; p.isFloatingPanel = true
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

    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { showMain(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    // MARK: WebView + JS bridge
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
        if v === web {
            bootLines = [(id: "page", status: "fail", text: "页面加载失败：\(e.localizedDescription)")]
            showProgress(title: "加载失败", error: true, actions: [("重启后台服务", "restart"), ("重试", "retry")])
        }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "wfCopy", let s = message.body as? String {
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string)
            return
        }
        guard message.name == "wfSetup", let cmd = message.body as? String else { return }
        switch cmd {
        case "retry": ready = false; startBootstrap()
        case "capture": ready = false; startCapture()
        case "restart": restartBackend()
        case "open_fda":
            if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                NSWorkspace.shared.open(u)
            }
        case "reveal_reader":
            let bin = "\(platformRoot())/wechat-realtime-reader/bin/wechat-reader"
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: bin)])
        case "install_clt":
            _ = run("/usr/bin/xcode-select", ["--install"])
        case "open_wechat":
            NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/WeChat.app"))
        case "open_wechat_store":
            if let u = URL(string: "macappstore://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?q=%E5%BE%AE%E4%BF%A1") {
                NSWorkspace.shared.open(u)
            }
        default: break
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
