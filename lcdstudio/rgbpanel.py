"""Вкладка «Подсветка»: настройки синхронизации RGB через OpenRGB."""
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from .props import color_button
from .rgb import MODES, download_openrgb, find_openrgb
from .sensorpicker import pick_sensor, sensor_title

DEFAULTS = {"enabled": True, "mode": "rainbow", "color": "#ff00c8ff", "color2": "#ffff0080", "speed": 50,
            "brightness": 100, "fps": 30, "sensor": "auto/cpu_temp", "sensor_min": 40.0, "sensor_max": 85.0,
            "ambient_monitor": 1, "ambient_average": False, "ambient_boost": 30, "openrgb_path": "",
            "stop_openrgb_on_exit": True, "devices": {}}


class RgbPanel(QWidget):
    def __init__(self, cfg, sync, save):
        super().__init__()
        self.cfg, self.sync, self.save = cfg, sync, save
        s = cfg.setdefault("rgb", {})
        for k, v in DEFAULTS.items():
            s.setdefault(k, v)
        self.s = s
        lay = QVBoxLayout(self)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        en = QCheckBox("Синхронизировать всю подсветку")
        en.setChecked(s["enabled"])
        en.toggled.connect(self._toggle)
        row.addWidget(en)
        self.dl = QPushButton("Скачать OpenRGB")
        self.dl.clicked.connect(self._download)
        row.addWidget(self.dl)
        rescan = QPushButton("Обновить устройства")
        rescan.clicked.connect(lambda: setattr(self.sync, "reconfigure", True))
        row.addWidget(rescan)
        row.addStretch()
        lay.addLayout(row)

        f = QFormLayout()
        lay.addLayout(f)
        mode = QComboBox()
        for k, t in MODES:
            mode.addItem(t, k)
        mode.setCurrentIndex(max(0, mode.findData(s["mode"])))
        mode.currentIndexChanged.connect(lambda: (self._set("mode", mode.currentData()), self._show_rows()))
        f.addRow("Эффект:", mode)

        colors = QHBoxLayout()
        colors.addWidget(color_button(s["color"], lambda v: self._set("color", v)))
        colors.addWidget(QLabel("второй"))
        colors.addWidget(color_button(s["color2"], lambda v: self._set("color2", v)))
        colors.addStretch()
        self.colors_row = self._row(f, "Цвета:", colors)

        self.speed_row = self._row(f, "Скорость:", self._slider("speed", 1, 200))
        self._row(f, "Яркость:", self._slider("brightness", 0, 100))

        sensor = QHBoxLayout()
        self.sensor_lbl = QLabel(sensor_title(s["sensor"]))
        pick = QPushButton("Выбрать...")
        pick.clicked.connect(self._pick_sensor)
        lo, hi = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, key in ((lo, "sensor_min"), (hi, "sensor_max")):
            sp.setRange(-1000, 100000)
            sp.setValue(s[key])
            sp.valueChanged.connect(lambda v, k=key: self._set(k, v))
        sensor.addWidget(self.sensor_lbl, 1)
        sensor.addWidget(pick)
        sensor.addWidget(QLabel("от"))
        sensor.addWidget(lo)
        sensor.addWidget(QLabel("до"))
        sensor.addWidget(hi)
        self.sensor_row = self._row(f, "Датчик:", sensor)

        amb = QHBoxLayout()
        mon = QSpinBox()
        mon.setRange(0, 8)
        mon.setSpecialValueText("все мониторы")
        mon.setPrefix("монитор ")
        mon.setValue(s["ambient_monitor"])
        mon.valueChanged.connect(lambda v: self._set("ambient_monitor", v))
        avg = QCheckBox("один средний цвет")
        avg.setChecked(s["ambient_average"])
        avg.toggled.connect(lambda v: self._set("ambient_average", v))
        boost = QSpinBox()
        boost.setRange(0, 200)
        boost.setSuffix(" % насыщенности")
        boost.setValue(s["ambient_boost"])
        boost.valueChanged.connect(lambda v: self._set("ambient_boost", v))
        amb.addWidget(mon)
        amb.addWidget(avg)
        amb.addWidget(boost)
        amb.addStretch()
        self.amb_row = self._row(f, "Ambilight:", amb)

        fps = QSpinBox()
        fps.setRange(1, 60)
        fps.setSuffix(" обновл./с")
        fps.setValue(s["fps"])
        fps.valueChanged.connect(lambda v: self._set("fps", v))
        f.addRow("Частота:", fps)

        lay.addWidget(QLabel("Устройства (галочка — участвует в синхронизации, число — светодиодов в ARGB-зоне):"))
        self.dev_list = QListWidget()
        self.dev_list.itemChanged.connect(self._dev_toggled)
        lay.addWidget(self.dev_list, 1)
        self.zone_box = QHBoxLayout()
        lay.addLayout(self.zone_box)
        self.dev_list.currentItemChanged.connect(lambda *_: self._show_zones())

        hint = QLabel("Подсветка корпуса подключается к ARGB-разъёму материнской платы (3 pin 5V) — она появится "
                      "в списке как зона материнской платы. Для памяти и части плат OpenRGB нужны права "
                      "администратора: перезапустите LCD Studio от имени администратора на вкладке «Мониторинг». "
                      "Фирменные программы подсветки (Aura, RGB Fusion, Mystic Light) лучше закрыть — они спорят "
                      "за контроллер.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:gray")
        lay.addWidget(hint)

        self._dev_sig = None
        self.timer = QTimer(self, interval=1000, timeout=self._refresh)
        self.timer.start()
        self._show_rows()
        self._refresh()

    def _row(self, form, label, layout):
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        form.addRow(label, w)
        return (form, w)

    def _slider(self, key, lo, hi):
        lay = QHBoxLayout()
        sl = QSlider(Qt.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(self.s[key])
        lbl = QLabel(str(self.s[key]))
        sl.valueChanged.connect(lambda v: (self._set(key, v), lbl.setText(str(v))))
        lay.addWidget(sl, 1)
        lay.addWidget(lbl)
        return lay

    def _set(self, key, value):
        self.s[key] = value
        self.sync._last_send = 0  # статичный режим применится сразу
        self.save()

    def _show_rows(self):
        m = self.s["mode"]
        vis = {
            self.colors_row: m in ("static", "breath", "gradient", "sensor"),
            self.speed_row: m in ("rainbow", "cycle", "breath", "gradient"),
            self.sensor_row: m == "sensor",
            self.amb_row: m.startswith("ambient"),
        }
        for (form, w), on in vis.items():
            form.setRowVisible(w, on)

    def _toggle(self, on):
        self._set("enabled", on)
        (self.sync.start if on else self.sync.stop)()

    def _pick_sensor(self):
        sid = pick_sensor(self, self.s["sensor"])
        if sid:
            self.sensor_lbl.setText(sensor_title(sid))
            self._set("sensor", sid)

    def _download(self):
        self.dl.setEnabled(False)

        def work():
            try:
                exe = download_openrgb(lambda t: setattr(self, "_dl_text", t))
                self._dl_text = f"OpenRGB установлен: {exe}" if exe else "Не удалось найти OpenRGB.exe в архиве"
                self.sync.reconfigure = True
            except Exception as e:
                self._dl_text = f"Ошибка скачивания: {e}"
            self._dl_done = True
        self._dl_text, self._dl_done = "Скачивание...", False
        threading.Thread(target=work, daemon=True).start()

    def _refresh(self):
        text = self.sync.status if self.s["enabled"] else "Синхронизация выключена"
        if getattr(self, "_dl_text", None):
            text = self._dl_text + "\n" + text
            if getattr(self, "_dl_done", False):
                self.dl.setEnabled(True)
        self.status.setText(text)
        self.dl.setText("Переустановить OpenRGB" if find_openrgb(self.s["openrgb_path"]) else "Скачать OpenRGB")
        sig = [(d["key"], len(d["leds"])) for d in self.sync.devices]
        if sig != self._dev_sig:
            self._dev_sig = sig
            self._fill_devices()

    def _fill_devices(self):
        self.dev_list.blockSignals(True)
        self.dev_list.clear()
        for d in self.sync.devices:
            it = QListWidgetItem(f"{d['name']} — светодиодов: {len(d['leds'])}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if d["enabled"] else Qt.Unchecked)
            it.setData(Qt.UserRole, d["key"])
            self.dev_list.addItem(it)
        self.dev_list.blockSignals(False)

    def _dev_toggled(self, it):
        key = it.data(Qt.UserRole)
        on = it.checkState() == Qt.Checked
        self.s["devices"].setdefault(key, {"enabled": True, "zones": {}})["enabled"] = on
        for d in self.sync.devices:
            if d["key"] == key:
                d["enabled"] = on
        self.sync.reconfigure = True
        self.save()

    def _show_zones(self):
        while self.zone_box.count():
            w = self.zone_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        it = self.dev_list.currentItem()
        dev = next((d for d in self.sync.devices if it and d["key"] == it.data(Qt.UserRole)), None)
        if not dev:
            return
        for zi, z in enumerate(dev["zones"]):
            if z["max"] <= z["min"]:
                continue
            sp = QSpinBox()
            sp.setRange(z["min"], min(z["max"], 1024))
            sp.setValue(z["count"])
            sp.setPrefix(f"{z['name']}: ")
            sp.setKeyboardTracking(False)

            def resize(v, key=dev["key"], zi=zi):
                self.s["devices"].setdefault(key, {"enabled": True, "zones": {}})["zones"][str(zi)] = v
                self.sync.reconfigure = True
                self.save()
            sp.valueChanged.connect(resize)
            self.zone_box.addWidget(sp)
        self.zone_box.addStretch()
