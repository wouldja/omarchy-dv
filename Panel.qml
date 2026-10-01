import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.wouldja.dv"
  ipcTarget: "io.github.wouldja.dv"

  property bool connected: false
  property string deckModel: ""
  property string transport: "missing"
  property string label: "NO DECK"
  property string statusText: ""
  property string timecode: ""
  property bool live: false
  property bool sound: false
  property bool recording: false
  property string capturePath: ""
  property int captureBytes: 0
  property string message: ""
  property bool loaded: false

  property string focusSection: "transport"
  property int selectedIndex: 1
  property bool cursorActive: false

  readonly property string iconGlyph: String.fromCodePoint(0xF03D)
  readonly property string deckBin: {
    var url = String(Qt.resolvedUrl("deck.py"))
    return url.indexOf("file://") === 0 ? url.slice(7) : url
  }

  function deckPath() {
    var path = root.deckBin
    if (path.indexOf("%") !== -1) {
      try { path = decodeURIComponent(path) } catch (e) {}
    }
    return path
  }

  function applyParsed(parsed) {
    if (!parsed) return
    root.connected = parsed.connected
    root.deckModel = parsed.model
    root.transport = parsed.transport
    root.label = parsed.label
    root.statusText = parsed.statusText
    root.timecode = parsed.timecode
    root.live = parsed.live
    root.sound = parsed.sound
    root.recording = parsed.recording
    root.capturePath = parsed.capturePath
    root.captureBytes = parsed.captureBytes
    root.message = parsed.message
    root.loaded = true
  }

  function run(args) {
    if (proc.running) return
    proc.command = ["python3", deckPath()].concat(args)
    proc.running = true
  }

  function refresh() {
    run(["status"])
  }

  function moveCursor(delta) {
    var order = ["transport", "live", "capture", "sound"]
    var index = order.indexOf(focusSection)
    if (index < 0) index = 0
    var next = index + delta
    if (next < 0) next = 0
    if (next > order.length - 1) next = order.length - 1
    focusSection = order[next]
    if (focusSection === "transport") {
      if (selectedIndex < 0) selectedIndex = 0
    } else {
      selectedIndex = 0
    }
  }

  function moveCursorH(delta) {
    if (focusSection !== "transport") return
    var next = selectedIndex + delta
    if (next < 0) next = 0
    if (next > Model.TRANSPORTS.length - 1) next = Model.TRANSPORTS.length - 1
    selectedIndex = next
  }

  function activateCursor() {
    if (focusSection === "live") { run(["live", "toggle"]); return }
    if (focusSection === "capture") { run(["record", "toggle"]); return }
    if (focusSection === "sound") { run(["sound", "toggle"]); return }
    if (selectedIndex >= 0 && selectedIndex < Model.TRANSPORTS.length)
      run(["transport", Model.TRANSPORTS[selectedIndex].id])
  }

  Component.onCompleted: refresh()

  onOpenedChanged: if (opened) refresh()

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: if (!proc.running) root.refresh()
  }

  Process {
    id: proc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyParsed(Model.parseStatus(text))
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.iconGlyph
    tooltipText: root.deckModel
      ? "DV  " + root.deckModel + "  " + root.label
      : "DV"
    onPressed: function(b) {
      if (b === Qt.RightButton) root.run(["live", "toggle"])
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(420))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.moveCursorH(dx)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: panelColumn
        width: parent.width
        spacing: Style.space(6)

        PanelHero {
          width: parent.width
          title: "DV"
          iconSize: Style.font.title
          detail: "1.3.0"
          meta: Model.heroMeta({
            connected: root.connected,
            model: root.deckModel,
            label: root.label,
            live: root.live,
            recording: root.recording
          })
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          iconComponent: heroIcon
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.timecode || root.statusText || (root.loaded ? "No FireWire deck" : "Checking the deck")
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          elide: Text.ElideRight
        }

        Text {
          visible: root.message !== ""
          width: parent.width
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          text: root.message
          color: Qt.darker(root.bar.foreground, 1.4)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        Grid {
          id: transportGrid
          width: parent.width
          columns: 4
          columnSpacing: Style.space(6)
          rowSpacing: Style.space(4)
          readonly property real cellWidth: (width - columnSpacing * 3) / 4

          Repeater {
            model: Model.TRANSPORTS

            Button {
              required property var modelData
              required property int index
              width: transportGrid.cellWidth
              text: modelData.label
              fontSize: Style.font.caption
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              horizontalPadding: Style.spacing.sm
              verticalPadding: Style.space(4)
              bordered: true
              selected: Model.transportSelected(root.transport, modelData.id)
              hasCursor: root.cursorActive && root.focusSection === "transport" && root.selectedIndex === index
              onClicked: root.run(["transport", modelData.id])
              onHovered: function(isHovered) {
                if (!isHovered) return
                root.cursorActive = true
                root.focusSection = "transport"
                root.selectedIndex = index
              }
            }
          }
        }

        Button {
          width: parent.width
          text: root.recording ? "Stop capture" : "Capture"
          fontSize: Style.font.caption
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          horizontalPadding: Style.spacing.sm
          verticalPadding: Style.space(4)
          bordered: true
          selected: root.recording
          hasCursor: root.cursorActive && root.focusSection === "capture"
          onClicked: root.run(["record", "toggle"])
          onHovered: function(isHovered) {
            if (!isHovered) return
            root.cursorActive = true
            root.focusSection = "capture"
            root.selectedIndex = 0
          }
        }

        Text {
          visible: root.capturePath !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: Model.captureLine({
            recording: root.recording,
            capturePath: root.capturePath,
            captureBytes: root.captureBytes
          })
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Button {
          width: parent.width
          text: root.live ? "Close preview" : "Live preview"
          fontSize: Style.font.caption
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          horizontalPadding: Style.spacing.sm
          verticalPadding: Style.space(4)
          bordered: true
          selected: root.live
          hasCursor: root.cursorActive && root.focusSection === "live"
          onClicked: root.run(["live", "toggle"])
          onHovered: function(isHovered) {
            if (!isHovered) return
            root.cursorActive = true
            root.focusSection = "live"
            root.selectedIndex = 0
          }
        }

        CompactToggle {
          width: parent.width
          label: "Preview sound"
          checked: root.sound
          hasCursor: root.cursorActive && root.focusSection === "sound"
          onClicked: root.run(["sound", "toggle"])
          onHovered: function(isHovered) {
            if (!isHovered) return
            root.cursorActive = true
            root.focusSection = "sound"
            root.selectedIndex = 0
          }
        }
      }
    }
  }

  Component {
    id: heroIcon
    Text {
      textFormat: Text.PlainText
      text: root.iconGlyph
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.title
    }
  }

  component CompactToggle: BorderSurface {
    id: toggleRow
    required property string label
    property bool checked: false
    property bool hasCursor: false
    signal clicked()
    signal hovered(bool isHovered)

    implicitHeight: Style.space(32)
    radius: Style.cornerRadius
    color: "transparent"
    borderSpec: Border.controlSpec(hasCursor || mouse.containsMouse ? "hover-cursor" : "normal", root.bar.foreground, Color.accent)

    Row {
      anchors.fill: parent
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(4)
      spacing: Style.space(6)

      Text {
        textFormat: Text.PlainText
        text: toggleRow.label
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        elide: Text.ElideRight
        width: parent.width - switchControl.implicitWidth - parent.spacing
        anchors.verticalCenter: parent.verticalCenter
      }

      ToggleSwitch {
        id: switchControl
        checked: toggleRow.checked
        interactive: false
        trackHeight: Style.space(16)
        foreground: root.bar.foreground
        accent: Color.accent
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      id: mouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: toggleRow.hovered(true)
      onExited: toggleRow.hovered(false)
      onClicked: toggleRow.clicked()
    }
  }
}
