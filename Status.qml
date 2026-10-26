import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Reads the NAS status, never with sudo: first from the local status page
// omarchy-nas keeps running (one request, served from its own short cache),
// else by running `omarchy-nas status`, which prints the same snapshot. One
// read at a time, on the refresh interval, when the panel opens, and a few
// times after a command the panel started in a terminal.
Item {
  id: root

  property var settings: ({})

  readonly property int refreshIntervalSec: Model.clampInt(setting("refreshIntervalSec", 60), 60, 15, 3600)
  readonly property int statusPort: Model.clampInt(setting("statusPort", 8787), 8787, 1024, 65535)
  readonly property string statusUrl: "http://127.0.0.1:" + statusPort

  property var summary: Model.summarize(null, null)
  // The last read came from the status page, so the panel can offer to open it.
  property bool pageReachable: false
  readonly property bool busy: pageProcess.running || cliProcess.running || _waiting > 0

  // A read is done once its output streams have finished and the process
  // has exited, in whichever order they arrive.
  property int _waiting: 0
  // Which read is in flight: "page" (curl) or "cli" (omarchy-nas status).
  property string _stage: ""
  property string _pageOut: ""
  property int _pageExit: 0
  property string _cliOut: ""
  property string _cliErr: ""
  property int _cliExit: 0

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function refresh() {
    if (busy) return
    _pageOut = ""
    _pageExit = 0
    _stage = "page"
    _waiting = 2
    readTimeout.restart()
    pageProcess.command = ["curl", "-fsS", "--max-time", "4", statusUrl + "/api/status"]
    pageProcess.running = true
  }

  // A command started in a terminal takes as long as typing a password and
  // the command itself, so look again every few seconds for a minute.
  function followUp() {
    settleTimer.ticks = 0
    settleTimer.restart()
  }

  function pageDone() {
    _waiting -= 1
    if (_waiting > 0) return
    var snapshot = _pageExit === 0 ? Model.parseSnapshot(_pageOut) : null
    if (snapshot) {
      readTimeout.stop()
      _stage = ""
      pageReachable = true
      summary = Model.summarize(snapshot, null)
      return
    }
    pageReachable = false
    readCli()
  }

  function readCli() {
    _cliOut = ""
    _cliErr = ""
    _cliExit = 0
    _stage = "cli"
    _waiting = 3
    readTimeout.restart()
    // Exit 127 says the command isn't installed on this machine.
    cliProcess.command = ["sh", "-c", "command -v omarchy-nas >/dev/null 2>&1 || exit 127; exec omarchy-nas status"]
    cliProcess.running = true
  }

  function cliDone() {
    _waiting -= 1
    if (_waiting > 0) return
    readTimeout.stop()
    _stage = ""
    var snapshot = _cliExit === 0 ? Model.parseSnapshot(_cliOut) : null
    if (snapshot) summary = Model.summarize(snapshot, null)
    else if (_cliExit === 127) summary = Model.summarize(null, { missing: true })
    else summary = Model.summarize(null, { failed: firstLine(_cliErr) || firstLine(_cliOut) || ("omarchy-nas status: " + _cliExit) })
  }

  function firstLine(text) {
    var lines = String(text || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim()
      if (line !== "") return line.length > 140 ? line.substring(0, 137) + "…" : line
    }
    return ""
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // A process that never started reports nothing: a status page request
  // that went silent falls back to the command; a silent command is given up
  // rather than left busy for good, and the next tick tries again.
  Timer {
    id: readTimeout
    interval: 20000
    repeat: false
    onTriggered: {
      if (pageProcess.running || cliProcess.running) {
        restart()
        return
      }
      if (root._stage === "page") {
        root.pageReachable = false
        root.readCli()
        return
      }
      root._stage = ""
      root._waiting = 0
    }
  }

  Timer {
    id: settleTimer
    property int ticks: 0
    interval: 4000
    repeat: true
    running: false
    onTriggered: {
      ticks += 1
      root.refresh()
      if (ticks >= 15) running = false
    }
  }

  Process {
    id: pageProcess
    running: false
    command: []
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root._pageOut = String(text || "")
        root.pageDone()
      }
    }
    onExited: function(exitCode) {
      root._pageExit = exitCode
      root.pageDone()
    }
  }

  Process {
    id: cliProcess
    running: false
    command: []
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root._cliOut = String(text || "")
        root.cliDone()
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root._cliErr = String(text || "")
        root.cliDone()
      }
    }
    onExited: function(exitCode) {
      root._cliExit = exitCode
      root.cliDone()
    }
  }
}
