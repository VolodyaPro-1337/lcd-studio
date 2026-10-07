"""Синхронизация всей RGB-подсветки ПК через OpenRGB (SDK-протокол версии 4).

OpenRGB знает почти все контроллеры: материнские платы (включая ARGB-разъёмы), память,
видеокарты, контроллеры вентиляторов, периферию. LCD Studio сам скачивает и запускает его
сервером, а эффекты считает у себя и шлёт цвета каждого светодиода в режиме Direct.
"""
import colorsys
import math
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

OPENRGB_URL = ("https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_1.0/"
               "OpenRGB_1.0_Windows_64_81bbe18.zip")
OPENRGB_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LcdStudio" / "OpenRGB"
PORT = 6742
PROTO = 4

PKT_COUNT, PKT_DATA, PKT_PROTO, PKT_NAME = 0, 1, 40, 50
PKT_DEVLIST_UPDATED = 100
PKT_RESIZE, PKT_UPDATELEDS, PKT_CUSTOM = 1000, 1050, 1100

MODES = [("static", "Один цвет"), ("rainbow", "Радуга (волна)"), ("cycle", "Смена цветов"),
         ("breath", "Дыхание"), ("gradient", "Градиент (бегущий)"), ("sensor", "Цвет по датчику"),
         ("ambient_lcd", "Как на экране корпуса"), ("ambient_screen", "Как на мониторе (Ambilight)"),
         ("off", "Выключить")]


# ---------- протокол ----------

class Reader:
    def __init__(self, data):
        self.d, self.p = data, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v[0] if len(v) == 1 else v

    def string(self):
        n = self.take("H")
        s = self.d[self.p:self.p + n].split(b"\0", 1)[0].decode("utf-8", "replace")
        self.p += n
        return s

    def skip(self, n):
        self.p += n


def parse_controller(data, proto=PROTO):
    r = Reader(data)
    r.take("I")  # размер блока
    c = {"type": r.take("i"), "name": r.string()}
    c["vendor"] = r.string() if proto >= 1 else ""
    c["description"], c["version"], c["serial"], c["location"] = r.string(), r.string(), r.string(), r.string()
    modes = []
    n_modes, c["active_mode"] = r.take("H"), r.take("i")
    for _ in range(n_modes):
        m = {"name": r.string(), "value": r.take("i"), "flags": r.take("I")}
        r.skip(8)                      # speed_min/max
        if proto >= 3:
            r.skip(8)                  # brightness_min/max
        r.skip(8)                      # colors_min/max
        r.skip(4)                      # speed
        if proto >= 3:
            r.skip(4)                  # brightness
        r.skip(8)                      # direction, color_mode
        r.skip(4 * r.take("H"))        # цвета режима
        modes.append(m)
    c["modes"] = modes
    zones = []
    for _ in range(r.take("H")):
        z = {"name": r.string(), "type": r.take("i"), "min": r.take("I"), "max": r.take("I"), "count": r.take("I")}
        r.skip(r.take("H"))            # матрица
        if proto >= 4:
            for _ in range(r.take("H")):  # сегменты
                r.string()
                r.skip(12)
        zones.append(z)
    c["zones"] = zones
    leds = []
    for _ in range(r.take("H")):
        leds.append(r.string())
        r.skip(4)
    c["leds"] = leds
    c["num_colors"] = r.take("H")
    return c


class OpenRGBClient:
    def __init__(self, host="127.0.0.1", port=PORT):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.sock.settimeout(None)
        self.q = queue.Queue()
        self.devlist_changed = False
        self.alive = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.send(PKT_PROTO, 0, struct.pack("<I", PROTO))
        server = struct.unpack("<I", self._wait(PKT_PROTO)[1])[0]
        self.proto = min(PROTO, server)
        self.send(PKT_NAME, 0, b"LCD Studio\0")

    def send(self, pid, dev, payload=b""):
        self.sock.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(payload)) + payload)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB закрыл соединение")
            buf += chunk
        return bytes(buf)

    def _read_loop(self):
        try:
            while True:
                hdr = self._recv_exact(16)
                if hdr[:4] != b"ORGB":
                    raise ConnectionError("неверный пакет")
                dev, pid, size = struct.unpack("<III", hdr[4:])
                data = self._recv_exact(size) if size else b""
                if pid == PKT_DEVLIST_UPDATED:
                    self.devlist_changed = True
                else:
                    self.q.put((pid, dev, data))
        except (OSError, ConnectionError):
            self.alive = False
            self.q.put((None, None, None))

    def _wait(self, pid, dev=None, timeout=10):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"нет ответа на пакет {pid}")
            got = self.q.get(timeout=left)
            if got[0] is None:
                raise ConnectionError("OpenRGB закрыл соединение")
            if got[0] == pid and (dev is None or got[1] == dev):
                return got[1], got[2]

    def controllers(self):
        self.send(PKT_COUNT, 0)
        n = struct.unpack("<I", self._wait(PKT_COUNT)[1])[0]
        out = []
        for i in range(n):
            self.send(PKT_DATA, i, struct.pack("<I", self.proto))
            c = parse_controller(self._wait(PKT_DATA, i)[1], self.proto)
            c["index"] = i
            out.append(c)
        self.devlist_changed = False
        return out

    def set_custom_mode(self, dev):
        self.send(PKT_CUSTOM, dev)

    def resize_zone(self, dev, zone, size):
        self.send(PKT_RESIZE, dev, struct.pack("<ii", zone, size))

    def update_leds(self, dev, colors):
        body = struct.pack("<H", len(colors)) + b"".join(struct.pack("<BBBx", *c) for c in colors)
        self.send(PKT_UPDATELEDS, dev, struct.pack("<I", 4 + len(body)) + body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- OpenRGB: поиск, скачивание, запуск ----------

def find_openrgb(custom=""):
    cands = [Path(custom)] if custom else []
    cands += list(OPENRGB_DIR.glob("**/OpenRGB.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            cands.append(Path(base) / "OpenRGB" / "OpenRGB.exe")
    return next((c for c in cands if c.is_file()), None)


def download_openrgb(progress=lambda text: None):
    OPENRGB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OPENRGB_DIR / "openrgb.zip"
    progress("Скачивание OpenRGB...")
    with urllib.request.urlopen(OPENRGB_URL, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(f"Скачивание OpenRGB: {done * 100 // total}%")
    progress("Распаковка...")
    with zipfile.ZipFile(tmp) as z:
        z.extractall(OPENRGB_DIR)
    tmp.unlink(missing_ok=True)
    return find_openrgb()


PAWNIO_URL = "https://github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe"


def pawnio_installed():
    """Драйвер PawnIO: через него OpenRGB 1.0 и LibreHardwareMonitor читают SMBus (память, часть плат, температура CPU)."""
    try:
        r = subprocess.run(["sc", "query", "PawnIO"], capture_output=True, timeout=5, creationflags=0x08000000)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_pawnio(progress=lambda text: None):
    import ctypes
    import tempfile
    path = Path(tempfile.gettempdir()) / "PawnIO_setup.exe"
    progress("Скачивание PawnIO...")
    with urllib.request.urlopen(PAWNIO_URL, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())
    progress("Установка PawnIO — подтвердите запрос Windows")
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), None, None, 1)
    if r <= 32:
        raise OSError(f"не удалось запустить установщик (код {r})")


def openrgb_running(port=PORT):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


_job = None


def _kill_with_us(proc):
    """Job Object с KILL_ON_JOB_CLOSE: OpenRGB завершится вместе с LCD Studio, даже при аварийном выходе."""
    global _job
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    if _job is None:
        class LIMITS(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", LIMITS), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            return
        _job = job
    h = k32.OpenProcess(0x0101, False, proc.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if h:
        k32.AssignProcessToJobObject(wintypes.HANDLE(_job), wintypes.HANDLE(h))
        k32.CloseHandle(wintypes.HANDLE(h))


def start_openrgb(exe):
    # без --gui OpenRGB работает только сервером, без окна
    proc = subprocess.Popen([str(exe), "--server", "--server-port", str(PORT), "--noautoconnect", "--localconfig"],
                            cwd=str(Path(exe).parent), creationflags=0x08000000)
    try:
        _kill_with_us(proc)
    except OSError:
        pass
    return proc


# ---------- эффекты ----------

def _hex(c):
    c = c.lstrip("#")
    if len(c) == 8:  # #AARRGGBB
        c = c[2:]
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _mix(a, b, t):
    t = min(max(t, 0.0), 1.0)
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class RgbSync:
    """Фоновый поток: держит связь с OpenRGB и рисует эффект на всех включённых устройствах."""

    def __init__(self, cfg, frame_getter, sensor_getter):
        self.cfg = cfg
        self.get_frame = frame_getter      # -> QImage кадра экрана корпуса
        self.get_sensor = sensor_getter    # sid -> float|None
        self.client = None
        self.devices = []
        self.status = "Выключено"
        self.proc = None
        self._stop = threading.Event()
        self._thread = None
        self._last_send = 0
        self._screen = None
        self.reconfigure = False

    @property
    def s(self):
        return self.cfg["rgb"]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._disconnect()
        if self.proc and self.proc.poll() is None and self.s.get("stop_openrgb_on_exit", True):
            self.proc.terminate()
        self.status = "Выключено"

    def _disconnect(self):
        if self.client:
            self.client.close()
        self.client = None

    def _connect(self):
        if not openrgb_running():
            exe = find_openrgb(self.s.get("openrgb_path", ""))
            if not exe:
                self.status = "OpenRGB не найден — нажмите «Скачать OpenRGB»"
                return False
            if self.proc is None or self.proc.poll() is not None:
                self.status = "Запуск OpenRGB и поиск устройств..."
                self.proc = start_openrgb(exe)
            for _ in range(60):  # первый поиск устройств может идти до ~30 с
                if self._stop.wait(0.5) or openrgb_running():
                    break
            if not openrgb_running():
                self.status = "OpenRGB не отвечает"
                return False
        try:
            self.client = OpenRGBClient()
            self._load_devices()
            return True
        except (OSError, ConnectionError, TimeoutError, struct.error) as e:
            self.status = f"Ошибка связи с OpenRGB: {e}"
            self._disconnect()
            return False

    def _load_devices(self):
        devs = self.client.controllers()
        conf = self.s.setdefault("devices", {})
        for d in devs:
            key = f"{d['name']}|{d['location']}"
            d["key"] = key
            dc = conf.setdefault(key, {"enabled": True, "zones": {}})
            d["enabled"] = dc["enabled"]
            # у ARGB-разъёмов число светодиодов по умолчанию 0 — без этого они не светятся
            for zi, z in enumerate(d["zones"]):
                want = dc["zones"].get(str(zi))
                if want is None and z["count"] == 0 and z["max"] > z["min"]:
                    want = min(z["max"], 60)
                    dc["zones"][str(zi)] = want
                if want is not None and z["min"] <= want <= z["max"] and want != z["count"]:
                    self.client.resize_zone(d["index"], zi, int(want))
            if d["enabled"]:
                self.client.set_custom_mode(d["index"])
        # после изменения размеров зон число светодиодов меняется — перечитываем
        if any(c["zones"] for c in conf.values()):
            time.sleep(0.3)
            fresh = self.client.controllers()
            for d, f in zip(devs, fresh):
                f.update(key=d["key"], enabled=d["enabled"])
            devs = fresh
        self.devices = devs
        n = sum(len(d["leds"]) for d in devs if d["enabled"])
        self.status = f"Подключено: устройств {len(devs)}, светодиодов {n}"

    def _run(self):
        while not self._stop.is_set():
            if not self.client or not self.client.alive:
                self._disconnect()
                if not self._connect():
                    self._stop.wait(5)
                    continue
            if self.client.devlist_changed or self.reconfigure:
                self.reconfigure = False
                try:
                    self._load_devices()
                except (OSError, ConnectionError, TimeoutError, struct.error) as e:
                    self.status = f"Ошибка: {e}"
                    self._disconnect()
                    continue
            t0 = time.monotonic()
            try:
                self._frame(t0)
            except (OSError, ConnectionError) as e:
                self.status = f"Связь с OpenRGB потеряна: {e}"
                self._disconnect()
                continue
            fps = max(1, min(60, self.s.get("fps", 30)))
            self._stop.wait(max(0.0, 1 / fps - (time.monotonic() - t0)))

    def _frame(self, now):
        s = self.s
        mode = s.get("mode", "rainbow")
        static = mode in ("static", "off")
        if static and now - self._last_send < 2:  # статичное — обновляем редко
            return
        devs = [d for d in self.devices if d["enabled"] and d["leds"]]
        total = sum(len(d["leds"]) for d in devs)
        if not total:
            return
        bright = s.get("brightness", 100) / 100
        speed = s.get("speed", 50) / 50
        c1, c2 = _hex(s.get("color", "#ff00c8ff")), _hex(s.get("color2", "#ffff0080"))
        samples = self._ambient_samples(mode, 64) if mode.startswith("ambient") else None
        avg = None
        if samples and s.get("ambient_average", False):
            avg = tuple(sum(c[i] for c in samples) // len(samples) for i in range(3))
        k = 0
        for d in devs:
            n = len(d["leds"])
            colors = []
            for j in range(n):
                g = (k + j) / max(total - 1, 1)     # позиция светодиода во всей системе
                local = j / max(n - 1, 1)           # позиция внутри устройства
                colors.append(self._color(mode, g, local, now, speed, c1, c2, samples, avg))
            k += n
            if bright < 1:
                colors = [tuple(int(v * bright) for v in c) for c in colors]
            self.client.update_leds(d["index"], colors)
        self._last_send = now

    def _color(self, mode, g, local, now, speed, c1, c2, samples, avg):
        if mode == "off":
            return (0, 0, 0)
        if mode == "static":
            return c1
        if mode == "rainbow":
            return _hsv(g - now * 0.2 * speed)
        if mode == "cycle":
            return _hsv(now * 0.1 * speed)
        if mode == "breath":
            return _mix((0, 0, 0), c1, 0.5 - 0.5 * math.cos(now * 2 * speed))
        if mode == "gradient":
            return _mix(c1, c2, 0.5 + 0.5 * math.sin((g * 2 - now * 0.5 * speed) * math.pi))
        if mode == "sensor":
            v = self.get_sensor(self.s.get("sensor", "auto/cpu_temp"))
            lo, hi = self.s.get("sensor_min", 40), self.s.get("sensor_max", 85)
            if v is None:
                return c1
            return _mix(c1, c2, (v - lo) / max(hi - lo, 1e-6))
        if samples:
            if avg:
                return avg
            return samples[min(int(local * len(samples)), len(samples) - 1)]
        return (0, 0, 0)

    def _ambient_samples(self, mode, n):
        from PySide6.QtCore import Qt
        img = None
        if mode == "ambient_lcd":
            img = self.get_frame()
        else:
            import mss
            if self._screen is None:
                self._screen = getattr(mss, "MSS", mss.mss)()
            mons = self._screen.monitors
            mon = mons[min(self.s.get("ambient_monitor", 1), len(mons) - 1)]
            shot = self._screen.grab(mon)
            from PySide6.QtGui import QImage
            img = QImage(shot.raw, shot.width, shot.height, QImage.Format_RGB32).copy()
        if img is None or img.isNull():
            return None
        small = img.scaled(n, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        sat = self.s.get("ambient_boost", 30) / 100
        out = []
        for x in range(n):
            c = small.pixelColor(x, 0)
            h, s_, v = c.hsvHueF(), c.hsvSaturationF(), c.valueF()
            out.append(_hsv(max(h, 0), min(1, s_ * (1 + sat)), v))
        return out
