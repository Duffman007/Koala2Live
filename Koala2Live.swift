import Cocoa
import Foundation

// ── Entry Point ───────────────────────────────────────────────────────────────
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()

// ── Helpers ───────────────────────────────────────────────────────────────────
func resourcePath(_ name: String) -> String {
    Bundle.main.path(forResource: (name as NSString).deletingPathExtension,
                     ofType: (name as NSString).pathExtension) ?? ""
}

func readResource(_ name: String) -> String {
    let path = resourcePath(name)
    return (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
}

func appVersion() -> String {
    let v = readResource("version.txt").trimmingCharacters(in: .whitespacesAndNewlines)
    return v.isEmpty ? "Unknown" : v
}

// ── App Delegate ──────────────────────────────────────────────────────────────
class AppDelegate: NSObject, NSApplicationDelegate {
    var window: MainWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenuBar()
        window = MainWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func setupMenuBar() {
        let mainMenu = NSMenu()

        // ── App menu ──────────────────────────────────────────────────────────
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu

        let aboutItem = NSMenuItem(title: "About Koala2Live",
                                   action: #selector(showAbout), keyEquivalent: "")
        aboutItem.target = self
        appMenu.addItem(aboutItem)

        let changelogItem = NSMenuItem(title: "View Change Log",
                                       action: #selector(showChangelog), keyEquivalent: "")
        changelogItem.target = self
        appMenu.addItem(changelogItem)

        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Koala2Live",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q"))

        // ── File menu ─────────────────────────────────────────────────────────
        let fileItem = NSMenuItem()
        mainMenu.addItem(fileItem)
        let fileMenu = NSMenu(title: "File")
        fileItem.submenu = fileMenu
        let openItem = NSMenuItem(title: "Open…", action: #selector(openFile), keyEquivalent: "o")
        openItem.target = self
        fileMenu.addItem(openItem)

        NSApp.mainMenu = mainMenu
    }

    @objc func openFile() {
        (window.contentViewController as? DropViewController)?.browseForFile()
    }

    @objc func showAbout() {
        let version = appVersion()
        let alert = NSAlert()
        alert.messageText = "Koala2Live"
        alert.informativeText = "Version \(version)\n\nConvert Koala Sampler projects\nto Ableton Live project files.\n\nCreated by Duffman"
        alert.alertStyle = .informational
        if let icon = NSApp.applicationIconImage { alert.icon = icon }
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc func showChangelog() {
        let changelog = readResource("changelog.txt")

        // Create a scrollable text window
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 420),
            styleMask:   [.titled, .closable, .resizable],
            backing:     .buffered,
            defer:       false)
        panel.title = "Change Log"
        panel.center()

        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 420))
        scrollView.hasVerticalScroller = true
        scrollView.autoresizingMask = [.width, .height]

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 500, height: 400))
        textView.string = changelog.isEmpty ? "(No changelog found)" : changelog
        textView.isEditable = false
        textView.isSelectable = true
        textView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textColor = .labelColor
        textView.backgroundColor = .textBackgroundColor
        textView.autoresizingMask = [.width]
        textView.textContainerInset = NSSize(width: 12, height: 12)

        scrollView.documentView = textView
        panel.contentView = scrollView

        panel.makeKeyAndOrderFront(nil)
    }
}

// ── Main Window ───────────────────────────────────────────────────────────────
class MainWindow: NSWindow {
    init() {
        let w: CGFloat = 550
        let h: CGFloat = 490
        let screen = NSScreen.main!.frame
        super.init(
            contentRect: NSRect(x: (screen.width-w)/2, y: (screen.height-h)/2, width: w, height: h),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false)
        title = "Koala2Live"
        isReleasedWhenClosed = false
        contentViewController = DropViewController()
    }
}

// ── Drop View Controller ──────────────────────────────────────────────────────
class DropViewController: NSViewController {

    let dropView    = DropZoneView()
    let statusLabel = NSTextField(labelWithString: "")
    let busToggle   = NSButton(checkboxWithTitle: "Use Bus Routing", target: nil, action: nil)

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 550, height: 490))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor(red: 0.10, green: 0.10, blue: 0.13, alpha: 1).cgColor

        // ── Background image ──────────────────────────────────────────────────
        let bgView = NSImageView(frame: NSRect(x: 0, y: 230, width: 550, height: 260))
        bgView.imageScaling = .scaleAxesIndependently
        if let bgImg = NSImage(contentsOfFile: resourcePath("background.png")) {
            bgView.image = bgImg
        }
        view.addSubview(bgView)

        // ── Drop zone ─────────────────────────────────────────────────────────
        dropView.frame = NSRect(x: 30, y: 88, width: 490, height: 136)
        dropView.controller = self
        view.addSubview(dropView)

        // ── Bus toggle ────────────────────────────────────────────────────────
        busToggle.frame = NSRect(x: 30, y: 44, width: 490, height: 38)
        busToggle.state = .on
        busToggle.contentTintColor = .systemOrange
        let toggleParagraph = NSMutableParagraphStyle()
        toggleParagraph.lineBreakMode = .byWordWrapping
        busToggle.attributedTitle = NSAttributedString(
            string: "No Bus mode - route all Koala pads direct to main output and bypass Koala Mixer",
            attributes: [
                .foregroundColor: NSColor.white,
                .font: NSFont.systemFont(ofSize: 13),
                .paragraphStyle: toggleParagraph
            ])
        busToggle.cell?.wraps = true
        busToggle.cell?.isScrollable = false
        view.addSubview(busToggle)

        // ── Status label ──────────────────────────────────────────────────────
        statusLabel.frame = NSRect(x: 30, y: 10, width: 490, height: 44)
        statusLabel.alignment = .center
        statusLabel.textColor = NSColor.secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.cell?.wraps = true
        statusLabel.cell?.isScrollable = false
        view.addSubview(statusLabel)
    }

    func processFile(_ path: String) {
        guard path.hasSuffix(".koala"), FileManager.default.fileExists(atPath: path) else {
            setStatus("⚠️  Please drop a .koala backup file", color: .systemOrange)
            dropView.setState(.warning); return
        }
        let name = URL(fileURLWithPath: path).lastPathComponent
        setStatus("Converting \(name)…", color: .systemBlue)
        dropView.setState(.working)

        DispatchQueue.global(qos: .userInitiated).async {
            let useBusses = self.busToggle.state == .on
            let r = self.runConversion(path, useBusses: useBusses)
            DispatchQueue.main.async {
                if r.success {
                    self.setStatus("✅  \"\(r.projectName) Project\" exported successfully!", color: .systemGreen)
                    self.dropView.setState(.success)
                } else {
                    let e = r.error.count > 140 ? String(r.error.prefix(140)) + "…" : r.error
                    self.setStatus("❌  \(e)", color: .systemRed)
                    self.dropView.setState(.error)
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
                    self.dropView.setState(.idle)
                    self.setStatus("", color: .secondaryLabelColor)
                }
            }
        }
    }

    func runConversion(_ path: String, useBusses: Bool = false) -> (success: Bool, projectName: String, error: String) {
        guard let script = Bundle.main.path(forResource: "KoalaALS", ofType: "py") else {
            return (false, "", "KoalaALS.py not found in app bundle")
        }
        let pythons = ["/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"]
        let python  = pythons.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"

        let task = Process()
        task.executableURL = URL(fileURLWithPath: python)
        var arguments = [script]
        if useBusses { arguments.append("--no-busses") }  // toggle ON = no bus mode
        arguments.append(path)
        task.arguments = arguments
        let out = Pipe(), err = Pipe()
        task.standardOutput = out; task.standardError = err
        do { try task.run(); task.waitUntilExit() }
        catch { return (false, "", "Failed to launch Python: \(error.localizedDescription)") }

        let outStr = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let errStr = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        if task.terminationStatus == 0 {
            let proj = outStr.components(separatedBy: "\n")
                .first { $0.contains("Project:") }?
                .components(separatedBy: "Project:").last?
                .trimmingCharacters(in: .whitespaces) ?? "project"
            return (true, proj, "")
        }
        return (false, "", (errStr.isEmpty ? outStr : errStr).trimmingCharacters(in: .whitespacesAndNewlines))
    }

    func setStatus(_ text: String, color: NSColor) {
        statusLabel.stringValue = text
        statusLabel.textColor = color
    }

    func browseForFile() {
        let panel = NSOpenPanel()
        panel.title = "Select Koala Backup"
        panel.allowedFileTypes = ["koala"]
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { processFile(url.path) }
    }
}

// ── Drop Zone States ──────────────────────────────────────────────────────────
enum DropState { case idle, working, success, error, warning }

// ── Drop Zone View ────────────────────────────────────────────────────────────
class DropZoneView: NSView {
    weak var controller: DropViewController?

    private let iconLabel  = NSTextField(labelWithString: "🐨")
    private let titleLabel = NSTextField(labelWithString: "Drag Koala Project Here")
    private let subLabel   = NSTextField(labelWithString: ".koala file  •  or click to browse")
    private let border     = CAShapeLayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor(white: 1, alpha: 0.05).cgColor
        layer?.cornerRadius = 14

        border.strokeColor = NSColor(white: 1, alpha: 0.25).cgColor
        border.fillColor   = NSColor.clear.cgColor
        border.lineWidth   = 1.5
        border.lineDashPattern = [7, 4]
        layer?.addSublayer(border)

        iconLabel.font      = .systemFont(ofSize: 32)
        iconLabel.alignment = .center
        addSubview(iconLabel)

        titleLabel.font      = .boldSystemFont(ofSize: 15)
        titleLabel.textColor = .white
        titleLabel.alignment = .center
        addSubview(titleLabel)

        subLabel.font      = .systemFont(ofSize: 11)
        subLabel.textColor = NSColor(white: 1, alpha: 0.45)
        subLabel.alignment = .center
        addSubview(subLabel)

        registerForDraggedTypes([.fileURL])
    }
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        let w = bounds.width, h = bounds.height
        border.path  = NSBezierPath(roundedRect: bounds.insetBy(dx: 1.5, dy: 1.5),
                                    xRadius: 13, yRadius: 13).cgPath
        border.frame = bounds
        iconLabel.frame  = NSRect(x: 0, y: h - 54, width: w, height: 42)
        titleLabel.frame = NSRect(x: 20, y: h - 84, width: w-40, height: 24)
        subLabel.frame   = NSRect(x: 20, y: h - 106, width: w-40, height: 18)
    }

    func setState(_ s: DropState) {
        switch s {
        case .idle:
            iconLabel.stringValue  = "🐨"
            titleLabel.stringValue = "Drag Koala Project Here"
            subLabel.stringValue   = ".koala file  •  or click to browse"
            border.strokeColor = NSColor(white: 1, alpha: 0.25).cgColor
            layer?.backgroundColor = NSColor(white: 1, alpha: 0.05).cgColor
        case .working:
            iconLabel.stringValue  = "⏳"
            titleLabel.stringValue = "Converting…"
            subLabel.stringValue   = "Please wait"
            border.strokeColor = NSColor.systemBlue.cgColor
            layer?.backgroundColor = NSColor(red: 0, green: 0.3, blue: 0.8, alpha: 0.1).cgColor
        case .success:
            iconLabel.stringValue  = "✅"
            titleLabel.stringValue = "Export Complete"
            subLabel.stringValue   = "Check the folder next to your .koala file"
            border.strokeColor = NSColor.systemGreen.cgColor
            layer?.backgroundColor = NSColor(red: 0, green: 0.5, blue: 0.2, alpha: 0.1).cgColor
        case .error:
            iconLabel.stringValue  = "❌"
            titleLabel.stringValue = "Conversion Failed"
            subLabel.stringValue   = "See details below"
            border.strokeColor = NSColor.systemRed.cgColor
            layer?.backgroundColor = NSColor(red: 0.6, green: 0, blue: 0, alpha: 0.1).cgColor
        case .warning:
            iconLabel.stringValue  = "⚠️"
            titleLabel.stringValue = "Wrong File Type"
            subLabel.stringValue   = "Drop a .koala backup file"
            border.strokeColor = NSColor.systemOrange.cgColor
            layer?.backgroundColor = NSColor(red: 0.5, green: 0.3, blue: 0, alpha: 0.1).cgColor
        }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        border.strokeColor = NSColor.systemBlue.cgColor
        layer?.backgroundColor = NSColor(red: 0, green: 0.3, blue: 0.8, alpha: 0.1).cgColor
        return .copy
    }
    override func draggingExited(_ sender: NSDraggingInfo?) {
        border.strokeColor = NSColor(white: 1, alpha: 0.25).cgColor
        layer?.backgroundColor = NSColor(white: 1, alpha: 0.05).cgColor
    }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self]) as? [URL],
              let url = urls.first else { return false }
        controller?.processFile(url.path); return true
    }

    override func mouseUp(with event: NSEvent) { controller?.browseForFile() }
    override func mouseEntered(with event: NSEvent) {
        border.strokeColor = NSColor(white: 1, alpha: 0.5).cgColor
    }
    override func mouseExited(with event: NSEvent) {
        border.strokeColor = NSColor(white: 1, alpha: 0.25).cgColor
    }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach { removeTrackingArea($0) }
        addTrackingArea(NSTrackingArea(rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways], owner: self))
    }
}

// NSBezierPath → CGPath
extension NSBezierPath {
    var cgPath: CGPath {
        let p = CGMutablePath()
        var pts = [CGPoint](repeating: .zero, count: 3)
        for i in 0..<elementCount {
            switch element(at: i, associatedPoints: &pts) {
            case .moveTo:    p.move(to: pts[0])
            case .lineTo:    p.addLine(to: pts[0])
            case .curveTo:   p.addCurve(to: pts[2], control1: pts[0], control2: pts[1])
            case .closePath: p.closeSubpath()
            default:         break
            }
        }
        return p
    }
}
