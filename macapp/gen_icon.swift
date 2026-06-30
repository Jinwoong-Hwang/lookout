// Lookout 앱 아이콘 생성기: 슬레이트→청록 그라데이션 + 눈(지켜봄) 심볼.
import Cocoa

let iconset = "Lookout.iconset"
try? FileManager.default.createDirectory(atPath: iconset, withIntermediateDirectories: true)

func draw(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: px, height: px)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext

    let p = CGFloat(px)
    let inset = p * 0.055
    let r = NSRect(x: inset, y: inset, width: p - 2 * inset, height: p - 2 * inset)
    let bg = NSBezierPath(roundedRect: r, xRadius: p * 0.225, yRadius: p * 0.225)
    bg.addClip()
    NSGradient(colors: [
        NSColor(srgbRed: 0.137, green: 0.161, blue: 0.220, alpha: 1),  // slate #232b38
        NSColor(srgbRed: 0.176, green: 0.831, blue: 0.749, alpha: 1),  // teal #2dd4bf
    ])!.draw(in: r, angle: -50)

    let cx = p / 2, cy = p / 2
    // 눈 흰자 (가로 아몬드형: 두 타원 교차 근사 → 넓은 타원)
    let eyeW = p * 0.66, eyeH = p * 0.40
    let eye = NSBezierPath(ovalIn: NSRect(x: cx - eyeW/2, y: cy - eyeH/2, width: eyeW, height: eyeH))
    NSColor(srgbRed: 0.945, green: 0.961, blue: 0.984, alpha: 1).setFill()   // #f1f5fb
    eye.fill()
    ctx.saveGState()
    eye.addClip()
    // 홍채
    let ir = p * 0.155
    NSColor(srgbRed: 0.055, green: 0.455, blue: 0.565, alpha: 1).setFill()   // deep teal #0e7490
    NSBezierPath(ovalIn: NSRect(x: cx - ir, y: cy - ir, width: 2*ir, height: 2*ir)).fill()
    // 동공
    let pr = p * 0.072
    NSColor(srgbRed: 0.067, green: 0.082, blue: 0.110, alpha: 1).setFill()   // #11151c
    NSBezierPath(ovalIn: NSRect(x: cx - pr, y: cy - pr, width: 2*pr, height: 2*pr)).fill()
    ctx.restoreGState()
    // 하이라이트
    let hr = p * 0.030
    NSColor.white.setFill()
    NSBezierPath(ovalIn: NSRect(x: cx - ir*0.55, y: cy + ir*0.35, width: 2*hr, height: 2*hr)).fill()
    // 눈 윤곽선
    NSColor(srgbRed: 0.067, green: 0.082, blue: 0.110, alpha: 0.85).setStroke()
    eye.lineWidth = max(1, p * 0.012); eye.stroke()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

func save(_ px: Int, _ name: String) {
    try! draw(px).write(to: URL(fileURLWithPath: "\(iconset)/\(name)"))
}

for base in [16, 32, 128, 256, 512] {
    save(base, "icon_\(base)x\(base).png")
    save(base * 2, "icon_\(base)x\(base)@2x.png")
}
print("Lookout iconset 생성 완료")
