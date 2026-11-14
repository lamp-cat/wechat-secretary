// Renders the app icon PNGs for iconutil (no external assets).
import Cocoa
let out = CommandLine.arguments[1]
try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
for (name, px) in [("16x16",16),("16x16@2x",32),("32x32",32),("32x32@2x",64),("128x128",128),("128x128@2x",256),("256x256",256),("256x256@2x",512),("512x512",512),("512x512@2x",1024)] {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let inset = s * 0.09
    let r = NSRect(x: inset, y: inset, width: s - 2*inset, height: s - 2*inset)
    let path = NSBezierPath(roundedRect: r, xRadius: s*0.2, yRadius: s*0.2)
    NSGradient(starting: NSColor(calibratedRed: 0.16, green: 0.82, blue: 0.47, alpha: 1), ending: NSColor(calibratedRed: 0.02, green: 0.62, blue: 0.33, alpha: 1))!.draw(in: path, angle: -90)
    // speech bubble
    let b = NSRect(x: s*0.24, y: s*0.34, width: s*0.52, height: s*0.36)
    NSColor.white.setFill()
    NSBezierPath(roundedRect: b, xRadius: s*0.1, yRadius: s*0.1).fill()
    let tail = NSBezierPath(); tail.move(to: NSPoint(x: s*0.34, y: s*0.36)); tail.line(to: NSPoint(x: s*0.30, y: s*0.24)); tail.line(to: NSPoint(x: s*0.46, y: s*0.35)); tail.close(); tail.fill()
    NSColor(calibratedRed: 0.02, green: 0.62, blue: 0.33, alpha: 1).setFill()
    for i in 0..<3 { NSBezierPath(ovalIn: NSRect(x: s*(0.335 + Double(i)*0.12), y: s*0.485, width: s*0.07, height: s*0.07)).fill() }
    NSGraphicsContext.restoreGraphicsState()
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(out)/icon_\(name).png"))
}
