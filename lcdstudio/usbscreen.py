"""USB-экраны корпусов на чипах MacroSilicon MS913x и AIC через SDK из программы «LCD Control».

SDK (MSDISPLAYSDKWRRAPER.dll, AicUsbDisplay.dll) и драйвер libusb берутся из установленной
LCD Control или из папки vendor рядом с программой. Сигнатуры повторяют P/Invoke из LCD Control.
"""
import ctypes
import os
import subprocess
import sys
import threading
from ctypes import POINTER, Structure, c_bool, c_char, c_int, c_uint, c_void_p
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

MS_DLL = "MSDISPLAYSDKWRRAPER.dll"
AIC_DLL = "AicUsbDisplay.dll"
VENDOR_IDS = ["VID_345F&PID_9132", "VID_345F&PID_9133", "VID_374A&PID_A101"]


class Resolution(Structure):
    _fields_ = [("width", c_int), ("height", c_int), ("refresh", c_int)]


class Picture(Structure):
    _fields_ = [("width", c_int), ("height", c_int), ("data", c_void_p)]


class MSDisplayTiming(Structure):
    _fields_ = [(n, c_uint) for n in ("vic", "polarity", "htotal", "vtotal", "hactive", "vactive", "pixclk",
                                      "vfreq", "hoffset", "voffset", "hsyncwidth", "vsyncwidth")]


class MSDisplayEdid(Structure):
    _fields_ = [("mode", c_int), ("edid_replace", c_char * 128), ("append_count", c_int),
                ("timing_append", MSDisplayTiming * 16)]


MS_ATTACH = ctypes.CFUNCTYPE(None, c_uint, POINTER(Resolution), c_int)
MS_DETACH = ctypes.CFUNCTYPE(None, c_uint)
# AIC передаёт указатель на своё устройство; LCD Control хранит его в uint, мы — целиком
AIC_ATTACH = ctypes.CFUNCTYPE(None, c_void_p, POINTER(Resolution), c_int)
AIC_DETACH = ctypes.CFUNCTYPE(None, c_void_p, POINTER(Resolution))


def _app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def find_vendor_dir():
    """Папка, где лежат MSDISPLAYSDKWRRAPER.dll / AicUsbDisplay.dll."""
    cands = []
    if os.environ.get("LCDSTUDIO_VENDOR"):
        cands.append(Path(os.environ["LCDSTUDIO_VENDOR"]))
    cands += [_app_dir() / "vendor", _app_dir() / "vendor" / "dll" / "x64"]
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as app:
                                try:
                                    name = winreg.QueryValueEx(app, "DisplayName")[0]
                                except OSError:
                                    continue
                                if "lcd control" in name.lower() or "soeyi" in name.lower():
                                    for key in ("InstallLocation", "DisplayIcon"):
                                        try:
                                            v = winreg.QueryValueEx(app, key)[0].strip('"')
                                        except OSError:
                                            continue
                                        p = Path(v)
                                        p = p.parent if p.suffix else p
                                        cands += [p / "dll" / "x64", p.parent / "dll" / "x64", p]
                except OSError:
                    pass
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            for d in Path(base).glob("*LCD Control*"):
                cands += [d / "dll" / "x64", d]
            for d in Path(base).glob("*/*LCD Control*"):
                cands += [d / "dll" / "x64", d]
    for c in cands:
        if (c / MS_DLL).exists() or (c / AIC_DLL).exists():
            return c
    return None


def find_driver_dir(vendor):
    """Папка libusb с MSUSBDisplay.inf (для установки драйвера экрана)."""
    if not vendor:
        return None
    for c in (vendor / "libusb", vendor.parent / "libusb", vendor.parent.parent / "libusb"):
        if (c / "MSUSBDisplay.inf").exists():
            return c
    return None


def connected_screens():
    """Аппаратные ID подключённых экранов (по реестру PnP, без прав администратора)."""
    found = []
    try:
        out = subprocess.run(["pnputil", "/enum-devices", "/connected"], capture_output=True, timeout=10,
                             creationflags=0x08000000).stdout.decode("utf-8", "replace").upper()
        for vid in VENDOR_IDS:
            if vid in out:
                found.append(vid)
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def vendor_app_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LCD Control.exe", "/NH"], capture_output=True,
                             timeout=5, creationflags=0x08000000).stdout.decode("cp866", "replace")
        return "LCD Control.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


class UsbScreen:
    """Один экран через одно из SDK. Кадры принимает в любом размере, масштабирует сам."""

    name = "Экран корпуса (USB)"

    def __init__(self):
        self.vendor = None
        self.ms = self.aic = None
        self.lock = threading.Lock()
        self.handle = None        # (sdk, handle)
        self.resolutions = []
        self.resolution = None
        self.pending = None       # разрешение, которое надо применить из основного потока
        self.state = "SDK не загружен"
        self.errors = 0
        self.flag = False
        self._dll_dirs = []
        self._cb = []

    # ---------- запуск ----------

    def open(self) -> bool:
        self.vendor = find_vendor_dir()
        if not self.vendor:
            self.state = ("Не найдены библиотеки экрана. Установите программу LCD Control от производителя "
                          "или положите её папку dll\\x64 в vendor рядом с LCD Studio.")
            return False
        for d in (self.vendor, self.vendor / "libusb" / "amd64", self.vendor.parent / "libusb" / "amd64",
                  self.vendor.parent.parent / "libusb" / "amd64", _app_dir() / "_internal"):
            if d.is_dir():
                self._dll_dirs.append(os.add_dll_directory(str(d)))
        started = []
        if (self.vendor / MS_DLL).exists():
            try:
                self._start_ms()
                started.append("MacroSilicon")
            except OSError as e:
                self.state = f"MS SDK: {e}"
        if (self.vendor / AIC_DLL).exists():
            try:
                self._start_aic()
                started.append("AIC")
            except OSError as e:
                self.state = f"AIC SDK: {e}"
        if not started:
            return False
        self.state = "Ожидание подключения экрана (SDK: " + ", ".join(started) + ")"
        if vendor_app_running():
            self.state += ". Закройте LCD Control — она занимает экран."
        return True

    def _start_ms(self):
        dll = ctypes.CDLL(str(self.vendor / MS_DLL))
        dll.Wrraper_MSDisplayStart.argtypes = [c_int, c_void_p]
        dll.Wrraper_MSDisplayStart.restype = c_int
        dll.Wrraper_MSDisplayStop.restype = c_int
        dll.Wrraper_MSDisplayRegisterCallback.argtypes = [MS_ATTACH, MS_DETACH]
        dll.Wrraper_MSDisplayRegisterCallback.restype = None
        dll.Wrraper_MSDisplaySetVideoParam.argtypes = [c_uint, POINTER(Resolution)]
        dll.Wrraper_MSDisplaySetVideoParam.restype = c_int
        dll.Wrraper_MSDisplaySendPicture.argtypes = [c_uint, POINTER(Picture), c_bool]
        dll.Wrraper_MSDisplaySendPicture.restype = c_int
        attach = MS_ATTACH(lambda h, res, n: self._on_attach("ms", h, res, n))
        detach = MS_DETACH(lambda h: self._on_detach("ms", h))
        self._cb += [attach, detach]
        dll.Wrraper_MSDisplayRegisterCallback(attach, detach)
        self._edid = MSDisplayEdid()  # mode 0 — родной EDID экрана
        r = dll.Wrraper_MSDisplayStart(0, ctypes.addressof(self._edid))
        if r < 0:
            raise OSError(f"MSDisplayStart вернул {r}")
        self.ms = dll

    def _start_aic(self):
        dll = ctypes.CDLL(str(self.vendor / AIC_DLL))
        dll.AICDispStart.restype = c_int
        dll.AICDispStop.restype = c_int
        dll.AICDispRegisterCallback.argtypes = [AIC_ATTACH, AIC_DETACH]
        dll.AICDispRegisterCallback.restype = None
        dll.AICDispSendPicture.argtypes = [c_void_p, POINTER(Picture)]
        dll.AICDispSendPicture.restype = c_int
        attach = AIC_ATTACH(lambda h, res, n: self._on_attach("aic", h, res, n))
        detach = AIC_DETACH(lambda h, res: self._on_detach("aic", h))
        self._cb += [attach, detach]
        dll.AICDispRegisterCallback(attach, detach)
        r = dll.AICDispStart()
        if r < 0:
            raise OSError(f"AICDispStart вернул {r}")
        self.aic = dll

    # ---------- события SDK (из их потоков) ----------

    def _on_attach(self, sdk, handle, res, count):
        modes = [(res[i].width, res[i].height, res[i].refresh) for i in range(max(0, min(count, 64)))]
        modes = [m for m in modes if m[0] > 0 and m[1] > 0]
        with self.lock:
            self.handle = (sdk, handle)
            self.resolutions = modes
            best = max(modes, key=lambda m: (m[0] * m[1], m[2])) if modes else None
            self.pending = best
            self.resolution = best[:2] if best else None
            self.errors = 0
            self.state = f"Подключено ({'MacroSilicon' if sdk == 'ms' else 'AIC'})" + \
                (f", {best[0]}x{best[1]}" if best else "")

    def _on_detach(self, sdk, handle):
        with self.lock:
            if self.handle and self.handle[0] == sdk and self.handle[1] == handle:
                self.handle = None
                self.state = "Экран отключён, ожидание подключения"

    # ---------- кадры ----------

    def send(self, img: QImage):
        with self.lock:
            hd, pending = self.handle, self.pending
            self.pending = None
        if not hd:
            return
        sdk, handle = hd
        if pending and sdk == "ms":
            mode = Resolution(*pending)
            r = self.ms.Wrraper_MSDisplaySetVideoParam(handle, ctypes.byref(mode))
            if r < 0:
                self.state = f"Не удалось задать режим {pending[0]}x{pending[1]} (код {r})"
        w, h = self.resolution or (img.width(), img.height())
        if (img.width(), img.height()) != (w, h):
            img = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        frame = img.convertToFormat(QImage.Format_RGB32)  # BGRA в памяти, как Bitmap 32bpp у LCD Control
        buf = (ctypes.c_char * frame.sizeInBytes()).from_buffer(frame.bits())
        pic = Picture(w, h, ctypes.addressof(buf))
        if sdk == "ms":
            r = self.ms.Wrraper_MSDisplaySendPicture(handle, ctypes.byref(pic), self.flag)
            if r == -5:      # предыдущий кадр ещё отправляется — пропускаем
                return
            if r == -4:      # устройство ещё не в режиме видео
                with self.lock:
                    self.pending = self.pending or (w, h, 60)
        else:
            r = self.aic.AICDispSendPicture(handle, ctypes.byref(pic))
        if r < 0:
            self.errors += 1
            if sdk == "ms" and self.errors == 30:
                self.flag = not self.flag  # назначение флага не документировано — пробуем второе значение
            self.state = f"Ошибка отправки кадра: код {r}"
        else:
            self.errors = 0

    def close(self):
        try:
            if self.ms:
                self.ms.Wrraper_MSDisplayStop()
            if self.aic:
                self.aic.AICDispStop()
        except OSError:
            pass
        self.ms = self.aic = None
        self.handle = None
        for d in self._dll_dirs:
            d.close()
        self._dll_dirs = []


def install_driver():
    """Ставит драйвер libusb для экрана MacroSilicon из комплекта LCD Control (нужны права администратора)."""
    drv = find_driver_dir(find_vendor_dir())
    if not drv:
        return "Не найдена папка libusb с MSUSBDisplay.inf"
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "pnputil.exe",
                                            f'/add-driver "{drv / "MSUSBDisplay.inf"}" /install', None, 1)
    return None if r > 32 else f"Не удалось запустить установку (код {r})"
