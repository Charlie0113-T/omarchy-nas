// Pure helpers for the Omarchy NAS bar widget: read the status snapshot that
// `omarchy-nas status` (and the local status page at /api/status) prints, turn
// it into what the bar and the panel show, and build the few commands the
// panel hands to a terminal. No QML here, so it can be tested with plain Node.

// Nerd Font glyphs, written as code points: editing tools can mangle raw
// private-use characters in source files.
function glyph(codePoint) {
  var n = codePoint - 0x10000
  return String.fromCharCode(0xD800 + (n >> 10), 0xDC00 + (n & 0x3FF))
}

var GLYPH = {
  nas: glyph(0xF08F3),          // md-nas
  disk: glyph(0xF02CA),         // md-harddisk
  diskOff: glyph(0xF104C),      // md-harddisk_remove
  alert: glyph(0xF05D6),        // md-alert_circle_outline
  pause: glyph(0xF03E4),        // md-pause
  play: glyph(0xF040A),         // md-play
  refresh: glyph(0xF0450),      // md-refresh
  copy: glyph(0xF018F),         // md-content_copy
  check: glyph(0xF012C),        // md-check
  web: glyph(0xF059F),          // md-web
  doctor: glyph(0xF04D9),       // md-stethoscope
  readOnly: glyph(0xF0341),     // md-lock_outline
  writable: glyph(0xF0CB6)      // md-pencil_outline
}

// The share names omarchy-nas accepts (core.valid_name). Anything else never
// reaches a command line.
var SHARE_NAME = /^[a-z][a-z0-9_-]{0,31}$/

var STRINGS = {
  "en": {
    title: "NAS",
    checking: "Checking…",
    missing: "omarchy-nas not installed",
    unconfigured: "Not set up",
    stopped: "Service stopped",
    online: "%1/%2 online",
    offline: "%1 offline",
    noShares: "No shares yet",
    paused: "Paused",
    copyAddress: "Copy the address of %1",
    pauseShare: "Pause %1",
    resumeShare: "Resume %1",
    statusPage: "Open status page",
    doctor: "Run diagnostics",
    refresh: "Refresh",
    setupHint: "Run omarchy-nas setup on this machine",
    readOnly: "read-only",
    writable: "read-write"
  },
  "zh-CN": {
    title: "NAS",
    checking: "正在检查…",
    missing: "未安装 omarchy-nas",
    unconfigured: "还没设置",
    stopped: "服务未运行",
    online: "%1/%2 在线",
    offline: "%1 个离线",
    noShares: "还没有共享",
    paused: "已暂停",
    copyAddress: "复制 %1 的地址",
    pauseShare: "暂停 %1",
    resumeShare: "继续 %1",
    statusPage: "打开状态页",
    doctor: "运行诊断",
    refresh: "刷新",
    setupHint: "在这台主机上运行 omarchy-nas setup",
    readOnly: "只读",
    writable: "可读写"
  },
  "zh-TW": {
    title: "NAS",
    checking: "正在檢查…",
    missing: "未安裝 omarchy-nas",
    unconfigured: "還沒設定",
    stopped: "服務未執行",
    online: "%1/%2 在線",
    offline: "%1 個離線",
    noShares: "還沒有共享",
    paused: "已暫停",
    copyAddress: "複製 %1 的位址",
    pauseShare: "暫停 %1",
    resumeShare: "繼續 %1",
    statusPage: "開啟狀態頁",
    doctor: "執行診斷",
    refresh: "重新整理",
    setupHint: "在這台主機上執行 omarchy-nas setup",
    readOnly: "唯讀",
    writable: "可讀寫"
  }
}

// Omarchy's language codes (~/.config/omarchy/language) to a catalog here.
function catalogFor(language) {
  var code = String(language || "en")
  if (STRINGS[code]) return STRINGS[code]
  if (code === "zh-HK" || code === "zh-MO") return STRINGS["zh-TW"]
  if (code.indexOf("zh") === 0) return STRINGS["zh-CN"]
  return STRINGS["en"]
}

// A string in the display language, English when that catalog lacks it,
// with %1, %2… filled from args.
function t(language, key, args) {
  var value = catalogFor(language)[key]
  if (typeof value !== "string") value = STRINGS["en"][key]
  if (typeof value !== "string") return key
  var list = args || []
  return value.replace(/%(\d)/g, function(match, digit) {
    var index = Number(digit) - 1
    return index < list.length ? String(list[index]) : match
  })
}

function clampInt(value, fallback, min, max) {
  var n = parseInt(String(value), 10)
  if (!isFinite(n)) n = fallback
  if (n < min) n = min
  if (n > max) n = max
  return n
}

// The snapshot as JSON, or null when it isn't one.
function parseSnapshot(raw) {
  try {
    var data = JSON.parse(String(raw || ""))
    return data && typeof data === "object" && !Array.isArray(data) ? data : null
  } catch (e) {
    return null
  }
}

// Disk sizes the way drives are sold: decimal units, one decimal under 10.
function formatBytes(bytes) {
  var n = Number(bytes)
  if (!isFinite(n) || n <= 0) return ""
  var units = ["B", "KB", "MB", "GB", "TB", "PB"]
  var i = 0
  while (n >= 1000 && i < units.length - 1) {
    n = n / 1000
    i++
  }
  var shown = n < 10 && i > 0 ? Math.round(n * 10) / 10 : Math.round(n)
  return shown + " " + units[i]
}

function share(raw) {
  var state = raw.enabled === false ? "paused" : (raw.state === "online" || raw.online === true ? "online" : "offline")
  if (raw.state === "paused") state = "paused"
  return {
    name: String(raw.name || ""),
    fs: String(raw.fs || ""),
    size: Number(raw.size || 0),
    readOnly: raw.mode !== "rw",
    state: state,
    url: String(raw.url || ""),
    error: state === "offline" ? String(raw.error || "") : ""
  }
}

/**
 * What the widget shows, from a snapshot (null: none could be read) and how
 * reading it went: { missing } when the omarchy-nas command isn't there,
 * { failed } with a message when it ran but gave nothing usable.
 *  state: checking | missing | unconfigured | stopped | degraded | ok
 *  level: dim | urgent | normal (the bar icon's colour)
 */
function summarize(snapshot, outcome) {
  var how = outcome || {}
  var result = {
    state: "checking", level: "dim", hostname: "", version: "",
    shares: [], online: 0, offline: 0, paused: 0, total: 0,
    errors: []
  }
  if (how.missing) {
    result.state = "missing"
    return result
  }
  if (!snapshot) {
    if (how.failed) {
      result.state = "degraded"
      result.level = "urgent"
      result.errors = [String(how.failed)]
    }
    return result
  }
  result.hostname = String(snapshot.hostname || "")
  result.version = String(snapshot.version || "")
  result.errors = (Array.isArray(snapshot.errors) ? snapshot.errors : []).map(String)
  var shares = Array.isArray(snapshot.shares) ? snapshot.shares : []
  for (var i = 0; i < shares.length; i++) {
    if (!shares[i] || typeof shares[i] !== "object") continue
    var s = share(shares[i])
    result.shares.push(s)
    result[s.state]++
  }
  result.total = result.shares.length

  if (snapshot.configured !== true) {
    result.state = "unconfigured"
    return result
  }
  if (snapshot.service_active !== true) {
    result.state = "stopped"
    result.level = "urgent"
    return result
  }
  if (result.offline > 0 || result.errors.length > 0) {
    result.state = "degraded"
    result.level = "urgent"
  } else {
    result.state = "ok"
    result.level = "normal"
  }
  return result
}

// One short line under the panel title, and the bar icon's tooltip.
function metaText(summary, language) {
  switch (summary.state) {
  case "checking": return t(language, "checking")
  case "missing": return t(language, "missing")
  case "unconfigured": return t(language, "unconfigured")
  case "stopped": return t(language, "stopped")
  }
  if (summary.total === 0) return t(language, "noShares")
  if (summary.offline > 0) return t(language, "offline", [summary.offline])
  if (summary.paused === summary.total) return t(language, "paused")
  return t(language, "online", [summary.online, summary.total - summary.paused])
}

function tooltipText(summary, language) {
  return t(language, "title") + " · " + metaText(summary, language)
}

// "12 TB · APFS · 只读": size, file system and mode, as data.
function shareDetail(item, language) {
  var parts = []
  var size = formatBytes(item.size)
  if (size) parts.push(size)
  if (item.fs) parts.push(item.fs.toUpperCase())
  parts.push(t(language, item.readOnly ? "readOnly" : "writable"))
  return parts.join(" · ")
}

// The command a share's pause/resume button runs in a terminal, or "" for a
// name omarchy-nas would never have accepted.
function toggleCommand(item) {
  if (!item || !SHARE_NAME.test(item.name)) return ""
  return "sudo omarchy-nas " + (item.state === "paused" ? "resume " : "pause ") + item.name
}

var DOCTOR_COMMAND = "omarchy-nas doctor"
