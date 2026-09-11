// AWSO_BSPT kiosk theme tokens — Ionic/qml-bootstrap language on Apple HIG rules.
// HIG: mobile default 17pt body (≈22px), minimum 11pt; avoid light weights;
// 44px+ touch targets; importance top-leading; dark default for kiosk glare.
var bg       = "#0e1420"
var panel    = "#131a2a"
var card     = "#1a2334"
var header   = "#0a0f18"
var border   = "#2a3a56"
var text     = "#eef2f8"
var muted    = "#8a9ab2"
var accent   = "#4da3ff"
var positive = "#2ecc71"
var bubbleAgent = "#20304d"
var fontFamily = "Segoe UI, Noto Sans, sans-serif"
var monoFamily = "Consolas, Courier New, monospace"
var hover    = "#26334d"        // interactive raised state (hover)
var pressed  = "#0d1424"        // pressed / sunken state
var small  = 15   // 11pt floor — never smaller (HIG minimum)
var body   = 21   // ~16pt
var h3     = 26   // ~20pt
var h2     = 32   // ~24pt
function modeColor(m) {
    return m === "green" ? "#2ecc71" : (m === "yellow" ? "#f1c40f" : "#e74c3c")
}
