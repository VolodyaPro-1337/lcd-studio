"""Сбор датчиков: psutil, LibreHardwareMonitor, nvidia-smi, ping.

Каждый датчик имеет строковый id, метаданные (имя, группа, тип, единица)
и историю значений. Виджеты читают снимок, не блокируя отрисовку.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil

CREATE_NO_WINDOW = 0x08000000
HISTORY = 900  # точек истории на датчик

TYPE_UNITS = {
    "Load": "%", "Temperature": "°C", "Clock": "МГц", "Power": "Вт", "Fan": "об/мин", "Voltage": "В",
    "Data": "ГБ", "SmallData": "МБ", "Throughput": "Б/с", "Control": "%", "Level": "%", "Factor": "×",
    "Frequency": "Гц", "Current": "А", "Energy": "мВт·ч", "Noise": "дБА", "Flow": "л/ч", "Humidity": "%",
    "TimeSpan": "с", "Conductivity": "мкСм/см",
}
TYPE_NAMES = {
    "Load": "Загрузка", "Temperature": "Температура", "Clock": "Частота", "Power": "Мощность",
    "Fan": "Вентиляторы", "Voltage": "Напряжение", "Data": "Данные", "SmallData": "Данные",
    "Throughput": "Скорость", "Control": "Управление", "Level": "Уровень", "Factor": "Множитель",
    "Frequency": "Частота", "Current": "Ток", "Energy": "Энергия", "Noise": "Шум", "Flow": "Поток",
    "Humidity": "Влажность", "TimeSpan": "Время",
}

# «умные» датчики: находят подходящий сенсор на любом железе
AUTO = [
    ("auto/cpu_load", "Загрузка CPU", "%"),
    ("auto/cpu_temp", "Температура CPU", "°C"),
    ("auto/cpu_power", "Мощность CPU", "Вт"),
    ("auto/cpu_clock", "Частота CPU", "МГц"),
    ("auto/gpu_load", "Загрузка GPU", "%"),
    ("auto/gpu_temp", "Температура GPU", "°C"),
    ("auto/gpu_hotspot", "Хотспот GPU", "°C"),
    ("auto/gpu_power", "Мощность GPU", "Вт"),
    ("auto/gpu_clock", "Частота GPU", "МГц"),
    ("auto/gpu_mem_clock", "Частота видеопамяти", "МГц"),
    ("auto/gpu_fan", "Вентилятор GPU", "об/мин"),
    ("auto/vram_used", "Видеопамять занято", "МБ"),
    ("auto/vram_load", "Видеопамять", "%"),
    ("auto/ram_load", "Оперативная память", "%"),
    ("auto/ram_used", "ОЗУ занято", "ГБ"),
    ("auto/net_down", "Сеть: загрузка", "Б/с"),
    ("auto/net_up", "Сеть: отдача", "Б/с"),
    ("auto/disk_read", "Диск: чтение", "Б/с"),
    ("auto/disk_write", "Диск: запись", "Б/с"),
]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def lhm_dir():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "lhm"


def fmt_value(v, unit, decimals=-1):
    if v is None:
        return "—"
    if unit == "Б/с":
        for u in ("Б/с", "КБ/с", "МБ/с", "ГБ/с"):
            if abs(v) < 1024 or u == "ГБ/с":
                return f"{v:.0f} {u}" if u == "Б/с" else f"{v:.{1 if decimals < 0 else decimals}f} {u}"
            v /= 1024
    if decimals < 0:
        decimals = {"В": 3, "ГБ": 1, "А": 2, "×": 2, "мс": 0}.get(unit, 1 if abs(v) < 10 and unit not in
                                                                    ("%", "°C", "МГц", "об/мин") else 0)
    text = f"{v:.{decimals}f}"
    return f"{text}{unit}" if unit in ("%", "°C") else f"{text} {unit}".strip()


def default_range(unit, hist_max):
    if unit in ("%",):
        return 0, 100
    if unit == "°C":
        return 0, 100
    if unit == "Б/с":
        return 0, max(hist_max * 1.2, 1024 * 1024)
    return 0, max(hist_max * 1.15, 1e-6)


class SensorHub:
    def __init__(self):
        self.meta = {}       # id -> dict(name, group, type, unit)
        self.values = {}     # id -> float
        self.history = {}    # id -> deque
        self.aliases = {}    # auto/* -> реальный id
        self.procs = []      # [(name, cpu, ram_mb)]
        self.info = {}       # текстовые сведения о системе
        self.interval = 1.0
        self.ping_hosts = set()
        self.want_procs = 0
        self.lhm_status = "не запускался"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._users = 0
        self._computer = None
        self._nvsmi = shutil.which("nvidia-smi")
        self._ping_vals = {}

    # ---------- жизненный цикл ----------

    def acquire(self):
        self._users += 1
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def release(self):
        self._users = max(0, self._users - 1)

    def register(self, sid):
        if sid.startswith("ping/"):
            self.ping_hosts.add(sid[5:])

    def shutdown(self):
        """Останавливает поток и закрывает LHM до выхода интерпретатора, иначе .NET падает при завершении."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._computer is not None:
            try:
                self._computer.Close()
            except Exception:
                pass
            self._computer = None

    # ---------- чтение ----------

    def resolve(self, sid):
        return self.aliases.get(sid, sid)

    def get(self, sid):
        with self._lock:
            rid = self.aliases.get(sid, sid)
            return self.values.get(rid)

    def get_meta(self, sid):
        rid = self.resolve(sid)
        m = self.meta.get(rid)
        if sid.startswith("auto/"):
            name, unit = next(((n, u) for a, n, u in AUTO if a == sid), (sid, ""))
            return {"name": name, "group": "Рекомендуемые", "type": "", "unit": m["unit"] if m else unit}
        return m

    def get_history(self, sid, n):
        with self._lock:
            h = self.history.get(self.aliases.get(sid, sid))
            return list(h)[-n:] if h else []

    def all_sensors(self):
        with self._lock:
            return dict(self.meta), dict(self.values)

    # ---------- сбор ----------

    def _put(self, vals, sid, value, name, group, typ, unit):
        if value is None:
            return
        vals[sid] = float(value)
        if sid not in self.meta:
            self.meta[sid] = {"name": name, "group": group, "type": typ, "unit": unit}

    def _run(self):
        self._init_lhm()
        self._init_info()
        psutil.cpu_percent(percpu=True)
        net0, disk0, t0 = psutil.net_io_counters(pernic=True), psutil.disk_io_counters(), time.monotonic()
        nics0 = net0
        tick = 0
        while not self._stop.is_set():
            if self._users == 0:  # никто не показывает датчики — не тратим CPU
                self._stop.wait(0.5)
                continue
            t1 = time.monotonic()
            dt = max(t1 - t0, 1e-3)
            vals = {}
            self._sys(vals, dt, nics0, disk0)
            nics0, disk0 = psutil.net_io_counters(pernic=True), psutil.disk_io_counters()
            if self._computer is not None:
                self._lhm(vals)
            elif self._nvsmi:
                self._nvidia(vals)
            for host in list(self.ping_hosts):
                if tick % 2 == 0:
                    threading.Thread(target=self._ping, args=(host,), daemon=True).start()
                if host in self._ping_vals:
                    self._put(vals, f"ping/{host}", self._ping_vals[host], f"Пинг {host}", "Сеть", "Ping", "мс")
            if self.want_procs and tick % 2 == 0:
                self._collect_procs()
            with self._lock:
                self.values = vals
                for k, v in vals.items():
                    self.history.setdefault(k, deque(maxlen=HISTORY)).append(v)
                self._resolve_aliases()
            t0 = t1
            tick += 1
            self._stop.wait(max(0.2, self.interval - (time.monotonic() - t1)))

    def _sys(self, vals, dt, nics0, disk0):
        g = "Система"
        cores = psutil.cpu_percent(percpu=True)
        self._put(vals, "sys/cpu", sum(cores) / len(cores), "Загрузка CPU", g, "Load", "%")
        for i, c in enumerate(cores):
            self._put(vals, f"sys/core/{i}", c, f"Поток #{i + 1}", "Система: потоки CPU", "Load", "%")
        f = psutil.cpu_freq()
        if f:
            self._put(vals, "sys/cpu_freq", f.current, "Частота CPU", g, "Clock", "МГц")
        vm = psutil.virtual_memory()
        self._put(vals, "sys/ram", vm.percent, "ОЗУ, %", g, "Load", "%")
        self._put(vals, "sys/ram_used", vm.used / 2**30, "ОЗУ занято", g, "Data", "ГБ")
        self._put(vals, "sys/ram_free", vm.available / 2**30, "ОЗУ свободно", g, "Data", "ГБ")
        self._put(vals, "sys/ram_total", vm.total / 2**30, "ОЗУ всего", g, "Data", "ГБ")
        sw = psutil.swap_memory()
        self._put(vals, "sys/swap", sw.percent, "Файл подкачки", g, "Load", "%")
        self._put(vals, "sys/uptime", (time.time() - psutil.boot_time()) / 3600, "Время работы", g, "TimeSpan", "ч")
        self._put(vals, "sys/procs", len(psutil.pids()), "Процессов", g, "Count", "")
        bat = psutil.sensors_battery()
        if bat:
            self._put(vals, "sys/battery", bat.percent, "Батарея", g, "Level", "%")
        nics = psutil.net_io_counters(pernic=True)
        down = up = 0.0
        for name, c in nics.items():
            o = nics0.get(name)
            if not o:
                continue
            d, u = (c.bytes_recv - o.bytes_recv) / dt, (c.bytes_sent - o.bytes_sent) / dt
            down, up = down + d, up + u
            self._put(vals, f"sys/nic/{name}/down", d, f"{name}: загрузка", "Сеть: адаптеры", "Throughput", "Б/с")
            self._put(vals, f"sys/nic/{name}/up", u, f"{name}: отдача", "Сеть: адаптеры", "Throughput", "Б/с")
        self._put(vals, "sys/net_down", down, "Сеть: загрузка", g, "Throughput", "Б/с")
        self._put(vals, "sys/net_up", up, "Сеть: отдача", g, "Throughput", "Б/с")
        disk = psutil.disk_io_counters()
        if disk and disk0:
            self._put(vals, "sys/disk_read", (disk.read_bytes - disk0.read_bytes) / dt, "Диск: чтение", g,
                      "Throughput", "Б/с")
            self._put(vals, "sys/disk_write", (disk.write_bytes - disk0.write_bytes) / dt, "Диск: запись", g,
                      "Throughput", "Б/с")
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            m = part.mountpoint.rstrip("\\")
            self._put(vals, f"sys/drive/{m}/pct", u.percent, f"Диск {m} заполнен", "Диски", "Load", "%")
            self._put(vals, f"sys/drive/{m}/free", u.free / 2**30, f"Диск {m} свободно", "Диски", "Data", "ГБ")

    def _nvidia(self, vals):
        try:
            out = subprocess.run(
                [self._nvsmi, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,"
                 "fan.speed,power.draw,clocks.gr,clocks.mem", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, creationflags=CREATE_NO_WINDOW,
            ).stdout.splitlines()[0].split(",")
        except (OSError, IndexError, subprocess.SubprocessError):
            return
        name = out[0].strip()

        def num(i):
            try:
                return float(out[i])
            except (ValueError, IndexError):
                return None
        for sid, i, title, typ, unit in [("load", 1, "Загрузка", "Load", "%"), ("temp", 2, "Температура", "Temperature", "°C"),
                                         ("vram", 3, "Видеопамять", "SmallData", "МБ"), ("fan", 5, "Вентилятор", "Control", "%"),
                                         ("power", 6, "Мощность", "Power", "Вт"), ("clock", 7, "Частота", "Clock", "МГц"),
                                         ("memclock", 8, "Частота памяти", "Clock", "МГц")]:
            self._put(vals, f"nv/{sid}", num(i), title, name, typ, unit)
        if num(3) is not None and num(4):
            self._put(vals, "nv/vram_pct", num(3) / num(4) * 100, "Видеопамять, %", name, "Load", "%")

    def _ping(self, host):
        try:
            out = subprocess.run(["ping", "-n", "1", "-w", "1000", host], capture_output=True, timeout=3,
                                 creationflags=CREATE_NO_WINDOW).stdout.decode("cp866", "replace")
            m = re.search(r"[=<]\s*(\d+)\s*(?:ms|мс)", out)
            self._ping_vals[host] = float(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            self._ping_vals[host] = None

    def _collect_procs(self):
        rows = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                cpu = p.cpu_percent(None)
                if p.info["name"] in ("System Idle Process", "Idle"):
                    continue
                rows.append((p.info["name"], cpu / psutil.cpu_count(), p.info["memory_info"].rss / 2**20))
            except (psutil.Error, AttributeError):
                pass
        with self._lock:
            self.procs = rows

    # ---------- LibreHardwareMonitor ----------

    def _init_lhm(self):
        d = lhm_dir()
        if not (d / "LibreHardwareMonitorLib.dll").exists():
            self.lhm_status = "библиотека не найдена"
            return
        try:
            from pythonnet import load
            try:
                load("netfx")
            except RuntimeError:
                pass  # уже загружено
            import clr
            from System import AppDomain, ResolveEventHandler
            from System.Reflection import Assembly, AssemblyName

            def resolve(_, args):
                p = d / (AssemblyName(args.Name).Name + ".dll")
                return Assembly.LoadFrom(str(p)) if p.exists() else None
            self._resolver = ResolveEventHandler(resolve)
            AppDomain.CurrentDomain.AssemblyResolve += self._resolver
            clr.AddReference(str(d / "LibreHardwareMonitorLib.dll"))
            from LibreHardwareMonitor.Hardware import Computer
            c = Computer()
            for attr in ("IsCpuEnabled", "IsGpuEnabled", "IsMemoryEnabled", "IsMotherboardEnabled",
                         "IsStorageEnabled", "IsNetworkEnabled", "IsControllerEnabled", "IsBatteryEnabled",
                         "IsPsuEnabled"):
                setattr(c, attr, True)
            c.Open()
            self._computer = c
            self.lhm_status = "работает" + (" (администратор)" if is_admin() else " (без прав администратора)")
        except Exception as e:
            self.lhm_status = f"ошибка: {e}"
            self._computer = None

    def _lhm(self, vals):
        def walk(hw, group):
            try:
                hw.Update()
            except Exception:
                return
            for s in hw.Sensors:
                v = s.Value
                if v is None:
                    continue
                typ = str(s.SensorType)
                self._put(vals, "lhm" + str(s.Identifier), v, str(s.Name), group, typ, TYPE_UNITS.get(typ, ""))
                self.meta["lhm" + str(s.Identifier)]["hw"] = str(hw.HardwareType)
            for sub in hw.SubHardware:
                walk(sub, f"{group} › {sub.Name}")
        try:
            for hw in self._computer.Hardware:
                walk(hw, str(hw.Name))
        except Exception as e:
            self.lhm_status = f"ошибка: {e}"

    def _init_info(self):
        info = {"cpu_name": "", "gpu_name": "", "os": "", "host": os.environ.get("COMPUTERNAME", "")}
        try:
            import platform
            info["os"] = f"Windows {platform.release()} ({platform.version()})"
            info["cpu_name"] = platform.processor()
        except Exception:
            pass
        if self._computer is not None:
            for hw in self._computer.Hardware:
                t = str(hw.HardwareType)
                if t == "Cpu":
                    info["cpu_name"] = str(hw.Name)
                elif t.startswith("Gpu") and not info["gpu_name"]:
                    info["gpu_name"] = str(hw.Name)
                elif t == "Motherboard":
                    info["board"] = str(hw.Name)
        self.info = info

    def _resolve_aliases(self):
        """Подбирает реальные сенсоры для auto/*. Вызывается под блокировкой."""
        def find(hw_prefix, typ, *names, nonzero=True):
            cands = [(k, m) for k, m in self.meta.items()
                     if m.get("hw", "").startswith(hw_prefix) and m["type"] == typ and k in self.values]
            for exact in (True, False):
                for n in names:
                    for k, m in cands:
                        hit = n.lower() == m["name"].lower() if exact else n.lower() in m["name"].lower()
                        if hit and (not nonzero or self.values[k]):
                            return k
            return None

        a = {}
        a["auto/cpu_load"] = find("Cpu", "Load", "CPU Total") or "sys/cpu"
        a["auto/cpu_temp"] = find("Cpu", "Temperature", "Tctl", "Package", "Core (Tdie)", "Core Average", "Core")
        a["auto/cpu_power"] = find("Cpu", "Power", "Package", "CPU Cores")
        a["auto/cpu_clock"] = find("Cpu", "Clock", "Cores (Average)", "Core #1") or "sys/cpu_freq"
        a["auto/gpu_load"] = find("Gpu", "Load", "GPU Core", "D3D 3D", nonzero=False) or "nv/load"
        a["auto/gpu_temp"] = find("Gpu", "Temperature", "GPU Core", nonzero=False) or "nv/temp"
        a["auto/gpu_hotspot"] = find("Gpu", "Temperature", "Hot Spot", nonzero=False)
        a["auto/gpu_power"] = find("Gpu", "Power", "Package", "GPU Core", nonzero=False) or "nv/power"
        a["auto/gpu_clock"] = find("Gpu", "Clock", "GPU Core", nonzero=False) or "nv/clock"
        a["auto/gpu_mem_clock"] = find("Gpu", "Clock", "GPU Memory", nonzero=False) or "nv/memclock"
        a["auto/gpu_fan"] = find("Gpu", "Fan", "GPU Fan", "Fan", nonzero=False)
        a["auto/vram_used"] = find("Gpu", "SmallData", "GPU Memory Used", "D3D Dedicated", nonzero=False) or "nv/vram"
        a["auto/vram_load"] = find("Gpu", "Load", "GPU Memory", nonzero=False) or "nv/vram_pct"
        a["auto/ram_load"] = "sys/ram"
        a["auto/ram_used"] = "sys/ram_used"
        a["auto/net_down"] = "sys/net_down"
        a["auto/net_up"] = "sys/net_up"
        a["auto/disk_read"] = "sys/disk_read"
        a["auto/disk_write"] = "sys/disk_write"
        self.aliases = {k: v for k, v in a.items() if v}


hub = SensorHub()
