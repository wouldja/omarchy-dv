var TRANSPORTS = [
  { id: "rewind", label: "Rew" },
  { id: "play", label: "Play" },
  { id: "pause", label: "Pause" },
  { id: "ff", label: "FF" },
  { id: "prev", label: "Prev" },
  { id: "stop", label: "Stop" },
  { id: "next", label: "Next" },
  { id: "reverse", label: "Rev" }
]

function emptyStatus() {
  return {
    connected: false,
    vendor: "",
    model: "",
    transport: "missing",
    label: "NO DECK",
    statusText: "",
    timecode: "",
    live: false,
    sound: false,
    recording: false,
    capturePath: "",
    captureBytes: 0,
    message: ""
  }
}

function asBool(value) {
  return value === true || value === "true" || value === 1 || value === "1"
}

function parseStatus(raw) {
  var status = emptyStatus()
  var parsed = null
  try { parsed = JSON.parse(String(raw || "")) } catch (e) { return status }
  if (!parsed || typeof parsed !== "object") return status
  status.connected = asBool(parsed.connected)
  status.vendor = String(parsed.vendor || "")
  status.model = String(parsed.model || "")
  status.transport = String(parsed.transport || "missing")
  status.label = String(parsed.label || "NO DECK")
  status.statusText = String(parsed.statusText || "")
  status.timecode = String(parsed.timecode || "")
  status.live = asBool(parsed.live)
  status.sound = asBool(parsed.sound)
  status.recording = asBool(parsed.recording)
  status.capturePath = String(parsed.capturePath || "")
  status.captureBytes = Number(parsed.captureBytes) || 0
  status.message = String(parsed.message || "")
  return status
}

function fileName(path) {
  var text = String(path || "")
  var slash = text.lastIndexOf("/")
  return slash >= 0 ? text.slice(slash + 1) : text
}

function sizeText(bytes) {
  var n = Number(bytes) || 0
  if (n < 1024) return n + " B"
  if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB"
  return (Math.round((n / 1024 / 1024) * 10) / 10) + " MB"
}

function captureLine(status) {
  if (!status.capturePath) return ""
  var name = fileName(status.capturePath)
  if (status.recording) return "Recording " + name + "  ·  " + sizeText(status.captureBytes)
  return "Saved " + name
}

function heroMeta(status) {
  var name = status.model || (status.connected ? "DV deck" : "No deck")
  var parts = [name, status.label || ""]
  if (status.recording) parts.push("REC")
  else if (status.live) parts.push("LIVE")
  return parts.filter(function(part) { return part }).join(" · ")
}

function transportSelected(transport, id) {
  if (id === "play") return transport === "playing"
  if (id === "pause") return transport === "paused"
  if (id === "stop") return transport === "stopped"
  if (id === "rewind") return transport === "rewinding"
  if (id === "ff") return transport === "winding"
  if (id === "reverse") return transport === "reverse"
  return false
}
