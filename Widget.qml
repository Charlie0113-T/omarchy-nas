import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Omarchy NAS in the bar: a NAS icon whose colour says how the shares are
// (faded before omarchy-nas answers or while it isn't set up, the urgent
// colour when a share is offline or the service is down or reports an
// error), and a panel listing each share with the few actions
// worth a click. The widget only reads status. Anything that needs root
// opens a terminal showing the command, where the person types their own
// password; the shell never runs sudo.
Panel {
  id: nasWidget
  moduleName: "io.github.charlie0113-t.omarchy-nas"
  ipcTarget: "io.github.charlie0113-t.omarchy-nas"
  manageIpc: false

  readonly property var summary: nas.summary
  // Omarchy 4.0 has no shell translations (I18n came with 4.5): English there.
  readonly property string language: Commons.I18n ? Commons.I18n.language : ""
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property int cursorIndex: 0
  property bool cursorActive: false
  // The share whose address was just copied: its glyph turns into a check.
  property string copiedName: ""

  function tr(key, args) {
    return Model.t(nasWidget.language, key, args)
  }

  function shareGlyph(item) {
    if (!item) return Model.GLYPH.disk
    if (item.state === "offline") return Model.GLYPH.diskOff
    if (item.state === "paused") return Model.GLYPH.pause
    return Model.GLYPH.disk
  }

  function cursorShare() {
    var list = summary.shares
    return list.length > 0 ? list[Math.max(0, Math.min(cursorIndex, list.length - 1))] : null
  }

  function moveCursor(dy) {
    var count = summary.shares.length
    if (count === 0 || dy === 0) return
    cursorIndex = Math.max(0, Math.min(count - 1, cursorIndex + dy))
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
  }

  function copyAddress(item) {
    if (!item || !item.url) return
    Quickshell.execDetached(["wl-copy", item.url])
    copiedName = item.name
    copiedTimer.restart()
  }

  function toggleShare(item) {
    var command = Model.toggleCommand(item)
    if (command !== "") runInTerminal(command)
  }

  function runInTerminal(command) {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", command])
    close()
    nas.followUp()
  }

  function openStatusPage() {
    if (!nas.pageReachable) return
    Qt.openUrlExternally(nas.statusUrl)
    close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    nas.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Status {
    id: nas
    settings: nasWidget.settings
  }

  Timer {
    id: copiedTimer
    interval: 1500
    repeat: false
    onTriggered: nasWidget.copiedName = ""
  }

  // Quickshell's IpcHandler rather than the shell's ShellIpc, which Omarchy 4.0
  // doesn't have; omarchy-shell still reaches it through qs ipc.
  IpcHandler {
    target: nasWidget.ipcTarget
    function open(): void { nasWidget.open() }
    function close(): void { nasWidget.close() }
    function show(): void { nasWidget.open() }
    function hide(): void { nasWidget.close() }
    function toggle(): void { nasWidget.toggle() }
    function refresh(): string { nas.refresh(); return "ok" }
    function status(): string { return nasWidget.summary.state }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: nasWidget.bar
    text: Model.GLYPH.nas
    active: nasWidget.summary.level === "urgent"
    dimmed: nasWidget.summary.level === "dim"
    tooltipText: Model.tooltipText(nasWidget.summary, nasWidget.language)
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) nas.refresh()
      else nasWidget.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: nasWidget
    bar: nasWidget.bar
    open: nasWidget.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    // A direct child rather than the content component Omarchy 4.5 added, so
    // the panel opens on 4.0 too.
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!nasWidget.cursorActive) {
          nasWidget.cursorActive = true
          return
        }
        nasWidget.moveCursor(dy)
      }
      onActivateRequested: if (nasWidget.cursorActive) nasWidget.copyAddress(nasWidget.cursorShare())
      onCloseRequested: nasWidget.close()
      onTabRequested: function(direction) { nasWidget.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") nas.refresh()
        else if (t === "p" || t === "P") nasWidget.toggleShare(nasWidget.cursorShare())
        else if (t === "s" || t === "S") nasWidget.openStatusPage()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: nasWidget.tr("title")
            meta: Model.metaText(nasWidget.summary, nasWidget.language)
            detail: nasWidget.summary.hostname
            foreground: nasWidget.foreground
            fontFamily: nasWidget.fontFamily
            iconOpacity: nasWidget.summary.level === "dim" ? 0.5 : 1.0
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: Model.GLYPH.nas
                color: nasWidget.summary.level === "urgent" ? nasWidget.urgent : nasWidget.foreground
                font.family: nasWidget.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              PanelActionButton {
                iconText: Model.GLYPH.refresh
                tooltipText: nasWidget.tr("refresh")
                foreground: nasWidget.foreground
                fontFamily: nasWidget.fontFamily
                enabled: !nas.busy
                onClicked: nas.refresh()
              }
            }
          }

          // Problems, one line each, as omarchy-nas words them.
          Repeater {
            model: nasWidget.summary.errors.slice(0, 3)
            Text {
              required property var modelData
              width: column.width
              text: String(modelData)
              textFormat: Text.PlainText
              color: nasWidget.urgent
              font.family: nasWidget.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
            }
          }

          Text {
            visible: nasWidget.summary.state === "missing" || nasWidget.summary.state === "unconfigured"
            width: column.width
            text: nasWidget.tr("setupHint")
            textFormat: Text.PlainText
            color: nasWidget.dim
            font.family: nasWidget.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            id: shareColumn
            visible: nasWidget.summary.shares.length > 0
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: nasWidget.summary.shares
              ShareRow {
                required property var modelData
                required property int index
                width: shareColumn.width
                item: modelData
                rowIndex: index
              }
            }
          }

          PanelSeparator {
            visible: nasWidget.summary.state !== "missing"
            foreground: nasWidget.foreground
          }

          Row {
            visible: nasWidget.summary.state !== "missing"
            spacing: Style.space(6)

            PanelActionButton {
              visible: nas.pageReachable
              iconText: Model.GLYPH.web
              tooltipText: nasWidget.tr("statusPage")
              foreground: nasWidget.foreground
              fontFamily: nasWidget.fontFamily
              onClicked: nasWidget.openStatusPage()
            }

            PanelActionButton {
              iconText: Model.GLYPH.doctor
              tooltipText: nasWidget.tr("doctor")
              foreground: nasWidget.foreground
              fontFamily: nasWidget.fontFamily
              onClicked: nasWidget.runInTerminal(Model.DOCTOR_COMMAND)
            }
          }
        }
      }
    }
  }

  // One share: its state glyph (a check for a moment after its address is
  // copied), its name, and its size, file system and mode, or why it is
  // offline. A click copies its smb:// address; the trailing button pauses
  // or resumes it in a terminal.
  component ShareRow: CursorSurface {
    id: shareRow
    property var item: null
    property int rowIndex: 0
    readonly property bool copied: !!item && nasWidget.copiedName === item.name
    readonly property bool paused: !!item && item.state === "paused"
    readonly property bool offline: !!item && item.state === "offline"

    hasCursor: nasWidget.cursorActive && nasWidget.cursorIndex === rowIndex
    foreground: nasWidget.foreground
    implicitHeight: shareContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: shareRow.item && shareRow.item.url ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: nasWidget.setCursor(shareRow.rowIndex)
      onClicked: nasWidget.copyAddress(shareRow.item)

      PanelToolTip {
        visible: rowMouse.containsMouse && !!shareRow.item && shareRow.item.url !== ""
        text: nasWidget.tr("copyAddress", [shareRow.item ? shareRow.item.name : ""])
        fontFamily: nasWidget.fontFamily
      }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: shareRow.copied ? Model.GLYPH.check : nasWidget.shareGlyph(shareRow.item)
        color: shareRow.offline ? nasWidget.urgent : (shareRow.paused ? nasWidget.dim : nasWidget.foreground)
        font.family: nasWidget.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: shareContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: shareRow.item ? shareRow.item.name : ""
          textFormat: Text.PlainText
          color: shareRow.paused ? nasWidget.dim : nasWidget.foreground
          font.family: nasWidget.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          text: !shareRow.item ? ""
            : shareRow.offline && shareRow.item.error ? shareRow.item.error
            : shareRow.paused ? nasWidget.tr("paused")
            : Model.shareDetail(shareRow.item, nasWidget.language)
          textFormat: Text.PlainText
          color: shareRow.offline ? nasWidget.urgent : nasWidget.dim
          font.family: nasWidget.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        visible: Model.toggleCommand(shareRow.item) !== ""
        iconText: shareRow.paused ? Model.GLYPH.play : Model.GLYPH.pause
        tooltipText: nasWidget.tr(shareRow.paused ? "resumeShare" : "pauseShare", [shareRow.item ? shareRow.item.name : ""])
        foreground: nasWidget.foreground
        fontFamily: nasWidget.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onClicked: nasWidget.toggleShare(shareRow.item)
      }
    }
  }
}
