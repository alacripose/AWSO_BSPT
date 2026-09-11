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
    property int updatesNew: 0          // unviewed releases (badge on tab pill)
    property var openHabit: null        // detail popup state: habit_detail dict
    property var releasesData: []       // Updates tab: changelog bubbles
    function releasesModel_newCount() {
        // unviewed = rows flagged new by the core (above last-viewed)
        var n = 0
        for (var i = 0; i < releasesData.length; i++)
            if (releasesData[i].new) n++
        return n
    }

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

    // Console verb runner — MUST live at root scope: QML resolves unqualified
    // names against the root object + document ids only, so a function on an
    // inner ColumnLayout is invisible to TextField/Button handlers.
    function runCmd() {
        if (!consoleIn.text) return
        consoleOut.append("habitctl> " + consoleIn.text)
        consoleOut.append(Bridge.runCommand(consoleIn.text))
        consoleIn.clear()
        refreshAll()
    }

    // ---------------- Fleet drawer (hamburger) ----------------
    // Same rule as runCmd: these are called from handlers inside the Drawer,
    // so they MUST live at root scope.
    property var fleetRows: []
    property string fleetFilter: ""
    function rebuildFleet() {
        var inv = Bridge.fleetModel()
        var rows = []
        // agents — delegate CLIs found on PATH
        for (var i = 0; i < inv.agents.length; i++)
            rows.push({ section: "AGENTS", name: inv.agents[i].name,
                        detail: inv.agents[i].detail })
        if (inv.agents.length === 0)
            rows.push({ section: "AGENTS", name: "(none installed)", detail: "" })
        // models — policy recommendation first, then the live inventory
        var rec = inv.recommended_model
        rows.push({ section: "MODELS",
                    name: rec.name + "  ★ recommended",
                    detail: rec.note + "  ·  fits " + rec.ram_gb + " GB RAM" })
        for (i = 0; i < inv.models.length; i++)
            rows.push({ section: "MODELS", name: inv.models[i].name,
                        detail: inv.models[i].detail })
        // skills — grouped by their installed category folder
        for (i = 0; i < inv.skills.length; i++) {
            var s = inv.skills[i]
            rows.push({ section: s.category ? s.category.toUpperCase() : "SKILLS",
                        name: s.name, detail: s.description })
        }
        // credits — upstream sources (license attribution)
        for (i = 0; i < inv.credits.length; i++)
            rows.push({ section: "CREDITS", name: inv.credits[i].label,
                        detail: inv.credits[i].url })
        fleetRows = rows
        fleetCounts.text = inv.counts.agents + " agents · "
            + inv.counts.models + " models · "
            + inv.counts.skills + " skills"
        applyFleetFilter()
    }
    function applyFleetFilter() {
        var q = fleetFilter.toLowerCase()
        if (!q) { fleetList.model = fleetRows; return }
        var out = []
        for (var i = 0; i < fleetRows.length; i++)
            if (fleetRows[i].name.toLowerCase().indexOf(q) >= 0
                || fleetRows[i].detail.toLowerCase().indexOf(q) >= 0)
                out.push(fleetRows[i])
        fleetList.model = out
    }

    // ---------------- Fleet drawer (hamburger panel) ----------------
    Drawer {
        id: fleetDrawer
        objectName: "fleetDrawer"
        width: Math.min(root.width * 0.42, 480); height: root.height
        edge: Qt.RightEdge
        // skills/PATH inventory can change between opens — rescan on open,
        // never on the 15s cycle (fleet_scan is sub-100ms but not free)
        onOpened: rebuildFleet()

        background: Rectangle { color: T.panel; border.color: T.border
            border.width: 1 }  // right-edge border, rest soft-drawn by drawer

        ColumnLayout {
            anchors.fill: parent; spacing: 0

            // header: title + counts + close
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 60
                color: T.header
                RowLayout { anchors.fill: parent; anchors.margins: 14
                    spacing: 10
                    Text { text: "☰ Fleet"; color: T.text
                        font.pixelSize: T.h3; font.weight: Font.DemiBold }
                    Item { Layout.fillWidth: true }
                    Text { id: fleetCounts; text: ""
                        color: T.muted; font.pixelSize: T.small
                        font.weight: Font.Medium } } }

            // live filter — 44px target (HIG)
            TextField {
                id: fleetSearch
                Layout.fillWidth: true; Layout.preferredHeight: 44
                Layout.margins: 10
                placeholderText: "Filter skills, agents, models…"
                color: T.text; font.pixelSize: T.small
                font.family: T.monoFamily; verticalAlignment: Text.AlignVCenter
                background: Rectangle { radius: 8; color: T.card
                    border.color: fleetSearch.activeFocus ? T.accent : T.border
                    border.width: 1 }
                onTextChanged: { root.fleetFilter = text
                    root.applyFleetFilter() }
            }

            // sectioned list — ListView virtualizes 159 skills; sections
            // keep the hamburger scannable without a tree control
            ListView {
                id: fleetList
                objectName: "fleetList"
                Layout.fillWidth: true; Layout.fillHeight: true
                Layout.margins: 10
                clip: true; spacing: 2
                model: []
                section.property: "section"
                section.delegate: Rectangle {
                    width: fleetList.width; height: 36
                    color: T.card
                    Text { anchors.fill: parent; anchors.leftMargin: 14
                        verticalAlignment: Text.AlignVCenter
                        text: section; color: T.accent
                        font.pixelSize: T.small; font.weight: Font.Bold
                        font.family: T.monoFamily }
                }
                delegate: Rectangle {
                    required property var modelData
                    width: fleetList.width
                    // fixed 2-state height: detail row wraps to 2 lines max
                    height: modelData.detail.length > 0
                        ? (modelData.detail.length > 60 ? 72 : 56) : 44
                    radius: 8
                    color: fleetHover.containsMouse ? T.hover : T.bg
                    border.color: T.border; border.width:  1
                    MouseArea { id: fleetHover
                        anchors.fill: parent; hoverEnabled: true }
                    ColumnLayout { anchors.fill: parent
                        anchors.margins: 10; spacing: 2
                        Text { text: modelData.name; color: T.text
                            font.pixelSize: T.small
                            font.weight: Font.Medium
                            Layout.fillWidth: true
                            elide: Text.ElideRight }
                        Text { visible: modelData.detail.length > 0
                            text: modelData.detail; color: T.muted
                            font.pixelSize: T.small
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight }
                    }
                }
                Text { visible: fleetList.count === 0
                    text: "No matches."; color: T.muted
                    font.pixelSize: T.small; padding: 12 }
            }
        }
    }

    // ---------------- Task detail popup ----------------
    // A task row click opens the full audit trail: description, checkins with
    // source attribution (human vs agent:awso-agentd), related events.
    Rectangle {
        id: detailOverlay; z: 500
        anchors.fill: parent; color: "#000000b0"; visible: openHabit !== null
        MouseArea { anchors.fill: parent  // click backdrop to close
            onClicked: openHabit = null }
        Rectangle {
            visible: openHabit !== null
            width: Math.min(parent.width - 80, 640); height: Math.min(parent.height - 80, 520)
            anchors.centerIn: parent; radius: 16; color: T.panel
            border.color: T.border; border.width: 1
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 20; spacing: 12
                RowLayout { Layout.fillWidth: true; spacing: 10
                    Text { text: openHabit ? openHabit.name : ""
                        color: T.text; font.pixelSize: T.h2
                        font.weight: Font.DemiBold; Layout.fillWidth: true
                        elide: Text.ElideRight }
                    Rectangle { radius: 14; color: T.card; border.color: T.border
                        border.width: 1; Layout.preferredHeight: 28
                        Layout.preferredWidth: streakLabel.implicitWidth + 24
                        Text { id: streakLabel; anchors.centerIn: parent
                            text: openHabit ? "🔥 " + openHabit.streak : ""
                            color: T.accent; font.pixelSize: T.small
                            font.weight: Font.Bold } }
                    Button { text: "✕"; implicitWidth: 40; implicitHeight: 40
                        background: Rectangle { radius: 20; color: T.card
                            border.color: T.border; border.width: 1 }
                        contentItem: Text { text: "✕"; color: T.text
                            font.pixelSize: T.body; anchors.centerIn: parent }
                        onClicked: openHabit = null }
                }
                Text { visible: openHabit && openHabit.description.length > 0
                    text: openHabit ? openHabit.description : ""
                    color: T.muted; font.pixelSize: T.small
                    wrapMode: Text.WordWrap; Layout.fillWidth: true }
                Text { visible: openHabit
                    text: openHabit ? (openHabit.cadence + "  ·  " + openHabit.effort
                        + " effort  ·  " + (openHabit.category || "no category")
                        + "  ·  due: " + (openHabit.due ? "yes" : "no")) : ""
                    color: T.muted; font.pixelSize: T.small }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1
                    color: T.border }
                Text { text: "Check-in history — who did what"
                    color: T.text; font.pixelSize: T.h3; font.weight: Font.DemiBold }
                // ListView manages its own contentHeight — no parent/child
                // size binding chain (those overflowed the JS stack when
                // combined with the Updates-tab implicit sizes)
                ListView {
                    id: checkinList
                    Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; spacing: 8
                    model: openHabit ? openHabit.checkins : []
                    delegate: Rectangle {
                        required property var modelData
                        width: checkinList.width
                        // fixed 2-state height: 64 normal, 84 with note
                        height: modelData.note.length > 0 ? 84 : 64
                        radius: 10
                        color: modelData.source === "human"
                            ? T.card : "#16324a"
                        border.color: T.border; border.width: 1
                        ColumnLayout { id: ciCol
                            anchors.fill: parent; anchors.margins: 10
                            spacing: 2
                            RowLayout { spacing: 8; Layout.fillWidth: true
                                Text { text: modelData.source === "human"
                                        ? "👤" : "🤖"
                                    font.pixelSize: T.small }
                                Text {
                                    // agent rows show their routine name
                                    text: modelData.source
                                    color: modelData.source === "human"
                                        ? T.text : T.accent
                                    font.pixelSize: T.small
                                    font.weight: Font.DemiBold }
                                Item { Layout.fillWidth: true }
                                Text { text: modelData.ts.replace("T", " ")
                                    color: T.muted; font.pixelSize: T.small }
                            }
                            Text { visible: modelData.note.length > 0
                                text: modelData.note; color: T.muted
                                font.pixelSize: T.small
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true }
                        }
                    }
                    Text { visible: openHabit && openHabit.checkins.length === 0
                        text: "No checkins yet."
                        color: T.muted; font.pixelSize: T.small; padding: 8 }
                }
            }
        }
    }

    // ---------------- Data ----------------
    Connections { target: Bridge; function onDataChanged() { refreshAll() } }
    Connections { target: Bridge; function onChatChanged() { chatList.model = Bridge.chatModel() } }
    // refresh timer: drives refreshAll + presence re-evaluation.
    // poll() emits dataChanged, which triggers refreshAll via onDataChanged —
    // so poll must ONLY be called here (never inside refreshAll: recursion).
    Timer { interval: 15000; running: true; repeat: true
        onTriggered: { Bridge.poll() } }
    property var dueData: []
    property var deferredData: []
    property var habitsData: []
    property var statsData: []
    property var activityData: []
    property var sosData: []
    function refreshAll() {
        dueData = Bridge.dueModel()
        deferredData = Bridge.deferredModel()
        habitsData = Bridge.habitsModel()
        statsData = Bridge.statsModel()
        eventLog.model = Bridge.activityModel()
        chatList.model = Bridge.chatModel()
        sosData = Bridge.sosModel()
        releasesData = Bridge.releasesModel()
        updatesNew = releasesModel_newCount()
        Bridge.syncMode()  // pick up external mode changes (agent CLI writes)
        // NOTE: no Bridge.poll() here — it emits dataChanged -> refreshAll
        // -> poll() -> infinite recursion. The refresh Timer calls poll().
        modeLabel.text = Bridge.mode.toUpperCase()
        modeLabel.color = T.modeColor(Bridge.mode)
        dueCount.text = String(dueData.length)
        sosCount.text = sosData.length > 0 ? "🆘 " + sosData.length : ""
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
                    RowLayout { anchors.fill: parent; anchors.leftMargin: 14
                        anchors.rightMargin: 14; spacing: 8
                        Text { text: "💬 Agent"; color: T.text
                            font.pixelSize: T.h3; font.weight: Font.DemiBold }
                        Item { Layout.fillWidth: true }
                        // presence: agentd (agentd.py) heartbeats into meta;
                        // GUI stays a pure view and just reads the stamp
                        Rectangle {
                            radius: 12; Layout.preferredHeight: 24
                            Layout.preferredWidth: statusText.implicitWidth + 20
                            color: Bridge.agentOnline ? "#123a24" : "#3a2028"
                            border.color: Bridge.agentOnline ? T.positive : "#e74c3c"
                            border.width: 1
                            Text { id: statusText
                                anchors.centerIn: parent
                                text: Bridge.agentOnline ? "● ONLINE" : "○ OFFLINE"
                                color: Bridge.agentOnline ? T.positive : "#e74c3c"
                                font.pixelSize: T.small; font.weight: Font.DemiBold }
                        }
                    } }
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
                    id: headerRow
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20
                    spacing: 12
                    // hamburger — opens the fleet/skills Drawer (44px HIG target)
                    Button {
                        id: menuBtn
                        text: "☰"; implicitWidth: 44; implicitHeight: 44
                        background: Rectangle { radius: 22; color: menuHover.containsMouse ? T.hover : T.card
                            border.color: T.border; border.width: 1 }
                        contentItem: Text { text: parent.text; color: T.text
                            font.pixelSize: T.body; font.weight: Font.DemiBold
                            anchors.centerIn: parent }
                        MouseArea { id: menuHover
                            anchors.fill: parent; hoverEnabled: true }
                        onClicked: { fleetDrawer.open(); rebuildFleet() }
                    }
                    // Title yields space first (elides before pills ever squeeze)
                    Text { id: headerTitle; text: "AWSO Habit Kiosk"; color: T.text
                        font.pixelSize: T.h3; font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Layout.preferredWidth: headerTitle.implicitWidth
                        Layout.fillWidth: true
                        Layout.maximumWidth: headerTitle.implicitWidth }
                    Text {
                        // build stamp: version + git hash (+• when tree dirty)
                        text: Bridge.buildStamp; color: T.muted
                        font.pixelSize: T.small; font.weight: Font.Medium
                        font.family: T.monoFamily
                        Layout.minimumWidth: implicitWidth }
                    Item { Layout.preferredWidth: 12; Layout.fillWidth: true
                        Layout.minimumWidth: 0 }
                    Repeater {
                        model: ["green", "yellow", "red"]
                        delegate: Button {
                            required property string modelData
                            required property int index
                            id: modeBtn
                            text: ["🟢", "🟡", "🔴"][index] + " " + modelData.toUpperCase()
                            implicitHeight: 44
                            implicitWidth: Math.max(84, modeBtn.implicitContentWidth + 28)
                            Layout.preferredWidth: modeBtn.implicitWidth
                            background: Rectangle {
                                radius: 22
                                // Bridge.mode is a notify-property now —
                                // re-evaluates on every setMode
                                color: Bridge.mode === modelData
                                    ? T.modeColor(modelData) : T.card
                                border.width: 1; border.color: T.border }
                            contentItem: Text { text: parent.text; color: "white"
                                font.pixelSize: T.small; font.weight: Font.DemiBold
                                horizontalAlignment: Text.AlignHCenter
                                anchors.centerIn: parent }
                            onClicked: { Bridge.setMode(modelData); refreshAll() } } }
                    Item { Layout.preferredWidth: 12; Layout.minimumWidth: 12 }
                    Text { id: dueCount; color: T.accent; font.pixelSize: T.h2
                        font.weight: Font.Bold
                        Layout.minimumWidth: implicitWidth }
                    Text { text: "due"; color: T.muted; font.pixelSize: T.small
                        font.weight: Font.Medium
                        Layout.minimumWidth: implicitWidth }
                    Text { id: sosCount; color: "#e74c3c"; font.pixelSize: T.body
                        font.weight: Font.Bold
                        Layout.maximumWidth: implicitWidth
                        visible: text.length > 0 }
                }
            }

            // Tabs — custom pill bar (stock TabBar looked dated)
            Rectangle {
                id: tabBarBg
                Layout.fillWidth: true; Layout.preferredHeight: 56
                color: T.header
                RowLayout {
                    id: tabBarRow
                    anchors.fill: parent
                    anchors.leftMargin: 16; anchors.rightMargin: 16
                    spacing: 8
                    Repeater {
                        id: tabRepeater
                        model: ["Today", "Habits", "Telemetry", "Updates", "Console"]
                        delegate: Rectangle {
                            required property string modelData
                            required property int index
                            id: tabPill
                            Layout.preferredHeight: 40
                            Layout.preferredWidth: tabLabel.implicitWidth + 36
                            radius: 20
                            color: tabBar.currentIndex === tabPill.index
                                ? T.accent
                                : (tabHover.containsMouse ? T.hover : T.panel)
                            border.width: tabBar.currentIndex === tabPill.index ? 0 : 1
                            border.color: T.border
                            Behavior on color { ColorAnimation { duration: 150 } }
                            // NEW-release badge on the Updates tab
                            Rectangle {
                                visible: tabPill.index === 3 && updatesNew > 0
                                width: badgeText.implicitWidth + 12; height: 18
                                radius: 9; anchors.right: parent.right
                                anchors.rightMargin: -6; anchors.top: parent.top
                                anchors.topMargin: -6
                                color: "#e74c3c"
                                Text { id: badgeText
                                    anchors.centerIn: parent
                                    text: updatesNew; color: "white"
                                    font.pixelSize: 11; font.weight: Font.Bold }
                            }
                            Text { id: tabLabel
                                anchors.centerIn: parent
                                text: tabPill.modelData
                                color: tabBar.currentIndex === tabPill.index
                                    ? "white" : T.muted
                                font.pixelSize: T.small
                                font.weight: tabBar.currentIndex === tabPill.index
                                    ? Font.DemiBold : Font.Medium
                                Behavior on color { ColorAnimation { duration: 150 } }
                            }
                            MouseArea {
                                id: tabHover
                                anchors.fill: parent
                                hoverEnabled: true
                                // generous 40px target, whole pill clickable
                                onClicked: tabBar.currentIndex = tabPill.index
                            }
                        }
                    }
                    Item { Layout.fillWidth: true }
                }
            }
            // the logical tab bar the StackLayout binds to (custom pills above)
            TabBar {
                id: tabBar; visible: false; height: 0
                Repeater {
                    model: ["Today", "Habits", "Telemetry", "Updates", "Console"]
                    delegate: TabButton { text: modelData }
                }
            }

            StackLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                currentIndex: tabBar.currentIndex

                // ============ TODAY ============
                ScrollView {
                    id: todayScroll
                    GridLayout {
                        width: Math.max(todayScroll.width - 32, 900)
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
                                        color: dueHover.containsMouse ? T.card : T.bg
                                        border.color: T.border; border.width: 1
                                        // tap anywhere on the row (not the button)
                                        // opens the audit-trail detail popup
                                        MouseArea { id: dueHover
                                            anchors.fill: parent; hoverEnabled: true
                                            onClicked: openHabit = Bridge.habitDetail(modelData.id)
                                            propagateComposedEvents: true }
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
                                                id: checkBtn
                                                text: "✓ Check"; implicitWidth: 104
                                                implicitHeight: 44
                                                background: Rectangle { radius: 22
                                                    color: checkBtn.pressed ? T.positive : T.accent }
                                                contentItem: Text { text: parent.text
                                                    color: "white"; font.pixelSize: T.small
                                                    font.weight: Font.DemiBold
                                                    anchors.centerIn: parent }
                                                onClicked: { Bridge.check(modelData.id)
                                                    refreshAll() } } }
                                    }
                                    Text { visible: dueList.count === 0
                                        text: "All clear — nothing due."
                                        color: T.muted; font.pixelSize: T.body
                                        width: dueList.width - 24
                                        wrapMode: Text.WordWrap; padding: 12 }
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
                                        font.pixelSize: T.small
                                        width: eventLog.width - 24
                                        wrapMode: Text.WordWrap; padding: 12 } } }
                        }

                        // Deferred habits strip
                        Rectangle {
                            Layout.column: 0; Layout.row: 1; Layout.columnSpan: 2
                            Layout.fillWidth: true; Layout.preferredHeight: 150
                            radius: 14; color: T.panel
                            border.color: T.border; border.width: 1
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16
                                spacing: 8
                                RowLayout { spacing: 10; Layout.fillWidth: true
                                    Text { text: "Deferred by capacity mode"; color: T.muted
                                        font.pixelSize: T.body; font.weight: Font.DemiBold }
                                    Item { Layout.fillWidth: true }
                                    Text { visible: sosData.length > 0
                                        text: "🆘 " + sosData.length + " SOS pending"
                                        color: "#e74c3c"; font.pixelSize: T.small
                                        font.weight: Font.Bold } }
                                ListView {
                                    id: deferList; Layout.fillWidth: true
                                    Layout.preferredHeight: 66; clip: true
                                    spacing: 6; orientation: ListView.Horizontal
                                    model: deferredData
                                    delegate: Rectangle {
                                        width: 230; height: 60; radius: 10
                                        color: T.card; border.color: T.border
                                        MouseArea { anchors.fill: parent
                                            onClicked: openHabit = Bridge.habitDetail(modelData.id) }
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
                                        font.pixelSize: T.small
                                        width: deferList.width - 24
                                        wrapMode: Text.WordWrap; padding: 12 }
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
                                color: habitsHover.containsMouse ? T.hover : T.card
                                border.color: T.border; border.width: 1
                                MouseArea { id: habitsHover
                                    anchors.fill: parent; hoverEnabled: true
                                    onClicked: openHabit = Bridge.habitDetail(modelData.id) }
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
                                color: T.muted; font.pixelSize: T.body
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap; padding: 12 } }
                        ColumnLayout { spacing: 8; Layout.fillWidth: true
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
                                            newEffort.currentText, "", newDesc.text)
                                        newName.clear(); newDesc.clear(); refreshAll() } } }
                            // description/annotation — stored with the habit so
                            // backups carry the operator's notes
                            TextField { id: newDesc; Layout.fillWidth: true
                                placeholderText: "Notes / description (stored, shown in the task detail)…"
                                color: T.text; font.pixelSize: T.small
                                background: Rectangle { radius: 8; color: T.card
                                    border.color: T.border; border.width: 1 } }
                        }
                    }
                }

                // ============ TELEMETRY ============
                ScrollView {
                    id: telemetryScroll
                    GridLayout {
                        width: Math.max(telemetryScroll.width - 32, 900)
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
                            color: T.muted; font.pixelSize: T.body
                            Layout.columnSpan: 2
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap; padding: 16 } }
                }

                // ============ UPDATES (CI changelog) ============
                Item {
                    ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 12
                        RowLayout { spacing: 10; Layout.fillWidth: true
                            Text { text: "System updates"; color: T.text
                                font.pixelSize: T.h2; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            Text { text: "build " + Bridge.buildStamp
                                color: T.muted; font.pixelSize: T.small
                                font.family: T.monoFamily }
                        }
                        Text { visible: releasesData.length === 0
                            text: "No releases recorded yet — agentd syncs git history into the update channel on every daily briefing; 'releases sync' does it on demand."
                            color: T.muted; font.pixelSize: T.small
                            Layout.fillWidth: true; wrapMode: Text.WordWrap }
                        ListView {
                            id: releasesList
                            Layout.fillWidth: true; Layout.fillHeight: true
                            clip: true; spacing: 10; model: releasesData
                            delegate: Rectangle {
                                width: releasesList.width; radius: 12
                                // bubble grows when selected, shows the body
                                // (deterministic 2-state height — no implicit
                                // chains: they overflowed the binding stack)
                                height: expanded ? 168 : 84
                                property bool expanded: false
                                color: modelData.new ? "#1c2a3f" : T.card
                                border.color: modelData.new ? T.accent : T.border
                                border.width: modelData.new ? 2 : 1
                                MouseArea { anchors.fill: parent
                                    onClicked: {
                                        expanded = !expanded
                                        Bridge.viewRelease(modelData.id)
                                        updatesNew = releasesModel_newCount()
                                    } }
                                ColumnLayout { id: relCol
                                    anchors.fill: parent; anchors.margins: 12
                                    spacing: 6
                                    RowLayout { id: relTop
                                        spacing: 10; Layout.fillWidth: true
                                        Rectangle { visible: modelData.new
                                            radius: 9; color: "#e74c3c"
                                            Layout.preferredHeight: 18
                                            Layout.preferredWidth: newTag.implicitWidth + 12
                                            Text { id: newTag; anchors.centerIn: parent
                                                text: "NEW"; color: "white"
                                                font.pixelSize: 11
                                                font.weight: Font.Bold } }
                                        Text { text: modelData.subject
                                            color: T.text; font.pixelSize: T.body
                                            font.weight: Font.Medium
                                            Layout.fillWidth: true
                                            elide: Text.ElideRight }
                                    }
                                    RowLayout { spacing: 10; Layout.fillWidth: true
                                        Text { text: modelData.hash
                                            color: T.accent; font.pixelSize: T.small
                                            font.family: T.monoFamily
                                            font.weight: Font.Medium }
                                        Text { text: modelData.ts.substring(0, 10)
                                            color: T.muted; font.pixelSize: T.small }
                                        Item { Layout.fillWidth: true }
                                        Text { text: modelData.author
                                            color: T.muted; font.pixelSize: T.small }
                                    }
                                    Text { visible: expanded && modelData.body.length > 0
                                        text: modelData.body
                                        color: T.text; font.pixelSize: T.small
                                        wrapMode: Text.WordWrap
                                        Layout.fillWidth: true }
                                }
                            }
                        }
                    }
                }

                // ============ CONSOLE ============
                Item {
                    ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 12
                        Text { text: "Command line — same verbs your agent uses. Type 'help' for the list; 'watch'/'gui'/'model' are daemon/dev-only and blocked here."
                            color: T.muted; font.pixelSize: T.small
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap }
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
                                placeholderText: "command — type 'help' for the verb list"
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
                    }
                }
            } // StackLayout
        } // main ColumnLayout
    } // RowLayout
} // ApplicationWindow
