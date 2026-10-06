import json
import os
from pathlib import Path

from .model import TYPES, new_item, new_scene, normalize_item

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "LcdStudio"
CONFIG_FILE = Path(os.environ.get("LCDSTUDIO_CONFIG") or CONFIG_DIR / "config.json")

DEFAULTS = {
    "width": 1920,
    "height": 480,
    "rotation": 0,
    "fps": 30,
    "brightness": 100,
    "scene_index": 0,
    "scene_cycle": False,
    "scene_cycle_interval": 30,
    "window_output": False,
    "window_screen": "",
    "device": "",
    "sensor_interval": 1.0,
}


def default_scenes(cw, ch):
    desk = new_scene("Рабочий стол", cw, ch)
    clock = new_item("clock", cw, ch)
    clock.update(w=cw // 6, h=ch // 4, x=cw - cw // 6 - 20, y=20)
    desk["items"].append(clock)

    mon = new_scene("Мониторинг", cw, ch, kinds=())
    mon["items"].append(new_item("sensors", cw, ch))
    return [desk, mon]


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if not cfg.get("scenes"):
        cfg["scenes"] = default_scenes(cfg["width"], cfg["height"])
    for sc in cfg["scenes"]:
        sc["items"] = [normalize_item(it) for it in sc["items"] if it.get("type") in TYPES]
    return cfg


def save(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG_FILE)
