"""Виджеты мониторинга: датчик в разных видах, настраиваемая панель, процессы, текст с датчиками."""
import math
import re
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen

from .sensors import default_range, fmt_value, hub

STYLES = [("ring", "Кольцо"), ("gauge", "Спидометр"), ("bar", "Полоса"), ("vbar", "Столбик"),
          ("graph", "График"), ("number", "Крупное число"), ("text", "Строка текста")]

AUTO_THRESHOLDS = {"%": (80, 92), "°C": (75, 88)}

DEFAULT_TILES = ["auto/cpu_load", "auto/cpu_temp", "auto/gpu_load", "auto/gpu_temp", "auto/ram_load",
                 "auto/vram_load", "auto/net_down", "auto/disk_read"]


def fit_font(p, text, w, h, family="Segoe UI", bold=True, fill=0.92):
    font = QFont(family)
    font.setBold(bold)
    lines = text.splitlines() or [" "]
    font.setPixelSize(100)
    p.setFont(font)
    fm = p.fontMetrics()
    tw = max(fm.horizontalAdvance(l) for l in lines) or 1
    th = fm.height() * len(lines)
    font.setPixelSize(max(5, int(100 * min(w * fill / tw, h * fill / th))))
    return font


def draw_text_fit(p, rect, text, color, family, bold=True, align=Qt.AlignCenter):
    if rect.width() < 2 or rect.height() < 2 or not text:
        return
    p.setPen(QColor(color))
    p.setFont(fit_font(p, text, rect.width(), rect.height(), family, bold))
    p.drawText(rect, align | Qt.AlignVCenter, text)


class SensorWidget:
    """Отрисовка одного датчика. Используется слоем «Датчик» и плитками панели."""

    def __init__(self, sensor="auto/cpu_load", style="ring", label="", show_label=True, show_value=True,
                 unit="", decimals=-1, min=0.0, max=0.0, color="#ff00c8ff", warn=0.0, warn_color="#ffffb43c",
                 crit=0.0, crit_color="#ffff5050", text_color="#ffffffff", track_color="#28ffffff",
                 bg="#00000000", history=60, thickness=10, font="Segoe UI", auto_thresholds=False, **_):
        self.sensor, self.style, self.label = sensor, style, label
        self.show_label, self.show_value = show_label, show_value
        self.unit, self.decimals = unit, decimals
        self.vmin, self.vmax = min, max
        self.color, self.warn, self.warn_color = QColor(color), warn, QColor(warn_color)
        self.crit, self.crit_color = crit, QColor(crit_color)
        self.text_color, self.track = QColor(text_color), QColor(track_color)
        self.bg = QColor(bg)
        self.history, self.thickness, self.family = history, thickness, font
        self.auto_thresholds = auto_thresholds

    def _value_color(self, v, unit=""):
        warn, crit = self.warn, self.crit
        if self.auto_thresholds:
            warn, crit = AUTO_THRESHOLDS.get(unit, (0, 0))
        if v is not None and crit and v >= crit:
            return self.crit_color
        if v is not None and warn and v >= warn:
            return self.warn_color
        return self.color

    def paint(self, p, w, h):
        p.setRenderHint(QPainter.Antialiasing)
        if self.bg.alpha():
            r = min(w, h) * 0.08
            p.setPen(Qt.NoPen)
            p.setBrush(self.bg)
            p.drawRoundedRect(QRectF(0, 0, w, h), r, r)
            pad = min(w, h) * 0.06
            p.translate(pad, pad)
            w, h = w - pad * 2, h - pad * 2
        meta = hub.get_meta(self.sensor) or {}
        v = hub.get(self.sensor)
        unit = self.unit or meta.get("unit", "")
        label = self.label or meta.get("name", self.sensor)
        text = fmt_value(v, unit, self.decimals) if v is not None else "нет данных"
        if self.unit and v is not None and meta.get("unit") != self.unit and meta.get("unit") not in ("Б/с",):
            text = f"{v:.{max(self.decimals, 0)}f} {self.unit}".strip()
        lo, hi = self.vmin, self.vmax
        if not hi > lo:
            hist = hub.get_history(self.sensor, 300)
            lo, hi = default_range(meta.get("unit", ""), max(hist) if hist else 0)
        frac = 0 if v is None else min(max((v - lo) / (hi - lo), 0), 1)
        color = self._value_color(v, meta.get("unit", ""))
        style = self.style
        lab = label if self.show_label else ""
        val = text if self.show_value else ""
        getattr(self, f"_{style}", self._ring)(p, w, h, frac, color, lab, val, lo, hi)

    def _arc(self, p, rect, start, span, frac, color, thick):
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self.track, thick, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, int(start * 16), int(span * 16))
        if frac > 0:
            p.setPen(QPen(color, thick, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, int(start * 16), int(span * frac * 16))

    def _ring(self, p, w, h, frac, color, lab, val, *_):
        sz = min(w, h)
        thick = sz * self.thickness / 100
        r = QRectF((w - sz) / 2 + thick / 2, (h - sz) / 2 + thick / 2, sz - thick, sz - thick)
        self._arc(p, r, 90, -360, frac, color, thick)
        inner = r.adjusted(thick, thick, -thick, -thick)
        if lab and val:
            draw_text_fit(p, QRectF(inner.x(), inner.y() + inner.height() * 0.18, inner.width(), inner.height() * 0.4),
                          val, self.text_color, self.family)
            draw_text_fit(p, QRectF(inner.x() + inner.width() * 0.12, inner.y() + inner.height() * 0.6,
                                    inner.width() * 0.76, inner.height() * 0.2), lab, self.text_color, self.family, False)
        else:
            draw_text_fit(p, inner.adjusted(inner.width() * 0.1, inner.height() * 0.25, -inner.width() * 0.1,
                                            -inner.height() * 0.25), val or lab, self.text_color, self.family)

    def _gauge(self, p, w, h, frac, color, lab, val, lo, hi):
        sz = min(w, h * 1.25)
        thick = sz * self.thickness / 100
        r = QRectF((w - sz) / 2 + thick / 2, (h - sz * 0.8) / 2 + thick / 2, sz - thick, sz - thick)
        self._arc(p, r, 210, -240, frac, color, thick)
        # стрелка
        ang = math.radians(210 - 240 * frac)
        c = r.center()
        rad = r.width() / 2 - thick * 1.2
        p.setPen(QPen(self.text_color, max(1.5, thick * 0.25), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + rad * math.cos(ang), c.y() - rad * math.sin(ang)))
        p.setBrush(self.text_color)
        p.drawEllipse(c, thick * 0.4, thick * 0.4)
        bottom = QRectF(r.x() + r.width() * 0.2, c.y() + r.height() * 0.12, r.width() * 0.6, r.height() * 0.22)
        draw_text_fit(p, bottom, val, self.text_color, self.family)
        draw_text_fit(p, QRectF(bottom.x(), bottom.bottom(), bottom.width(), r.height() * 0.12), lab,
                      self.text_color, self.family, False)

    def _bar(self, p, w, h, frac, color, lab, val, *_):
        top = QRectF(0, 0, w, h * 0.55) if (lab or val) else QRectF()
        if lab or val:
            font = fit_font(p, f"{lab}    {val}", w, top.height(), self.family)
            if font.pixelSize() < top.height() * 0.5:  # длинная подпись — обрежем её, а не значение
                small = fit_font(p, val or "0", w * 0.45, top.height(), self.family).pixelSize()
                font.setPixelSize(max(5, int(min(small, top.height() * 0.5))))
            p.setFont(font)
            vw = p.fontMetrics().horizontalAdvance(val + "  ")
            p.setPen(self.text_color)
            p.drawText(top, Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(lab, Qt.ElideRight, int(max(0, w - vw))))
            p.drawText(top, Qt.AlignRight | Qt.AlignVCenter, val)
        bh = max(2, h * (0.3 if (lab or val) else 0.8) * self.thickness / 10)
        bh = min(bh, h - top.height())
        bar = QRectF(0, h - bh - (h - top.height() - bh) / 2, w, bh)
        rad = bh / 2
        p.setPen(Qt.NoPen)
        p.setBrush(self.track)
        p.drawRoundedRect(bar, rad, rad)
        if frac > 0:
            p.setBrush(color)
            p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(bh, w * frac), bh), rad, rad)

    def _vbar(self, p, w, h, frac, color, lab, val, *_):
        th = h * 0.15
        bw = min(w * 0.6, w * self.thickness / 20)
        bar = QRectF((w - bw) / 2, th if val else 0, bw, h - (th if val else 0) - (th if lab else 0))
        rad = min(bw / 2, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(self.track)
        p.drawRoundedRect(bar, rad, rad)
        if frac > 0:
            fh = bar.height() * frac
            p.setBrush(color)
            p.drawRoundedRect(QRectF(bar.x(), bar.bottom() - fh, bw, fh), rad, rad)
        draw_text_fit(p, QRectF(0, 0, w, th), val, self.text_color, self.family)
        draw_text_fit(p, QRectF(0, h - th, w, th), lab, self.text_color, self.family, False)

    def _graph(self, p, w, h, frac, color, lab, val, lo, hi):
        head = h * 0.25 if (lab or val) else 0
        if head:
            p.setFont(fit_font(p, f"{lab}    {val}", w, head, self.family))
            p.setPen(self.text_color)
            p.drawText(QRectF(0, 0, w, head), Qt.AlignLeft | Qt.AlignVCenter, lab)
            p.setPen(color)
            p.drawText(QRectF(0, 0, w, head), Qt.AlignRight | Qt.AlignVCenter, val)
        area = QRectF(0, head, w, h - head)
        pts = hub.get_history(self.sensor, max(2, int(self.history / max(hub.interval, 0.2))))
        p.setPen(QPen(self.track, 1))
        for i in range(1, 4):
            y = area.y() + area.height() * i / 4
            p.drawLine(QPointF(0, y), QPointF(w, y))
        if len(pts) < 2:
            return
        n = max(2, int(self.history / max(hub.interval, 0.2)))
        step = w / (n - 1)
        x0 = w - step * (len(pts) - 1)
        path = QPainterPath()
        for i, v in enumerate(pts):
            f = min(max((v - lo) / (hi - lo), 0), 1)
            pt = QPointF(x0 + i * step, area.bottom() - f * area.height())
            path.lineTo(pt) if i else path.moveTo(pt)
        fill = QPainterPath(path)
        fill.lineTo(w, area.bottom())
        fill.lineTo(x0, area.bottom())
        g = QLinearGradient(0, area.top(), 0, area.bottom())
        c1 = QColor(color)
        c1.setAlpha(110)
        c2 = QColor(color)
        c2.setAlpha(0)
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillPath(fill, g)
        p.setPen(QPen(color, max(1.5, h * self.thickness / 400), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def _number(self, p, w, h, frac, color, lab, val, *_):
        lh = h * 0.28 if lab else 0
        draw_text_fit(p, QRectF(0, 0, w, lh), lab, self.text_color, self.family, False)
        draw_text_fit(p, QRectF(0, lh, w, h - lh), val, color, self.family)

    def _text(self, p, w, h, frac, color, lab, val, *_):
        draw_text_fit(p, QRectF(0, 0, w, h), f"{lab} {val}".strip(), self.text_color, self.family)


class SensorSource:
    finished = False

    def __init__(self, **props):
        self.widget = SensorWidget(**props)

    def start(self):
        hub.acquire()
        hub.register(self.widget.sensor)

    def stop(self):
        hub.release()

    def paint(self, p, w, h, fit):
        self.widget.paint(p, w, h)


class DashboardSource:
    """Панель из плиток; список датчиков и вид задаются пользователем."""
    finished = False

    def __init__(self, tiles=None, style="ring", columns=0, accent="#ff00c8ff", text_color="#ffffffff",
                 bg="#ff0a0c12", tile_bg="#ff161a24", gap=2, font="Segoe UI", **_):
        self.tiles = tiles if tiles is not None else list(DEFAULT_TILES)
        self.columns, self.gap = columns, gap
        self.bg = QColor(bg)
        self.widgets = [SensorWidget(sensor=s, style=style, color=accent, text_color=text_color, bg=tile_bg,
                                     font=font, auto_thresholds=style != "graph")
                        for s in self.tiles]

    def start(self):
        hub.acquire()
        for s in self.tiles:
            hub.register(s)

    def stop(self):
        hub.release()

    def layout(self, w, h):
        n = len(self.widgets)
        if not n:
            return []
        cols = self.columns or (n if w / h >= 2.2 else math.ceil(math.sqrt(n * w / h)))
        cols = max(1, min(cols, n))
        rows = math.ceil(n / cols)
        gap = min(w, h) * self.gap / 100
        tw, th = (w - gap * (cols + 1)) / cols, (h - gap * (rows + 1)) / rows
        return [QRectF(gap + (i % cols) * (tw + gap), gap + (i // cols) * (th + gap), tw, th) for i in range(n)]

    def paint(self, p, w, h, fit):
        if self.bg.alpha():
            p.fillRect(QRectF(0, 0, w, h), self.bg)
        if not self.widgets:
            draw_text_fit(p, QRectF(0, 0, w, h * 0.3), "Добавьте датчики в свойствах", "#888888", "Segoe UI")
        for wd, r in zip(self.widgets, self.layout(w, h)):
            p.save()
            p.translate(r.topLeft())
            p.setClipRect(QRectF(0, 0, r.width(), r.height()))
            wd.paint(p, r.width(), r.height())
            p.restore()


class ProcessesSource:
    """Топ процессов по CPU или памяти."""
    finished = False

    def __init__(self, count=5, sort="cpu", text_color="#ffffffff", color="#ff00c8ff", bg="#00000000",
                 font="Segoe UI", **_):
        self.count, self.sort = count, sort
        self.text_color, self.color, self.bg, self.family = QColor(text_color), QColor(color), QColor(bg), font

    def start(self):
        hub.acquire()
        hub.want_procs += 1

    def stop(self):
        hub.release()
        hub.want_procs = max(0, hub.want_procs - 1)

    def paint(self, p, w, h, fit):
        if self.bg.alpha():
            p.fillRect(QRectF(0, 0, w, h), self.bg)
        key = 1 if self.sort == "cpu" else 2
        rows = sorted(hub.procs, key=lambda r: r[key], reverse=True)[:self.count]
        if not rows:
            draw_text_fit(p, QRectF(0, 0, w, h * 0.3), "Сбор данных...", "#888888", self.family)
            return
        rh = h / self.count
        font = QFont(self.family)
        font.setPixelSize(max(6, int(rh * 0.6)))
        p.setFont(font)
        for i, (name, cpu, ram) in enumerate(rows):
            r = QRectF(0, i * rh, w, rh)
            val = f"{cpu:.1f}%" if self.sort == "cpu" else fmt_value(ram, "МБ", 0)
            p.setPen(self.text_color)
            p.drawText(r.adjusted(0, 0, -w * 0.3, 0), Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(name.removesuffix(".exe"), Qt.ElideRight, int(w * 0.68)))
            p.setPen(self.color)
            p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, val)


PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def has_placeholders(text):
    return bool(PLACEHOLDER.search(text or ""))


def substitute(text):
    """{id датчика}, {id|0} — без единицы с 0 знаков, {time}, {date}, {cpu_name}, {gpu_name}, {os}, {host}."""
    now = datetime.now()

    def rep(m):
        key = m.group(1)
        sid, _, opt = key.partition("|")
        if sid == "time":
            return now.strftime(opt or "%H:%M")
        if sid == "date":
            return now.strftime(opt or "%d.%m.%Y")
        if sid in hub.info:
            return hub.info[sid]
        v = hub.get(sid)
        if v is None:
            return "—" if (hub.get_meta(sid) or sid.startswith(("auto/", "lhm/", "sys/", "nv/", "ping/"))) else m.group(0)
        if opt.isdigit():
            return f"{v:.{int(opt)}f}"
        return fmt_value(v, (hub.get_meta(sid) or {}).get("unit", ""))
    return PLACEHOLDER.sub(rep, text)
