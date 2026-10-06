import ctypes
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
from PIL import Image, ImageSequence
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen, QPolygonF

from .monitor import DashboardSource, ProcessesSource, SensorSource, has_placeholders, substitute
from .sensors import hub

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".m4v", ".flv"}


def draw_fitted(p: QPainter, img: QImage, w, h, fit):
    if img is None or img.isNull():
        return
    sw, sh = img.width(), img.height()
    if fit == "stretch":
        p.drawImage(QRectF(0, 0, w, h), img)
        return
    k = max(w / sw, h / sh) if fit == "cover" else min(w / sw, h / sh)
    dw, dh = sw * k, sh * k
    p.drawImage(QRectF((w - dw) / 2, (h - dh) / 2, dw, dh), img)


def draw_message(p, w, h, text):
    p.setPen(QColor(150, 150, 150))
    f = QFont("Segoe UI")
    f.setPixelSize(max(14, h // 12))
    p.setFont(f)
    p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter | Qt.TextWordWrap, text)


class Source:
    finished = False

    def start(self):
        pass

    def stop(self):
        pass

    def paint(self, p: QPainter, w, h, fit):
        raise NotImplementedError


class ImageSource(Source):
    """Картинка; GIF/WebP проигрываются с родными задержками кадров."""

    def __init__(self, path):
        self.path = path
        self.frames = []
        self.durations = []
        self.t0 = 0
        self.error = None

    def start(self):
        self.t0 = time.monotonic()
        self.finished = False
        if not self.path:
            return
        try:
            im = Image.open(self.path)
            for fr in ImageSequence.Iterator(im):
                rgba = fr.convert("RGBA")
                q = QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format_RGBA8888).copy()
                self.frames.append(q)
                self.durations.append(max(fr.info.get("duration", 100), 20) / 1000)
        except Exception as e:
            self.error = f"Не удалось открыть картинку: {e}"

    def stop(self):
        self.frames.clear()
        self.durations.clear()

    def paint(self, p, w, h, fit):
        if not self.frames:
            draw_message(p, w, h, self.error or "Выберите картинку")
            return
        if len(self.frames) == 1:
            draw_fitted(p, self.frames[0], w, h, fit)
            return
        t = (time.monotonic() - self.t0) % sum(self.durations)
        for fr, d in zip(self.frames, self.durations):
            if t < d:
                break
            t -= d
        draw_fitted(p, fr, w, h, fit)


class VideoSource(Source):
    """Декодирует видео в отдельном потоке в реальном темпе ролика."""

    def __init__(self, path, loop=True):
        self.path = path
        self.loop = loop
        self.frame = None
        self.error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.finished = False
        if not self.path:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.frame = None

    def _run(self):
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            self.error = "Не удалось открыть видео"
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        if fps > 240 or fps < 1:
            fps = 30
        t0, n = time.monotonic(), 0
        while not self._stop.is_set():
            target = int((time.monotonic() - t0) * fps)
            if target - n > fps:  # сильно отстали — прыгаем вперёд
                cap.set(cv2.CAP_PROP_POS_FRAMES, cap.get(cv2.CAP_PROP_POS_FRAMES) + (target - n))
                n = target
            while n < target - 1 and cap.grab():
                n += 1
            ok, bgr = cap.read()
            if not ok:
                if not self.loop:
                    self.finished = True
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                t0, n = time.monotonic(), 0
                continue
            n += 1
            h, w = bgr.shape[:2]
            q = QImage(bgr.data, w, h, bgr.strides[0], QImage.Format_BGR888).copy()
            with self._lock:
                self.frame = q
            delay = t0 + n / fps - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
        cap.release()

    def paint(self, p, w, h, fit):
        with self._lock:
            fr = self.frame
        if fr is None:
            draw_message(p, w, h, self.error or ("Загрузка..." if self.path else "Выберите видео или перетащите его в окно"))
        else:
            draw_fitted(p, fr, w, h, fit)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class ScreenSource(Source):
    """Дубликат рабочего стола: весь монитор или выбранная область."""

    def __init__(self, monitor=1, region=None, cursor=True):
        self.monitor = monitor
        self.region = region
        self.cursor = cursor
        self.sct = None

    def start(self):
        self.sct = getattr(mss, "MSS", mss.mss)()

    def stop(self):
        if self.sct:
            self.sct.close()
            self.sct = None

    def _area(self):
        if self.region:
            x, y, w, h = self.region
            return {"left": x, "top": y, "width": max(w, 1), "height": max(h, 1)}
        mons = self.sct.monitors
        return mons[self.monitor] if 0 <= self.monitor < len(mons) else mons[1]

    def paint(self, p, w, h, fit):
        if not self.sct:
            self.start()
        area = self._area()
        try:
            shot = self.sct.grab(area)
        except mss.exception.ScreenShotError as e:
            draw_message(p, w, h, f"Ошибка захвата: {e}")
            return
        img = QImage(shot.raw, shot.width, shot.height, QImage.Format_RGB32)
        if self.cursor:
            img = img.copy()
            pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            cx, cy = pt.x - area["left"], pt.y - area["top"]
            if 0 <= cx < shot.width and 0 <= cy < shot.height:
                self._draw_cursor(img, cx, cy, max(1.0, shot.height / 1080))
        draw_fitted(p, img, w, h, fit)

    @staticmethod
    def _draw_cursor(img, x, y, s):
        pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
        poly = QPolygonF([QPointF(x + a * s * 1.3, y + b * s * 1.3) for a, b in pts])
        cp = QPainter(img)
        cp.setRenderHint(QPainter.Antialiasing)
        cp.setPen(QPen(QColor("black"), 1.2 * s))
        cp.setBrush(QColor("white"))
        cp.drawPolygon(poly)
        cp.end()


class SlideshowSource(Source):
    """Картинки и видео из папки по кругу. Видео играет до конца."""

    def __init__(self, folder, interval):
        self.folder = folder
        self.interval = interval
        self.files = []
        self.idx = -1
        self.cur = None
        self.t0 = 0

    def start(self):
        try:
            self.files = sorted(f for f in Path(self.folder).iterdir()
                                if f.suffix.lower() in IMAGE_EXT | VIDEO_EXT)
        except OSError:
            self.files = []
        self._next()

    def stop(self):
        if self.cur:
            self.cur.stop()
            self.cur = None

    def _next(self):
        self.stop()
        if not self.files:
            return
        self.idx = (self.idx + 1) % len(self.files)
        f = self.files[self.idx]
        self.cur = VideoSource(str(f), loop=False) if f.suffix.lower() in VIDEO_EXT else ImageSource(str(f))
        self.cur.start()
        self.t0 = time.monotonic()

    def paint(self, p, w, h, fit):
        if not self.cur:
            draw_message(p, w, h, "Выберите папку с картинками и видео")
            return
        is_video = isinstance(self.cur, VideoSource)
        if (is_video and self.cur.finished) or (not is_video and time.monotonic() - self.t0 >= self.interval):
            self._next()
        self.cur.paint(p, w, h, fit)


def fit_font(p, text, w, h, family="Segoe UI", bold=True, italic=False, size=0, fill=0.9):
    """Шрифт максимального размера, при котором текст влезает в w x h."""
    font = QFont(family)
    font.setBold(bold)
    font.setItalic(italic)
    if size:
        font.setPixelSize(size)
        return font
    lines = text.splitlines() or [" "]
    font.setPixelSize(100)
    p.setFont(font)
    fm = p.fontMetrics()
    tw = max(fm.horizontalAdvance(l) for l in lines) or 1
    th = fm.height() * len(lines)
    font.setPixelSize(max(6, int(100 * min(w * fill / tw, h * fill / th))))
    return font


ALIGN = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}


class TextSource(Source):
    """Статичный текст или бегущая строка."""

    def __init__(self, text="", color="#ffffffff", bg="#00000000", speed=0, size=0, font="Segoe UI",
                 bold=True, italic=False, align="center"):
        self.text = text or " "
        self.color = QColor(color)
        self.bg = QColor(bg)
        self.speed = speed  # пикселей в секунду, 0 — без прокрутки
        self.size = size    # 0 — подобрать под размер слоя
        self.family, self.bold, self.italic, self.align = font, bold, italic, align
        self.t0 = 0
        self.live = False

    def start(self):
        self.t0 = time.monotonic()
        self.live = has_placeholders(self.text)
        if self.live:
            hub.acquire()
            for sid in re.findall(r"\{([^{}|]+)", self.text):
                hub.register(sid)

    def stop(self):
        if self.live:
            hub.release()

    def paint(self, p, w, h, fit):
        if self.bg.alpha():
            p.fillRect(QRectF(0, 0, w, h), self.bg)
        p.setPen(self.color)
        text = substitute(self.text) if self.live else self.text
        if self.speed <= 0:
            p.setFont(fit_font(p, text, w, h, self.family, self.bold, self.italic, self.size))
            p.drawText(QRectF(0, 0, w, h), ALIGN.get(self.align, Qt.AlignHCenter) | Qt.AlignVCenter, text)
            return
        line = " ".join(text.split())
        font = QFont(self.family)
        font.setBold(self.bold)
        font.setItalic(self.italic)
        font.setPixelSize(self.size or max(6, int(h * 0.7)))
        p.setFont(font)
        tw = p.fontMetrics().horizontalAdvance(line) + w * 0.3
        x = w - ((time.monotonic() - self.t0) * self.speed) % (tw + w)
        p.save()
        p.setClipRect(QRectF(0, 0, w, h))
        p.drawText(QRectF(x, 0, tw, h), Qt.AlignVCenter | Qt.AlignLeft, line)
        p.restore()


class ClockSource(Source):
    """Часы/дата по формату strftime; | — перенос строки."""

    def __init__(self, format="%H:%M", color="#ffffffff", font="Segoe UI", bold=True):
        self.fmt, self.color, self.family, self.bold = format, QColor(color), font, bold

    def paint(self, p, w, h, fit):
        try:
            text = datetime.now().strftime(self.fmt).replace("|", "\n") or " "
        except ValueError:
            text = "?"
        p.setPen(self.color)
        p.setFont(fit_font(p, text, w, h, self.family, self.bold))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, text)


class ColorSource(Source):
    def __init__(self, color="#ff101420", color2="", angle=0):
        self.c1 = QColor(color)
        self.c2 = QColor(color2) if color2 else None
        self.angle = angle

    def paint(self, p, w, h, fit):
        if not self.c2:
            p.fillRect(QRectF(0, 0, w, h), self.c1)
            return
        a = math.radians(self.angle)
        cx, cy = w / 2, h / 2
        r = (abs(w * math.cos(a)) + abs(h * math.sin(a))) / 2
        g = QLinearGradient(cx - r * math.cos(a), cy - r * math.sin(a), cx + r * math.cos(a), cy + r * math.sin(a))
        g.setColorAt(0, self.c1)
        g.setColorAt(1, self.c2)
        p.fillRect(QRectF(0, 0, w, h), g)


# ---------- захват окна через PrintWindow: работает и для перекрытых окон ----------

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32
_dwm = ctypes.windll.dwmapi


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BMI(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


for _f in (_u32.GetWindowDC, _g32.CreateCompatibleDC, _g32.CreateCompatibleBitmap, _g32.SelectObject):
    _f.restype = ctypes.c_void_p
_u32.GetWindowDC.argtypes = [ctypes.c_void_p]
_u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_u32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_g32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_g32.DeleteObject.argtypes = [ctypes.c_void_p]
_g32.DeleteDC.argtypes = [ctypes.c_void_p]
_g32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]


def list_windows():
    """Заголовки видимых окон верхнего уровня."""
    titles = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd) and not _u32.GetWindow(hwnd, 4):  # без владельца
            n = _u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _u32.GetWindowTextW(hwnd, buf, n + 1)
                r = _RECT()
                _u32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left > 50 and r.bottom - r.top > 50:
                    titles.append(buf.value)
        return True
    _u32.EnumWindows(proto(cb), 0)
    return sorted(set(titles), key=str.lower)


def _find_window(title):
    hwnd = _u32.FindWindowW(None, title)
    if hwnd:
        return hwnd
    for t in list_windows():  # заголовок мог поменяться (вкладка браузера, трек плеера)
        if title.lower() in t.lower() or t.lower() in title.lower():
            return _u32.FindWindowW(None, t)
    return None


class WindowSource(Source):
    def __init__(self, title):
        self.title = title
        self.hwnd = None
        self.last_lookup = 0

    def _grab(self):
        if not self.hwnd or not _u32.IsWindow(self.hwnd):
            if time.monotonic() - self.last_lookup < 1:
                return None
            self.last_lookup = time.monotonic()
            self.hwnd = _find_window(self.title) if self.title else None
            if not self.hwnd:
                return None
        if _u32.IsIconic(self.hwnd):
            return "min"
        r = _RECT()
        _u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        ww, wh = r.right - r.left, r.bottom - r.top
        if ww <= 0 or wh <= 0:
            return None
        wdc = _u32.GetWindowDC(self.hwnd)
        mdc = _g32.CreateCompatibleDC(wdc)
        bmp = _g32.CreateCompatibleBitmap(wdc, ww, wh)
        old = _g32.SelectObject(mdc, bmp)
        _u32.PrintWindow(self.hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
        bmi = _BMI(ctypes.sizeof(_BMI), ww, -wh, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(ww * wh * 4)
        _g32.GetDIBits(mdc, bmp, 0, wh, buf, ctypes.byref(bmi), 0)
        _g32.SelectObject(mdc, old)
        _g32.DeleteObject(bmp)
        _g32.DeleteDC(mdc)
        _u32.ReleaseDC(self.hwnd, wdc)
        img = QImage(buf, ww, wh, QImage.Format_RGB32).copy()
        # обрезаем невидимую рамку Windows 10/11
        fr = _RECT()
        if _dwm.DwmGetWindowAttribute(self.hwnd, 9, ctypes.byref(fr), ctypes.sizeof(fr)) == 0:
            img = img.copy(fr.left - r.left, fr.top - r.top, fr.right - fr.left, fr.bottom - fr.top)
        return img

    def paint(self, p, w, h, fit):
        img = self._grab()
        if img == "min":
            draw_message(p, w, h, "Окно свёрнуто")
        elif img is None:
            draw_message(p, w, h, "Окно не найдено" if self.title else "Выберите окно")
        else:
            draw_fitted(p, img, w, h, fit)


def make_source(item):
    pr = item["props"]
    kind = item["type"]
    if kind == "screen":
        return ScreenSource(pr["monitor"], pr["region"] if pr["use_region"] else None, pr["cursor"])
    if kind == "window":
        return WindowSource(pr["title"])
    if kind == "image":
        return ImageSource(pr["path"])
    if kind == "video":
        return VideoSource(pr["path"])
    if kind == "slideshow":
        return SlideshowSource(pr["folder"], pr["interval"])
    if kind == "text":
        return TextSource(**pr)
    if kind == "clock":
        return ClockSource(**pr)
    if kind == "sensor":
        return SensorSource(**pr)
    if kind == "sensors":
        return DashboardSource(**pr)
    if kind == "procs":
        return ProcessesSource(**pr)
    if kind == "color":
        return ColorSource(**pr)
    raise ValueError(kind)
