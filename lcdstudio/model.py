import copy
import uuid

# тип -> (название, свойства по умолчанию, занимает весь экран при добавлении)
TYPES = {
    "screen": ("Рабочий стол", {"monitor": 1, "use_region": False, "region": [0, 0, 1920, 1080], "cursor": True}, True),
    "window": ("Окно программы", {"title": ""}, True),
    "image": ("Картинка / GIF", {"path": ""}, True),
    "video": ("Видео", {"path": ""}, True),
    "slideshow": ("Слайд-шоу", {"folder": "", "interval": 10}, True),
    "text": ("Текст", {"text": "Текст", "color": "#ffffffff", "bg": "#00000000", "speed": 0, "size": 0,
                       "font": "Segoe UI", "bold": True, "italic": False, "align": "center"}, False),
    "clock": ("Часы", {"format": "%H:%M", "color": "#ffffffff", "font": "Segoe UI", "bold": True}, False),
    "sensor": ("Датчик", {"sensor": "auto/cpu_load", "style": "ring", "label": "", "show_label": True,
                          "show_value": True, "unit": "", "decimals": -1, "min": 0.0, "max": 0.0,
                          "color": "#ff00c8ff", "warn": 0.0, "warn_color": "#ffffb43c", "crit": 0.0,
                          "crit_color": "#ffff5050", "text_color": "#ffffffff", "track_color": "#28ffffff",
                          "bg": "#00000000", "history": 60, "thickness": 10, "font": "Segoe UI"}, False),
    "sensors": ("Панель мониторинга", {"tiles": ["auto/cpu_load", "auto/cpu_temp", "auto/gpu_load", "auto/gpu_temp",
                                                 "auto/ram_load", "auto/vram_load", "auto/net_down", "auto/disk_read"],
                                       "style": "ring", "columns": 0, "accent": "#ff00c8ff",
                                       "text_color": "#ffffffff", "bg": "#ff0a0c12", "tile_bg": "#ff161a24",
                                       "gap": 2, "font": "Segoe UI"}, True),
    "procs": ("Топ процессов", {"count": 5, "sort": "cpu", "text_color": "#ffffffff", "color": "#ff00c8ff",
                                "bg": "#00000000", "font": "Segoe UI"}, False),
    "color": ("Цвет / градиент", {"color": "#ff101420", "color2": "", "angle": 0}, True),
}

FITS = [("cover", "Заполнить"), ("contain", "Вписать"), ("stretch", "Растянуть")]


def new_item(kind, cw, ch, x=None, y=None):
    title, props, full = TYPES[kind]
    if full:
        w, h = cw, ch
    else:
        w, h = max(40, cw // 4), max(30, ch // 3)
    return {
        "id": uuid.uuid4().hex[:8],
        "type": kind,
        "name": title,
        "x": (cw - w) / 2 if x is None else x - w / 2,
        "y": (ch - h) / 2 if y is None else y - h / 2,
        "w": w, "h": h,
        "rotation": 0,
        "opacity": 100,
        "visible": True,
        "locked": False,
        "fit": "cover",
        "props": copy.deepcopy(props),
    }


def new_scene(name, cw, ch, kinds=("screen",)):
    return {"name": name, "bg": "#ff000000", "items": [new_item(k, cw, ch) for k in kinds]}


OLD_METRICS = {"cpu": "auto/cpu_load", "cpu_freq": "auto/cpu_clock", "ram": "auto/ram_load",
               "ram_used": "auto/ram_used", "gpu": "auto/gpu_load", "gpu_temp": "auto/gpu_temp",
               "vram": "auto/vram_used", "gpu_fan": "auto/gpu_fan", "net_down": "auto/net_down",
               "net_up": "auto/net_up", "disk_read": "auto/disk_read", "disk_write": "auto/disk_write"}


def normalize_item(it):
    """Дополняет свойства значениями по умолчанию и убирает устаревшие ключи."""
    defaults = TYPES[it["type"]][1]
    old = it.get("props", {})
    if it["type"] == "sensor" and "metric" in old:
        old["sensor"] = OLD_METRICS.get(old.pop("metric"), "auto/cpu_load")
    if it["type"] == "sensors" and "transparent" in old:
        old = {"accent": old.get("accent", defaults["accent"])}
    it["props"] = {k: old.get(k, copy.deepcopy(v)) for k, v in defaults.items()}
    return it


def duplicate_item(item):
    dup = copy.deepcopy(item)
    dup["id"] = uuid.uuid4().hex[:8]
    dup["x"] += 20
    dup["y"] += 20
    return dup
