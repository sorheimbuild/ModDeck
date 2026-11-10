#!/usr/bin/env python3
"""
ModDeck - a local Minecraft mod manager for SKLauncher / PollyMC / Prism-style folders.

Standard library only. Starts a small HTTP server on 127.0.0.1 and opens the UI in an
Edge app window. Nothing is ever deleted: disabled mods move to `mods.disabled` and
removed files move to `data/trash/<timestamp>/`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "ModDeck"
APP_VERSION = "1.1.0"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
WEB_DIR = RESOURCE_DIR / "web"
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or str(BASE_DIR)) / "ModDeck"
else:
    DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
CACHE_PATH = DATA_DIR / "cache.json"
INSTALLED_PATH = DATA_DIR / "installed.json"
LOG_PATH = DATA_DIR / "moddeck.log"
PORT_PATH = DATA_DIR / "port.txt"
UA = f"{APP_NAME}/{APP_VERSION} (local Minecraft mod manager)"
TOKEN = secrets.token_urlsafe(24)

BUILTIN_DEPS = {
    "minecraft", "java", "fabricloader", "fabric-loader", "fabric_loader",
    "forge", "neoforge", "quilt_loader", "quilt-loader", "javafml",
    "lowcodefml", "mixinextras", "modlauncher",
}
MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
CF_GAME_ID = 432
CF_CLASS_MODS = 6
CF_LOADERS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
LOADER_ALIASES = {
    "fabric-loader": "fabric", "fabric": "fabric", "forge": "forge",
    "neoforge": "neoforge", "quilt-loader": "quilt", "quilt": "quilt",
    "optifine": "optifine", "vanilla": "vanilla", "unknown": "unknown",
}
DEFAULT_SETTINGS = {
    "customInstances": [],   # [{"path": "...", "name": "..."}]
    "overrides": {},         # {instance_id: {"mcVersion": "...", "loader": "..."}}
    "curseforgeKey": "",
    "lastInstance": "",
    "askDeleteOnDisable": True,
}

# --------------------------------------------------------------------------- log


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ----------------------------------------------------------------------- helpers


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log(f"write {path} failed: {e}")


SETTINGS = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_PATH, {})}
INSTALLED = _read_json(INSTALLED_PATH, {"files": {}})
if not isinstance(INSTALLED.get("files"), dict):
    INSTALLED = {"files": {}}

_CACHE: dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def cget(key: str):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and item[1] > time.time():
            return item[0]
    return None


def cset(key: str, value, ttl: float = 600) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, time.time() + ttl)


def save_settings() -> None:
    _write_json(SETTINGS_PATH, SETTINGS)


def save_installed() -> None:
    _write_json(INSTALLED_PATH, INSTALLED)


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _first_str(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        for item in v:
            s = _first_str(item)
            if s:
                return s
    if isinstance(v, dict):
        for key in ("text", "en_us", "en_US"):
            if isinstance(v.get(key), str):
                return v[key]
        for item in v.values():
            s = _first_str(item)
            if s:
                return s
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _authors(v) -> list[str]:
    out: list[str] = []
    for item in _as_list(v):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("id") or ""
            if name:
                out.append(str(name))
    return [a for a in out if a][:8]


# --------------------------------------------------------------- version ranges


def _vt(version: str) -> tuple[int, ...]:
    version = str(version).strip().lower().split("+")[0]
    core = version.split("-")[0]
    parts = re.findall(r"\d+", core)
    return tuple(int(p) for p in parts[:5]) if parts else (0,)


def _cmp(a, b) -> int:
    va, vb = _vt(a), _vt(b)
    n = max(len(va), len(vb))
    va = va + (0,) * (n - len(va))
    vb = vb + (0,) * (n - len(vb))
    return (va > vb) - (va < vb)


def version_satisfies(version: str, expr: str) -> bool:
    """Best-effort Maven/semver range check used for warnings only."""
    if not expr or str(expr).strip() in ("*", ""):
        return True
    expr = str(expr).strip()

    # Maven style: [1.20.4,1.21)  or  [1.20,1.21],[1.22,)
    if expr[0] in "[(":
        chunks = re.findall(r"[\[(][^\])]*[\])]", expr)
        if not chunks:
            return True
        for chunk in chunks:
            lo_inc = chunk[0] == "["
            hi_inc = chunk[-1] == "]"
            body = chunk[1:-1]
            if "," in body:
                lo, hi = body.split(",", 1)
            else:
                lo = hi = body
            lo, hi = lo.strip(), hi.strip()
            ok = True
            if lo:
                c = _cmp(version, lo)
                ok = ok and (c >= 0 if lo_inc else c > 0)
            if hi:
                c = _cmp(version, hi)
                ok = ok and (c <= 0 if hi_inc else c < 0)
            if ok:
                return True
        return False

    # "1.20.1 - 1.20.4"
    m = re.match(r"^(\d[\w.\-+]*)\s+-\s+(\d[\w.\-+]*)$", expr)
    if m:
        return _cmp(version, m.group(1)) >= 0 and _cmp(version, m.group(2)) <= 0

    for token in expr.split():
        token = token.strip().rstrip(".")
        if not token or token == "*":
            continue
        m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$", token)
        if not m:
            continue
        op = m.group(1) or ""
        val = m.group(2).strip()
        if val.lower().endswith(".x"):
            pref = _vt(val[:-2])
            if _vt(version)[: len(pref)] != pref:
                return False
            continue
        if op in (">=", ">", "<=", "<"):
            c = _cmp(version, val)
            ok = {">=": c >= 0, ">": c > 0, "<=": c <= 0, "<": c < 0}[op]
            if not ok:
                return False
        elif op == "^":
            vv = _vt(val)
            upper = f"{vv[0] + 1}.0.0" if vv else "9999"
            if not (_cmp(version, val) >= 0 and _cmp(version, upper) < 0):
                return False
        elif op == "~":
            vv = _vt(val)
            upper = f"{vv[0]}.{vv[1] + 1}.0" if len(vv) > 1 else f"{vv[0] + 1}.0.0"
            if not (_cmp(version, val) >= 0 and _cmp(version, upper) < 0):
                return False
        else:
            vv = _vt(val)
            if not (_cmp(version, val) == 0 or _vt(version)[: len(vv)] == vv):
                return False
    return True


# ------------------------------------------------------------------ jar parsing


def _manifest(z: zipfile.ZipFile) -> dict[str, str]:
    try:
        raw = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _fabric(z: zipfile.ZipFile, info: dict) -> None:
    data = json.loads(z.read("fabric.mod.json").decode("utf-8-sig", "replace"))
    info["loader"] = "fabric"
    info["id"] = str(data.get("id") or "").strip() or None
    info["name"] = _first_str(data.get("name")) or info["name"]
    info["version"] = _first_str(data.get("version"))
    info["description"] = _first_str(data.get("description"))
    info["authors"] = _authors(data.get("authors"))
    env = data.get("environment")
    info["env"] = env if env in ("client", "server") else "both"
    info["provides"] = [str(x) for x in _as_list(data.get("provides")) if x]
    for mixin in _as_list(data.get("mixins")):
        if isinstance(mixin, str):
            info["mixinConfigs"].append(mixin)
        elif isinstance(mixin, dict) and mixin.get("config"):
            info["mixinConfigs"].append(str(mixin["config"]))

    icon = data.get("icon")
    if isinstance(icon, str):
        info["iconPath"] = icon
    elif isinstance(icon, dict):
        for key in ("1x", "2x", "4x"):
            if isinstance(icon.get(key), str):
                info["iconPath"] = icon[key]
                break

    depends = data.get("depends")
    if isinstance(depends, dict):
        for dep_id, ranges in depends.items():
            for rng in _as_list(ranges) or ["*"]:
                info["deps"].append({"id": str(dep_id), "range": str(rng or "*"), "required": True})
    elif isinstance(depends, list):
        for entry in depends:
            if isinstance(entry, dict) and entry.get("id"):
                info["deps"].append({
                    "id": str(entry["id"]),
                    "range": str(entry.get("versions") or "*"),
                    "required": True,
                })

    for key in ("breaks", "conflicts"):
        raw = data.get(key)
        if isinstance(raw, dict):
            for dep_id, ranges in raw.items():
                info[key].append({"id": str(dep_id), "range": str(_as_list(ranges)[0] if ranges else "*")})
        elif isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and entry.get("id"):
                    info[key].append({"id": str(entry["id"]), "range": str(entry.get("versions") or "*")})

    for dep in info["deps"]:
        if dep["id"] == "minecraft":
            info["mcRange"] = dep["range"]


def _quilt(z: zipfile.ZipFile, info: dict) -> None:
    data = json.loads(z.read("quilt.mod.json").decode("utf-8-sig", "replace"))
    loader = data.get("quilt_loader") or {}
    info["loader"] = "quilt"
    info["id"] = str(loader.get("id") or "").strip() or None
    meta = loader.get("metadata") or {}
    info["name"] = _first_str(meta.get("name")) or info["id"] or info["name"]
    info["version"] = _first_str(loader.get("version")) or _first_str(meta.get("version"))
    info["description"] = _first_str(meta.get("description"))
    info["authors"] = _authors(meta.get("contributors"))
    info["provides"] = [str(x.get("id")) for x in _as_list(loader.get("provides")) if isinstance(x, dict) and x.get("id")]
    for dep in _as_list(loader.get("depends")):
        if isinstance(dep, str):
            info["deps"].append({"id": dep, "range": "*", "required": True})
        elif isinstance(dep, dict) and dep.get("id"):
            info["deps"].append({"id": str(dep["id"]), "range": str(dep.get("versions") or "*"), "required": True})
            if dep["id"] == "minecraft":
                info["mcRange"] = str(dep.get("versions") or "*")
    mc = data.get("minecraft") or {}
    if mc.get("environment") in ("client", "server"):
        info["env"] = mc["environment"]


def _forge(z: zipfile.ZipFile, info: dict, member: str, loader: str) -> None:
    text = z.read(member).decode("utf-8-sig", "replace")
    try:
        data = tomllib.loads(text)
    except Exception:
        # A few old mods ship slightly broken TOML; salvage the basics.
        mid = re.search(r'modId\s*=\s*"([^"]+)"', text)
        info["loader"] = loader
        info["id"] = mid.group(1) if mid else None
        info["issues"].append("mods.toml could not be fully parsed.")
        return
    mods = _as_list(data.get("mods"))
    mod = mods[0] if mods and isinstance(mods[0], dict) else {}
    info["loader"] = loader
    info["id"] = str(mod.get("modId") or "").strip() or None
    info["name"] = _first_str(mod.get("displayName")) or info["id"] or info["name"]
    info["version"] = _first_str(mod.get("version"))
    info["description"] = _first_str(mod.get("description"))
    info["authors"] = _authors(mod.get("authors"))
    logo = mod.get("logoFile")
    if isinstance(logo, str):
        info["iconPath"] = logo

    if not info["id"]:
        mid = re.search(r'modId\s*=\s*"([^"]+)"', text)
        info["id"] = mid.group(1) if mid else None

    declared_side = str(data.get("displayTest") or "").upper()
    if declared_side == "IGNORE_SERVER_VERSION":
        info["env"] = "both"

    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for _owner, entries in deps.items():
            for dep in _as_list(entries):
                if not isinstance(dep, dict):
                    continue
                dep_id = dep.get("modId")
                if not dep_id:
                    continue
                dep_id = str(dep_id)
                rng = str(dep.get("versionRange") or "*")
                mandatory = bool(dep.get("mandatory", True))
                if dep_id == "minecraft":
                    info["mcRange"] = rng
                    continue
                info["deps"].append({
                    "id": dep_id,
                    "range": rng,
                    "required": mandatory,
                    "side": dep.get("side"),
                })
    if "$" in str(info.get("version") or ""):
        manifest = _manifest(z)
        info["version"] = manifest.get("implementation-version") or info["version"]

    for mixin in _as_list(data.get("mixins")):
        if isinstance(mixin, str):
            info["mixinConfigs"].append(mixin)
        elif isinstance(mixin, dict) and mixin.get("config"):
            info["mixinConfigs"].append(str(mixin["config"]))


def _legacy(z: zipfile.ZipFile, info: dict) -> None:
    data = json.loads(z.read("mcmod.info").decode("utf-8-sig", "replace"))
    entries = data.get("modList") if isinstance(data, dict) else data
    entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    info["loader"] = "forge"
    info["id"] = str(entry.get("modid") or "").strip() or None
    info["name"] = _first_str(entry.get("name")) or info["id"] or info["name"]
    info["version"] = _first_str(entry.get("version"))
    info["description"] = _first_str(entry.get("description"))
    info["authors"] = _authors(entry.get("authorList") or entry.get("author"))
    info["mcRange"] = _first_str(entry.get("mcversion")) or None


_JAR_CACHE: dict[tuple, dict] = {}
_JAR_LOCK = threading.Lock()


def analyze_jar(path: Path) -> dict:
    info = {
        "id": None, "name": path.stem, "version": None, "loader": "unknown",
        "description": "", "authors": [], "iconPath": None, "env": "both",
        "deps": [], "breaks": [], "conflicts": [], "mcRange": None,
        "provides": [], "mixinConfigs": [], "issues": [], "kind": "mod", "nested": 0,
    }
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            info["nested"] = sum(1 for n in names if n.lower().endswith(".jar"))
            if "fabric.mod.json" in names:
                _fabric(z, info)
            elif "quilt.mod.json" in names:
                _quilt(z, info)
            elif "META-INF/neoforge.mods.toml" in names:
                _forge(z, info, "META-INF/neoforge.mods.toml", "neoforge")
            elif "META-INF/mods.toml" in names:
                _forge(z, info, "META-INF/mods.toml", "forge")
            elif "mcmod.info" in names:
                _legacy(z, info)
            elif "pack.mcmeta" in names and any(n.startswith("assets/") for n in names):
                info["kind"] = "resourcepack"
                info["issues"].append("This is a resource pack, not a mod - move it to resourcepacks/.")
            else:
                info["issues"].append("No recognisable mod metadata (fabric.mod.json / mods.toml).")
    except zipfile.BadZipFile:
        info["issues"].append("File is not a valid jar (corrupt or incomplete download).")
    except Exception as e:
        info["issues"].append(f"Could not read jar: {e}")

    if not info["name"] or info["name"] == path.stem:
        cleaned = re.sub(r"[-_.+]?(fabric|forge|neoforge|quilt)?[-_.+]?(mc)?\d+(\.\d+)*.*$", "", path.stem, flags=re.I)
        info["name"] = (cleaned or path.stem).replace("-", " ").replace("_", " ").strip()
    if not info["version"]:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?)", path.stem)
        if m:
            info["version"] = m.group(1)
    return info


# -------------------------------------------------------------------- analysis

KNOWN_CONFLICTS = [
    (r"optifine|optifabric", r"sodium|embeddium|rubidium",
     "OptiFine and Sodium/Embeddium both replace the renderer - running them together "
     "crashes on launch or shows a black screen. Pick one."),
    (r"optifine|optifabric", r"iris|oculus",
     "OptiFine's shader system clashes with Iris/Oculus. Use Iris (with Sodium), not OptiFine, for shaders."),
    (r"^sodium$", r"^embeddium$|^rubidium$",
     "Sodium, Embeddium and Rubidium are competing renderer replacements. Only one may be installed."),
    (r"^iris$", r"^oculus$",
     "Iris and Oculus are the same mod for different loaders - having both will conflict."),
    (r"^starlight$|starlight", r"^phosphor$|phosphor",
     "Starlight and Phosphor are both lighting engines and overwrite the same code."),
    (r"^lithium$", r"^radium$",
     "Lithium and Radium target the same server-side optimisations and conflict."),
]


def analyze_modset(mods: list[dict], instance: dict) -> tuple[list[dict], dict[str, int]]:
    issues: list[dict] = []
    flags: dict[str, int] = {}
    by_id: dict[str, list[dict]] = {}
    provided: set[str] = set()

    for mod in mods:
        if mod.get("id"):
            by_id.setdefault(mod["id"], []).append(mod)
            provided.add(mod["id"])
        for p in mod.get("provides") or []:
            provided.add(str(p))

    has_fabric_api = any(m.get("id") == "fabric-api" for m in mods)
    loader = instance.get("loader") or "unknown"
    mc_version = instance.get("mcVersion") or ""

    def is_provided(dep_id: str) -> bool:
        if dep_id in BUILTIN_DEPS:
            return True
        if dep_id in provided:
            return True
        if has_fabric_api and dep_id.startswith("fabric-"):
            return True
        return False

    def flag(files: list[str], level: int) -> None:
        for f in files:
            flags[f] = max(flags.get(f, 0), level)

    # 1. duplicate mod ids
    for mod_id, group in sorted(by_id.items()):
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda m: m.get("mtime", 0), reverse=True)
        keep = group[0]
        drop = group[1:]
        issues.append({
            "kind": "duplicate",
            "severity": "error",
            "title": f"Duplicate mod: {keep.get('name') or mod_id}",
            "detail": (
                f"{len(group)} copies of mod id '{mod_id}' are enabled at the same time "
                f"({', '.join(m['file'] for m in group)}). Fabric/Forge will only load one, "
                f"usually the wrong one - this is a common cause of launch failures."
            ),
            "files": [m["file"] for m in group],
            "modIds": [mod_id],
            "keep": keep["file"],
            "disable": [m["file"] for m in drop],
        })
        flag([m["file"] for m in group], 2)

    # 2. missing required dependencies
    missing: dict[str, dict] = {}
    for mod in mods:
        for dep in mod.get("deps") or []:
            if not dep.get("required"):
                continue
            dep_id = str(dep.get("id") or "")
            if not dep_id or is_provided(dep_id):
                continue
            entry = missing.setdefault(dep_id, {"range": dep.get("range"), "neededBy": []})
            if mod["file"] not in entry["neededBy"]:
                entry["neededBy"].append(mod["file"])
    fabric_missing = {k: v for k, v in missing.items() if k.startswith("fabric-")}
    if fabric_missing:
        needed_by = sorted({f for v in fabric_missing.values() for f in v["neededBy"]})
        issues.append({
            "kind": "missing-dep",
            "severity": "error",
            "title": "Missing Fabric API",
            "detail": (
                f"{len(needed_by)} mod(s) need Fabric API modules ("
                + ", ".join(sorted(fabric_missing)[:4])
                + ("…" if len(fabric_missing) > 4 else "")
                + ") that ship inside Fabric API. Install fabric-api and they resolve at once."
            ),
            "files": needed_by,
            "dep": "fabric-api",
            "search": "fabric api",
            "modules": sorted(fabric_missing),
        })
        flag(needed_by, 2)
        for key in fabric_missing:
            missing.pop(key, None)

    for dep_id, entry in sorted(missing.items()):
        issues.append({
            "kind": "missing-dep",
            "severity": "error",
            "title": f"Missing dependency: {dep_id}",
            "detail": (
                f"{len(entry['neededBy'])} mod(s) require '{dep_id}'"
                f"{' (' + entry['range'] + ')' if entry.get('range') not in (None, '*') else ''}, "
                f"but it is not installed. The game will fail to load those mods."
            ),
            "files": entry["neededBy"],
            "dep": dep_id,
            "range": entry.get("range"),
            "search": dep_id,
        })
        flag(entry["neededBy"], 2)

    # 3. declared conflicts / breaks (only if the installed version falls in the broken range)
    for mod in mods:
        for kind in ("breaks", "conflicts"):
            for entry in mod.get(kind) or []:
                target = str(entry.get("id") or "")
                if not target or target == mod.get("id") or target in BUILTIN_DEPS:
                    continue
                others = by_id.get(target) or []
                if not others:
                    continue
                rng = str(entry.get("range") or "*")
                hits: list[dict] = []
                unknown: list[dict] = []
                for other in others:
                    ver = str(other.get("version") or "")
                    if not ver:
                        unknown.append(other)
                    elif rng in ("*", ""):
                        hits.append(other)
                    elif version_satisfies(ver, rng):
                        hits.append(other)
                if not hits and not unknown:
                    continue  # installed version is newer/older than the broken range
                affected = hits or unknown
                files = [mod["file"], *[m["file"] for m in affected]]
                if hits:
                    detail = (
                        f"'{mod['file']}' declares that it {'breaks' if kind == 'breaks' else 'conflicts with'} "
                        f"'{target}' {rng}. The installed version "
                        f"({', '.join(str(m.get('version')) for m in affected)}) is inside that range."
                    )
                    severity, level = "error", 2
                else:
                    detail = (
                        f"'{mod['file']}' declares a conflict with '{target}' {rng}, and the installed "
                        f"version could not be determined. Check this pair on the mod page."
                    )
                    severity, level = "warning", 1
                issues.append({
                    "kind": "declared-conflict",
                    "severity": severity,
                    "title": f"{mod.get('name') or mod['file']} conflicts with {target}",
                    "detail": detail,
                    "files": files,
                    "modIds": [mod.get("id"), target],
                })
                flag(files, level)

    # 4. known community conflicts
    for left_pat, right_pat, explanation in KNOWN_CONFLICTS:
        left = [m for m in mods if re.search(left_pat, f"{m.get('id') or ''} {m.get('name') or ''}".lower())]
        right = [m for m in mods if re.search(right_pat, f"{m.get('id') or ''} {m.get('name') or ''}".lower())]
        left_ids = {m.get("id") for m in left}
        right_ids = {m.get("id") for m in right}
        if left and right and not (left_ids & right_ids):
            files = list({m["file"] for m in left + right})
            issues.append({
                "kind": "known-conflict",
                "severity": "error",
                "title": f"{left[0].get('name')} + {right[0].get('name')} is a known bad combination",
                "detail": explanation,
                "files": files,
                "modIds": [m.get("id") for m in left + right if m.get("id")],
            })
            flag(files, 2)

    # 5. loader mismatch
    for mod in mods:
        mloader = (mod.get("loader") or "unknown").lower()
        if mloader == "unknown":
            issues.append({
                "kind": "unknown-jar",
                "severity": "warning",
                "title": f"Unrecognised file: {mod['file']}",
                "detail": (
                    "No mod metadata was found. If this is a library, it is harmless; "
                    "if it is a mod for another loader it will be ignored or crash the game."
                ),
                "files": [mod["file"]],
            })
            flag([mod["file"]], 1)
            continue
        if mod.get("kind") == "resourcepack":
            issues.append({
                "kind": "wrong-kind",
                "severity": "warning",
                "title": f"Resource pack in mods folder: {mod['file']}",
                "detail": "This jar contains a resource pack. Move it to resourcepacks/ - mod loaders ignore it here.",
                "files": [mod["file"]],
            })
            flag([mod["file"]], 1)
            continue
        compatible = {
            "fabric": {"fabric", "quilt"},
            "quilt": {"quilt", "fabric"},
            "forge": {"forge"},
            "neoforge": {"neoforge", "forge"},
            "vanilla": set(),
            "unknown": set(),
        }.get(loader, set())
        if loader not in ("unknown", "") and mloader not in compatible:
            issues.append({
                "kind": "loader-mismatch",
                "severity": "error",
                "title": f"{mod.get('name') or mod['file']} is a {mloader} mod, but this instance is {loader}",
                "detail": f"'{mod['file']}' declares {mloader} metadata. A {loader} loader cannot load it - remove or disable it.",
                "files": [mod["file"]],
            })
            flag([mod["file"]], 2)

    # 6. Minecraft version mismatch (warning only - ranges are often loose)
    if mc_version:
        for mod in mods:
            rng = mod.get("mcRange")
            if not rng or rng == "*":
                continue
            if not version_satisfies(mc_version, str(rng)):
                issues.append({
                    "kind": "mc-mismatch",
                    "severity": "warning",
                    "title": f"{mod.get('name') or mod['file']} targets {rng}, not {mc_version}",
                    "detail": (
                        f"This jar declares support for Minecraft {rng} while the instance is {mc_version}. "
                        "It may still work, but crashes or missing behaviour are likely."
                    ),
                    "files": [mod["file"]],
                    "range": str(rng),
                })
                flag([mod["file"]], 1)

    # 7. two mods writing the same mixin config name (rare but fatal)
    mixin_owners: dict[str, list[str]] = {}
    for mod in mods:
        for name in mod.get("mixinConfigs") or []:
            mixin_owners.setdefault(str(name), []).append(mod["file"])
    for name, files in mixin_owners.items():
        if len({f for f in files}) > 1:
            issues.append({
                "kind": "mixin-clash",
                "severity": "error",
                "title": f"Two mods declare the same mixin config: {name}",
                "detail": "Only one jar should ship this mixin configuration file. One of these mods is probably a duplicate build.",
                "files": list(dict.fromkeys(files)),
            })
            flag(files, 2)

    return issues, flags


def score_for(issues: list[dict]) -> int:
    errors = sum(1 for i in issues if i["severity"] == "error")
    warnings = sum(1 for i in issues if i["severity"] == "warning")
    score = 100 - errors * 18 - warnings * 4
    return max(0, min(100, score))


# ------------------------------------------------------------------- instances


def _parse_version_id(version_id: str) -> tuple[str, str | None, str | None]:
    """Return (loader, loaderVersion, mcVersion) from a launcher version id."""
    vid = version_id or ""
    low = vid.lower()
    parts = vid.split("-")
    if low.startswith("fabric-loader-") and len(parts) >= 4:
        return "fabric", parts[2], "-".join(parts[3:])
    if low.startswith("quilt-loader-") and len(parts) >= 4:
        return "quilt", parts[2], "-".join(parts[3:])
    if low.startswith("neoforge-"):
        return "neoforge", "-".join(parts[1:]), None
    if low.startswith("forge-") and len(parts) >= 3:
        return "forge", "-".join(parts[2:]), parts[1]
    m = re.match(r"^(\d+\.\d+(?:\.\d+)?)(?:-|$)", vid)
    if m and "optifine" in low:
        return "optifine", None, m.group(1)
    if m:
        return "vanilla", None, m.group(1)
    return "unknown", None, None


def _detect_from_mmc_pack(path: Path) -> dict:
    data = _read_json(path, {})
    out = {"loader": "unknown", "loaderVersion": None, "mcVersion": None, "name": None}
    for comp in data.get("components") or []:
        uid = str(comp.get("uid") or "")
        ver = comp.get("version")
        if uid == "net.minecraft":
            out["mcVersion"] = ver
        elif uid == "net.fabricmc.fabric-loader":
            out["loader"], out["loaderVersion"] = "fabric", ver
        elif uid == "org.quiltmc.quilt-loader":
            out["loader"], out["loaderVersion"] = "quilt", ver
        elif uid == "net.minecraftforge":
            out["loader"], out["loaderVersion"] = "forge", ver
        elif uid == "net.neoforged":
            out["loader"], out["loaderVersion"] = "neoforge", ver
    return out


def _detect_from_profiles(folder: Path) -> dict:
    data = _read_json(folder / "launcher_profiles.json", {})
    out = {"loader": None, "loaderVersion": None, "mcVersion": None, "name": None}
    profiles = data.get("profiles") or {}
    selected = data.get("selectedProfile")
    profile = None
    if selected and selected in profiles:
        profile = profiles[selected]
    else:
        usable = [p for p in profiles.values() if isinstance(p, dict) and p.get("lastUsed")]
        usable.sort(key=lambda p: p.get("lastUsed") or "", reverse=True)
        profile = usable[0] if usable else None
    if isinstance(profile, dict):
        loader, lv, mc = _parse_version_id(str(profile.get("lastVersionId") or ""))
        out.update({"loader": loader, "loaderVersion": lv, "mcVersion": mc, "name": profile.get("name")})
    if not out["mcVersion"]:
        versions = folder / "versions"
        if versions.is_dir():
            cands = [p for p in versions.iterdir() if p.is_dir()]
            cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for cand in cands[:12]:
                loader, lv, mc = _parse_version_id(cand.name)
                if mc and loader in ("fabric", "quilt", "forge", "neoforge", "vanilla"):
                    out.update({"loader": loader, "loaderVersion": lv, "mcVersion": mc})
                    break
    return out


def _detect_loader_from_mods(mods_dir: Path) -> str | None:
    if not mods_dir.is_dir():
        return None
    counts = {"fabric": 0, "forge": 0, "neoforge": 0, "quilt": 0}
    for jar in list(mods_dir.glob("*.jar"))[:25]:
        try:
            with zipfile.ZipFile(jar) as z:
                names = set(z.namelist())
        except Exception:
            continue
        if "fabric.mod.json" in names:
            counts["fabric"] += 1
        elif "quilt.mod.json" in names:
            counts["quilt"] += 1
        elif "META-INF/neoforge.mods.toml" in names:
            counts["neoforge"] += 1
        elif "META-INF/mods.toml" in names:
            counts["forge"] += 1
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] else None


def instance_id(path: str) -> str:
    return "i" + hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]


def build_instance(name: str, path: Path, kind: str) -> dict:
    mods_dir = path / "mods"
    detected = {"loader": "unknown", "loaderVersion": None, "mcVersion": None}
    mmc = path / "mmc-pack.json"
    if mmc.is_file():
        d = _detect_from_mmc_pack(mmc)
        detected.update({k: v for k, v in d.items() if v})
        if not name:
            cfg = path / "instance.cfg"
            try:
                for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.lower().startswith("name="):
                        name = line.split("=", 1)[1]
                        break
            except Exception:
                pass
    if not detected.get("mcVersion") or detected.get("loader") == "unknown":
        d = _detect_from_profiles(path)
        for key in ("loader", "loaderVersion", "mcVersion"):
            if not detected.get(key) and d.get(key):
                detected[key] = d[key]
        if not name:
            name = d.get("name") or name
    if detected.get("loader") == "unknown":
        guessed = _detect_loader_from_mods(mods_dir)
        if guessed:
            detected["loader"] = guessed

    iid = instance_id(str(path))
    override = (SETTINGS.get("overrides") or {}).get(iid) or {}
    loader = LOADER_ALIASES.get(str(override.get("loader") or detected.get("loader") or "unknown"), "unknown")
    mc_version = override.get("mcVersion") or detected.get("mcVersion") or ""

    return {
        "id": iid,
        "name": name or path.name or "Instance",
        "path": str(path),
        "type": kind,
        "loader": loader,
        "loaderVersion": detected.get("loaderVersion"),
        "mcVersion": mc_version,
        "detected": {
            "loader": LOADER_ALIASES.get(str(detected.get("loader") or "unknown"), "unknown"),
            "mcVersion": detected.get("mcVersion") or "",
        },
        "modsPath": str(mods_dir),
        "hasModsFolder": mods_dir.is_dir(),
    }


def detect_instances() -> list[dict]:
    out: list[dict] = []
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    seen: set[str] = set()

    mc = appdata / ".minecraft"
    if mc.is_dir():
        inst = build_instance("SKLauncher (vanilla folder)", mc, "sklauncher")
        out.append(inst)
        seen.add(str(mc).lower())

    for base, kind in (
        (appdata / "PollyMC" / "instances", "pollymc"),
        (appdata / "PrismLauncher" / "instances", "prism"),
        (appdata / "MultiMC" / "instances", "multimc"),
    ):
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir() or str(folder).lower() in seen:
                continue
            if not (folder / "mmc-pack.json").is_file() and not (folder / "instance.cfg").is_file():
                continue
            out.append(build_instance(folder.name, folder, kind))
            seen.add(str(folder).lower())

    for entry in SETTINGS.get("customInstances") or []:
        try:
            p = Path(str(entry.get("path") or "")).expanduser()
            if p.name.lower() == "mods":
                p = p.parent
            if not p.is_dir() or str(p).lower() in seen:
                continue
            out.append(build_instance(entry.get("name") or p.name, p, "custom"))
            seen.add(str(p).lower())
        except Exception:
            continue

    for inst in out:
        try:
            inst["modsCount"] = len([p for p in Path(inst["modsPath"]).glob("*.jar")]) if inst["hasModsFolder"] else 0
        except Exception:
            inst["modsCount"] = 0
    return out


def get_instance(iid: str) -> dict | None:
    for inst in detect_instances():
        if inst["id"] == iid:
            return inst
    return None


# ------------------------------------------------------------------ mod scanning


def scan_instance(inst: dict) -> dict:
    mods_dir = Path(inst["modsPath"])
    disabled_dir = mods_dir.parent / "mods.disabled"
    entries: list[tuple[Path, bool]] = []
    for folder, enabled in ((mods_dir, True), (disabled_dir, False)):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            low = p.name.lower()
            if not (low.endswith(".jar") or low.endswith(".jar.disabled") or low.endswith(".litemod")):
                continue
            entries.append((p, enabled))

    def work(item):
        p, enabled = item
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        with _JAR_LOCK:
            cached = _JAR_CACHE.get(key)
        info = dict(cached) if cached else analyze_jar(p)
        if not cached:
            with _JAR_LOCK:
                _JAR_CACHE[key] = dict(info)
        info.update({
            "file": p.name,
            "path": str(p),
            "size": st.st_size,
            "sizeHuman": human_size(st.st_size),
            "mtime": st.st_mtime,
            "enabled": enabled and not p.name.lower().endswith(".jar.disabled"),
            "icon": f"/api/icon?instance={inst['id']}&file={urllib.parse.quote(p.name)}" if info.get("iconPath") else None,
        })
        return info

    if entries:
        with ThreadPoolExecutor(max_workers=8) as pool:
            mods = list(pool.map(work, entries))
    else:
        mods = []

    enabled_mods = [m for m in mods if m["enabled"]]
    issues, flags = analyze_modset(enabled_mods, inst)
    for mod in mods:
        if not mod["enabled"]:
            mod["status"] = "disabled"
        else:
            level = flags.get(mod["file"], 0)
            mod["status"] = "error" if level == 2 else "warn" if level == 1 else "ok"

    mods.sort(key=lambda m: (0 if m["enabled"] else 1, (m.get("name") or m["file"]).lower()))
    return {
        "instance": inst,
        "mods": mods,
        "issues": issues,
        "score": score_for(issues),
        "counts": {
            "total": len(mods),
            "enabled": len(enabled_mods),
            "disabled": len(mods) - len(enabled_mods),
            "errors": sum(1 for m in mods if m.get("status") == "error"),
            "warnings": sum(1 for m in mods if m.get("status") == "warn"),
            "updates": 0,
        },
        "scannedAt": time.time(),
    }


# ------------------------------------------------------------------ http client


def http_json(url: str, method: str = "GET", payload=None, headers: dict | None = None,
              timeout: float = 25, retries: int = 1):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last_err = ApiError(e.code, detail or str(e))
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise last_err
        except Exception as e:
            last_err = ApiError(0, f"{type(e).__name__}: {e}")
            if attempt < retries:
                time.sleep(1.0)
                continue
            raise last_err
    raise last_err or ApiError(0, "request failed")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def mr_versions(project_id: str, loader: str | None, mc_version: str | None) -> list[dict]:
    params = {}
    if loader and loader not in ("unknown", "vanilla", "optifine"):
        params["loaders"] = json.dumps([loader])
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    url = f"{MODRINTH}/project/{project_id}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    key = "mrv:" + url
    cached = cget(key)
    if cached is not None:
        return cached
    data = http_json(url) or []
    if not isinstance(data, list):
        data = []
    cset(key, data, ttl=600)
    return data


def cf_headers() -> dict:
    key = (SETTINGS.get("curseforgeKey") or "").strip()
    if not key:
        raise ApiError(401, "No CurseForge API key. Add one in Settings to search CurseForge.")
    return {"x-api-key": key}


# -------------------------------------------------------------------- endpoints


def api_state(_q):
    instances = detect_instances()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "instances": instances,
        "settings": {
            "curseforgeKey": bool((SETTINGS.get("curseforgeKey") or "").strip()),
            "lastInstance": SETTINGS.get("lastInstance") or "",
            "askDeleteOnDisable": bool(SETTINGS.get("askDeleteOnDisable", True)),
        },
        "dataDir": str(DATA_DIR),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def api_mods(q):
    inst = get_instance(q.get("instance", ""))
    if not inst:
        raise ApiError(404, "Unknown instance")
    return scan_instance(inst)


def _safe_child(folder: Path, filename: str) -> Path:
    name = Path(filename).name
    target = folder / name
    if target.parent != folder:
        raise ApiError(400, "Invalid file name")
    return target


def api_mods_action(payload):
    inst = get_instance(payload.get("instance", ""))
    if not inst:
        raise ApiError(404, "Unknown instance")
    action = payload.get("action")
    files = payload.get("files") or []
    if not isinstance(files, list) or not files:
        raise ApiError(400, "No files given")

    mods_dir = Path(inst["modsPath"])
    disabled_dir = mods_dir.parent / "mods.disabled"
    mods_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for raw in files:
        src = _safe_child(mods_dir, str(raw))
        if not src.exists():
            src = _safe_child(disabled_dir, str(raw))
        if not src.exists():
            results.append({"file": raw, "ok": False, "error": "Not found"})
            continue
        try:
            if action == "disable":
                if src.parent == disabled_dir:
                    results.append({"file": src.name, "ok": True, "note": "already disabled"})
                    continue
                disabled_dir.mkdir(exist_ok=True)
                dest = disabled_dir / src.name
                if dest.exists():
                    dest = disabled_dir / (src.stem + f"-{int(time.time())}" + src.suffix)
                shutil.move(str(src), str(dest))
                results.append({"file": src.name, "ok": True})
            elif action == "enable":
                dest_name = src.name
                if dest_name.lower().endswith(".jar.disabled"):
                    dest_name = dest_name[: -len(".disabled")]
                dest = _safe_child(mods_dir, dest_name)
                if dest.exists():
                    results.append({"file": src.name, "ok": False, "error": "A file with that name is already enabled"})
                    continue
                shutil.move(str(src), str(dest))
                results.append({"file": dest.name, "ok": True})
            elif action == "delete":
                trash = DATA_DIR / "trash" / time.strftime("%Y%m%d-%H%M%S") / Path(inst["path"]).name
                trash.mkdir(parents=True, exist_ok=True)
                dest = trash / src.name
                if dest.exists():
                    dest = trash / (src.stem + f"-{int(time.time())}" + src.suffix)
                shutil.move(str(src), str(dest))
                results.append({"file": src.name, "ok": True, "trash": str(dest)})
            else:
                raise ApiError(400, f"Unknown action '{action}'")
        except ApiError:
            raise
        except Exception as e:
            results.append({"file": raw, "ok": False, "error": str(e)})

    return {"results": results}


def api_icon(q):
    inst = get_instance(q.get("instance", ""))
    if not inst:
        raise ApiError(404, "Unknown instance")
    filename = q.get("file", "")
    names = [Path(filename).name]
    if " " in filename:
        names.append(Path(filename).name.replace(" ", "+"))
    candidates = []
    for name in names:
        candidates.append(Path(inst["modsPath"]) / name)
        candidates.append(Path(inst["modsPath"]).parent / "mods.disabled" / name)
    for jar in candidates:
        if not jar.is_file():
            continue
        try:
            info = analyze_jar(jar)
            icon_path = info.get("iconPath")
            if not icon_path:
                continue
            icon_path = str(icon_path).lstrip("/")
            with zipfile.ZipFile(jar) as z:
                if icon_path not in z.namelist():
                    continue
                data = z.read(icon_path)
            ext = Path(icon_path).suffix.lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(ext, "image/png")
            return {"__raw__": data, "contentType": ctype}
        except Exception:
            continue
    raise ApiError(404, "No icon")


def api_search(q):
    source = (q.get("source") or "modrinth").lower()
    query = q.get("q") or ""
    offset = max(0, int(q.get("offset") or 0))
    sort = q.get("sort") or "relevance"
    loader = q.get("loader") or ""
    mc_version = q.get("mc") or ""

    if source == "curseforge":
        params = {
            "gameId": CF_GAME_ID,
            "classId": CF_CLASS_MODS,
            "searchFilter": query,
            "sortField": {"relevance": 2, "downloads": 6, "updated": 3, "newest": 3}.get(sort, 2),
            "sortOrder": "desc",
            "pageSize": 20,
            "index": offset,
        }
        if mc_version:
            params["gameVersion"] = mc_version
        if loader in CF_LOADERS:
            params["modLoaderType"] = CF_LOADERS[loader]
        url = f"{CURSEFORGE}/mods/search?" + urllib.parse.urlencode(params)
        key = "cfsearch:" + url
        data = cget(key)
        if data is None:
            data = http_json(url, headers=cf_headers()) or {}
            cset(key, data, 240)
        hits = []
        for m in data.get("data") or []:
            hits.append({
                "source": "curseforge",
                "id": str(m.get("id")),
                "slug": m.get("slug"),
                "title": m.get("name"),
                "description": m.get("summary"),
                "icon": ((m.get("logo") or {}).get("thumbnailUrl") or ""),
                "downloads": m.get("downloadCount") or 0,
                "follows": 0,
                "author": ", ".join(a.get("name", "") for a in (m.get("authors") or []))[:80],
                "categories": [c.get("name") for c in (m.get("categories") or []) if c.get("name")],
                "updated": m.get("dateModified"),
            })
        return {"hits": hits, "total": (data.get("pagination") or {}).get("totalCount", len(hits)), "source": source}

    facets = [["project_type:mod"]]
    if loader in ("fabric", "forge", "neoforge", "quilt"):
        facets.append([f"categories:{loader}"])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    params = {
        "query": query,
        "limit": 20,
        "offset": offset,
        "index": {"relevance": "relevance", "downloads": "downloads", "updated": "updated", "newest": "newest"}.get(sort, "relevance"),
        "facets": json.dumps(facets),
    }
    url = f"{MODRINTH}/search?" + urllib.parse.urlencode(params)
    key = "mrsearch:" + url
    data = cget(key)
    if data is None:
        data = http_json(url) or {}
        cset(key, data, 240)
    hits = [{
        "source": "modrinth",
        "id": h.get("project_id"),
        "slug": h.get("slug"),
        "title": h.get("title"),
        "description": h.get("description"),
        "icon": h.get("icon_url") or "",
        "downloads": h.get("downloads") or 0,
        "follows": h.get("follows") or 0,
        "author": h.get("author"),
        "categories": h.get("categories") or [],
        "updated": h.get("date_modified"),
    } for h in data.get("hits") or []]
    return {"hits": hits, "total": data.get("total_hits") or len(hits), "source": source}


def _map_mr_version(v: dict) -> dict:
    files = v.get("files") or []
    primary = next((f for f in files if f.get("primary")), files[0] if files else {})
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "versionNumber": v.get("version_number"),
        "published": v.get("date_published"),
        "downloads": v.get("downloads") or 0,
        "gameVersions": v.get("game_versions") or [],
        "loaders": v.get("loaders") or [],
        "releaseType": v.get("version_type"),
        "fileName": primary.get("filename"),
        "fileSize": primary.get("size") or 0,
        "downloadUrl": primary.get("url"),
        "sha1": (primary.get("hashes") or {}).get("sha1"),
        "dependencies": [{
            "projectId": d.get("project_id"),
            "versionId": d.get("version_id"),
            "type": d.get("dependency_type"),
        } for d in v.get("dependencies") or []],
    }


def api_project(q):
    source = (q.get("source") or "modrinth").lower()
    project_id = q.get("id") or ""
    loader = q.get("loader") or ""
    mc_version = q.get("mc") or ""
    if source == "curseforge":
        mod = http_json(f"{CURSEFORGE}/mods/{project_id}", headers=cf_headers()) or {}
        mod = mod.get("data") or {}
        params = {"pageSize": 50}
        if mc_version:
            params["gameVersion"] = mc_version
        if loader in CF_LOADERS:
            params["modLoaderType"] = CF_LOADERS[loader]
        files = http_json(f"{CURSEFORGE}/mods/{project_id}/files?" + urllib.parse.urlencode(params),
                          headers=cf_headers()) or {}
        versions = []
        for f in files.get("data") or []:
            versions.append({
                "id": str(f.get("id")),
                "name": f.get("displayName"),
                "versionNumber": f.get("displayName"),
                "published": f.get("fileDate"),
                "downloads": f.get("downloadCount") or 0,
                "gameVersions": f.get("gameVersions") or [],
                "loaders": [],
                "releaseType": {1: "release", 2: "beta", 3: "alpha"}.get(f.get("releaseType"), "release"),
                "fileName": f.get("fileName"),
                "fileSize": f.get("fileLength") or 0,
                "downloadUrl": f.get("downloadUrl"),
                "sha1": None,
                "dependencies": [],
            })
        versions.sort(key=lambda v: str(v.get("published") or ""), reverse=True)
        try:
            desc = http_json(f"{CURSEFORGE}/mods/{project_id}/description", headers=cf_headers()) or {}
            body = desc.get("data") or ""
        except ApiError:
            body = ""
        links = mod.get("links") or {}
        return {
            "source": "curseforge",
            "id": project_id,
            "title": mod.get("name"),
            "description": mod.get("summary"),
            "body": body,
            "bodyIsHtml": True,
            "icon": ((mod.get("logo") or {}).get("thumbnailUrl") or ""),
            "downloads": mod.get("downloadCount") or 0,
            "follows": 0,
            "author": ", ".join(a.get("name", "") for a in (mod.get("authors") or [])),
            "website": links.get("websiteUrl") or f"https://www.curseforge.com/minecraft/mc-mods/{mod.get('slug') or project_id}",
            "categories": [c.get("name") for c in (mod.get("categories") or []) if c.get("name")],
            "gallery": [{"url": s.get("url"), "title": s.get("title") or ""} for s in (mod.get("screenshots") or []) if s.get("url")],
            "license": None,
            "published": mod.get("dateCreated"),
            "updated": mod.get("dateModified"),
            "clientSide": None,
            "serverSide": None,
            "links": {k: v for k, v in {
                "Source": links.get("sourceUrl"),
                "Issues": links.get("issuesUrl"),
                "Wiki": links.get("wikiUrl"),
            }.items() if v},
            "versions": versions,
        }

    project = http_json(f"{MODRINTH}/project/{project_id}") or {}
    versions = [_map_mr_version(v) for v in mr_versions(project_id, loader, mc_version)]
    links = {k: v for k, v in {
        "Source": project.get("source_url"),
        "Issues": project.get("issues_url"),
        "Wiki": project.get("wiki_url"),
        "Discord": project.get("discord_url"),
        "Donate": ((project.get("donation_urls") or [{}])[0] or {}).get("url"),
    }.items() if v}
    license_info = project.get("license") or {}
    return {
        "source": "modrinth",
        "id": project_id,
        "title": project.get("title"),
        "description": project.get("description"),
        "body": (project.get("body") or "")[:60000],
        "bodyIsHtml": False,
        "icon": project.get("icon_url") or "",
        "downloads": project.get("downloads") or 0,
        "follows": project.get("followers") or 0,
        "author": "",
        "website": f"https://modrinth.com/mod/{project.get('slug')}" if project.get("slug") else "",
        "categories": (project.get("categories") or []) + (project.get("additional_categories") or []),
        "gallery": [{"url": g.get("url"), "title": g.get("title") or "", "featured": g.get("featured")}
                    for g in (project.get("gallery") or []) if g.get("url")],
        "license": (license_info.get("name") or license_info.get("id")) if license_info else None,
        "published": project.get("published"),
        "updated": project.get("updated"),
        "clientSide": project.get("client_side"),
        "serverSide": project.get("server_side"),
        "links": links,
        "versions": versions,
    }


def trash_move(path: Path, inst: dict) -> Path | None:
    trash = DATA_DIR / "trash" / time.strftime("%Y%m%d-%H%M%S") / Path(inst["path"]).name
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / path.name
    if dest.exists():
        dest = trash / (path.stem + f"-{int(time.time())}" + path.suffix)
    shutil.move(str(path), str(dest))
    return dest


def _download(url: str, filename: str, mods_dir: Path, inst: dict, expect_sha1: str | None) -> dict:
    filename = Path(filename or "download.jar").name
    if not filename.lower().endswith(".jar"):
        filename += ".jar"
    mods_dir.mkdir(parents=True, exist_ok=True)
    tmp = mods_dir / (filename + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    try:
        actual = sha1_file(tmp)
        if expect_sha1 and actual.lower() != expect_sha1.lower():
            tmp.unlink(missing_ok=True)
            raise ApiError(502, "Downloaded file failed its SHA-1 check - try again or pick another version.")
        target = mods_dir / filename
        if target.exists():
            if sha1_file(target) == actual:
                tmp.unlink(missing_ok=True)
                return {"file": filename, "skipped": True}
            trash_move(target, inst)
        os.replace(tmp, target)
        return {"file": filename, "sha1": actual, "size": target.stat().st_size}
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _record_install(inst: dict, filename: str, sha1: str | None, source: str, project_id, version_id, version_number):
    key = f"{inst['id']}/{filename}"
    INSTALLED["files"][key] = {
        "sha1": sha1,
        "source": source,
        "projectId": str(project_id) if project_id else None,
        "versionId": str(version_id) if version_id else None,
        "versionNumber": version_number,
        "installedAt": time.time(),
    }
    save_installed()


def install_modrinth(inst: dict, project_id: str, version_id: str | None, install_deps: bool,
                     depth: int = 0, installed_projects: set | None = None) -> dict:
    if depth > 4:
        return {"installed": [], "errors": ["Dependency chain too deep"]}
    installed_projects = installed_projects if installed_projects is not None else set()
    loader = inst.get("loader")
    mc_version = inst.get("mcVersion")
    if version_id:
        version = http_json(f"{MODRINTH}/version/{version_id}")
    else:
        versions = mr_versions(project_id, loader, mc_version)
        if not versions:
            raise ApiError(404, "No version of this mod supports your loader and Minecraft version.")
        version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
    if not version:
        raise ApiError(404, "Version not found")

    files = version.get("files") or []
    primary = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not primary or not primary.get("url"):
        raise ApiError(404, "This version has no downloadable file.")

    result = _download(primary["url"], primary.get("filename"), Path(inst["modsPath"]), inst,
                       (primary.get("hashes") or {}).get("sha1"))
    _record_install(inst, result["file"], result.get("sha1"), "modrinth", project_id, version.get("id"), version.get("version_number"))
    out = {"installed": [{"file": result["file"], "version": version.get("version_number"), "skipped": result.get("skipped", False)}],
           "errors": [], "dependencies": []}
    installed_projects.add(str(project_id))

    if install_deps:
        for dep in version.get("dependencies") or []:
            if dep.get("dependency_type") != "required":
                continue
            dep_project = dep.get("project_id")
            if not dep_project or str(dep_project) in installed_projects:
                continue
            try:
                sub = install_modrinth(inst, str(dep_project), dep.get("version_id"), True, depth + 1, installed_projects)
                out["installed"] += sub["installed"]
                out["errors"] += sub["errors"]
                out["dependencies"].append(str(dep_project))
            except ApiError as e:
                out["errors"].append(f"Dependency {dep_project}: {e.message}")
            except Exception as e:
                out["errors"].append(f"Dependency {dep_project}: {e}")
    return out


def api_install(payload):
    inst = get_instance(payload.get("instance", ""))
    if not inst:
        raise ApiError(404, "Unknown instance")
    source = (payload.get("source") or "modrinth").lower()
    project_id = str(payload.get("projectId") or "")
    version_id = payload.get("versionId")
    install_deps = bool(payload.get("installDeps", True))

    if source == "curseforge":
        file_data = http_json(f"{CURSEFORGE}/mods/{project_id}/files/{version_id}", headers=cf_headers()) or {}
        file_data = file_data.get("data") or {}
        url = file_data.get("downloadUrl")
        if not url:
            mod = http_json(f"{CURSEFORGE}/mods/{project_id}", headers=cf_headers()) or {}
            website = ((mod.get("data") or {}).get("links") or {}).get("websiteUrl")
            return {"needsManual": True, "website": website or f"https://www.curseforge.com/minecraft/mc-mods/{project_id}",
                    "message": "The author disabled third-party downloads. Open the CurseForge page and download it manually."}
        result = _download(url, file_data.get("fileName"), Path(inst["modsPath"]), inst,
                           next((h.get("value") for h in (file_data.get("hashes") or []) if h.get("algo") == 1), None))
        _record_install(inst, result["file"], result.get("sha1"), "curseforge", project_id, version_id,
                        file_data.get("displayName"))
        return {"installed": [{"file": result["file"], "version": file_data.get("displayName")}], "errors": []}

    result = install_modrinth(inst, project_id, version_id, install_deps)
    if install_deps and result.get("installed") and inst.get("loader") == "fabric":
        # Fabric mods often depend on individual fabric-* modules whose real home is Fabric API.
        # Modrinth does not always list fabric-api as a project dependency, so add it ourselves.
        try:
            scan = scan_instance(inst)
            enabled = [m for m in scan["mods"] if m["enabled"]]
            has_api = any(m.get("id") == "fabric-api" for m in enabled)
            need_api = any(
                issue.get("kind") == "missing-dep" and str(issue.get("dep", "")).startswith("fabric-")
                for issue in scan["issues"]
            )
            if need_api and not has_api:
                extra = install_modrinth(inst, "fabric-api", None, False)
                result["installed"] += extra["installed"]
                result["dependencies"].append("fabric-api")
        except Exception as e:
            result.setdefault("errors", []).append(f"Fabric API auto-install: {e}")
    return result


def api_updates(q):
    inst = get_instance(q.get("instance", ""))
    if not inst:
        raise ApiError(404, "Unknown instance")
    mods_dir = Path(inst["modsPath"])
    jars = [p for p in mods_dir.glob("*.jar") if p.is_file()] if mods_dir.is_dir() else []
    if not jars:
        return {"rows": [], "checked": 0}

    rows = []
    hashes: dict[str, Path] = {}
    for jar in jars:
        try:
            digest = sha1_file(jar)
        except Exception:
            continue
        hashes[digest] = jar
    hash_list = list(hashes.keys())

    resolved: dict[str, dict] = {}
    for start in range(0, len(hash_list), 100):
        chunk = hash_list[start:start + 100]
        cache_key = "hashlookup:" + ",".join(sorted(chunk))[:2000]
        try:
            data = http_json(f"{MODRINTH}/version_files", "POST",
                             {"hashes": chunk, "algorithm": "sha1"}) or {}
        except ApiError as e:
            log(f"hash lookup failed: {e.message}")
            data = {}
        for h, v in data.items():
            resolved[h] = v

    project_ids = sorted({str(v.get("project_id")) for v in resolved.values() if v.get("project_id")})
    projects: dict[str, dict] = {}
    for start in range(0, len(project_ids), 50):
        chunk = project_ids[start:start + 50]
        try:
            data = http_json(f"{MODRINTH}/projects?ids=" + urllib.parse.quote(json.dumps(chunk))) or []
            for p in data:
                projects[str(p.get("id"))] = p
        except ApiError as e:
            log(f"project batch failed: {e.message}")

    for digest, jar in hashes.items():
        version = resolved.get(digest)
        record = (INSTALLED.get("files") or {}).get(f"{inst['id']}/{jar.name}") or {}
        project_id = str(version.get("project_id")) if version else record.get("projectId")
        entry = {
            "file": jar.name,
            "sha1": digest,
            "size": jar.stat().st_size,
            "mtime": jar.stat().st_mtime,
            "projectId": project_id,
            "source": "modrinth" if version else (record.get("source") or "unknown"),
            "currentVersion": version.get("version_number") if version else record.get("versionNumber"),
            "currentVersionId": version.get("id") if version else record.get("versionId"),
            "title": (projects.get(project_id) or {}).get("title"),
            "icon": (projects.get(project_id) or {}).get("icon_url"),
            "slug": (projects.get(project_id) or {}).get("slug"),
            "status": "unidentified",
            "latest": None,
        }
        rows.append(entry)

    # Flag duplicate copies of the same project so "Update all" cannot create more of them.
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("projectId"):
            groups.setdefault(str(row["projectId"]), []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (_vt(str(r.get("currentVersion") or "0")), r.get("mtime") or 0), reverse=True)
        keep = group[0]
        for row in group[1:]:
            row["status"] = "duplicate"
            row["duplicateOf"] = keep["file"]

    # CurseForge installed files can't be hashed on Modrinth; report them as external.
    for row in rows:
        if not row["projectId"] or row["status"] == "duplicate":
            continue
        try:
            versions = mr_versions(row["projectId"], inst.get("loader"), inst.get("mcVersion"))
        except ApiError as e:
            row["status"] = "error"
            row["error"] = e.message
            continue
        if not versions:
            row["status"] = "no-compatible"
            continue
        # Prefer the newest stable release; never offer a downgrade to an older release
        # just because the installed build happens to be a beta.
        releases = [v for v in versions if v.get("version_type") == "release"]
        candidate = releases[0] if releases else versions[0]
        current_published = str(version.get("date_published") or "") if version else ""
        row["latest"] = {
            "id": candidate.get("id"),
            "versionNumber": candidate.get("version_number"),
            "published": candidate.get("date_published"),
            "fileName": ((candidate.get("files") or [{}])[0] or {}).get("filename"),
        }
        same_id = str(candidate.get("id")) == str(row["currentVersionId"])
        same_number = row["currentVersion"] and str(candidate.get("version_number")) == str(row["currentVersion"])
        older = bool(current_published) and str(candidate.get("date_published") or "") <= current_published
        if same_id or same_number or older:
            row["status"] = "current"
        else:
            row["status"] = "update"

    order = {"update": 0, "duplicate": 1, "no-compatible": 2, "error": 3, "unidentified": 4, "current": 5}
    rows.sort(key=lambda r: (order.get(r["status"], 9), (r.get("title") or r["file"]).lower()))
    return {"rows": rows, "checked": len(rows)}


def api_tags(_q):
    cached = cget("tags")
    if cached:
        return cached
    versions = http_json(f"{MODRINTH}/tag/game_version") or []
    releases = [v["version"] for v in versions if v.get("version_type") == "release"]
    out = {"gameVersions": releases[:60], "allVersions": [v["version"] for v in versions][:120],
           "loaders": ["fabric", "neoforge", "forge", "quilt", "vanilla"]}
    cset("tags", out, 3600)
    return out


def api_settings(payload):
    if "curseforgeKey" in payload:
        SETTINGS["curseforgeKey"] = str(payload.get("curseforgeKey") or "").strip()
    if "lastInstance" in payload:
        SETTINGS["lastInstance"] = str(payload.get("lastInstance") or "")
    if "askDeleteOnDisable" in payload:
        SETTINGS["askDeleteOnDisable"] = bool(payload.get("askDeleteOnDisable"))
    save_settings()
    return {"ok": True, "settings": {
        "curseforgeKey": bool((SETTINGS.get("curseforgeKey") or "").strip()),
        "askDeleteOnDisable": bool(SETTINGS.get("askDeleteOnDisable", True)),
    }}


def api_instance_update(payload):
    iid = payload.get("id") or ""
    if not get_instance(iid):
        raise ApiError(404, "Unknown instance")
    overrides = SETTINGS.setdefault("overrides", {})
    entry = overrides.setdefault(iid, {})
    if "mcVersion" in payload:
        entry["mcVersion"] = str(payload.get("mcVersion") or "")
    if "loader" in payload:
        entry["loader"] = str(payload.get("loader") or "")
    save_settings()
    return {"ok": True, "instance": get_instance(iid)}


def api_instance_add(payload):
    raw = str(payload.get("path") or "").strip().strip('"')
    if not raw:
        raise ApiError(400, "No folder given")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.name.lower() == "mods":
        p = p.parent
    if not p.is_dir():
        raise ApiError(400, f"Folder not found: {p}")
    customs = SETTINGS.setdefault("customInstances", [])
    if any(str(Path(c.get("path", "")).resolve()).lower() == str(p.resolve()).lower() for c in customs):
        raise ApiError(400, "That folder is already in the list")
    customs.append({"path": str(p), "name": payload.get("name") or p.name})
    save_settings()
    inst = build_instance(payload.get("name") or p.name, p, "custom")
    return {"ok": True, "instance": inst, "instances": detect_instances()}


def api_instance_remove(payload):
    iid = payload.get("id") or ""
    customs = SETTINGS.get("customInstances") or []
    target = None
    for c in customs:
        if instance_id(str(Path(c.get("path", "")))) == iid:
            target = c
            break
    if not target:
        raise ApiError(400, "Only folders you added manually can be removed")
    customs.remove(target)
    (SETTINGS.get("overrides") or {}).pop(iid, None)
    save_settings()
    return {"ok": True, "instances": detect_instances()}


def api_browse(_payload):
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Select a Minecraft instance folder (or its mods folder)';"
        "$d.ShowNewFolderButton = $false;"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        path = (proc.stdout or "").strip()
        return {"path": path or None}
    except Exception as e:
        return {"path": None, "error": str(e)}


def api_open(payload):
    inst = get_instance(payload.get("instance", "")) if payload.get("instance") else None
    what = payload.get("what") or "mods"
    target = None
    if inst:
        if what == "mods":
            target = Path(inst["modsPath"])
            target.mkdir(parents=True, exist_ok=True)
        else:
            target = Path(inst["path"])
    elif payload.get("path"):
        target = Path(str(payload["path"]))
    if not target or not target.exists():
        raise ApiError(404, "Folder not found")
    try:
        if payload.get("url"):
            webbrowser.open(str(payload["url"]))
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
        return {"ok": True}
    except Exception as e:
        raise ApiError(500, str(e))


def api_project_link(q):
    source = (q.get("source") or "modrinth").lower()
    slug = q.get("slug") or q.get("id") or ""
    if source == "curseforge":
        return {"url": q.get("url") or f"https://www.curseforge.com/minecraft/mc-mods/{slug}"}
    return {"url": f"https://modrinth.com/mod/{slug}"}


# ---------------------------------------------------------------------- trash


def _trash_root() -> Path:
    return DATA_DIR / "trash"


def _safe_trash_path(rel: str) -> Path:
    root = _trash_root().resolve()
    target = (root / str(rel)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ApiError(400, "Invalid trash path")
    return target


def api_trash(_q):
    root = _trash_root()
    files = []
    if root.is_dir():
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            st = p.stat()
            parts = rel.split("/")
            files.append({
                "rel": rel,
                "file": p.name,
                "instance": parts[1] if len(parts) > 2 else "",
                "size": st.st_size,
                "sizeHuman": human_size(st.st_size),
                "mtime": st.st_mtime,
            })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    total = sum(f["size"] for f in files)
    return {"files": files, "totalSize": total, "totalHuman": human_size(total)}


def api_trash_empty(payload):
    rel = payload.get("file")
    if rel:
        target = _safe_trash_path(rel)
        if target.is_file():
            target.unlink()
    else:
        shutil.rmtree(_trash_root(), ignore_errors=True)
    return {"ok": True}


def api_trash_restore(payload):
    rel = payload.get("rel") or payload.get("file") or ""
    inst = get_instance(payload.get("instance") or "")
    if not inst:
        raise ApiError(404, "Unknown instance")
    src = _safe_trash_path(rel)
    if not src.is_file():
        raise ApiError(404, "That trashed file no longer exists")
    mods_dir = Path(inst["modsPath"])
    mods_dir.mkdir(parents=True, exist_ok=True)
    dest = mods_dir / src.name
    if dest.exists():
        raise ApiError(409, "A file with that name is already in mods")
    shutil.move(str(src), str(dest))
    return {"ok": True, "file": dest.name}


ROUTES_GET = {
    "state": api_state,
    "mods": api_mods,
    "icon": api_icon,
    "search": api_search,
    "project": api_project,
    "updates": api_updates,
    "tags": api_tags,
    "link": api_project_link,
    "trash": api_trash,
}
ROUTES_POST = {
    "mods/action": api_mods_action,
    "settings": api_settings,
    "install": api_install,
    "instance/update": api_instance_update,
    "instance/add": api_instance_add,
    "instance/remove": api_instance_remove,
    "browse": api_browse,
    "open": api_open,
    "trash/empty": api_trash_empty,
    "trash/restore": api_trash_restore,
}


# ---------------------------------------------------------------------- server

class Handler(SimpleHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers -------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Forbidden")
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            name = parsed.path[5:]
            fn = ROUTES_GET.get(name)
            if not fn:
                return self._error(404, "Unknown endpoint")
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                result = fn(q)
                if isinstance(result, dict) and "__raw__" in result:
                    return self._send(200, result["__raw__"], result.get("contentType", "application/octet-stream"))
                return self._json(result)
            except ApiError as e:
                return self._error(e.status or 500, e.message)
            except Exception as e:
                log("GET %s failed: %s\n%s" % (name, e, traceback.format_exc()))
                return self._error(500, str(e))
        return self._serve_static(parsed.path)

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "Forbidden")
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._error(404, "Not found")
        if self.headers.get("X-ModDeck-Token") != TOKEN:
            return self._error(403, "Bad token")
        name = parsed.path[5:]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._error(400, "Invalid JSON")
        if name == "shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        fn = ROUTES_POST.get(name)
        if not fn:
            return self._error(404, "Unknown endpoint")
        try:
            return self._json(fn(payload))
        except ApiError as e:
            return self._error(e.status or 500, e.message)
        except Exception as e:
            log("POST %s failed: %s\n%s" % (name, e, traceback.format_exc()))
            return self._error(500, str(e))

    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        if rel == "favicon.ico":
            rel = "favicon.svg"
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error(403, "Forbidden")
        if not target.is_file():
            return self._error(404, "Not found")
        if target.suffix.lower() in (".html", ".htm"):
            html = target.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__VERSION__", APP_VERSION)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        ctypes = {
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".json": "application/json",
        }
        ctype = ctypes.get(target.suffix.lower(), "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)


# ------------------------------------------------------------------------ main


def open_window(url: str):
    """Open the Edge app window. Returns the Edge process when possible."""
    edge_candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    profile = DATA_DIR / "edge-profile"
    for edge in edge_candidates:
        if edge.is_file():
            try:
                return subprocess.Popen([
                    str(edge), f"--app={url}", "--window-size=1500,980",
                    f"--user-data-dir={profile}", "--no-first-run",
                    "--no-default-browser-check", "--disable-features=msEdgeSidebarV2",
                ])
            except Exception:
                continue
    webbrowser.open(url)
    return None


def watch_window(server, url: str) -> None:
    """Shut the backend down when the Edge app window is closed.

    Python cannot see the window directly, but the Edge process we launched
    lives as long as that app window. Without this, closing the window leaves an
    invisible server holding a port until the machine reboots.
    """
    proc = open_window(url)
    if proc is None:
        return
    try:
        proc.wait()
    except Exception:
        return
    log("app window closed; stopping the local server")
    server.shutdown()


class ModDeckHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process silently bind an in-use port
    # (only one socket receives connections). Disable it so the port scan below
    # actually detects a running ModDeck and moves to the next free port.
    allow_reuse_address = os.name != "nt"


def build_server(port: int = 7319, host: str = "127.0.0.1"):
    """Create the HTTP server, trying nearby ports if the first is taken."""
    last_error: Exception | None = None
    for offset in range(20):
        try:
            return ModDeckHTTPServer((host, port + offset), Handler), port + offset
        except OSError as e:
            last_error = e
    raise RuntimeError(f"Could not bind a local port: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7319)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        instances = detect_instances()
        for inst in instances:
            scan = scan_instance(inst)
            print(f"\n=== {inst['name']} [{inst['loader']} {inst['mcVersion']}] {inst['modsPath']}")
            print(f"    score={scan['score']} mods={scan['counts']['total']} errors={scan['counts']['errors']}")
            for issue in scan["issues"]:
                print(f"    ! {issue['severity']}: {issue['title']}")
            for mod in scan["mods"]:
                print(f"      - {mod['name']} {mod.get('version') or ''} [{mod['status']}] {mod['file']}")
        return 0

    try:
        server, port = build_server(args.port)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/"
    try:
        PORT_PATH.write_text(str(port), encoding="utf-8")
    except Exception:
        pass
    log(f"starting {APP_NAME} {APP_VERSION} on {url}")
    print(f"{APP_NAME} {APP_VERSION} -> {url}")
    print("Close the window or use Settings > Quit to stop the server.")
    if not args.no_browser:
        threading.Timer(0.6, watch_window, args=(server, url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
