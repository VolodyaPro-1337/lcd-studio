from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .sensors import AUTO, TYPE_NAMES, fmt_value, hub


def sensor_title(sid):
    m = hub.get_meta(sid)
    if not m:
        return sid
    return m["name"] if m["group"] == "Рекомендуемые" else f"{m['group']} › {m['name']}"


class SensorPicker(QDialog):
    """Дерево всех датчиков с текущими значениями. Двойной клик — выбрать."""

    def __init__(self, parent=None, current="", browse=False):
        super().__init__(parent)
        self.setWindowTitle("Все датчики" if browse else "Выбор датчика")
        self.resize(720, 640)
        self.result_id = current
        hub.acquire()
        lay = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск: температура, fan, gpu, диск...")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Датчик", "Значение", "ID (для текста)"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.itemDoubleClicked.connect(lambda it, _: self._choose(it))
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Пинг до:"))
        self.ping = QLineEdit("1.1.1.1")
        row.addWidget(self.ping)
        add_ping = QPushButton("Выбрать пинг")
        add_ping.clicked.connect(self._choose_ping)
        row.addWidget(add_ping)
        row.addStretch()
        lay.addLayout(row)

        self.status = QLabel()
        self.status.setStyleSheet("color:gray")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Close if browse else QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(lambda: self._choose(self.tree.currentItem()))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if browse:
            add_ping.hide()
            self.ping.hide()
            row.itemAt(0).widget().hide()

        self.items = {}
        self.count = -1
        self.timer = QTimer(self, interval=1000, timeout=self._refresh)
        self.timer.start()
        self._refresh()
        QTimer.singleShot(1500, self._refresh)

    def _refresh(self):
        meta, vals = hub.all_sensors()
        ids = [a for a, _, _ in AUTO if hub.resolve(a) in vals] + sorted(meta, key=self._sort_key)
        if len(ids) != self.count:
            self._rebuild(ids)
        for sid, item in self.items.items():
            v = hub.get(sid)
            m = hub.get_meta(sid) or {}
            item.setText(1, fmt_value(v, m.get("unit", "")))
        self.status.setText(f"Датчиков: {len(meta)}. LibreHardwareMonitor: {hub.lhm_status}. "
                            "В текстовом слое датчик вставляется как {ID}.")

    @staticmethod
    def _sort_key(sid):
        m = hub.meta[sid]
        return (not sid.startswith("sys/"), m["group"], m["type"], sid)

    def _rebuild(self, ids):
        self.count = len(ids)
        self.tree.clear()
        self.items = {}
        groups = {}
        for sid in ids:
            m = hub.get_meta(sid)
            if not m:
                continue
            gkey = m["group"]
            if gkey not in groups:
                groups[gkey] = QTreeWidgetItem(self.tree, [gkey])
                groups[gkey].setFlags(Qt.ItemIsEnabled)
                groups[gkey].setExpanded(True)
            parent = groups[gkey]
            if m["type"] and gkey != "Рекомендуемые":
                tkey = (gkey, m["type"])
                if tkey not in groups:
                    groups[tkey] = QTreeWidgetItem(parent, [TYPE_NAMES.get(m["type"], m["type"])])
                    groups[tkey].setFlags(Qt.ItemIsEnabled)
                    groups[tkey].setExpanded(True)
                parent = groups[tkey]
            it = QTreeWidgetItem(parent, [m["name"], "", sid])
            it.setData(0, Qt.UserRole, sid)
            self.items[sid] = it
            if sid == self.result_id:
                self.tree.setCurrentItem(it)
        self._filter(self.search.text())

    def _filter(self, text):
        t = text.lower().strip()

        def walk(node):
            visible = False
            for i in range(node.childCount()):
                ch = node.child(i)
                sid = ch.data(0, Qt.UserRole)
                if sid:
                    ok = not t or t in ch.text(0).lower() or t in sid.lower() or t in (node.text(0).lower())
                else:
                    ok = walk(ch) or (t and t in ch.text(0).lower())
                    if ok and t and t in ch.text(0).lower():
                        for j in range(ch.childCount()):
                            ch.child(j).setHidden(False)
                ch.setHidden(not ok)
                visible |= bool(ok)
            return visible
        walk(self.tree.invisibleRootItem())

    def _choose(self, item):
        sid = item.data(0, Qt.UserRole) if item else None
        if sid:
            self.result_id = sid
            self.accept()

    def _choose_ping(self):
        host = self.ping.text().strip()
        if host:
            self.result_id = f"ping/{host}"
            hub.register(self.result_id)
            self.accept()

    def done(self, r):
        self.timer.stop()
        hub.release()
        super().done(r)


def pick_sensor(parent, current=""):
    dlg = SensorPicker(parent, current)
    return dlg.result_id if dlg.exec() == QDialog.Accepted else None


class SensorListDialog(QDialog):
    def __init__(self, parent, ids):
        super().__init__(parent)
        self.setWindowTitle("Датчики панели")
        self.resize(520, 480)
        hub.acquire()
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        for sid in ids:
            self._add(sid)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in [("Добавить...", self._pick), ("Заменить...", self._replace), ("Удалить", self._remove),
                         ("▲", lambda: self._move(-1)), ("▼", lambda: self._move(1))]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add(self, sid, row=None):
        it = QListWidgetItem(sensor_title(sid))
        it.setData(Qt.UserRole, sid)
        if row is None:
            self.list.addItem(it)
        else:
            self.list.insertItem(row, it)
        return it

    def _pick(self):
        sid = pick_sensor(self)
        if sid:
            self.list.setCurrentItem(self._add(sid))

    def _replace(self):
        row = self.list.currentRow()
        if row < 0:
            return
        sid = pick_sensor(self, self.list.item(row).data(Qt.UserRole))
        if sid:
            self.list.takeItem(row)
            self.list.setCurrentItem(self._add(sid, row))

    def _remove(self):
        self.list.takeItem(self.list.currentRow())

    def _move(self, d):
        row = self.list.currentRow()
        if 0 <= row + d < self.list.count() and row >= 0:
            it = self.list.takeItem(row)
            self.list.insertItem(row + d, it)
            self.list.setCurrentRow(row + d)

    def ids(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def done(self, r):
        hub.release()
        super().done(r)
