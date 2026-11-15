"""
bambu-live-mcp - MCP server (FastMCP) per configurare, slicare e mostrare in tempo reale
file .3mf in Bambu Studio su Windows.

- Non tocca rete / LAN mode / account: la connessione Cloud di Bambu Studio resta intatta.
- Lo slicing usa la CLI headless di bambu-studio.exe.
- I profili (stampante / processo / filamento) sono configurabili in config.json
  (default: Bambu Lab A1 0.4 nozzle + 0.20mm Standard @BBL A1 + SUNLU PLA Matte @BBL A1).

Avvio:  venv\\Scripts\\python.exe bambu_live_mcp.py            (server MCP su stdio)
        venv\\Scripts\\python.exe bambu_live_mcp.py --selftest  (diagnostica)
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import datetime as _dt
import functools
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATES_DIR = BASE_DIR / "templates"
WORK_DIR = BASE_DIR / "work"

DEFAULT_CONFIG: dict[str, Any] = {
    # "" = rilevamento automatico
    "bambu_studio_exe": "",
    # "" = tutte le cartelle numeriche in %APPDATA%\BambuStudio\user
    "bambu_user_id": "",
    "profiles": {
        "machine": "Bambu Lab A1 0.4 nozzle",
        "process": "0.20mm Standard @BBL A1",
        "filament": "SUNLU PLA Matte @BBL A1",
    },
    # Secondi di attesa per la chiusura pulita (WM_CLOSE) di Bambu Studio
    "gui_close_timeout_s": 12,
    # Se la finestra del file del tool non si chiude entro il timeout, termina SOLO quel processo
    # (mai istanze con altri progetti o con modifiche non confermate) e ne cancella i dati di ripristino
    "gui_force_kill_on_timeout": True,
    # Se Bambu Studio ha aperto un ALTRO progetto: "skip" (non toccare) | "open" (apri comunque)
    "gui_when_other_project_open": "open",
    # Cosa aprire nella GUI dopo lo slicing: "sliced" (.gcode.3mf, anteprima) | "project"
    "gui_open_target": "sliced",
    "slice_timeout_s": 900,
    "backups_to_keep": 10,
    # Analisi geometrica degli sbalzi (gradi dal piano orizzontale, sotto cui serve supporto)
    "overhang_critical_angle_deg": 30,
    "overhang_warning_angle_deg": 45,
}

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_LOCK = threading.Lock()


def log(msg: str) -> None:
    # MAI su stdout: lo stdout e' il canale del protocollo MCP (stdio)
    print(f"[bambu-live-mcp] {msg}", file=sys.stderr, flush=True)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as e:  # config rotto: usa i default
            log(f"config.json non valido ({e}), uso i default")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------------------
# Rilevamento Bambu Studio
# --------------------------------------------------------------------------------------
def bambu_data_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "BambuStudio"


def _registry_install_dirs() -> list[Path]:
    out: list[Path] = []
    try:
        import winreg
    except ImportError:
        return out
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key in roots:
        try:
            with winreg.OpenKey(hive, key) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                            if "bambu" not in name.lower():
                                continue
                            for val in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    v = str(winreg.QueryValueEx(sk, val)[0]).strip('"').split(",")[0]
                                    p = Path(v)
                                    out.append(p if p.is_dir() else p.parent)
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def find_bambu_exe(cfg: dict | None = None) -> Path | None:
    cfg = cfg or load_config()
    cands: list[Path] = []
    for v in (cfg.get("bambu_studio_exe"), os.environ.get("BAMBU_STUDIO_EXE")):
        if v:
            cands.append(Path(v))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(env):
            cands.append(Path(os.environ[env]) / "Bambu Studio" / "bambu-studio.exe")
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Bambu Studio" / "bambu-studio.exe")
    for d in _registry_install_dirs():
        cands.append(d / "bambu-studio.exe")
    w = shutil.which("bambu-studio")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c.is_file():
            return c
    return None


def require_exe() -> Path:
    exe = find_bambu_exe()
    if not exe:
        raise RuntimeError(
            "bambu-studio.exe non trovato. Imposta 'bambu_studio_exe' in config.json "
            "o la variabile d'ambiente BAMBU_STUDIO_EXE."
        )
    return exe


def studio_version(exe: Path | None = None) -> str:
    exe = exe or find_bambu_exe()
    if exe:
        try:
            info = bambu_data_dir() / "system" / "BBL.json"
            if info.exists():
                return "profiles " + json.loads(info.read_text(encoding="utf-8-sig")).get("version", "?")
        except Exception:
            pass
    return "?"


# --------------------------------------------------------------------------------------
# Indice dei profili (sistema + utente) con risoluzione dell'ereditarieta'
# --------------------------------------------------------------------------------------
_KINDS = ("machine", "process", "filament")


class ProfileIndex:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.entries: dict[str, dict] = {}
        self._raw_cache: dict[str, dict] = {}
        self._flat_cache: dict[str, dict] = {}
        self.roots: list[tuple[Path, str]] = []
        exe = find_bambu_exe(cfg)
        if exe:
            res = exe.parent / "resources" / "profiles"
            for vendor in sorted(res.glob("*")) if res.is_dir() else []:
                if vendor.is_dir():
                    self.roots.append((vendor, "system"))
        sysdir = bambu_data_dir() / "system"
        for vendor in sorted(sysdir.glob("*")) if sysdir.is_dir() else []:
            if vendor.is_dir():
                self.roots.append((vendor, "system"))
        for udir in self.user_dirs():
            self.roots.append((udir, "user"))
        for root, source in self.roots:  # ordine: le ultime sovrascrivono (utente vince)
            for p in root.rglob("*.json"):
                kind = next((k for k in _KINDS if k in [x.lower() for x in p.relative_to(root).parts[:-1]]), None)
                if not kind:
                    continue
                try:
                    d = json.loads(p.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                if not isinstance(d, dict) or "name" not in d:
                    continue
                self.entries[d["name"]] = {
                    "path": p, "kind": kind, "source": source,
                    "instantiation": str(d.get("instantiation", "true" if source == "user" else "")).lower(),
                    "inherits": d.get("inherits", ""),
                }

    def user_dirs(self) -> list[Path]:
        udir = bambu_data_dir() / "user"
        uid = str(self.cfg.get("bambu_user_id") or "")
        if uid:
            return [udir / uid] if (udir / uid).is_dir() else []
        dirs = [d for d in sorted(udir.glob("*")) if d.is_dir() and d.name.isdigit()]
        if (udir / "default").is_dir():
            dirs.insert(0, udir / "default")
        return dirs

    def raw(self, name: str) -> dict:
        if name not in self._raw_cache:
            e = self.entries.get(name)
            if not e:
                raise KeyError(f"Profilo non trovato: '{name}'")
            self._raw_cache[name] = json.loads(e["path"].read_text(encoding="utf-8-sig"))
        return copy.deepcopy(self._raw_cache[name])

    def flatten(self, name: str, _depth: int = 0) -> dict:
        if name in self._flat_cache:
            return copy.deepcopy(self._flat_cache[name])
        if _depth > 20:
            raise RuntimeError(f"Catena di ereditarieta' troppo profonda per '{name}'")
        d = self.raw(name)
        out: dict = {}
        if d.get("inherits"):
            out.update(self.flatten(d["inherits"], _depth + 1))
        for inc in d.get("include", []) or []:
            try:
                out.update(self.flatten(inc, _depth + 1))
            except KeyError:
                log(f"include non trovato: {inc}")
        out.update(d)
        out.pop("include", None)
        self._flat_cache[name] = out
        return copy.deepcopy(out)

    def nearest_system_ancestor(self, name: str) -> str:
        cur = self.raw(name).get("inherits", "")
        while cur:
            e = self.entries.get(cur)
            if not e:
                return cur
            if e["source"] == "system":
                return cur
            cur = self.raw(cur).get("inherits", "")
        return ""

    def kind_of(self, name: str) -> str | None:
        e = self.entries.get(name)
        return e["kind"] if e else None

    def list(self, kind: str = "", keyword: str = "", only_instantiable: bool = True) -> list[dict]:
        kw = keyword.lower()
        res = []
        for n, e in self.entries.items():
            if kind and e["kind"] != kind:
                continue
            if kw and kw not in n.lower():
                continue
            if only_instantiable and e["source"] == "system" and e["instantiation"] != "true":
                continue
            res.append({"name": n, "kind": e["kind"], "source": e["source"]})
        return sorted(res, key=lambda r: (r["kind"], r["source"] != "user", r["name"]))


_INDEX: ProfileIndex | None = None


def get_index(refresh: bool = False) -> ProfileIndex:
    global _INDEX
    if _INDEX is None or refresh:
        _INDEX = ProfileIndex(load_config())
    return _INDEX


_STRIP_KEYS = ("include", "instantiation")


def export_flat_preset(index: ProfileIndex, name: str, out_path: Path) -> Path:
    """Scrive un preset completamente risolto, accettato dalla CLI di Bambu Studio."""
    kind = index.kind_of(name)
    d = index.flatten(name)
    for k in _STRIP_KEYS:
        d.pop(k, None)
    d["type"] = kind
    d["name"] = name
    if index.entries[name]["source"] == "user":
        d["from"] = "User"
        d["inherits"] = index.nearest_system_ancestor(name)
    else:
        d["from"] = "system"
        d["inherits"] = ""
    if kind == "filament" and d.get("default_filament_colour") and not d.get("filament_colour"):
        d["filament_colour"] = d["default_filament_colour"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------------------
# Template di riferimento per i .3mf "grezzi" da CAD
# --------------------------------------------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60]


def _profile_names(machine: str = "", process: str = "", filament: str = "") -> dict:
    p = load_config()["profiles"]
    return {"machine": machine or p["machine"], "process": process or p["process"],
            "filament": filament or p["filament"]}


def template_dir_for(names: dict) -> Path:
    return TEMPLATES_DIR / _slug(f"{names['machine']}__{names['process']}__{names['filament']}")


def ensure_template(names: dict, force: bool = False) -> Path:
    """Crea/aggiorna templates/<set>/ con machine.json, process.json, filament.json risolti,
    profile_set.json e project_settings.reference.json (impostazioni progetto complete)."""
    index = get_index()
    for kind in _KINDS:
        n = names[kind]
        if n not in index.entries:
            index = get_index(refresh=True)
        if n not in index.entries:
            raise KeyError(f"Profilo {kind} '{n}' non trovato. Usa list_profiles per vedere i nomi disponibili.")
        if index.kind_of(n) != kind:
            raise ValueError(f"'{n}' e' un profilo {index.kind_of(n)}, non {kind}")
    tdir = template_dir_for(names)
    meta_p = tdir / "profile_set.json"
    sources = {k: str(index.entries[names[k]]["path"]) for k in _KINDS}
    if not force and meta_p.exists() and (tdir / "project_settings.reference.json").exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            newest = max(Path(s).stat().st_mtime for s in sources.values())
            if meta.get("names") == names and meta.get("generated_ts", 0) >= newest:
                return tdir
        except Exception:
            pass
    log(f"Genero template profili in {tdir}")
    tdir.mkdir(parents=True, exist_ok=True)
    for kind in _KINDS:
        export_flat_preset(index, names[kind], tdir / f"{kind}.json")
    # project_settings di riferimento: inizializza un cubo di prova tramite la CLI
    cube = tdir / "_cube.3mf"
    write_cube_3mf(cube, 20.0)
    work = _new_workdir("template")
    try:
        proj = cli_init_project(cube, tdir, work)
        ps = read_project_settings(proj)
        (tdir / "project_settings.reference.json").write_text(
            json.dumps(ps, indent=2, ensure_ascii=False), encoding="utf-8")
    finally:
        cube.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    meta_p.write_text(json.dumps({"names": names, "sources": sources, "generated_ts": time.time(),
                                  "generated": _dt.datetime.now().isoformat(timespec="seconds"),
                                  "profiles_version": studio_version()}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    return tdir


def write_cube_3mf(path: Path, size: float = 20.0) -> None:
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    t = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    verts = "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in t)
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}'
             '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)


# --------------------------------------------------------------------------------------
# Utility 3MF
# --------------------------------------------------------------------------------------
PS_ENTRY = "Metadata/project_settings.config"
MODEL_ENTRY = "3D/3dmodel.model"


def read_project_settings(path: Path) -> dict | None:
    with zipfile.ZipFile(path) as z:
        if PS_ENTRY not in z.namelist():
            return None
        return json.loads(z.read(PS_ENTRY).decode("utf-8-sig"))


def rewrite_zip(path: Path, replacements: dict[str, bytes | None]) -> None:
    """Riscrive l'archivio sostituendo/rimuovendo le voci indicate (None = rimuovi)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        done = set()
        for item in zin.infolist():
            if item.filename in replacements:
                done.add(item.filename)
                data = replacements[item.filename]
                if data is None:
                    continue
                zout.writestr(item.filename, data, compress_type=zipfile.ZIP_DEFLATED)
            else:
                zout.writestr(item, zin.read(item.filename))
        for name, data in replacements.items():
            if name not in done and data is not None:
                zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    os.replace(tmp, path)


def _fmt_num(v: float) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return str(int(v))
    return f"{v:.6g}"


def _to_str(v: Any, like: str | None = None) -> str:
    if isinstance(v, bool):
        s = "1" if v else "0"
    elif isinstance(v, (int, float)):
        s = _fmt_num(v)
    else:
        s = str(v)
    if like is not None and like.endswith("%") and not s.endswith("%") and re.fullmatch(r"-?\d+(\.\d+)?", s):
        s += "%"
    return s


def normalize_value(old: Any, new: Any) -> Any:
    """Adatta il valore al formato di project_settings.config (stringhe / liste di stringhe)."""
    if isinstance(old, list):
        like = old[0] if old and isinstance(old[0], str) else None
        if isinstance(new, list):
            return [_to_str(x, like) for x in new]
        n = max(len(old), 1)
        return [_to_str(new, like)] * n  # valore scalare -> applicato a tutti i filamenti/estrusori
    like = old if isinstance(old, str) else None
    if isinstance(new, list):
        return [_to_str(x, like) for x in new]
    return _to_str(new, like)


def apply_patch(ps: dict, patch: dict, names_for_diff: dict | None = None) -> dict:
    changed, unchanged, new_keys = {}, [], []
    patch = dict(patch)
    # Bambu Studio 2.x tiene sincronizzate queste densita' con sparse_infill_density
    if "sparse_infill_density" in patch:
        for linked in ("skeleton_infill_density", "skin_infill_density"):
            if linked in ps and linked not in patch:
                patch[linked] = patch["sparse_infill_density"]
    for k, v in patch.items():
        old = ps.get(k)
        if k not in ps:
            new_keys.append(k)
        nv = normalize_value(old, v)
        if old == nv:
            unchanged.append(k)
            continue
        ps[k] = nv
        changed[k] = {"old": old, "new": nv}
    _update_different_settings(ps, list(changed))
    return {"changed": changed, "unchanged": unchanged,
            "new_keys_not_in_project": new_keys}


def _update_different_settings(ps: dict, keys: list[str]) -> None:
    """Aggiorna different_settings_to_system cosi' la GUI mostra i preset come 'modificati'."""
    dst = ps.get("different_settings_to_system")
    if not isinstance(dst, list) or len(dst) < 2 or not keys:
        return
    index = get_index()
    flats = {}
    for kind, key in (("process", "print_settings_id"), ("machine", "printer_settings_id")):
        try:
            flats[kind] = index.flatten(str(ps.get(key)))
        except Exception:
            flats[kind] = {}
    fil_ids = ps.get("filament_settings_id") or []
    try:
        flats["filament"] = index.flatten(str(fil_ids[0])) if fil_ids else {}
    except Exception:
        flats["filament"] = {}
    sets = [set(filter(None, s.split(";"))) for s in dst]
    for k in keys:
        if k in flats["process"]:
            sets[0].add(k)
        elif k in flats["filament"]:
            for i in range(1, len(sets) - 1):
                sets[i].add(k)
        elif k in flats["machine"]:
            sets[-1].add(k)
    ps["different_settings_to_system"] = [";".join(sorted(s)) for s in sets]


# ---- geometria / trasformazioni ------------------------------------------------------
_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')


def _attrs(s: str) -> dict:
    return dict(_ATTR_RE.findall(s))


def _parse_transform(t: str | None) -> list[float]:
    if not t:
        return [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    vals = [float(x) for x in t.split()]
    return vals if len(vals) == 12 else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def _fmt_transform(m: list[float]) -> str:
    return " ".join(f"{x:.9g}" for x in m)


def _compose(a: list[float], b: list[float]) -> list[float]:
    """3MF usa vettori riga: p' = p * A * B. Restituisce A*B (4x3 affine)."""
    A = [a[0:3] + [0], a[3:6] + [0], a[6:9] + [0], a[9:12] + [1]]
    B = [b[0:3] + [0], b[3:6] + [0], b[6:9] + [0], b[9:12] + [1]]
    C = [[sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    return C[0][:3] + C[1][:3] + C[2][:3] + C[3][:3]


def rotate_build_items(model_xml: str, deg: float) -> tuple[str, int]:
    """Ruota ogni item del <build> attorno all'asse Z passante per la sua origine
    (Bambu Studio centra l'origine dell'oggetto sul suo bounding box)."""
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    count = 0

    def rot_item(m: re.Match) -> str:
        nonlocal count
        tag = m.group(0)
        tm = re.search(r'transform="([^"]*)"', tag)
        M = _parse_transform(tm.group(1) if tm else None)
        for r in range(3):
            x, y = M[r * 3], M[r * 3 + 1]
            M[r * 3], M[r * 3 + 1] = x * c - y * s, x * s + y * c
        count += 1
        new_t = f'transform="{_fmt_transform(M)}"'
        if tm:
            return tag[:tm.start()] + new_t + tag[tm.end():]
        return tag.replace("<item", f"<item {new_t}", 1)

    build = re.search(r"<build\b.*?</build>", model_xml, re.S)
    if not build:
        return model_xml, 0
    new_build = re.sub(r"<item\b[^>]*>", rot_item, build.group(0))
    return model_xml[:build.start()] + new_build + model_xml[build.end():], count


def center_build_items(model_xml: str, cx: float, cy: float) -> str:
    build = re.search(r"<build\b.*?</build>", model_xml, re.S)
    if not build:
        return model_xml
    items = re.findall(r"<item\b[^>]*>", build.group(0))
    mats = [_parse_transform((re.search(r'transform="([^"]*)"', t) or [None, None])[1]) for t in items]
    if not mats:
        return model_xml
    dx = cx - sum(m[9] for m in mats) / len(mats)
    dy = cy - sum(m[10] for m in mats) / len(mats)

    def shift(m: re.Match) -> str:
        tag = m.group(0)
        tm = re.search(r'transform="([^"]*)"', tag)
        M = _parse_transform(tm.group(1) if tm else None)
        M[9] += dx
        M[10] += dy
        nt = f'transform="{_fmt_transform(M)}"'
        return tag[:tm.start()] + nt + tag[tm.end():] if tm else tag.replace("<item", f"<item {nt}", 1)

    nb = re.sub(r"<item\b[^>]*>", shift, build.group(0))
    return model_xml[:build.start()] + nb + model_xml[build.end():]


def _bed_center(ps: dict) -> tuple[float, float] | None:
    try:
        pts = [tuple(float(x) for x in p.split("x")) for p in ps.get("printable_area", [])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    except Exception:
        return None


def load_world_triangles(threemf: Path, max_tris: int = 3_000_000) -> list[tuple]:
    """Restituisce i triangoli in coordinate del piatto (lista di 3 tuple xyz)."""
    with zipfile.ZipFile(threemf) as z:
        names = set(z.namelist())
        cache: dict[str, dict] = {}

        def objects_of(entry: str) -> dict:
            if entry not in cache:
                xml = z.read(entry).decode("utf-8", "replace")
                objs = {}
                for om in re.finditer(r"<object\b([^>]*)>(.*?)</object>", xml, re.S):
                    a = _attrs(om.group(1))
                    objs[a.get("id")] = om.group(2)
                cache[entry] = objs
            return cache[entry]

        tris: list[tuple] = []

        def emit(entry: str, oid: str, M: list[float], depth: int = 0) -> None:
            if depth > 10 or len(tris) > max_tris:
                return
            body = objects_of(entry).get(oid)
            if body is None:
                return
            if "<mesh" in body:
                vs = [(float(a.get("x", 0)), float(a.get("y", 0)), float(a.get("z", 0)))
                      for a in (_attrs(x) for x in re.findall(r"<vertex\b([^>]*)/?>", body))]
                W = [(x * M[0] + y * M[3] + zz * M[6] + M[9],
                      x * M[1] + y * M[4] + zz * M[7] + M[10],
                      x * M[2] + y * M[5] + zz * M[8] + M[11]) for x, y, zz in vs]
                for t in re.findall(r"<triangle\b([^>]*)/?>", body):
                    a = _attrs(t)
                    try:
                        tris.append((W[int(a["v1"])], W[int(a["v2"])], W[int(a["v3"])]))
                    except (KeyError, IndexError, ValueError):
                        pass
            for cm in re.finditer(r"<component\b([^>]*)/?>", body):
                a = _attrs(cm.group(1))
                path = a.get("p:path", "")
                sub = path.lstrip("/") if path else entry
                if sub not in names:
                    continue
                emit(sub, a.get("objectid"), _compose(_parse_transform(a.get("transform")), M), depth + 1)

        root = z.read(MODEL_ENTRY).decode("utf-8", "replace")
        build = re.search(r"<build\b.*?</build>", root, re.S)
        for it in re.findall(r"<item\b([^>]*)>", build.group(0) if build else ""):
            a = _attrs(it)
            if a.get("printable", "1") == "0":
                continue
            path = a.get("p:path", "")
            emit(path.lstrip("/") if path else MODEL_ENTRY, a.get("objectid"), _parse_transform(a.get("transform")))
    return tris


def analyze_overhangs(threemf: Path, critical_deg: float, warning_deg: float) -> dict:
    """Stima geometrica degli sbalzi: facce rivolte verso il basso con angolo dal piano
    orizzontale inferiore alla soglia, escluse quelle appoggiate al piatto."""
    try:
        tris = load_world_triangles(threemf)
    except Exception as e:
        return {"error": f"analisi geometrica non riuscita: {e}"}
    if not tris:
        return {"error": "nessuna mesh trovata"}
    zmin = min(min(p[2] for p in t) for t in tris)
    zmax = max(max(p[2] for p in t) for t in tris)
    cos_crit = math.cos(math.radians(critical_deg))
    cos_warn = math.cos(math.radians(warning_deg))
    crit_area = warn_area = 0.0
    crit_z: dict[float, float] = {}
    bbox = [math.inf, math.inf, -math.inf, -math.inf]
    for a, b, c in tris:
        if max(a[2], b[2], c[2]) <= zmin + 0.3:
            continue  # appoggiato al piatto
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln == 0 or nz >= 0:
            continue
        area = ln / 2
        down = -nz / ln  # 1 = soffitto piatto
        if down >= cos_crit:
            crit_area += area
            zl = round(min(a[2], b[2], c[2]) - zmin, 1)
            crit_z[zl] = crit_z.get(zl, 0) + area
            for p in (a, b, c):
                bbox = [min(bbox[0], p[0]), min(bbox[1], p[1]), max(bbox[2], p[0]), max(bbox[3], p[1])]
        elif down >= cos_warn:
            warn_area += area
    top_levels = sorted(crit_z.items(), key=lambda kv: -kv[1])[:8]
    res = {
        "triangles": len(tris),
        "height_mm": round(zmax - zmin, 2),
        "critical_overhang_area_mm2": round(crit_area, 1),
        "moderate_overhang_area_mm2": round(warn_area, 1),
        "critical_angle_deg": critical_deg,
        "warning_angle_deg": warning_deg,
        "critical_levels_z_mm": [{"z": z, "area_mm2": round(ar, 1)} for z, ar in sorted(top_levels)],
    }
    if crit_area > 0:
        res["critical_region_xy_bbox"] = [round(x, 1) for x in bbox]
    if crit_area > 25:
        res["verdict"] = ("ATTENZIONE: superfici a sbalzo critiche senza supporto (possono essere anche "
                          "ponti tra due appoggi, che di solito stampano bene). Valuta supporti, rotazione o modifica CAD.")
    elif crit_area > 0 or warn_area > 0:
        res["verdict"] = "Sbalzi lievi: probabilmente stampabile senza supporti."
    else:
        res["verdict"] = "Nessuno sbalzo critico rilevato."
    return res


# --------------------------------------------------------------------------------------
# CLI Bambu Studio
# --------------------------------------------------------------------------------------
def _new_workdir(tag: str) -> Path:
    d = WORK_DIR / f"{_dt.datetime.now():%Y%m%d_%H%M%S_%f}_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_cli(args: list[str], outdir: Path, timeout: int | None = None) -> dict:
    exe = require_exe()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").unlink(missing_ok=True)
    cmd = [str(exe)] + args
    log("CLI: " + subprocess.list2cmdline(cmd))
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=str(outdir),
                           timeout=timeout or load_config()["slice_timeout_s"], creationflags=CREATE_NO_WINDOW)
        rc, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, so, se = -999, str(e.stdout or ""), "timeout"
    try:  # la CLI lascia cartelle di ripristino in %TEMP%amboo_model: rimuovile
        clear_recovery_data(origin_under=[WORK_DIR, TEMPLATES_DIR])
    except Exception as e:
        log(f"pulizia dati di ripristino CLI fallita: {e}")
    result = {}
    rj = outdir / "result.json"
    if rj.exists():
        try:
            result = json.loads(rj.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"returncode": rc, "seconds": round(time.time() - t0, 1), "result": result,
            "stdout_tail": so[-4000:], "stderr_tail": se[-4000:], "command": cmd}


def _cli_error(r: dict, what: str) -> RuntimeError:
    res = r.get("result") or {}
    return RuntimeError(f"{what} fallito (exit {r['returncode']}): {res.get('error_string', '')} "
                        f"{r.get('stderr_tail', '')[-500:]}".strip())


def cli_init_project(src: Path, tdir: Path, work: Path) -> Path:
    """Converte un .3mf/.stl/.step grezzo in progetto Bambu Studio con i profili del template."""
    out = work / "init"
    r = run_cli(["--load-settings", f"{tdir / 'machine.json'};{tdir / 'process.json'}",
                 "--load-filaments", str(tdir / "filament.json"),
                 "--outputdir", str(out), "--export-3mf", "project.3mf", str(src)], out, timeout=600)
    proj = out / "project.3mf"
    if r["returncode"] != 0 or not proj.exists():
        raise _cli_error(r, "Inizializzazione progetto")
    return proj


_WARN_RE = re.compile(r"overhang|floating|cantilever|support|sbalz|unsupported|bridge|empty layer|"
                      r"outside|collision|too thin|warning", re.I)
_IGNORED_WARN = {"not_support_traditional_timelapse"}


def _parse_slice_info(sliced: Path) -> dict:
    info: dict[str, Any] = {"plates": []}
    with zipfile.ZipFile(sliced) as z:
        names = z.namelist()
        if "Metadata/slice_info.config" in names:
            xml = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
            for pm in re.finditer(r"<plate>(.*?)</plate>", xml, re.S):
                body = pm.group(1)
                meta = {a["key"]: a.get("value") for a in
                        (_attrs(x) for x in re.findall(r"<metadata\b([^>]*)/>", body)) if "key" in a}
                fils = [_attrs(x) for x in re.findall(r"<filament\b([^>]*)/>", body)]
                warns = [_attrs(x) for x in re.findall(r"<warning\b([^>]*)/>", body)]
                info["plates"].append({"meta": meta, "filaments": fils, "warnings": warns})
        gcodes = sorted(n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
        header: dict[str, str] = {}
        if gcodes:
            with z.open(gcodes[0]) as f:
                for i, raw in enumerate(f):
                    if i > 400:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if "HEADER_BLOCK_END" in line or "CONFIG_BLOCK_START" in line:
                        break
                    for part in line.lstrip(";").split(";"):  # "model printing time: x; total estimated time: y"
                        m = re.match(r"\s*([^:=]+?)\s*[:=]\s*(.+)$", part)
                        if m and len(m.group(2)) < 120:
                            header.setdefault(m.group(1).strip(), m.group(2).strip())
        info["gcode_header"] = header
    return info


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def cli_slice(project: Path, out_name: str, work: Path) -> dict:
    out = work / "slice"
    r = run_cli(["--slice", "0", "--outputdir", str(out), "--export-3mf", out_name, str(project)], out)
    sliced = out / out_name
    res = r.get("result") or {}
    report: dict[str, Any] = {"cli_returncode": r["returncode"], "cli_seconds": r["seconds"],
                              "cli_message": res.get("error_string")}
    if r["returncode"] != 0 or not sliced.exists():
        report["ok"] = False
        report["stderr_tail"] = r["stderr_tail"][-1500:]
        report["stdout_tail"] = r["stdout_tail"][-1500:]
        return report
    report["ok"] = True
    report["sliced_file_tmp"] = str(sliced)
    ps = read_project_settings(project) or {}
    plates = []
    info = _parse_slice_info(sliced)
    warnings: list[str] = []
    for i, pl in enumerate(res.get("sliced_plates", [])):
        p: dict[str, Any] = {"plate": pl.get("id", i + 1)}
        secs = pl.get("total_predication") or 0
        p["estimated_time_s"] = round(secs)
        p["estimated_time"] = _fmt_secs(secs)
        p["objects"] = [{"name": o.get("name"), "bbox": o.get("bbox")} for o in pl.get("objects", [])]
        if pl.get("warning_message"):
            warnings.append(f"plate {p['plate']}: {pl['warning_message']}")
        plates.append(p)
    for i, pl in enumerate(info["plates"]):
        p = plates[i] if i < len(plates) else {"plate": i + 1}
        if i >= len(plates):
            plates.append(p)
        meta = pl["meta"]
        if not p.get("estimated_time_s") and meta.get("prediction"):
            p["estimated_time_s"] = int(float(meta["prediction"]))
            p["estimated_time"] = _fmt_secs(float(meta["prediction"]))
        p["support_used"] = meta.get("support_used")
        p["outside_plate"] = meta.get("outside")
        fl = []
        for f in pl["filaments"]:
            used_m = float(f.get("used_m") or 0)
            used_g = float(f.get("used_g") or 0)
            if used_g == 0 and used_m > 0:  # stima da densita' e diametro
                try:
                    idx = int(f.get("id", 1)) - 1
                    dens = float(ps.get("filament_density", ["1.24"])[idx])
                    dia = float(ps.get("filament_diameter", ["1.75"])[idx])
                    used_g = used_m * 1000 * math.pi * (dia / 2) ** 2 * dens / 1000
                    f["used_g_estimated"] = True
                except Exception:
                    pass
            fl.append({"id": f.get("id"), "type": f.get("type"), "color": f.get("color"),
                       "used_m": round(used_m, 2), "used_g": round(used_g, 2),
                       **({"used_g_estimated": True} if f.get("used_g_estimated") else {})})
        p["filament"] = fl
        p["filament_total_g"] = round(sum(x["used_g"] for x in fl), 2)
        for w in pl["warnings"]:
            if w.get("msg") not in _IGNORED_WARN:
                warnings.append(f"{w.get('msg')} (level {w.get('level')}, code {w.get('error_code')})")
    report["plates"] = plates
    report["gcode_header"] = info.get("gcode_header", {})
    for line in (r["stdout_tail"] + "\n" + r["stderr_tail"]).splitlines():
        if _WARN_RE.search(line) and len(warnings) < 40:
            warnings.append(line.strip()[:300])
    report["slicer_warnings"] = warnings
    return report


# --------------------------------------------------------------------------------------
# Controllo GUI (Windows)
# --------------------------------------------------------------------------------------
user32 = ctypes.windll.user32 if os.name == "nt" else None
WM_CLOSE = 0x0010


def _bambu_pids() -> list[int]:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq bambu-studio.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("bambu-studio") and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _windows_of(pids: list[int]) -> list[tuple[int, str, int]]:
    if not user32 or not pids:
        return []
    res: list[tuple[int, str, int]] = []
    pidset = set(pids)
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pidset:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value:
                res.append((hwnd, buf.value, pid.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return res


def _stems(*paths: Path) -> list[str]:
    out = []
    for p in paths:
        n = p.name
        for suf in (".gcode.3mf", ".3mf", ".stl", ".step", ".stp", ".obj"):
            if n.lower().endswith(suf):
                n = n[: -len(suf)]
                break
        out.append(n.lower())
    return out


def _foreground(hwnd: int) -> None:
    if not user32:
        return
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_t = user32.GetWindowThreadProcessId(fg, None)
        tgt_t = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(cur, fg_t, True)
        user32.AttachThreadInput(cur, tgt_t, True)
        user32.keybd_event(0x12, 0, 0, 0)  # ALT: sblocca SetForegroundWindow
        user32.keybd_event(0x12, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(cur, fg_t, False)
        user32.AttachThreadInput(cur, tgt_t, False)
    except Exception as e:
        log(f"foreground fallito: {e}")


# ---- dati di ripristino di Bambu Studio ----------------------------------------------
# Ogni istanza (GUI o CLI) crea %TEMP%\bamboo_model\<giorno>\<ora>#<pid>#<n>\ con lock.txt (pid)
# e origin.txt (file aperto). Se il processo muore lasciando la cartella con un backup ".3mf",
# al riavvio Bambu Studio propone di ripristinare il progetto.
def _recovery_roots() -> list[Path]:
    tmp = Path(tempfile.gettempdir())
    return [tmp / "bamboo_model", tmp / "bambu_model"]


def recovery_entries() -> list[dict]:
    out = []
    for root in _recovery_roots():
        if not root.is_dir():
            continue
        for d in root.glob("*/*"):
            if not d.is_dir():
                continue
            pid = None
            m = re.search(r"#(\d+)#", d.name)
            try:
                pid = int((d / "lock.txt").read_text(encoding="utf-8", errors="replace").strip() or 0) or None
            except (OSError, ValueError):
                pid = int(m.group(1)) if m else None
            try:
                origin = (d / "origin.txt").read_text(encoding="utf-8", errors="replace").strip() or None
            except OSError:
                origin = None
            out.append({"dir": d, "pid": pid, "origin": origin, "has_backup": (d / ".3mf").exists(),
                        "mtime": d.stat().st_mtime})
    return out


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _path_under(p: str, roots: list[Path]) -> bool:
    np_ = _norm(p)
    return any(np_.startswith(_norm(r) + os.sep) for r in roots)


def origin_of_pid(pid: int) -> str | None:
    cand = [e for e in recovery_entries() if e["pid"] == pid and e["origin"]]
    return max(cand, key=lambda e: e["mtime"])["origin"] if cand else None


def clear_recovery_data(pids: list[int] | None = None, origin_under: list[Path] | None = None) -> list[str]:
    """Cancella le cartelle di ripristino di processi NON piu' in esecuzione che appartengono
    ai pid indicati oppure a file dentro origin_under (es. le cartelle di lavoro della CLI)."""
    alive = set(_bambu_pids())
    removed = []
    for e in recovery_entries():
        if e["pid"] in alive:
            continue
        hit = bool(pids and e["pid"] in pids)
        if not hit and origin_under and e["origin"]:
            hit = _path_under(e["origin"], origin_under)
        if hit:
            shutil.rmtree(e["dir"], ignore_errors=True)
            if not e["dir"].exists():
                removed.append(str(e["dir"]))
    return removed


# ---- finestre ------------------------------------------------------------------------
_MAIN_TITLE_RE = re.compile(r"^(?P<name>.*?)\s+-\s+BambuStudio\s*$", re.I)
_SAVE_DIALOG_RE = re.compile(r"-\s*(save|salva)\b|unsaved changes|modifiche non salvate", re.I)
_DISCARD_LABELS = {"no", "don't save", "dont save", "do not save", "discard", "non salvare", "scarta",
                   "no, non salvare", "don't save all", "discard all"}
_CANCEL_LABELS = {"cancel", "annulla"}


def _win_text(h: int) -> str:
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def _win_class(h: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def _children(h: int) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(c, _):
        if user32.IsWindowVisible(c):
            out.append((c, _win_class(c), _win_text(c)))
        return True

    user32.EnumChildWindows(h, EnumProc(cb), 0)
    return out


def _click(h: int) -> None:
    """Click sintetico inviato alla finestra (nessun movimento reale di mouse/tastiera)."""
    r = wintypes.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    lp = ((r.bottom // 2) << 16) | (r.right // 2)
    user32.PostMessageW(h, 0x0201, 1, lp)  # WM_LBUTTONDOWN
    time.sleep(0.05)
    user32.PostMessageW(h, 0x0202, 0, lp)  # WM_LBUTTONUP


def _dialog_button(dlg: int, labels: set[str]) -> tuple[int, str] | None:
    for c, _cls, txt in _children(dlg):
        norm = txt.strip().lower().replace("’", "'").replace("&", "")
        if norm in labels:
            return c, txt
    return None


def _project_name_from_title(name: str) -> tuple[str, bool]:
    unsaved = name.startswith("*") or name.endswith("*")
    return name.strip("* ").strip(), unsaved


def bambu_instances() -> list[dict]:
    """Istanze di Bambu Studio in esecuzione, con progetto aperto e stato 'modifiche non salvate'."""
    pids = _bambu_pids()
    wins = _windows_of(pids)
    out = []
    for pid in pids:
        mine = [w for w in wins if w[2] == pid]
        main = next((w for w in mine if _MAIN_TITLE_RE.match(w[1]) and _win_class(w[0]) == "wxWindowNR"), None)
        inst: dict[str, Any] = {"pid": pid, "hwnd": main[0] if main else None,
                                "title": main[1] if main else None,
                                "dialogs": [w[1] for w in mine if not main or w[0] != main[0]],
                                "origin": origin_of_pid(pid)}
        if main:
            inst["project_name"], inst["unsaved"] = _project_name_from_title(_MAIN_TITLE_RE.match(main[1])["name"])
        else:
            inst["project_name"], inst["unsaved"] = None, False
        out.append(inst)
    return out


def _relation(inst: dict, targets: list[Path]) -> str:
    """'target' = ha aperto esattamente un file che il tool sovrascrive;
    'ambiguous' = stesso nome ma percorso diverso/sconosciuto; 'other' = altro progetto."""
    if inst.get("origin"):
        # percorso noto: o e' esattamente il file del tool, o e' un altro progetto (anche se ha lo stesso nome)
        return "target" if any(_norm(inst["origin"]) == _norm(t) for t in targets) else "other"
    name = inst.get("project_name") or ""
    if name and _stems(Path(name + ".3mf"))[0] in _stems(*targets):  # "X.gcode" -> "x"
        return "ambiguous"
    return "other"


def _close_instance(inst: dict, discard_ok: bool, cfg: dict) -> dict:
    pid = inst["pid"]
    r: dict[str, Any] = {"save_dialog_seen": False, "discarded_unsaved_changes": False}
    if not inst.get("hwnd"):
        r["action"] = "no_main_window"
        return r
    user32.PostMessageW(inst["hwnd"], WM_CLOSE, 0, 0)
    deadline = time.time() + float(cfg["gui_close_timeout_s"])
    while time.time() < deadline:
        if pid not in _bambu_pids():
            break
        for h, title, _ in _windows_of([pid]):
            if not _SAVE_DIALOG_RE.search(title):
                continue  # es. "New version of Bambu Studio": non blocca la chiusura
            r["save_dialog_seen"] = True
            if discard_ok:
                btn = _dialog_button(h, _DISCARD_LABELS)
                if btn:
                    _click(btn[0])
                    r["discarded_unsaved_changes"] = True
                    r["clicked"] = f"{title}: {btn[1]}"
            else:
                btn = _dialog_button(h, _CANCEL_LABELS)
                if btn:
                    _click(btn[0])
                r["action"] = "left_open_save_dialog_cancelled"
                return r
        time.sleep(0.4)
    if pid not in _bambu_pids():
        r["action"] = "closed_discarding_unsaved_changes" if r["discarded_unsaved_changes"] else "closed"
    elif cfg["gui_force_kill_on_timeout"] and (discard_ok or not inst.get("unsaved")):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        for _ in range(20):
            if pid not in _bambu_pids():
                break
            time.sleep(0.25)
        r["action"] = "force_killed"
        r["still_open_windows"] = [w[1] for w in _windows_of([pid])]
    else:
        r["action"] = "left_open_timeout"
        r["still_open_windows"] = [w[1] for w in _windows_of([pid])]
    if pid not in _bambu_pids():
        # niente prompt "ripristinare il progetto?" al prossimo avvio
        r["recovery_data_removed"] = clear_recovery_data(pids=[pid])
    return r


def gui_close_matching(paths: list[Path], confirm_close_unsaved: bool = False) -> dict:
    """Chiude SOLO le istanze di Bambu Studio che hanno aperto i file che il tool sta per
    sovrascrivere. Le altre (anche con modifiche non salvate) non vengono toccate."""
    cfg = load_config()
    report: dict[str, Any] = {"instances": [], "warnings": [], "needs_confirmation": False,
                              "unsaved_changes_found": False}
    insts = bambu_instances()
    report["bambu_running"] = bool(insts)
    for inst in insts:
        rel = _relation(inst, paths)
        entry = {"pid": inst["pid"], "title": inst["title"], "file": inst["origin"] or inst["project_name"],
                 "relation": rel, "unsaved_changes": inst["unsaved"], "other_dialogs": inst["dialogs"]}
        report["instances"].append(entry)
        if inst["unsaved"]:
            report["unsaved_changes_found"] = True
        if rel == "other":
            entry["action"] = "left_open_other_project"
            if inst["unsaved"]:
                report["warnings"].append(
                    f"'{inst['title']}' ha modifiche non salvate e non e' il file del tool: lasciata aperta.")
            continue
        if rel == "ambiguous" and inst["unsaved"] and not confirm_close_unsaved:
            entry["action"] = "left_open_needs_confirmation"
            report["needs_confirmation"] = True
            report["warnings"].append(
                f"'{inst['title']}' ha modifiche non salvate e non posso verificare che sia proprio "
                f"{paths[0].name}: NON chiusa. Salva/chiudi a mano oppure rilancia con "
                f"confirm_close_unsaved=true per chiuderla scartando le modifiche.")
            continue
        entry.update(_close_instance(inst, discard_ok=(rel == "target" or confirm_close_unsaved), cfg=cfg))
        if entry["action"] == "force_killed":
            lost = " Le modifiche non salvate sono andate perse." if inst["unsaved"] else ""
            report["warnings"].append(
                f"'{inst['title']}' non si chiudeva: processo {inst['pid']} terminato, dati di ripristino "
                f"rimossi.{lost}")
        elif entry.get("discarded_unsaved_changes"):
            report["warnings"].append(
                f"'{inst['title']}' aveva modifiche non salvate: scartate (il file viene sovrascritto dal tool, "
                f"backup in .bambu_mcp_backup).")
        elif entry["action"].startswith("left_open"):
            report["warnings"].append(f"'{inst['title']}' non si e' chiusa ({entry['action']}).")
    report["target_open"] = any(e["relation"] != "other" for e in report["instances"])
    report["other_project_open"] = any(e["relation"] == "other" for e in report["instances"])
    report["target_still_open"] = any(e["relation"] != "other" and e.get("action", "").startswith("left_open")
                                      for e in report["instances"])
    return report


def gui_open(path: Path, wait_s: float = 45) -> dict:
    exe = require_exe()
    try:
        user32 and user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        pass
    kw = dict(cwd=str(exe.parent), close_fds=True, stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:  # esci dal job object del client MCP, cosi' la GUI sopravvive al riavvio del server
        proc = subprocess.Popen([str(exe), str(path)], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        proc = subprocess.Popen([str(exe), str(path)], creationflags=flags, **kw)
    stem = _stems(path)[0]
    deadline = time.time() + wait_s
    hit = None
    while time.time() < deadline:
        wins = _windows_of([proc.pid])
        hit = next((w for w in wins if _MAIN_TITLE_RE.match(w[1]) and stem in w[1].lower()), None)
        if hit:
            break
        time.sleep(0.7)
    if hit:
        time.sleep(0.8)
        _foreground(hit[0])
    return {"opened": str(path), "pid": proc.pid, "window_title": hit[1] if hit else None,
            "window_found": bool(hit)}


def gui_reload(project: Path, sliced: Path | None, pre_close_report: dict | None) -> dict:
    cfg = load_config()
    target = sliced if (sliced and sliced.exists() and cfg["gui_open_target"] == "sliced") else project
    rep: dict[str, Any] = {"close": pre_close_report}
    pc = pre_close_report or {}
    if pc.get("target_still_open"):
        rep["open"] = {"skipped": "Una finestra con questo file e' ancora aperta (vedi warnings): non apro un duplicato."}
    elif pc.get("other_project_open") and not pc.get("target_open") and \
            cfg["gui_when_other_project_open"] == "skip":
        rep["open"] = {"skipped": "Bambu Studio ha un altro progetto aperto (gui_when_other_project_open=skip)"}
    else:
        rep["open"] = gui_open(target)
    rep["summary"] = _gui_summary(rep)
    return rep


def _gui_summary(rep: dict) -> list[str]:
    out = []
    pc = rep.get("close") or {}
    names = {"closed": "chiusa normalmente", "closed_discarding_unsaved_changes": "chiusa scartando le modifiche non salvate",
             "force_killed": "terminata forzatamente (dati di ripristino rimossi)",
             "left_open_other_project": "lasciata aperta (altro progetto)",
             "left_open_needs_confirmation": "lasciata aperta: serve conferma",
             "left_open_timeout": "lasciata aperta: non si e' chiusa in tempo",
             "left_open_save_dialog_cancelled": "lasciata aperta: dialogo Save annullato"}
    for e in pc.get("instances", []):
        st = "con modifiche non salvate" if e.get("unsaved_changes") else "senza modifiche non salvate"
        out.append(f"{e.get('title') or 'pid ' + str(e['pid'])} ({st}): {names.get(e.get('action'), e.get('action'))}")
    if not pc.get("instances"):
        out.append("Nessuna istanza di Bambu Studio aperta.")
    op = rep.get("open") or {}
    out.append(f"Riaperto: {op['window_title'] or op['opened']}" if op.get("opened") else f"Non riaperto: {op.get('skipped')}")
    return out


# --------------------------------------------------------------------------------------
# Logica principale
# --------------------------------------------------------------------------------------
RAW_EXTS = (".stl", ".obj", ".step", ".stp", ".amf")


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    cfg = load_config()
    bdir = path.parent / ".bambu_mcp_backup"
    bdir.mkdir(exist_ok=True)
    dst = bdir / f"{path.stem}_{_dt.datetime.now():%Y%m%d_%H%M%S}{path.suffix}"
    shutil.copy2(path, dst)
    olds = sorted(bdir.glob(f"{path.stem}_*{path.suffix}"))
    for o in olds[: max(0, len(olds) - int(cfg["backups_to_keep"]))]:
        o.unlink(missing_ok=True)
    return str(dst)


def _sliced_path_for(project: Path) -> Path:
    stem = project.name[: -len(".3mf")] if project.name.lower().endswith(".3mf") else project.stem
    if stem.lower().endswith(".gcode"):
        stem = stem[: -len(".gcode")]
    return project.parent / f"{stem}.gcode.3mf"


def _replace_with_retry(src: Path, dst: Path, tries: int = 10) -> None:
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def process_3mf(threemf_path: str, settings_patch: dict | str | None, rotate_z_deg: float = 0.0,
                run_slice: bool = True, reload_gui: bool = True, machine: str = "", process: str = "",
                filament: str = "", confirm_close_unsaved: bool = False) -> dict:
    cfg = load_config()
    src = Path(os.path.expandvars(threemf_path.strip().strip('"'))).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"File non trovato: {src}")
    if isinstance(settings_patch, str):
        settings_patch = json.loads(settings_patch) if settings_patch.strip() else {}
    patch = dict(settings_patch or {})
    report: dict[str, Any] = {"input": str(src)}
    work = _new_workdir(_slug(src.stem)[:30])
    ok = False
    try:
        is_raw_ext = src.suffix.lower() in RAW_EXTS
        project_dst = src.with_suffix(".3mf") if is_raw_ext else src
        ps = None if is_raw_ext else read_project_settings(src)
        proj = work / "project.3mf"
        if ps is None:
            names = _profile_names(machine, process, filament)
            tdir = ensure_template(names)
            init = cli_init_project(src, tdir, work)
            shutil.copy2(init, proj)
            ps = read_project_settings(proj) or {}
            # colore filamento dal preset + centratura sul piatto
            flat_f = json.loads((tdir / "filament.json").read_text(encoding="utf-8"))
            if flat_f.get("default_filament_colour"):
                ps["filament_colour"] = flat_f["default_filament_colour"]
            with zipfile.ZipFile(proj) as z:
                model = z.read(MODEL_ENTRY).decode("utf-8")
            c = _bed_center(ps)
            if c:
                model = center_build_items(model, *c)
            rewrite_zip(proj, {PS_ENTRY: json.dumps(ps, indent=4, ensure_ascii=False).encode(),
                               MODEL_ENTRY: model.encode()})
            report["initialized_from_cad"] = {"profiles": names, "template_dir": str(tdir),
                                              "bed_center": c}
        else:
            shutil.copy2(src, proj)
        ps = read_project_settings(proj) or {}
        report["profiles_in_project"] = {"printer": ps.get("printer_settings_id"),
                                         "process": ps.get("print_settings_id"),
                                         "filament": ps.get("filament_settings_id")}
        repl: dict[str, bytes | None] = {}
        if patch:
            report["patch"] = apply_patch(ps, patch)
            repl[PS_ENTRY] = json.dumps(ps, indent=4, ensure_ascii=False).encode()
        if rotate_z_deg:
            with zipfile.ZipFile(proj) as z:
                model = z.read(MODEL_ENTRY).decode("utf-8")
            model, n = rotate_build_items(model, float(rotate_z_deg))
            repl[MODEL_ENTRY] = model.encode()
            report["rotation"] = {"deg_z": rotate_z_deg, "items_rotated": n}
        if repl:
            with zipfile.ZipFile(proj) as z:
                stale = [nm for nm in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode(\.md5)?", nm)]
            for nm in stale:  # G-code nel progetto non piu' valido
                repl[nm] = None
            rewrite_zip(proj, repl)

        sliced_dst = _sliced_path_for(project_dst)
        sliced_tmp = None
        if run_slice or rotate_z_deg:
            sr = cli_slice(proj, sliced_dst.name, work)
            report["slice"] = sr
            if sr.get("ok"):
                sliced_tmp = Path(sr.pop("sliced_file_tmp"))
        report["overhang_analysis"] = analyze_overhangs(
            proj, float(cfg["overhang_critical_angle_deg"]), float(cfg["overhang_warning_angle_deg"]))

        close_rep = gui_close_matching([project_dst, sliced_dst], confirm_close_unsaved) if reload_gui else None

        if repl or "initialized_from_cad" in report:
            if project_dst.exists():
                report["backup"] = _backup(project_dst)
            _replace_with_retry(proj, project_dst)
        report["project_file"] = str(project_dst)
        if sliced_tmp:
            _replace_with_retry(sliced_tmp, sliced_dst)
            report["sliced_file"] = str(sliced_dst)
        if reload_gui:
            report["gui"] = gui_reload(project_dst, Path(report["sliced_file"]) if sliced_tmp else None,
                                       close_rep)
            report["gui_summary"] = report["gui"].get("summary")
            report["unsaved_changes_found"] = bool(close_rep and close_rep.get("unsaved_changes_found"))
            if close_rep and close_rep.get("warnings"):
                report["warnings"] = close_rep["warnings"]
            if close_rep and close_rep.get("needs_confirmation"):
                report["needs_confirmation"] = True
        ok = bool(not report.get("slice") or report["slice"].get("ok"))
        report["ok"] = ok
        return report
    finally:
        if ok:
            shutil.rmtree(work, ignore_errors=True)
        else:
            report["work_dir_kept_for_debug"] = str(work)
        # pulizia vecchie cartelle di lavoro
        try:
            olds = sorted(WORK_DIR.glob("*"))
            for o in olds[:-10]:
                shutil.rmtree(o, ignore_errors=True)
        except Exception:
            pass


def default_cad_patch(seam_position: str = "back", infill_percent: int = 18, bed_temp: int = 60,
                      nozzle_temp: int = 215) -> tuple[dict, list[str]]:
    notes = []
    if not 15 <= infill_percent <= 20:
        notes.append(f"infill_percent={infill_percent} fuori dal range consigliato 15-20%")
    if not 55 <= bed_temp <= 60:
        notes.append(f"bed_temp={bed_temp} fuori dal range consigliato 55-60 C")
    if not 205 <= nozzle_temp <= 230:
        notes.append(f"nozzle_temp={nozzle_temp} insolita per Sunlu PLA Matte (205-230 C)")
    if seam_position not in ("aligned", "back", "nearest", "random"):
        notes.append(f"seam_position '{seam_position}' non standard (aligned/back/nearest/random)")
    patch = {
        "layer_height": "0.2",
        "initial_layer_print_height": "0.2",
        "curr_bed_type": "Textured PEI Plate",
        "textured_plate_temp": str(bed_temp),
        "textured_plate_temp_initial_layer": str(bed_temp),
        "nozzle_temperature": str(nozzle_temp),
        "nozzle_temperature_initial_layer": str(nozzle_temp),
        "wall_generator": "arachne",
        "wall_loops": "3",
        "top_shell_layers": "5",
        "bottom_shell_layers": "4",
        "sparse_infill_pattern": "gyroid",
        "sparse_infill_density": f"{infill_percent}%",
        "enable_support": "0",
        "seam_position": seam_position,
        "brim_type": "no_brim",
        "elefant_foot_compensation": "0.15",
    }
    return patch, notes


# --------------------------------------------------------------------------------------
# Server MCP
# --------------------------------------------------------------------------------------
mcp = FastMCP(
    "bambu-live-mcp",
    instructions=(
        "Strumenti per configurare, slicare e mostrare dal vivo file .3mf in Bambu Studio (Windows). "
        "Usa inspect_3mf_settings per scoprire le chiavi (project_settings.config), poi "
        "apply_settings_and_preview con un dizionario settings_patch {chiave: valore}. I valori per "
        "filamento possono essere scalari (applicati a tutti i filamenti) o liste. "
        "apply_default_cad_preset applica il preset standard (0.2mm, Textured PEI, Arachne 3 pareti, "
        "gyroid, niente supporti/brim). I .3mf grezzi dal CAD vengono inizializzati coi profili di "
        "config.json (list_profiles / set_default_profiles per cambiarli, es. altra stampante)."
    ),
)


def _truncate(v: Any, n: int = 160) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... (+{len(v) - n} caratteri)"
    if isinstance(v, list):
        return [_truncate(x, n) for x in v]
    return v


@mcp.tool()
async def inspect_3mf_settings(threemf_path: str, filter_keyword: str = "") -> dict:
    """Legge Metadata/project_settings.config dal .3mf.
    filter_keyword: una o piu' parole separate da virgola (es. 'wall', 'seam,temp', 'speed',
    'support', 'infill', 'ironing', 'plate'); cerca nei nomi delle chiavi (e, se nessuna chiave
    corrisponde, nei valori). Vuoto = elenco completo di tutte le chiavi modificabili con valori
    abbreviati. Se il .3mf e' grezzo dal CAD mostra le chiavi del template di default."""
    def run() -> dict:
        p = Path(threemf_path.strip().strip('"')).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"File non trovato: {p}")
        ps = read_project_settings(p) if p.suffix.lower() == ".3mf" else None
        out: dict[str, Any] = {"file": str(p), "is_bambu_project": ps is not None}
        if ps is None:
            tdir = ensure_template(_profile_names())
            ps = json.loads((tdir / "project_settings.reference.json").read_text(encoding="utf-8"))
            out["note"] = ("File senza project_settings.config (export CAD): mostro i valori del template "
                           "di default che verra' applicato automaticamente.")
            out["template"] = str(tdir)
        out["profiles"] = {"printer": ps.get("printer_settings_id"), "process": ps.get("print_settings_id"),
                           "filament": ps.get("filament_settings_id"), "bed": ps.get("curr_bed_type")}
        out["total_keys"] = len(ps)
        kws = [k.strip().lower() for k in filter_keyword.split(",") if k.strip()]
        if kws:
            sel = {k: v for k, v in ps.items() if any(w in k.lower() for w in kws)}
            if not sel:
                sel = {k: v for k, v in ps.items() if any(w in json.dumps(v).lower() for w in kws)}
                out["matched_on"] = "values"
            out["settings"] = {k: _truncate(sel[k], 400) for k in sorted(sel)}
            out["matched"] = len(sel)
        else:
            out["settings"] = {k: _truncate(ps[k], 80) for k in sorted(ps)}
        if p.suffix.lower() == ".3mf":
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                out["contains_gcode"] = any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in names)
                root = z.read(MODEL_ENTRY).decode("utf-8", "replace") if MODEL_ENTRY in names else ""
                out["build_items"] = [_attrs(t).get("transform", "identity")
                                      for t in re.findall(r"<item\b([^>]*)>", root)]
        return out
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def apply_settings_and_preview(threemf_path: str, settings_patch: dict, rotate_z_deg: float = 0.0,
                                     run_slice: bool = True, reload_gui: bool = True,
                                     confirm_close_unsaved: bool = False) -> dict:
    """Applica settings_patch (qualsiasi chiave di project_settings.config, es.
    {"wall_loops": 4, "sparse_infill_density": "20%", "nozzle_temperature": 215}) al .3mf.
    - .3mf grezzo dal CAD (o .stl/.step): viene prima inizializzato con i profili base
      (default Bambu Lab A1 0.4 + Sunlu PLA Matte) e centrato sul piatto.
    - rotate_z_deg: ruota gli oggetti attorno all'asse Z (gradi, antiorario).
    - run_slice (o rotazione): slicing headless -> <nome>.gcode.3mf con tempo stimato,
      consumo filamento, avvisi dello slicer e analisi geometrica degli sbalzi.
    - reload_gui: chiude SOLO la finestra di Bambu Studio che ha aperto questo file (chiusura
      normale; se compare "Save" sceglie "No", perche' il file viene comunque sovrascritto) e lo
      riapre in primo piano con l'anteprima aggiornata. Le altre finestre non vengono toccate.
      Se una finestra con lo stesso nome ha modifiche non salvate ma il percorso non e' verificabile,
      non viene chiusa: la risposta contiene needs_confirmation=true e warnings. Chiedi all'utente e,
      solo se conferma, rilancia con confirm_close_unsaved=true.
    Controlla sempre 'gui_summary' e 'warnings' nella risposta e riferiscili all'utente.
    Un backup del progetto viene salvato in .bambu_mcp_backup."""
    def run() -> dict:
        with _LOCK:
            return process_3mf(threemf_path, settings_patch, rotate_z_deg, run_slice, reload_gui,
                               confirm_close_unsaved=confirm_close_unsaved)
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def apply_default_cad_preset(threemf_path: str, seam_position: str = "back", infill_percent: int = 18,
                                   bed_temp: int = 60, nozzle_temp: int = 215,
                                   rotate_z_deg: float = 0.0, confirm_close_unsaved: bool = False) -> dict:
    """Preset rapido per pezzi da CAD: layer 0.2 mm (anche primo layer), Textured PEI Plate a
    bed_temp (55-60 C), ugello nozzle_temp (Sunlu PLA Matte), Arachne con 3 pareti, 5 strati
    superiori / 4 inferiori, gyroid al infill_percent (15-20%), supporti OFF (con verifica sbalzi),
    cucitura seam_position ('back' o 'aligned'), brim OFF, compensazione piede d'elefante 0.15 mm.
    rotate_z_deg ruota il pezzo sull'asse Z (es. per allontanare la cucitura dalle linguette).
    Poi slicing e ricarica della GUI (stesse regole di chiusura sicura di apply_settings_and_preview;
    confirm_close_unsaved=true solo dopo conferma esplicita dell'utente)."""
    def run() -> dict:
        patch, notes = default_cad_patch(seam_position, infill_percent, bed_temp, nozzle_temp)
        with _LOCK:
            rep = process_3mf(threemf_path, patch, rotate_z_deg, True, True,
                              confirm_close_unsaved=confirm_close_unsaved)
        if notes:
            rep["preset_notes"] = notes
        return rep
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def list_profiles(kind: str = "", keyword: str = "", refresh: bool = False) -> dict:
    """Elenca i profili Bambu Studio disponibili (sistema + utente).
    kind: 'machine' | 'process' | 'filament' | '' (tutti). keyword: filtro sul nome (es. 'A1', 'SUNLU', 'P1S')."""
    def run() -> dict:
        idx = get_index(refresh=refresh)
        items = idx.list(kind, keyword)
        cfg = load_config()
        return {"current_defaults": cfg["profiles"], "count": len(items), "profiles": items[:300],
                "truncated": len(items) > 300, "user_dirs": [str(d) for d in idx.user_dirs()]}
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def set_default_profiles(machine: str = "", process: str = "", filament: str = "") -> dict:
    """Cambia i profili base usati per inizializzare i .3mf grezzi (es. un'altra stampante:
    machine='Bambu Lab P1S 0.4 nozzle', process='0.20mm Standard @BBL X1C', filament='Bambu PLA Basic @BBL X1C').
    Campi vuoti = invariati. Rigenera e verifica il template tramite la CLI."""
    def run() -> dict:
        with _LOCK:
            names = _profile_names(machine, process, filament)
            tdir = ensure_template(names, force=True)
            cfg = load_config()
            cfg["profiles"] = names
            save_config(cfg)
            return {"ok": True, "profiles": names, "template_dir": str(tdir)}
    return await anyio.to_thread.run_sync(run)


# --------------------------------------------------------------------------------------
# Self-test / CLI
# --------------------------------------------------------------------------------------
def selftest(do_slice: bool) -> int:
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    exe = find_bambu_exe()
    print(f"bambu-studio.exe: {exe or 'NON TROVATO'}")
    print(f"Dati Bambu Studio: {bambu_data_dir()}  ({studio_version()})")
    idx = get_index()
    print(f"Cartelle utente: {[str(d) for d in idx.user_dirs()]}")
    print(f"Profili indicizzati: {len(idx.entries)}")
    names = _profile_names()
    for k in _KINDS:
        e = idx.entries.get(names[k])
        print(f"  {k:8s}: {names[k]}  -> {e['source'] + ': ' + str(e['path']) if e else 'NON TROVATO'}")
    if not exe:
        return 1
    tdir = ensure_template(names, force=True)
    ref = json.loads((tdir / "project_settings.reference.json").read_text(encoding="utf-8"))
    print(f"Template: {tdir}  ({len(ref)} chiavi, nozzle {ref.get('nozzle_temperature')}, "
          f"filament {ref.get('filament_settings_id')})")
    if do_slice:
        test_dir = BASE_DIR / "test"
        test_dir.mkdir(exist_ok=True)
        cube = test_dir / "selftest_cube.3mf"
        write_cube_3mf(cube, 20)
        patch, _ = default_cad_patch()
        rep = process_3mf(str(cube), patch, rotate_z_deg=15, run_slice=True, reload_gui=False)
        sl = rep.get("slice", {})
        print(f"Slice ok: {sl.get('ok')}  piatti: {json.dumps(sl.get('plates'), ensure_ascii=False)}")
        print(f"Avvisi: {sl.get('slicer_warnings')}")
        print(f"Sbalzi: {rep['overhang_analysis'].get('verdict')}")
        print(f"Output: {rep.get('sliced_file')}")
        if not sl.get("ok"):
            return 2
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="bambu-live-mcp")
    ap.add_argument("--selftest", action="store_true", help="diagnostica (profili, template)")
    ap.add_argument("--slice", action="store_true", help="con --selftest: slicing di un cubo di prova")
    ap.add_argument("--build-template", action="store_true", help="rigenera il template dei profili")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest(a.slice))
    if a.build_template:
        print(ensure_template(_profile_names(), force=True))
        sys.exit(0)
    mcp.run()  # stdio
