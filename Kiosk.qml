// AWSO_BSPT — Kiosk.qml
// Touch-first kiosk interface: screensaver ← idle → any touch wakes instantly.
// Design: qml-bootstrap/Ionic tokens + Apple HIG (44px+ targets, no light
// weights, importance top-leading, instant feedback, dark default).
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "theme.js" as T

ApplicationWindow {
    id: root
    visible: true
    width: 1280; height: 800
    title: "AWSO Habit Kiosk"
    color: T.bg
    font.family: T.fontFamily
    font.pixelSize: T.body

    // kiosk: fullscreen + screensaver; desktop: normal window
    property bool isKiosk: Bridge.kiosk

    Component.onCompleted: {
        if (isKiosk) visibility = ApplicationWindow.FullScreen
        refreshAll()
    }

    // ---------------- Screensaver ----------------
    // After 60s idle → screensaver. ANY interaction returns to the app instantly.
    property int idleMs: 60000
    Timer {
        id: idleTimer; interval: root.idleMs; running: true; repeat: false
        onTriggered: screensaver.activate()
    }
    Rectangle {
        id: screensaver; anchors.fill: parent; z: 1000
        color: "#05070c"; opacity: 0; visible: opacity > 0
        function activate() { state = "on" }
        function wake() { state = "off"; idleTimer.restart() }
        state: "off"
        states: [
            State { name: "on"; PropertyChanges { target: screensaver
                opacity: 1 } }
        ]
        Behavior on opacity { NumberAnimation { duration: 300 } }
        Column {
            anchors.centerIn: parent; spacing: 20
            Text { text: "AWSO"; color: T.accent; font.pixelSize: 72
                font.weight: Font.DemiBold; anchors.horizontalCenter: parent.horizontalCenter }
            Text {
                text: Qt.formatDateTime(new Date(), "dddd, MMMM d   HH:mm")
                color: T.muted; font.pixelSize: 30; font.weight: Font.Normal
                anchors.horizontalCenter: parent.horizontalCenter
                // live clock while the kiosk sleeps
                Timer { interval: 1000; running: screensaver.state === "on"
                    repeat: true; onTriggered: parent.text = Qt.formatDateTime(
                        new Date(), "dddd, MMMM d   HH:mm") }
            }
            Text { text: "touch to wake"; color: T.border; font.pixelSize: T.small
                anchors.horizontalCenter: parent.horizontalCenter }
        }
        MouseArea { anchors.fill: parent; onClicked: screensaver.wake() }
        Keys.onPressed: screensaver.wake()
    }
    // any interaction inside the app resets the idle clock too
    MouseArea {
        id: globalInput; anchors.fill: parent; z: 900
        propagateComposedEvents: true
        onPressed: function(mouse) { idleTimer.restart(); mouse.accepted = false }
    }

    // ---------------- Data ----------------
    Connections { target: Bridge; function onDataChanged() { refreshAll() } }
    Connections { target: Bridge; function onChatChanged() { chatList.model = Bridge.chatModel() } }
    Timer { interval: 15000; running: true; repeat: true; onTriggered: refreshAll() }
    property var dueData: []
    property var deferredData: []
    property var habitsData: []
    property var statsData: []
    property var activityData: []
    function refreshAll() {
        dueData = Bridge.dueModel()
        deferredData = Bridge.deferredModel()
        habitsData = Bridge.habitsModel()
        statsData = Bridge.statsModel()
        eventLog.model = Bridge.activityModel()
        chatList.model = Bridge.chatModel()
        modeLabel.text = Bridge.mode().toUpperCase()
        modeLabel.color = T.modeColor(Bridge.mode())
        dueCount.text = String(dueData.length)
        idleTimer.restart()
    }

    // ---------------- Layout: chat docked left + main ----------------
    RowLayout {
        anchors.fill: parent; spacing: 0

        // ===== Left dock: agent chat =====
        Rectangle {
            id: chatDock; Layout.preferredWidth: 320; Layout.fillHeight: true
            color: T.panel; z: 5
            ColumnLayout {
                anchors.fill: parent; spacing: 0
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 56; color: T.header
                    Text { anchors.centerIn: parent; text: "💬 Agent"
                        color: T.text; font.pixelSize: T.h3; font.weight: Font.DemiBold } }
                ListView {
                    id: chatList; Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; spacing: 6; Layout.margins: 8
                    model: []
                    delegate: Rectangle {
                        width: chatList.width - 16
                        height: msgCol.implicitHeight + 20
                        radius: 10
                        color: modelData.sender === "human" ? T.card : T.bubbleAgent
                        border.color: T.border; border.width: 1
                        ColumnLayout { id: msgCol
                            anchors.fill: parent; anchors.margins: 10; spacing: 2
                            Text { text: modelData.sender; color: T.muted
                                font.pixelSize: T.small; font.weight: Font.Medium }
                            Text { text: modelData.text; color: T.text
                                Layout.fillWidth: true; wrapMode: Text.WordWrap
                                font.pixelSize: T.body; font.weight: Font.Normal } }
                    }
                    onCountChanged: positionViewAtEnd()
                    onModelChanged: positionViewAtEnd()
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 68; color: T.header
                    RowLayout { anchors.fill: parent; anchors.margins: 8; spacing: 8
                        TextField {
                            id: chatInput; Layout.fillWidth: true
                            placeholderText: "Message your agent…"
                            color: T.text; font.pixelSize: T.body
                            font.weight: Font.Normal
                            background: Rectangle { radius: 8; color: T.bg
                                border.color: T.border; border.width: 1 }
                            onAccepted: { Bridge.sendChat(text); clear() } }
                        Button {
                            text: "➤"; implicitWidth: 48; implicitHeight: 48
                            background: Rectangle { radius: 8; color: T.accent }
                            contentItem: Text { text: parent.text; color: "white"
                                font.pixelSize: 20; font.weight: Font.DemiBold
                                anchors.centerIn: parent }
                            onClicked: { Bridge.sendChat(chatInput.text)
                                chatInput.clear() } } } }
            }
        }

        // ===== Main area =====
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0

            // Header: mode pills + due count (importance top-leading)
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 64; color: T.header
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20
                    spacing: 16
                    Text { text: "AWSO Habit Kiosk"; color: T.text
                        font.pixelSize: T.h3; font.weight: Font.DemiBold }
                    Item { Layout.fillWidth: true }
                    Repeater {
                        model: ["green", "yellow", "red"]
                        delegate: Button {
                            required property string modelData
                            required property int index
                            text: ["🟢", "🟡", "🔴"][index] + " " + modelData.toUpperCase()
                            implicitHeight: 44; implicitWidth: 110
                            background: Rectangle {
                                radius: 22
                                color: Bridge.mode() === modelData
                                    ? T.modeColor(modelData) : T.card
                                border.width: 1; border.color: T.border }
                            contentItem: Text { text: parent.text; color: "white"
                                font.pixelSize: T.small; font.weight: Font.DemiBold
                                anchors.centerIn: parent }
                            onClicked: { Bridge.setMode(modelData); refreshAll() } } }
                    Item { width: 12 }
                    Text { id: dueCount; color: T.accent; font.pixelSize: T.h2
                        font.weight: Font.Bold }
                    Text { text: "due"; color: T.muted; font.pixelSize: T.small
                        font.weight: Font.Medium }
                }
            }

            // Tabs
            TabBar {
                id: tabbar; Layout.fillWidth: true
                background: Rectangle { color: T.panel }
                Repeater {
                    model: ["Today", "Habits", "Telemetry", "Console"]
                    delegate: TabButton {
                        required property string modelData
                        text: modelData; implicitHeight: 48
                        contentItem: Text { text: parent.text
                            color: parent.checked ? T.accent : T.muted
                            font.pixelSize: T.body
                            font.weight: parent.checked ? Font.DemiBold : Font.Medium
                            anchors.centerIn: parent } } }
            }

            StackLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                currentIndex: tabbar.currentIndex

                // ============ TODAY ============
                ScrollView {
                    GridLayout {
                        width: Math.max(parent.width, 900)
                        columns: 2; columnSpacing: 16; rowSpacing: 16
                        x: 16; y: 16

                        // Due card — most important, top-leading
                        Rectangle {
                            Layout.column: 0; Layout.row: 0
                            Layout.fillWidth: true; Layout.preferredHeight: 340
                            radius: 14; color: T.card
                            border.color: T.border; border.width: 1
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 16; spacing: 10
                                RowLayout { spacing: 10
                                    Text { text: "Due now"; color: T.text
                                        font.pixelSize: T.h2; font.weight: Font.DemiBold }
                                    Item { Layout.fillWidth: true }
                                    Text { id: modeLabel; text: "GREEN"
                                        font.pixelSize: T.small; font.weight: Font.Bold } }
                                ListView {
                                    id: dueList; Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true; spacing: 8; model: dueData
                                    delegate: Rectangle {
                                        width: dueList.width; height: 76; radius: 10
                                        color: T.bg; border.color: T.border; border.width: 1
                                        RowLayout { anchors.fill: parent
                                            anchors.margins: 10; spacing: 12
                                            Text { text: modelData.streak > 0
                                                    ? "🔥" + modelData.streak : "·"
                                                color: T.accent; font.pixelSize: T.h3
                                                font.weight: Font.Bold }
                                            ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                Text { text: modelData.name; color: T.text
                                                    font.pixelSize: T.body
                                                    font.weight: Font.Medium }
                                                Text { text: modelData.cadence + "  ·  effort "
                                                        + modelData.effort
                                                    color: T.muted; font.pixelSize: T.small } }
                                            Button {
                                                text: "✓ Check"; implicitWidth: 104
                                                implicitHeight: 44
                                                background: Rectangle { radius: 22
                                                    color: pressed ? T.positive : T.accent }
                                                contentItem: Text { text: parent.text
                                                    color: "white"; font.pixelSize: T.small
                                                    font.weight: Font.DemiBold
                                                    anchors.centerIn: parent }
                                                onClicked: { Bridge.check(modelData.id)
                                                    refreshAll() } } } }
                                    }
                                    Text { visible: dueList.count === 0
                                        text: "All clear — nothing due."
                                        color: T.muted; font.pixelSize: T.body; padding: 12 }
                                }
                            }
                        }

                        // Activity feed
                        Rectangle {
                            Layout.column: 1; Layout.row: 0
                            Layout.fillWidth: true; Layout.preferredHeight: 340
                            radius: 14; color: T.card
                            border.color: T.border; border.width: 1
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16
                                spacing: 10
                                Text { text: "Recent activity"; color: T.text
                                    font.pixelSize: T.h2; font.weight: Font.DemiBold }
                                ListView {
                                    id: eventLog; Layout.fillWidth: true
                                    Layout.fillHeight: true; clip: true; spacing: 4
                                    model: []
                                    delegate: RowLayout {
                                        width: eventLog.width; height: 42; spacing: 10
                                        Text { text: modelData.ts.slice(11, 19)
                                            color: T.muted; font.pixelSize: T.small
                                            font.weight: Font.Medium }
                                        Text { text: modelData.kind; color: T.accent
                                            font.pixelSize: T.small
                                            font.weight: Font.DemiBold }
                                        Text { Layout.fillWidth: true
                                            text: modelData.message; color: T.text
                                            elide: Text.ElideRight
                                            font.pixelSize: T.small } }
                                    Text { visible: eventLog.count === 0
                                        text: "No activity yet."; color: T.muted
                                        font.pixelSize: T.small; padding: 12 } } }
                        }

                        // Deferred habits strip
                        Rectangle {
                            Layout.column: 0; Layout.row: 1; Layout.columnSpan: 2
                            Layout.fillWidth: true; Layout.preferredHeight: 150
                            radius: 14; color: T.panel
                            border.color: T.border; border.width: 1
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16
                                spacing: 8
                                Text { text: "Deferred by capacity mode"; color: T.muted
                                    font.pixelSize: T.body; font.weight: Font.DemiBold }
                                ListView {
                                    id: deferList; Layout.fillWidth: true
                                    Layout.preferredHeight: 66; clip: true
                                    spacing: 6; orientation: ListView.Horizontal
                                    model: deferredData
                                    delegate: Rectangle {
                                        width: 230; height: 60; radius: 10
                                        color: T.card; border.color: T.border
                                        Column { anchors.centerIn: parent; spacing: 2
                                            Text { text: modelData.name; color: T.text
                                                font.pixelSize: T.small
                                                font.weight: Font.Medium
                                                anchors.horizontalCenter: parent.horizontalCenter }
                                            Text { text: "effort " + modelData.effort
                                                color: T.muted; font.pixelSize: T.small
                                                anchors.horizontalCenter: parent.horizontalCenter } } }
                                    Text { visible: deferList.count === 0
                                        text: "Nothing deferred."; color: T.muted
                                        font.pixelSize: T.small; padding: 12 }
                                }
                            }
                        }
                    }
                }

                // ============ HABITS ============
                Item {
                    ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 12
                        ListView {
                            id: habitsList; Layout.fillWidth: true
                            Layout.fillHeight: true; clip: true; spacing: 8
                            model: habitsData
                            delegate: Rectangle {
                                width: habitsList.width; height: 88; radius: 12
                                color: T.card; border.color: T.border; border.width: 1
                                ColumnLayout { anchors.fill: parent
                                    anchors.margins: 12; spacing: 4
                                    RowLayout { spacing: 10; Layout.fillWidth: true
                                        Text { text: modelData.name
                                                + (modelData.archived ? "  (archived)" : "")
                                            color: T.text; font.pixelSize: T.body
                                            font.weight: Font.DemiBold }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "🔥 " + modelData.streak
                                            color: T.accent; font.pixelSize: T.body
                                            font.weight: Font.Bold } }
                                    Text { text: modelData.cadence + "  ·  "
                                            + modelData.effort + " effort  ·  "
                                            + (modelData.category || "no category")
                                            + "  ·  due: " + (modelData.due ? "yes" : "no")
                                        color: T.muted; font.pixelSize: T.small } } }
                            Text { visible: habitsList.count === 0
                                text: "No habits yet — add one below, or ask your agent to."
                                color: T.muted; font.pixelSize: T.body; padding: 12 } }
                        RowLayout { spacing: 12
                            TextField { id: newName; Layout.fillWidth: true
                                placeholderText: "New habit name…"; color: T.text
                                font.pixelSize: T.body
                                background: Rectangle { radius: 8; color: T.card
                                    border.color: T.border; border.width: 1 } }
                            ComboBox { id: newCadence; implicitHeight: 44
                                model: ["daily", "weekdays", "weekly:7", "interval:6h"] }
                            ComboBox { id: newEffort; implicitHeight: 44
                                model: ["green", "yellow", "red"] }
                            Button { text: "＋ Add"; implicitHeight: 44
                                background: Rectangle { radius: 8; color: T.positive }
                                contentItem: Text { text: parent.text; color: "white"
                                    font.pixelSize: T.body; font.weight: Font.DemiBold
                                    anchors.centerIn: parent }
                                onClicked: {
                                    Bridge.addHabit(newName.text, newCadence.currentText,
                                        newEffort.currentText, "", "")
                                    newName.clear(); refreshAll() } } }
                    }
                }

                // ============ TELEMETRY ============
                ScrollView {
                    GridLayout {
                        width: Math.max(parent.width, 900)
                        columns: 2; columnSpacing: 16; rowSpacing: 16
                        x: 16; y: 16
                        Repeater {
                            model: statsData
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true; Layout.preferredHeight: 170
                                radius: 14; color: T.card
                                border.color: T.border; border.width: 1
                                ColumnLayout { anchors.fill: parent
                                    anchors.margins: 14; spacing: 6
                                    RowLayout { spacing: 10
                                        Text { text: modelData.key; color: T.text
                                            font.pixelSize: T.h3
                                            font.weight: Font.DemiBold }
                                        Item { Layout.fillWidth: true }
                                        Text { text: modelData.last_value + " " + modelData.unit
                                            color: T.accent; font.pixelSize: T.h2
                                            font.weight: Font.Bold } }
                                    Text { text: "min " + modelData.min + "    avg "
                                            + modelData.avg + "    max " + modelData.max
                                            + "    (" + modelData.count + " samples)"
                                        color: T.muted; font.pixelSize: T.small }
                                    Canvas {
                                        id: spark; Layout.fillWidth: true
                                        Layout.preferredHeight: 44
                                        onPaint: {
                                            var rs = Bridge.readingsModel(modelData.key)
                                            var ctx = getContext("2d"); ctx.reset()
                                            if (!rs || rs.length < 2) return
                                            var mn = Infinity, mx = -Infinity
                                            rs.forEach(function(r) {
                                                mn = Math.min(mn, r.value)
                                                mx = Math.max(mx, r.value) })
                                            var span = mx - mn || 1
                                            ctx.strokeStyle = T.accent; ctx.lineWidth = 2
                                            ctx.beginPath()
                                            for (var i = 0; i < rs.length; i++) {
                                                var x = width * i / (rs.length - 1)
                                                var y = height - 4
                                                    - (rs[i].value - mn) / span * (height - 8)
                                                if (i) ctx.lineTo(x, y)
                                                else ctx.moveTo(x, y) }
                                            ctx.stroke() }
                                    } }
                            }
                        }
                        Text { visible: statsData.length === 0
                            text: "No telemetry yet — your agent logs readings via 'habitctl log-reading'."
                            color: T.muted; font.pixelSize: T.body; padding: 16 } }
                }

                // ============ CONSOLE ============
                Item {
                    ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 12
                        Text { text: "Command line — same verbs your agent uses (add, due, check 1, mode yellow, …)"
                            color: T.muted; font.pixelSize: T.small }
                        ScrollView {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            TextArea {
                                id: consoleOut; readOnly: true
                                wrapMode: TextArea.WordWrap; color: T.text
                                font.family: T.monoFamily; font.pixelSize: T.small
                                background: Rectangle { color: T.bg; radius: 10
                                    border.color: T.border; border.width: 1 } }
                        }
                        RowLayout { spacing: 12
                            TextField { id: consoleIn; Layout.fillWidth: true
                                placeholderText: "command (e.g. status, due --json)"
                                color: T.text; font.family: T.monoFamily
                                font.pixelSize: T.small
                                background: Rectangle { radius: 8; color: T.card
                                    border.color: T.border; border.width: 1 }
                                onAccepted: runCmd() }
                            Button { text: "Run"; implicitHeight: 44
                                background: Rectangle { radius: 8; color: T.accent }
                                contentItem: Text { text: parent.text; color: "white"
                                    font.pixelSize: T.body; font.weight: Font.DemiBold
                                    anchors.centerIn: parent }
                                onClicked: runCmd() }
                        }
                        function runCmd() {
                            if (!consoleIn.text) return
                            consoleOut.append("habitctl> " + consoleIn.text)
                            consoleOut.append(Bridge.runCommand(consoleIn.text))
                            consoleIn.clear(); refreshAll()
                        }
                    }
                }
            }
        }
    }
