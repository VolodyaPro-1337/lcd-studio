"""Скачивает LibreHardwareMonitorLib и его зависимости для .NET Framework в папку lhm/.

Запуск: python tools/fetch_lhm.py [папка] [версия]
"""
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "lhm"
VERSION = sys.argv[2] if len(sys.argv) > 2 else "0.9.6"
API = "https://api.nuget.org/v3-flatcontainer"
TFMS = ["net472", "net471", "net47", "net462", "net461", "net46", "net45", "netstandard2.0"]
GROUPS = [".NETFramework4.7.2", ".NETFramework4.6.2", ".NETFramework4.6.1", ".NETFramework4.6",
          ".NETFramework4.5", ".NETStandard2.0"]
# есть в самом .NET Framework — отдельные копии только мешают
SKIP = {"system.management", "system.codedom", "microsoft.win32.registry", "system.io.filesystem.accesscontrol",
        "netstandard.library", "microsoft.netcore.platforms"}
seen = set()


def fetch(pid, ver):
    key = pid.lower()
    if key in seen or key in SKIP:
        return
    seen.add(key)
    ver = ver.strip("[]()").split(",")[0].lower()
    with urllib.request.urlopen(f"{API}/{key}/{ver}/{key}.{ver}.nupkg", timeout=60) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    names = z.namelist()
    for tfm in TFMS:
        for prefix in (f"runtimes/win/lib/{tfm}/", f"runtimes/win-x64/lib/{tfm}/", f"lib/{tfm}/"):
            dlls = [n for n in names if n.startswith(prefix) and n.endswith(".dll")]
            if dlls:
                for n in dlls:
                    (OUT / Path(n).name).write_bytes(z.read(n))
                print(f"{pid} {ver}: {', '.join(Path(n).name for n in dlls)}")
                break
        else:
            continue
        break
    nuspec = z.read(next(n for n in names if n.endswith(".nuspec"))).decode("utf-8", "replace")
    groups = dict(re.findall(r'<group targetFramework="([^"]*)"\s*/?>(.*?)(?=</group>|<group|$)', nuspec, re.S))
    deps = next((groups[g] for g in GROUPS if g in groups), "" if groups else nuspec)
    for dpid, dver in re.findall(r'<dependency id="([^"]+)" version="([^"]+)"', deps):
        fetch(dpid, dver)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fetch("LibreHardwareMonitorLib", VERSION)
