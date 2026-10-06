from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QWidget

HANDLE_LOCAL = [(0, 0), (0.5, 0), (1, 0), (1, 0.5), (1, 1), (0.5, 1), (0, 1), (0, 0.5)]
LEFT, RIGHT, TOP, BOTTOM = {0, 6, 7}, {2, 3, 4}, {0, 1, 2}, {4, 5, 6}
CORNERS = {0, 2, 4, 6}
HANDLE_CURSORS = [Qt.SizeFDiagCursor, Qt.SizeVerCursor, Qt.SizeBDiagCursor, Qt.SizeHorCursor] * 2


def item_transform(it, x=None, y=None, w=None, h=None):
    x = it["x"] if x is None else x
    y = it["y"] if y is None else y
    w = it["w"] if w is None else w
    h = it["h"] if h is None else h
    return QTransform().translate(x + w / 2, y + h / 2).rotate(it["rotation"]).translate(-w / 2, -h / 2)


def render_scene(scene, sources, cw, ch):
    img = QImage(cw, ch, QImage.Format_RGB32)
    img.fill(QColor(scene.get("bg", "#ff000000")))
    p = QPainter(img)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setRenderHint(QPainter.Antialiasing)
    for it in scene["items"]:
        src = sources.get(it["id"])
        if not it["visible"] or src is None or it["w"] < 1 or it["h"] < 1:
            continue
        p.save()
        p.setOpacity(it["opacity"] / 100)
        p.setTransform(item_transform(it))
        p.setClipRect(QRectF(0, 0, it["w"], it["h"]))
        try:
            src.paint(p, it["w"], it["h"], it["fit"])
        except Exception as e:  # один сломанный слой не должен ронять весь кадр
            p.setPen(QColor("red"))
            p.drawText(QRectF(0, 0, it["w"], it["h"]), Qt.AlignCenter, str(e))
        p.restore()
    p.end()
    return img


class CanvasEditor(QWidget):
    """Холст в стиле OBS: выбор, перемещение, изменение размера слоёв мышью."""

    selected = Signal(str)          # id слоя или ""
    edit_started = Signal()         # перед изменением — для отмены
    geometry_changed = Signal(bool)  # True — изменение завершено
    context_menu = Signal(str, QPointF)
    files_dropped = Signal(list, QPointF)
    nudge = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.scene = None
        self.cw, self.ch = 1920, 480
        self.frame = None
        self.sel = ""
        self.mode = None
        self.guides = []
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)

    # ---------- геометрия вида ----------

    def _view(self):
        k = min((self.width() - 20) / self.cw, (self.height() - 20) / self.ch)
        k = max(k, 0.01)
        return k, (self.width() - self.cw * k) / 2, (self.height() - self.ch * k) / 2

    def to_canvas(self, pt):
        k, ox, oy = self._view()
        return QPointF((pt.x() - ox) / k, (pt.y() - oy) / k)

    def to_view(self, pt):
        k, ox, oy = self._view()
        return QPointF(pt.x() * k + ox, pt.y() * k + oy)

    def _item(self, iid):
        return next((it for it in self.scene["items"] if it["id"] == iid), None) if self.scene else None

    def _handles(self, it):
        t = item_transform(it)
        return [self.to_view(t.map(QPointF(a * it["w"], b * it["h"]))) for a, b in HANDLE_LOCAL]

    def _hit_handle(self, pos):
        it = self._item(self.sel)
        if not it or it["locked"]:
            return None
        for i, hp in enumerate(self._handles(it)):
            if abs(hp.x() - pos.x()) <= 7 and abs(hp.y() - pos.y()) <= 7:
                return i
        return None

    def _hit_item(self, cpt):
        for it in reversed(self.scene["items"]):
            if not it["visible"]:
                continue
            lp, ok = item_transform(it).inverted()
            p = lp.map(cpt)
            if 0 <= p.x() <= it["w"] and 0 <= p.y() <= it["h"]:
                return it
        return None

    # ---------- отрисовка ----------

    def set_frame(self, img):
        self.frame = img
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 31, 36))
        k, ox, oy = self._view()
        canvas = QRectF(ox, oy, self.cw * k, self.ch * k)
        if self.frame is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(canvas, self.frame)
        p.setPen(QPen(QColor(90, 90, 100), 1))
        p.drawRect(canvas)
        if not self.scene:
            return
        p.setRenderHint(QPainter.Antialiasing)
        for it in self.scene["items"]:
            if it["visible"] and it["id"] != self.sel:
                h = self._handles(it)
                p.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawPolygon(QPolygonF([h[0], h[2], h[4], h[6]]))
        it = self._item(self.sel)
        if it:
            h = self._handles(it)
            color = QColor(255, 160, 0) if it["locked"] else QColor(255, 60, 60)
            p.setPen(QPen(color, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([h[0], h[2], h[4], h[6]]))
            if not it["locked"]:
                p.setBrush(color)
                for hp in h:
                    p.drawRect(QRectF(hp.x() - 4, hp.y() - 4, 8, 8))
        p.setPen(QPen(QColor(0, 220, 255), 1, Qt.DashLine))
        for g in self.guides:
            p.drawLine(QLineF(self.to_view(g.p1()), self.to_view(g.p2())))

    # ---------- мышь ----------

    def mousePressEvent(self, e):
        self.setFocus()
        pos = e.position()
        cpt = self.to_canvas(pos)
        if e.button() == Qt.LeftButton:
            hnd = self._hit_handle(pos)
            if hnd is not None:
                it = self._item(self.sel)
            else:
                it = self._hit_item(cpt)
                self._select(it["id"] if it else "")
            if it and not it["locked"]:
                self.mode = ("resize", hnd) if hnd is not None else ("move", None)
                self.press = cpt
                self.g0 = dict(it)
                self.started = False
        elif e.button() == Qt.RightButton:
            it = self._hit_item(cpt)
            self._select(it["id"] if it else "")
            self.context_menu.emit(self.sel, e.globalPosition())

    def _select(self, iid):
        if iid != self.sel:
            self.sel = iid
            self.selected.emit(iid)
            self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self.mode:
            hnd = self._hit_handle(pos)
            if hnd is not None:
                it = self._item(self.sel)
                idx = (hnd + round(it["rotation"] / 45)) % 8
                self.setCursor(HANDLE_CURSORS[idx])
            else:
                it = self._hit_item(self.to_canvas(pos)) if self.scene else None
                self.setCursor(Qt.SizeAllCursor if it and not it["locked"] else Qt.ArrowCursor)
            return
        it = self._item(self.sel)
        if not it:
            return
        if not self.started:
            self.started = True
            self.edit_started.emit()
        cpt = self.to_canvas(pos)
        snap = not (e.modifiers() & Qt.ControlModifier)
        if self.mode[0] == "move":
            self._do_move(it, cpt, snap)
        else:
            self._do_resize(it, cpt, self.mode[1], snap, bool(e.modifiers() & Qt.ShiftModifier))
        self.geometry_changed.emit(False)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.mode and self.started:
            self.geometry_changed.emit(True)
        self.mode = None
        self.guides = []
        self.update()

    def _snap_targets(self, it):
        xs, ys = [0, self.cw / 2, self.cw], [0, self.ch / 2, self.ch]
        for o in self.scene["items"]:
            if o["id"] != it["id"] and o["visible"] and o["rotation"] % 360 == 0:
                xs += [o["x"], o["x"] + o["w"] / 2, o["x"] + o["w"]]
                ys += [o["y"], o["y"] + o["h"] / 2, o["y"] + o["h"]]
        return xs, ys

    def _snap(self, values, targets):
        """Лучшая поправка, чтобы одно из values совпало с одной из targets."""
        thr = 8 / self._view()[0]
        best = None
        for v in values:
            for t in targets:
                if abs(t - v) <= thr and (best is None or abs(t - v) < abs(best[0])):
                    best = (t - v, t)
        return best

    def _do_move(self, it, cpt, snap):
        g = self.g0
        x, y = g["x"] + cpt.x() - self.press.x(), g["y"] + cpt.y() - self.press.y()
        self.guides = []
        if snap:
            xs, ys = self._snap_targets(it)
            if it["rotation"] % 360 == 0:
                vx, vy = [x, x + g["w"] / 2, x + g["w"]], [y, y + g["h"] / 2, y + g["h"]]
            else:
                vx, vy = [x + g["w"] / 2], [y + g["h"] / 2]
            sx, sy = self._snap(vx, xs), self._snap(vy, ys)
            if sx:
                x += sx[0]
                self.guides.append(QLineF(sx[1], 0, sx[1], self.ch))
            if sy:
                y += sy[0]
                self.guides.append(QLineF(0, sy[1], self.cw, sy[1]))
        it["x"], it["y"] = round(x), round(y)

    def _do_resize(self, it, cpt, hnd, snap, free):
        g = self.g0
        t0 = item_transform(g)
        lp = t0.inverted()[0].map(cpt)
        l, t, r, b = 0.0, 0.0, float(g["w"]), float(g["h"])
        if hnd in LEFT:
            l = min(lp.x(), r - 4)
        if hnd in RIGHT:
            r = max(lp.x(), l + 4)
        if hnd in TOP:
            t = min(lp.y(), b - 4)
        if hnd in BOTTOM:
            b = max(lp.y(), t + 4)
        self.guides = []
        keep_aspect = hnd in CORNERS and not free
        if keep_aspect:
            ratio = g["w"] / max(g["h"], 1)
            nw, nh = r - l, b - t
            if nw / g["w"] > nh / g["h"]:
                nh = nw / ratio
            else:
                nw = nh * ratio
            if hnd in LEFT:
                l = r - nw
            else:
                r = l + nw
            if hnd in TOP:
                t = b - nh
            else:
                b = t + nh
        elif snap and g["rotation"] % 360 == 0:
            xs, ys = self._snap_targets(it)
            ox, oy = g["x"], g["y"]
            if hnd in LEFT and (s := self._snap([ox + l], xs)):
                l += s[0]
                self.guides.append(QLineF(s[1], 0, s[1], self.ch))
            if hnd in RIGHT and (s := self._snap([ox + r], xs)):
                r += s[0]
                self.guides.append(QLineF(s[1], 0, s[1], self.ch))
            if hnd in TOP and (s := self._snap([oy + t], ys)):
                t += s[0]
                self.guides.append(QLineF(0, s[1], self.cw, s[1]))
            if hnd in BOTTOM and (s := self._snap([oy + b], ys)):
                b += s[0]
                self.guides.append(QLineF(0, s[1], self.cw, s[1]))
        w, h = r - l, b - t
        c = t0.map(QPointF((l + r) / 2, (t + b) / 2))
        it["w"], it["h"] = round(w), round(h)
        it["x"], it["y"] = round(c.x() - w / 2), round(c.y() - h / 2)

    def keyPressEvent(self, e):
        step = 10 if e.modifiers() & Qt.ShiftModifier else 1
        d = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0), Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}
        if e.key() in d and self.sel:
            self.nudge.emit(*d[e.key()])
        else:
            super().keyPressEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths, self.to_canvas(e.position()))
