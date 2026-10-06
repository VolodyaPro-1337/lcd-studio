from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFontComboBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .monitor import STYLES
from .sensorpicker import SensorListDialog, pick_sensor, sensor_title
from .sources import IMAGE_EXT, VIDEO_EXT, list_windows

IMG_FILTER = "Картинки (" + " ".join("*" + e for e in sorted(IMAGE_EXT)) + ")"
VID_FILTER = "Видео (" + " ".join("*" + e for e in sorted(VIDEO_EXT)) + ")"


def _monitors():
    import mss
    with getattr(mss, "MSS", mss.mss)() as s:
        mons = s.monitors
    out = [(0, f"Все мониторы ({mons[0]['width']}x{mons[0]['height']})")]
    out += [(i, f"Монитор {i}: {m['width']}x{m['height']}") for i, m in enumerate(mons[1:], 1)]
    return out


# тип -> [(ключ, подпись, вид, параметры)]
SCHEMA = {
    "screen": [("monitor", "Монитор", "choice", _monitors), ("cursor", "Курсор мыши", "bool", None),
               ("use_region", "Только область", "bool", None), ("region", "Область X Y Ш В", "region", None)],
    "window": [("title", "Окно", "window", None)],
    "image": [("path", "Файл", "file", IMG_FILTER)],
    "video": [("path", "Файл", "file", VID_FILTER)],
    "slideshow": [("folder", "Папка", "dir", None), ("interval", "Картинка, с", "int", (1, 3600, ""))],
    "text": [("text", "Текст", "multiline", None), ("font", "Шрифт", "font", None),
             ("bold", "Жирный", "bool", None), ("italic", "Курсив", "bool", None),
             ("size", "Размер (0 — авто)", "int", (0, 2000, " px")),
             ("align", "Выравнивание", "choice", [("left", "Слева"), ("center", "По центру"), ("right", "Справа")]),
             ("color", "Цвет", "color", None), ("bg", "Фон", "color", None),
             ("speed", "Бегущая строка", "int", (0, 3000, " px/с"))],
    "clock": [("format", "Формат", "line", None), ("font", "Шрифт", "font", None), ("bold", "Жирный", "bool", None),
              ("color", "Цвет", "color", None)],
    "sensor": [("sensor", "Датчик", "sensor", None), ("style", "Вид", "choice", STYLES),
               ("label", "Подпись (пусто — авто)", "line", None), ("show_label", "Показывать подпись", "bool", None),
               ("show_value", "Показывать значение", "bool", None),
               ("decimals", "Знаков после запятой", "int", (-1, 4, "", "авто")),
               ("unit", "Своя единица", "line", None),
               ("min", "Шкала: минимум", "float", None), ("max", "Шкала: максимум", "float", "авто"),
               ("color", "Цвет", "color", None),
               ("warn", "Порог «внимание»", "float", "выкл"), ("warn_color", "Цвет «внимание»", "color", None),
               ("crit", "Порог «опасно»", "float", "выкл"), ("crit_color", "Цвет «опасно»", "color", None),
               ("text_color", "Цвет текста", "color", None), ("track_color", "Цвет фона шкалы", "color", None),
               ("bg", "Подложка", "color", None), ("thickness", "Толщина линий", "int", (1, 50, " %")),
               ("history", "График: период", "int", (5, 900, " с")), ("font", "Шрифт", "font", None)],
    "sensors": [("tiles", "Датчики", "sensorlist", None), ("style", "Вид плиток", "choice", STYLES),
                ("columns", "Колонок", "int", (0, 30, "", "авто")), ("gap", "Отступы", "int", (0, 20, " %")),
                ("accent", "Цвет шкал", "color", None), ("text_color", "Цвет текста", "color", None),
                ("tile_bg", "Фон плиток", "color", None), ("bg", "Фон панели", "color", None),
                ("font", "Шрифт", "font", None)],
    "procs": [("count", "Сколько процессов", "int", (1, 30, "")),
              ("sort", "Сортировка", "choice", [("cpu", "По загрузке CPU"), ("ram", "По памяти")]),
              ("text_color", "Цвет названий", "color", None), ("color", "Цвет значений", "color", None),
              ("bg", "Фон", "color", None), ("font", "Шрифт", "font", None)],
    "color": [("color", "Цвет", "color", None), ("color2", "Второй цвет (градиент)", "color_opt", None),
              ("angle", "Угол градиента", "int", (0, 360, "°"))],
}

HINTS = {
    "clock": "%H часы, %M минуты, %S секунды, %d.%m.%Y дата, %A день недели, | перенос строки",
    "window": "Окно захватывается, даже если перекрыто. Свёрнутое показать нельзя.",
    "video": "Видео крутится по кругу, без звука.",
    "slideshow": "Картинки и видео из папки по очереди. Видео играют до конца.",
    "sensor": "Пороги: при значении выше порога цвет меняется. Максимум 0 — шкала подбирается сама.",
    "sensors": "Правый клик по панели на холсте → «Разложить на отдельные слои» — "
               "каждая плитка станет отдельным датчиком, который можно двигать и настраивать.",
    "text": "Датчики в тексте: {ID датчика}, например «CPU {auto/cpu_temp}». {ID|1} — число без единицы "
            "с 1 знаком. Также {time}, {date}, {cpu_name}, {gpu_name}, {os}, {host}.",
}


def color_button(value, on_change, optional=False):
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    btn = QPushButton()
    btn.setFixedWidth(70)
    state = {"v": value}

    def paint():
        v = state["v"]
        btn.setText("" if v else "нет")
        btn.setStyleSheet(f"background:{QColor(v).name()}" if v else "")
        btn.setToolTip(f"Прозрачность {QColor(v).alpha() * 100 // 255}%" if v else "")

    def pick():
        c = QColorDialog.getColor(QColor(state["v"] or "#ffffffff"), btn, "Цвет", QColorDialog.ShowAlphaChannel)
        if c.isValid():
            state["v"] = c.name(QColor.HexArgb)
            paint()
            on_change(state["v"])
    btn.clicked.connect(pick)
    lay.addWidget(btn)
    if optional:
        clr = QPushButton("Убрать")
        clr.clicked.connect(lambda: (state.update(v=""), paint(), on_change("")))
        lay.addWidget(clr)
    lay.addStretch()
    paint()
    return box


def build_editor(form, item, on_change, own_title=""):
    """Добавляет в form редакторы свойств источника; on_change(key, value)."""
    pr = item["props"]
    for key, label, kind, arg in SCHEMA[item["type"]]:
        v = pr[key]
        set_ = lambda val, k=key: on_change(k, val)
        if kind == "bool":
            w = QCheckBox()
            w.setChecked(v)
            w.toggled.connect(set_)
        elif kind == "int":
            w = QSpinBox()
            lo, hi, suffix = arg[:3]
            w.setRange(lo, hi)
            w.setSuffix(suffix)
            if len(arg) > 3:
                w.setSpecialValueText(arg[3])
            w.setValue(v)
            w.valueChanged.connect(set_)
        elif kind == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(2)
            if arg:
                w.setRange(0, 1e9)
                w.setSpecialValueText(arg)
            w.setValue(v)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(set_)
        elif kind == "line":
            w = QLineEdit(v)
            w.textChanged.connect(set_)
        elif kind == "multiline":
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            edit = QPlainTextEdit(v)
            edit.setMaximumHeight(80)
            edit.textChanged.connect(lambda e=edit, k=key: on_change(k, e.toPlainText()))
            vl.addWidget(edit)
            ins = QPushButton("Вставить датчик...")

            def insert(_=False, e=edit):
                sid = pick_sensor(e)
                if sid:
                    e.insertPlainText("{" + sid + "}")
            ins.clicked.connect(insert)
            vl.addWidget(ins)
        elif kind == "sensor":
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(sensor_title(v))
            lbl.setWordWrap(True)
            btn = QPushButton("Выбрать...")

            def choose(_=False, l=lbl, k=key):
                sid = pick_sensor(l, item["props"][k])
                if sid:
                    l.setText(sensor_title(sid))
                    on_change(k, sid)
            btn.clicked.connect(choose)
            lay.addWidget(lbl, 1)
            lay.addWidget(btn)
        elif kind == "sensorlist":
            w = QPushButton(f"Настроить ({len(v)})...")

            def edit_list(_=False, b=w, k=key):
                dlg = SensorListDialog(b, item["props"][k])
                if dlg.exec() == QDialog.Accepted:
                    ids = dlg.ids()
                    b.setText(f"Настроить ({len(ids)})...")
                    on_change(k, ids)
            w.clicked.connect(edit_list)
        elif kind == "font":
            w = QFontComboBox()
            w.setCurrentFont(QFont(v))
            w.currentFontChanged.connect(lambda f, k=key: on_change(k, f.family()))
        elif kind in ("color", "color_opt"):
            w = color_button(v, set_, optional=kind == "color_opt")
        elif kind == "choice":
            w = QComboBox()
            for val, text in (arg() if callable(arg) else arg):
                w.addItem(text, val)
            w.setCurrentIndex(max(0, w.findData(v)))
            w.currentIndexChanged.connect(lambda _, ww=w, k=key: on_change(k, ww.currentData()))
        elif kind in ("file", "dir"):
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(v)
            edit.setReadOnly(True)
            btn = QPushButton("Обзор...")

            def browse(_=False, e=edit, k=key, kd=kind, flt=arg):
                path = (QFileDialog.getExistingDirectory(e, "Папка") if kd == "dir"
                        else QFileDialog.getOpenFileName(e, "Файл", "", flt)[0])
                if path:
                    e.setText(path)
                    on_change(k, path)
            btn.clicked.connect(browse)
            lay.addWidget(edit, 1)
            lay.addWidget(btn)
        elif kind == "region":
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            spins = []
            for i in range(4):
                sp = QSpinBox()
                sp.setRange(-20000, 20000)
                sp.setValue(v[i])
                spins.append(sp)
                lay.addWidget(sp)
            for sp in spins:
                sp.valueChanged.connect(lambda _, ss=spins, k=key: on_change(k, [s.value() for s in ss]))
        elif kind == "window":
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.setMinimumContentsLength(15)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            refresh = QPushButton("Обновить")

            def fill(_=False, c=combo, cur=v):
                c.blockSignals(True)
                c.clear()
                c.addItems([t for t in list_windows() if t != own_title])
                c.setCurrentText(c.currentText() or cur)
                c.blockSignals(False)
            fill()
            combo.setCurrentText(v)
            refresh.clicked.connect(fill)
            combo.currentTextChanged.connect(set_)
            lay.addWidget(combo, 1)
            lay.addWidget(refresh)
        else:
            continue
        form.addRow(label + ":", w)
    if item["type"] in HINTS:
        hint = QLabel(HINTS[item["type"]])
        hint.setWordWrap(True)
        hint.setStyleSheet("color:gray")
        form.addRow(hint)
