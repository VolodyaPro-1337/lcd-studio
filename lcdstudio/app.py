import copy
import ctypes
import json
import subprocess
import sys
import time
import winreg
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton, QScrollArea, QSlider,
    QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import config
from .editor import CanvasEditor, render_scene
from .model import FITS, TYPES, duplicate_item, new_item, new_scene
from .outputs import DEVICES, WindowOutput
from .props import build_editor, color_button
from .rgb import RgbSync
from .rgbpanel import RgbPanel
from .monitor import AUTO_THRESHOLDS, DashboardSource
from .sensorpicker import SensorPicker
from .sensors import hub, is_admin
from .sources import IMAGE_EXT, VIDEO_EXT, make_source

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "LcdStudio"
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(__file__).resolve().parent.parent
TITLE = "LCD Studio — экран корпуса"
RESOLUTIONS = ["1920x480", "1920x440", "1280x480", "1024x600", "960x360", "800x480", "480x480"]
MEDIA_TYPES = {"image", "video", "slideshow", "screen", "window"}


def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#00c8ff"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 18, 56, 28, 6, 6)
    p.setBrush(QColor("#0a0c12"))
    p.drawRoundedRect(9, 23, 46, 18, 3, 3)
    p.end()
    return QIcon(pm)


def _launch_cmd():
    if FROZEN:
        return f'"{sys.executable}"', "--minimized"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}"', f'"{ROOT / "run.pyw"}" --minimized'


def _schtask(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, creationflags=0x08000000).returncode == 0


def autostart_enabled():
    if _schtask("/query", "/tn", APP_NAME):
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    """С правами администратора — задача планировщика (иначе датчики CPU недоступны), иначе ключ Run."""
    exe, args = _launch_cmd()
    if on and is_admin():
        if _schtask("/create", "/f", "/tn", APP_NAME, "/sc", "onlogon", "/rl", "highest", "/tr", f"{exe} {args}"):
            on = False  # ключ Run больше не нужен
    elif not on and is_admin():
        _schtask("/delete", "/f", "/tn", APP_NAME)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f"{exe} {args}")
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def spin(lo, hi, value, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(int(value))
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.sources = {}   # id слоя -> (подпись свойств, источник)
        self.sel = ""
        self.undo, self.redo = [], []
        self.last_mark = 0
        self.window_out = WindowOutput()
        self.device = None
        self._dev_res_seen = None
        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=lambda: config.save(self.cfg))

        self.setWindowTitle(TITLE)
        self.setWindowIcon(make_icon())
        self.resize(1280, 820)

        self.editor = CanvasEditor()
        self.editor.selected.connect(self.select)
        self.editor.edit_started.connect(lambda: self.mark(force=True))
        self.editor.geometry_changed.connect(self._on_geometry)
        self.editor.context_menu.connect(self._context_menu)
        self.editor.files_dropped.connect(self._on_drop)
        self.editor.nudge.connect(self._nudge)

        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(self._scenes_panel())
        bottom.addWidget(self._layers_panel())
        self.tabs = QTabWidget()
        self.props_area = QScrollArea()
        self.props_area.setWidgetResizable(True)
        self.tabs.addTab(self.props_area, "Свойства слоя")
        self.tabs.addTab(self._output_panel(), "Экран корпуса")
        self.tabs.addTab(self._monitor_panel(), "Мониторинг")
        self.last_frame = None
        self.rgb = RgbSync(cfg, lambda: self.last_frame, hub.get)
        self.tabs.addTab(RgbPanel(cfg, self.rgb, self.save), "Подсветка")
        if cfg["rgb"]["enabled"]:
            self.rgb.start()
        bottom.addWidget(self.tabs)
        bottom.setSizes([200, 280, 600])

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.editor)
        split.addWidget(bottom)
        split.setSizes([380, 440])
        self.setCentralWidget(split)

        for keys, fn in [("Delete", self.remove_layer), ("Ctrl+D", self.duplicate_layer),
                         ("Ctrl+Z", self.do_undo), ("Ctrl+Y", self.do_redo), ("Ctrl+Shift+Z", self.do_redo)]:
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(fn)

        hub.interval = cfg["sensor_interval"]
        self.timer = QTimer(self, timeout=self.tick)
        self.timer.start(int(1000 / cfg["fps"]))
        self.cycle_timer = QTimer(self, timeout=self._cycle_scene)
        self._apply_cycle()

        self.refresh_scenes()
        self._apply_window_output()
        self._apply_device()
        self._make_tray()

    # ---------- состояние ----------

    @property
    def scene(self):
        scenes = self.cfg["scenes"]
        self.cfg["scene_index"] = max(0, min(self.cfg["scene_index"], len(scenes) - 1))
        return scenes[self.cfg["scene_index"]]

    def item(self, iid=None):
        iid = self.sel if iid is None else iid
        return next((it for it in self.scene["items"] if it["id"] == iid), None)

    def save(self):
        self.save_timer.start()

    def mark(self, force=False):
        """Снимок для отмены. Серия мелких правок подряд — один шаг."""
        now = time.monotonic()
        if force or now - self.last_mark > 0.8:
            self.undo.append(json.dumps([self.cfg["scenes"], self.cfg["scene_index"]]))
            del self.undo[:-100]
            self.redo.clear()
        self.last_mark = now

    def _restore(self, snap):
        self.cfg["scenes"], self.cfg["scene_index"] = json.loads(snap)
        self.refresh_scenes()
        self.save()

    def do_undo(self):
        if self.undo:
            self.redo.append(json.dumps([self.cfg["scenes"], self.cfg["scene_index"]]))
            self._restore(self.undo.pop())
            self.last_mark = 0

    def do_redo(self):
        if self.redo:
            self.undo.append(json.dumps([self.cfg["scenes"], self.cfg["scene_index"]]))
            self._restore(self.redo.pop())
            self.last_mark = 0

    def sync_sources(self):
        """Пересоздаёт источники, у которых изменились свойства; гасит лишние."""
        alive = {}
        for it in self.scene["items"]:
            sig = json.dumps([it["type"], it["props"]], sort_keys=True)
            old = self.sources.pop(it["id"], None)
            if old and old[0] == sig:
                alive[it["id"]] = old
                continue
            if old:
                old[1].stop()
            src = make_source(it)
            src.start()
            alive[it["id"]] = (sig, src)
        for _, src in self.sources.values():
            src.stop()
        self.sources = alive

    # ---------- сцены ----------

    def _scenes_panel(self):
        box = QGroupBox("Сцены")
        lay = QVBoxLayout(box)
        self.scene_list = QListWidget()
        self.scene_list.currentRowChanged.connect(self._on_scene_row)
        self.scene_list.itemChanged.connect(self._on_scene_renamed)
        lay.addWidget(self.scene_list)
        row = QHBoxLayout()
        for text, tip, fn in [("+", "Новая сцена", self.add_scene), ("−", "Удалить сцену", self.remove_scene),
                              ("⧉", "Копия сцены", self.duplicate_scene),
                              ("▲", "Выше", lambda: self.move_scene(-1)), ("▼", "Ниже", lambda: self.move_scene(1))]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(30)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        hint = QLabel("Двойной клик — переименовать")
        hint.setStyleSheet("color:gray")
        lay.addWidget(hint)
        return box

    def refresh_scenes(self):
        self.scene_list.blockSignals(True)
        self.scene_list.clear()
        for sc in self.cfg["scenes"]:
            li = QListWidgetItem(sc["name"])
            li.setFlags(li.flags() | Qt.ItemIsEditable)
            self.scene_list.addItem(li)
        self.scene_list.setCurrentRow(self.cfg["scene_index"])
        self.scene_list.blockSignals(False)
        self._scene_changed()

    def _scene_changed(self):
        self.editor.scene = self.scene
        self.editor.cw, self.editor.ch = self.cfg["width"], self.cfg["height"]
        if not self.item():
            self.sel = ""
        self.editor.sel = self.sel
        self.sync_sources()
        self.refresh_layers()
        self.refresh_props()
        if hasattr(self, "scene_bg"):
            self._refresh_scene_bg()

    def _on_scene_row(self, row):
        if 0 <= row < len(self.cfg["scenes"]) and row != self.cfg["scene_index"]:
            self.cfg["scene_index"] = row
            self.sel = ""
            self._scene_changed()
            self.save()

    def switch_scene(self, row):
        self.scene_list.setCurrentRow(row)

    def _on_scene_renamed(self, li):
        row = self.scene_list.row(li)
        if li.text().strip() and self.cfg["scenes"][row]["name"] != li.text():
            self.mark(force=True)
            self.cfg["scenes"][row]["name"] = li.text().strip()
            self.save()

    def add_scene(self):
        self.mark(force=True)
        n = len(self.cfg["scenes"]) + 1
        self.cfg["scenes"].append(new_scene(f"Сцена {n}", self.cfg["width"], self.cfg["height"], kinds=()))
        self.cfg["scene_index"] = len(self.cfg["scenes"]) - 1
        self.refresh_scenes()
        self.save()

    def duplicate_scene(self):
        self.mark(force=True)
        dup = copy.deepcopy(self.scene)
        dup["name"] += " (копия)"
        for it in dup["items"]:
            it["id"] = duplicate_item(it)["id"]
        self.cfg["scenes"].insert(self.cfg["scene_index"] + 1, dup)
        self.cfg["scene_index"] += 1
        self.refresh_scenes()
        self.save()

    def remove_scene(self):
        if len(self.cfg["scenes"]) <= 1:
            return
        self.mark(force=True)
        del self.cfg["scenes"][self.cfg["scene_index"]]
        self.refresh_scenes()
        self.save()

    def move_scene(self, d):
        i, sc = self.cfg["scene_index"], self.cfg["scenes"]
        j = i + d
        if 0 <= j < len(sc):
            self.mark(force=True)
            sc[i], sc[j] = sc[j], sc[i]
            self.cfg["scene_index"] = j
            self.refresh_scenes()
            self.save()

    def _cycle_scene(self):
        if len(self.cfg["scenes"]) > 1:
            self.switch_scene((self.cfg["scene_index"] + 1) % len(self.cfg["scenes"]))

    def _apply_cycle(self):
        if self.cfg["scene_cycle"]:
            self.cycle_timer.start(self.cfg["scene_cycle_interval"] * 1000)
        else:
            self.cycle_timer.stop()

    # ---------- слои ----------

    def _layers_panel(self):
        box = QGroupBox("Слои (верхний — поверх всех)")
        lay = QVBoxLayout(box)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layer_list.currentRowChanged.connect(self._on_layer_row)
        self.layer_list.itemChanged.connect(self._on_layer_check)
        self.layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(
            lambda pos: self._context_menu(self.sel, QPointF(self.layer_list.mapToGlobal(pos))))
        lay.addWidget(self.layer_list)
        row = QHBoxLayout()
        add = QToolButton()
        add.setText("+ Добавить")
        add.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(add)
        for kind, (title, _, _) in TYPES.items():
            menu.addAction(title, lambda k=kind: self.add_layer(k))
        add.setMenu(menu)
        row.addWidget(add)
        for text, tip, fn in [("−", "Удалить (Delete)", self.remove_layer),
                              ("⧉", "Дублировать (Ctrl+D)", self.duplicate_layer),
                              ("▲", "Выше", lambda: self.move_layer(1)), ("▼", "Ниже", lambda: self.move_layer(-1)),
                              ("🔒", "Закрепить / открепить", self.toggle_lock)]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(30)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        hint = QLabel("Галочка — видимость. Файлы можно перетащить на холст.")
        hint.setStyleSheet("color:gray")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return box

    def refresh_layers(self):
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        items = self.scene["items"]
        for it in reversed(items):
            li = QListWidgetItem(("🔒 " if it["locked"] else "") + it["name"])
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Checked if it["visible"] else Qt.Unchecked)
            li.setData(Qt.UserRole, it["id"])
            self.layer_list.addItem(li)
            if it["id"] == self.sel:
                self.layer_list.setCurrentItem(li)
        self.layer_list.blockSignals(False)

    def _on_layer_row(self, row):
        li = self.layer_list.item(row)
        self.select(li.data(Qt.UserRole) if li else "")

    def _on_layer_check(self, li):
        it = self.item(li.data(Qt.UserRole))
        vis = li.checkState() == Qt.Checked
        if it and it["visible"] != vis:
            self.mark(force=True)
            it["visible"] = vis
            self.save()

    def select(self, iid):
        if iid == self.sel:
            return
        self.sel = iid
        self.editor.sel = iid
        self.editor.update()
        for i in range(self.layer_list.count()):
            if self.layer_list.item(i).data(Qt.UserRole) == iid:
                self.layer_list.blockSignals(True)
                self.layer_list.setCurrentRow(i)
                self.layer_list.blockSignals(False)
                break
        else:
            self.layer_list.clearSelection()
        self.refresh_props()
        if iid:
            self.tabs.setCurrentIndex(0)

    def add_layer(self, kind, x=None, y=None, props=None):
        self.mark(force=True)
        it = new_item(kind, self.cfg["width"], self.cfg["height"], x, y)
        if props:
            it["props"].update(props)
            it["name"] = Path(next(iter(props.values()))).name or it["name"]
        self.scene["items"].append(it)
        self.sel = it["id"]
        self._scene_changed()
        self.tabs.setCurrentIndex(0)
        self.save()

    def remove_layer(self):
        if not self.item():
            return
        self.mark(force=True)
        self.scene["items"] = [it for it in self.scene["items"] if it["id"] != self.sel]
        self.sel = ""
        self._scene_changed()
        self.save()

    def duplicate_layer(self):
        it = self.item()
        if not it:
            return
        self.mark(force=True)
        dup = duplicate_item(it)
        items = self.scene["items"]
        items.insert(items.index(it) + 1, dup)
        self.sel = dup["id"]
        self._scene_changed()
        self.save()

    def move_layer(self, d, to_end=False):
        it = self.item()
        if not it:
            return
        items = self.scene["items"]
        i = items.index(it)
        j = (len(items) - 1 if d > 0 else 0) if to_end else i + d
        if 0 <= j < len(items) and j != i:
            self.mark(force=True)
            items.insert(j, items.pop(i))
            self.refresh_layers()
            self.save()

    def toggle_lock(self):
        it = self.item()
        if it:
            self.mark(force=True)
            it["locked"] = not it["locked"]
            self.refresh_layers()
            self.editor.update()
            self.save()

    def _set_geom(self, mode):
        it = self.item()
        if not it:
            return
        self.mark(force=True)
        cw, ch = self.cfg["width"], self.cfg["height"]
        if mode == "fill":
            it.update(x=0, y=0, w=cw, h=ch, rotation=0)
        elif mode == "fit":
            k = min(cw / it["w"], ch / it["h"])
            it["w"], it["h"] = round(it["w"] * k), round(it["h"] * k)
            it["x"], it["y"] = round((cw - it["w"]) / 2), round((ch - it["h"]) / 2)
        elif mode == "center":
            it["x"], it["y"] = round((cw - it["w"]) / 2), round((ch - it["h"]) / 2)
        elif mode == "hcenter":
            it["x"] = round((cw - it["w"]) / 2)
        elif mode == "vcenter":
            it["y"] = round((ch - it["h"]) / 2)
        elif mode == "unrotate":
            it["rotation"] = 0
        self._on_geometry(True)

    def _on_geometry(self, final):
        self._update_transform_fields()
        self.editor.update()
        if final:
            self.save()

    def _nudge(self, dx, dy):
        it = self.item()
        if it and not it["locked"]:
            self.mark()
            it["x"] += dx
            it["y"] += dy
            self._on_geometry(True)

    def _context_menu(self, iid, gpos):
        m = QMenu(self)
        it = self.item(iid) if iid else None
        if it:
            m.addAction("Растянуть на весь экран", lambda: self._set_geom("fill"))
            m.addAction("Вписать в экран", lambda: self._set_geom("fit"))
            m.addAction("По центру", lambda: self._set_geom("center"))
            m.addAction("По центру по горизонтали", lambda: self._set_geom("hcenter"))
            m.addAction("По центру по вертикали", lambda: self._set_geom("vcenter"))
            m.addAction("Сбросить поворот", lambda: self._set_geom("unrotate"))
            if it["type"] == "sensors":
                m.addAction("Разложить на отдельные слои", lambda: self.explode_dashboard(it))
            m.addSeparator()
            m.addAction("Наверх", lambda: self.move_layer(1, True))
            m.addAction("Выше", lambda: self.move_layer(1))
            m.addAction("Ниже", lambda: self.move_layer(-1))
            m.addAction("Вниз", lambda: self.move_layer(-1, True))
            m.addSeparator()
            m.addAction("Открепить" if it["locked"] else "Закрепить", self.toggle_lock)
            m.addAction("Скрыть" if it["visible"] else "Показать", lambda: self._toggle_visible(it))
            m.addAction("Дублировать", self.duplicate_layer)
            m.addAction("Удалить", self.remove_layer)
            m.addSeparator()
        add = m.addMenu("Добавить слой")
        for kind, (title, _, _) in TYPES.items():
            add.addAction(title, lambda k=kind: self.add_layer(k))
        m.exec(gpos.toPoint())

    def explode_dashboard(self, it):
        """Каждая плитка панели становится отдельным слоем «Датчик»."""
        self.mark(force=True)
        pr = it["props"]
        dash = DashboardSource(**pr)
        items = self.scene["items"]
        idx = items.index(it)
        new = []
        if QColor(pr["bg"]).alpha():
            bg = new_item("color", self.cfg["width"], self.cfg["height"])
            bg.update(name="Фон панели", x=it["x"], y=it["y"], w=it["w"], h=it["h"], rotation=it["rotation"])
            bg["props"]["color"] = pr["bg"]
            new.append(bg)
        for sid, r in zip(pr["tiles"], dash.layout(it["w"], it["h"])):
            s = new_item("sensor", self.cfg["width"], self.cfg["height"])
            s.update(x=round(it["x"] + r.x()), y=round(it["y"] + r.y()), w=round(r.width()), h=round(r.height()))
            warn, crit = (0, 0) if pr["style"] == "graph" else \
                AUTO_THRESHOLDS.get((hub.get_meta(sid) or {}).get("unit"), (0, 0))
            s["props"].update(sensor=sid, style=pr["style"], color=pr["accent"], text_color=pr["text_color"],
                              bg=pr["tile_bg"], font=pr["font"], warn=float(warn), crit=float(crit))
            s["name"] = (hub.get_meta(sid) or {}).get("name", "Датчик")
            new.append(s)
        items[idx:idx + 1] = new
        self.sel = new[-1]["id"] if new else ""
        self._scene_changed()
        self.save()

    def _toggle_visible(self, it):
        self.mark(force=True)
        it["visible"] = not it["visible"]
        self.refresh_layers()
        self.save()

    def _on_drop(self, paths, cpt):
        for path in paths:
            p = Path(path)
            ext = p.suffix.lower()
            if p.is_dir():
                self.add_layer("slideshow", props={"folder": str(p)})
            elif ext in IMAGE_EXT:
                self.add_layer("image", props={"path": str(p)})
            elif ext in VIDEO_EXT:
                self.add_layer("video", props={"path": str(p)})

    # ---------- свойства ----------

    def refresh_props(self):
        it = self.item()
        w = QWidget()
        form = QFormLayout(w)
        self.tf = {}
        if not it:
            form.addRow(QLabel("Выберите слой на холсте или в списке,\nлибо добавьте новый кнопкой «+ Добавить»."))
            self.props_area.setWidget(w)
            return

        name = QLineEdit(it["name"])
        name.textChanged.connect(lambda t: self._set_field("name", t, layers=True))
        form.addRow("Название:", name)

        row = QHBoxLayout()
        for key, label in [("x", "X"), ("y", "Y"), ("w", "Ш"), ("h", "В")]:
            lo = 1 if key in "wh" else -20000
            s = spin(lo, 20000, it[key])
            s.valueChanged.connect(lambda v, k=key: self._set_field(k, v, geom=True))
            row.addWidget(QLabel(label))
            row.addWidget(s, 1)
            self.tf[key] = s
        form.addRow("Положение:", row)

        row = QHBoxLayout()
        rot = spin(-360, 360, it["rotation"], "°")
        rot.valueChanged.connect(lambda v: self._set_field("rotation", v, geom=True))
        self.tf["rotation"] = rot
        op = QSlider(Qt.Horizontal)
        op.setRange(0, 100)
        op.setValue(it["opacity"])
        op_lbl = QLabel(f"{it['opacity']}%")
        op.valueChanged.connect(lambda v: (self._set_field("opacity", v), op_lbl.setText(f"{v}%")))
        row.addWidget(QLabel("Поворот"))
        row.addWidget(rot)
        row.addWidget(QLabel("Прозрачн."))
        row.addWidget(op, 1)
        row.addWidget(op_lbl)
        form.addRow(row)

        row = QHBoxLayout()
        for text, mode in [("На весь экран", "fill"), ("Вписать", "fit"), ("По центру", "center")]:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, m=mode: self._set_geom(m))
            row.addWidget(b)
        form.addRow(row)

        if it["type"] in MEDIA_TYPES:
            fit = QComboBox()
            for k, t in FITS:
                fit.addItem(t, k)
            fit.setCurrentIndex(max(0, fit.findData(it["fit"])))
            fit.currentIndexChanged.connect(lambda _: self._set_field("fit", fit.currentData()))
            form.addRow("Масштаб:", fit)

        sep = QLabel(f"<b>{TYPES[it['type']][0]}</b>")
        form.addRow(sep)
        build_editor(form, it, self._set_prop, TITLE)
        self.props_area.setWidget(w)

    def _update_transform_fields(self):
        it = self.item()
        if not it or not getattr(self, "tf", None):
            return
        for key, s in self.tf.items():
            if s.value() != round(it[key]):
                s.blockSignals(True)
                s.setValue(round(it[key]))
                s.blockSignals(False)

    def _set_field(self, key, value, geom=False, layers=False):
        it = self.item()
        if not it or it[key] == value:
            return
        self.mark()
        it[key] = value
        if layers:
            self.refresh_layers()
        self.editor.update()
        self.save()

    def _set_prop(self, key, value):
        it = self.item()
        if not it or it["props"].get(key) == value:
            return
        self.mark()
        it["props"][key] = value
        if key in ("path", "folder") and value:
            it["name"] = Path(value).name
            self.refresh_layers()
        self.sync_sources()
        self.save()

    # ---------- экран ----------

    def _output_panel(self):
        w = QWidget()
        f = QFormLayout(w)

        self.res_combo = QComboBox()
        self.res_combo.setEditable(True)
        self.res_combo.addItems(RESOLUTIONS)
        self.res_combo.setCurrentText(f"{self.cfg['width']}x{self.cfg['height']}")
        self.res_combo.currentTextChanged.connect(self._on_res)
        f.addRow("Разрешение:", self.res_combo)

        rot = QComboBox()
        rot.addItems(["0°", "90°", "180°", "270°"])
        rot.setCurrentIndex(self.cfg["rotation"] // 90)
        rot.currentIndexChanged.connect(lambda i: self._set_cfg("rotation", i * 90))
        f.addRow("Поворот экрана:", rot)

        bright = QSlider(Qt.Horizontal)
        bright.setRange(5, 100)
        bright.setValue(self.cfg["brightness"])
        bl = QLabel(f"{self.cfg['brightness']}%")
        bright.valueChanged.connect(lambda v: (self._set_cfg("brightness", v), bl.setText(f"{v}%")))
        row = QHBoxLayout()
        row.addWidget(bright, 1)
        row.addWidget(bl)
        f.addRow("Яркость:", row)

        fps = spin(1, 60, self.cfg["fps"])
        fps.valueChanged.connect(lambda v: (self._set_cfg("fps", v), self.timer.setInterval(int(1000 / v))))
        f.addRow("Кадров/с:", fps)

        self.scene_bg = QHBoxLayout()
        f.addRow("Фон сцены:", self.scene_bg)
        self._refresh_scene_bg()

        row = QHBoxLayout()
        cyc = QCheckBox("Менять сцены каждые")
        cyc.setChecked(self.cfg["scene_cycle"])
        cyc.toggled.connect(lambda v: (self._set_cfg("scene_cycle", v), self._apply_cycle()))
        iv = spin(2, 86400, self.cfg["scene_cycle_interval"], " с")
        iv.valueChanged.connect(lambda v: (self._set_cfg("scene_cycle_interval", v), self._apply_cycle()))
        row.addWidget(cyc)
        row.addWidget(iv)
        row.addStretch()
        f.addRow(row)

        self.win_chk = QCheckBox("Выводить окном на монитор:")
        self.win_chk.setChecked(self.cfg["window_output"])
        self.win_chk.toggled.connect(lambda v: (self._set_cfg("window_output", v), self._apply_window_output()))
        self.screen_combo = QComboBox()
        for s in QApplication.screens():
            g = s.geometry()
            self.screen_combo.addItem(f"{s.name()} ({g.width()}x{g.height()})", s.name())
        i = self.screen_combo.findData(self.cfg["window_screen"])
        self.screen_combo.setCurrentIndex(i if i >= 0 else self.screen_combo.count() - 1)
        self.screen_combo.currentIndexChanged.connect(
            lambda: (self._set_cfg("window_screen", self.screen_combo.currentData()), self._apply_window_output()))
        f.addRow(self.win_chk, self.screen_combo)

        self.dev_combo = QComboBox()
        self.dev_combo.addItem("— нет —", "")
        for d in DEVICES:
            self.dev_combo.addItem(d.name, d.name)
        if not DEVICES:
            self.dev_combo.setEnabled(False)
        self.dev_combo.currentIndexChanged.connect(
            lambda: (self._set_cfg("device", self.dev_combo.currentData()), self._apply_device()))
        self.dev_combo.blockSignals(True)
        self.dev_combo.setCurrentIndex(max(0, self.dev_combo.findData(self.cfg["device"])))
        self.dev_combo.blockSignals(False)
        f.addRow("USB-экран:", self.dev_combo)
        self.status = QLabel()
        self.status.setWordWrap(True)
        f.addRow(self.status)
        row = QHBoxLayout()
        fit_btn = QPushButton("Холст под разрешение экрана")
        fit_btn.clicked.connect(self._fit_canvas_to_device)
        drv_btn = QPushButton("Установить драйвер экрана")
        drv_btn.setToolTip("Драйвер libusb для экранов MacroSilicon из комплекта LCD Control (нужны права администратора)")
        drv_btn.clicked.connect(self._install_driver)
        row.addWidget(fit_btn)
        row.addWidget(drv_btn)
        f.addRow(row)
        self.dev_timer = QTimer(self, interval=1000, timeout=self._update_dev_status)
        self.dev_timer.start()

        auto = QCheckBox("Запускать вместе с Windows (свёрнутым в трей)")
        auto.setChecked(autostart_enabled())
        auto.toggled.connect(set_autostart)
        f.addRow(auto)

        help_ = QLabel("Холст: перетаскивание — двигать, маркеры — размер (Shift — без пропорций), "
                       "Ctrl — без прилипания, стрелки — сдвиг (Shift ×10), Delete — удалить, "
                       "Ctrl+D — дублировать, Ctrl+Z / Ctrl+Y — отмена/повтор, ПКМ — меню.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color:gray")
        f.addRow(help_)
        return w

    def _monitor_panel(self):
        w = QWidget()
        f = QFormLayout(w)
        self.mon_status = QLabel()
        self.mon_status.setWordWrap(True)
        f.addRow(self.mon_status)
        if not is_admin():
            adm = QPushButton("Перезапустить от имени администратора")
            adm.setToolTip("Нужно для температуры и мощности CPU, датчиков материнской платы и дисков")
            adm.clicked.connect(self.restart_as_admin)
            f.addRow(adm)
        browse = QPushButton("Все датчики...")
        browse.clicked.connect(lambda: SensorPicker(self, browse=True).exec())
        f.addRow(browse)
        iv = QDoubleSpinBox()
        iv.setRange(0.25, 10)
        iv.setSingleStep(0.25)
        iv.setSuffix(" с")
        iv.setValue(self.cfg["sensor_interval"])
        iv.valueChanged.connect(lambda v: (self._set_cfg("sensor_interval", v), setattr(hub, "interval", v)))
        f.addRow("Опрос датчиков каждые:", iv)
        tips = QLabel(
            "Добавьте слой «Датчик» (любой показатель: кольцо, спидометр, полоса, столбик, график, число, текст), "
            "«Панель мониторинга» (свой набор плиток) или «Топ процессов». "
            "В слое «Текст» можно вставлять значения датчиков в любом месте строки.\n\n"
            "Температура CPU на некоторых платформах требует драйвер PawnIO (pawnio.eu) "
            "и запуск от имени администратора.")
        tips.setWordWrap(True)
        tips.setStyleSheet("color:gray")
        f.addRow(tips)
        hub.acquire()  # статус и «умные» датчики доступны сразу
        self.mon_timer = QTimer(self, interval=2000, timeout=self._update_mon_status)
        self.mon_timer.start()
        self._update_mon_status()
        return w

    def _update_mon_status(self):
        meta, _ = hub.all_sensors()
        cpu_t = hub.get("auto/cpu_temp")
        self.mon_status.setText(
            f"<b>Датчиков найдено:</b> {len(meta)}<br>"
            f"<b>LibreHardwareMonitor:</b> {hub.lhm_status}<br>"
            f"<b>Права администратора:</b> {'да' if is_admin() else 'нет'}<br>"
            f"<b>Температура CPU:</b> {f'{cpu_t:.0f}°C' if cpu_t else 'недоступна'}")

    def restart_as_admin(self):
        exe, args = _launch_cmd()
        args = args.replace("--minimized", "").strip()
        config.save(self.cfg)
        release_instance_lock()
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe.strip('"'), args, None, 1)
        if r > 32:
            self._quit()
        else:
            acquire_instance_lock()

    def _refresh_scene_bg(self):
        while self.scene_bg.count():
            old = self.scene_bg.takeAt(0).widget()
            old.hide()
            old.deleteLater()

        def set_bg(v):
            self.mark(force=True)
            self.scene["bg"] = v
            self.save()
        self.scene_bg.addWidget(color_button(self.scene.get("bg", "#ff000000"), set_bg))

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        self.save()

    def _on_res(self, text):
        try:
            w, h = (int(x) for x in text.lower().replace("х", "x").split("x"))
        except ValueError:
            return
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            self.cfg["width"], self.cfg["height"] = w, h
            self.editor.cw, self.editor.ch = w, h
            self.save()

    def _apply_window_output(self):
        if not self.cfg["window_output"]:
            self.window_out.hide()
            return
        name = self.cfg["window_screen"] or self.screen_combo.currentData()
        screen = next((s for s in QApplication.screens() if s.name() == name), QApplication.screens()[-1])
        self.window_out.show_on(screen)

    def _apply_device(self):
        if self.device:
            self.device.close()
            self.device = None
        cls = next((d for d in DEVICES if d.name == self.cfg["device"]), None)
        if cls is None:
            self.status.setText("" if DEVICES else "USB-драйвер экрана будет добавлен после его подключения.")
            return
        dev = cls()
        if dev.open():
            self.device = dev
        self.status.setText(dev.state)
        self._dev_res_seen = None

    def _update_dev_status(self):
        if not self.device:
            return
        self.status.setText(self.device.state)
        res = self.device.resolution
        if res and res != self._dev_res_seen:
            self._dev_res_seen = res
            # при первом подключении подгоняем холст, если он ещё не под этот экран
            if res != (self.cfg["width"], self.cfg["height"]) and self.cfg.get("auto_fit_device", True):
                self._fit_canvas_to_device()

    def _fit_canvas_to_device(self):
        res = self.device.resolution if self.device else None
        if not res:
            self.status.setText("Экран ещё не подключён — разрешение неизвестно")
            return
        w, h = res
        if self.cfg["rotation"] in (90, 270):
            w, h = h, w
        self.res_combo.setCurrentText(f"{w}x{h}")

    def _install_driver(self):
        from .usbscreen import install_driver
        err = install_driver()
        self.status.setText(err or "Установка драйвера запущена. После неё переподключите экран.")

    # ---------- кадр ----------

    def render(self):
        """Кадр в логической ориентации (как в редакторе)."""
        img = render_scene(self.scene, {k: v[1] for k, v in self.sources.items()},
                           self.cfg["width"], self.cfg["height"])
        if self.cfg["brightness"] < 100:
            p = QPainter(img)
            p.fillRect(img.rect(), QColor(0, 0, 0, int(255 * (1 - self.cfg["brightness"] / 100))))
            p.end()
        return img

    def tick(self):
        frame = self.render()
        self.last_frame = frame
        if self.isVisible() and not self.isMinimized():
            self.editor.set_frame(frame)
        rot = self.cfg["rotation"]
        out = frame.transformed(QTransform().rotate(rot)) if rot else frame
        if self.device:
            try:
                self.device.send(out)
            except Exception as e:
                self.status.setText(f"Ошибка отправки: {e}")
                self.device.close()
                self.device = None
        if self.window_out.isVisible():
            self.window_out.send(out)

    # ---------- трей ----------

    def _make_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("LCD Studio")
        menu = QMenu()
        menu.addAction("Открыть", self._show)
        scenes = menu.addMenu("Сцена")
        scenes.aboutToShow.connect(lambda: self._fill_scene_menu(scenes))
        menu.addSeparator()
        menu.addAction("Выход", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _fill_scene_menu(self, menu):
        menu.clear()
        for i, sc in enumerate(self.cfg["scenes"]):
            a = menu.addAction(sc["name"], lambda i=i: self.switch_scene(i))
            a.setCheckable(True)
            a.setChecked(i == self.cfg["scene_index"])

    def _show(self):
        self.showNormal()
        self.activateWindow()

    def _quit(self):
        self.timer.stop()
        self.rgb.stop()
        hub.shutdown()
        for _, src in self.sources.values():
            src.stop()
        if self.device:
            self.device.close()
        config.save(self.cfg)
        self.window_out.close()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, e):
        # Крестик сворачивает в трей, экран продолжает работать
        e.ignore()
        self.hide()
        config.save(self.cfg)
        self.tray.showMessage("LCD Studio", "Работает в трее. Выход — через меню значка.",
                              QSystemTrayIcon.Information, 2000)


_mutex = None


def acquire_instance_lock():
    global _mutex
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "LcdStudio_single_instance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def release_instance_lock():
    global _mutex
    if _mutex:
        ctypes.windll.kernel32.CloseHandle(_mutex)
        _mutex = None


def main():
    if not acquire_instance_lock():
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = config.load()
    win = MainWindow(cfg)
    if "--minimized" not in sys.argv:
        win.show()
    sys.exit(app.exec())
