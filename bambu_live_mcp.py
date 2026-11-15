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
import difflib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bambu_project as bp  # noqa: E402

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
    # I ponti (soffitti appoggiati su due lati opposti) fino a questa luce sono considerati ok
    "bridge_ok_span_mm": 15,
    # Distanza tra gli oggetti quando il tool li ridispone
    "arrange_spacing_mm": 6,
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


def _resolve(path: str) -> Path:
    p = Path(os.path.expandvars(str(path).strip().strip('"'))).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"File non trovato: {p}")
    return p


def _prepare_project(src: Path, work: Path, machine: str = "", process: str = "",
                     filament: str = "") -> tuple[Path, Path, dict | None]:
    """Copia di lavoro del progetto; i file grezzi dal CAD vengono prima inizializzati coi profili."""
    is_raw_ext = src.suffix.lower() in RAW_EXTS
    project_dst = src.with_suffix(".3mf") if is_raw_ext else src
    ps = None if is_raw_ext else read_project_settings(src)
    proj = work / "project.3mf"
    if ps is not None:
        shutil.copy2(src, proj)
        return proj, project_dst, None
    names = _profile_names(machine, process, filament)
    tdir = ensure_template(names)
    init = cli_init_project(src, tdir, work)
    shutil.copy2(init, proj)
    ps = read_project_settings(proj) or {}
    flat_f = json.loads((tdir / "filament.json").read_text(encoding="utf-8"))
    if flat_f.get("default_filament_colour"):
        ps["filament_colour"] = flat_f["default_filament_colour"]
    with zipfile.ZipFile(proj) as z:
        model = z.read(MODEL_ENTRY).decode("utf-8")
    c = _bed_center(ps)
    if c:
        model = center_build_items(model, *c)
    rewrite_zip(proj, {PS_ENTRY: json.dumps(ps, indent=4, ensure_ascii=False).encode(), MODEL_ENTRY: model.encode()})
    return proj, project_dst, {"profiles": names, "template_dir": str(tdir), "bed_center": c}


def open_as_project(path: Path, work: Path, tag: str = "src") -> Path:
    """Restituisce un progetto Bambu (inizializzando i file grezzi) in una sottocartella di lavoro."""
    sub = work / tag
    sub.mkdir(parents=True, exist_ok=True)
    proj, _, _ = _prepare_project(path, sub)
    return proj


def cli_normalize(project: Path, work: Path) -> dict:
    """Fa riscrivere il progetto a Bambu Studio (CLI): rigenera miniature di ogni piatto,
    filament_sequence e relazioni interne, e verifica che il file sia caricabile."""
    out = work / "normalize"
    r = run_cli(["--outputdir", str(out), "--export-3mf", "project.3mf", str(project)], out, timeout=600)
    res = out / "project.3mf"
    if r["returncode"] != 0 or not res.exists():
        return {"ok": False, "message": (r.get("result") or {}).get("error_string") or r.get("stderr_tail", "")[-300:]}
    try:
        before = len(bp.Project(project).objects())
        after = len(bp.Project(res).objects())
    except Exception as e:
        return {"ok": False, "message": f"output CLI non leggibile: {e}"}
    if before != after:
        return {"ok": False, "message": f"la CLI ha cambiato il numero di oggetti ({before} -> {after}): scartato"}
    shutil.copy2(res, project)
    return {"ok": True}


_CAUSES = [
    (r"conflict", "Oggetti sovrapposti o troppo vicini sullo stesso piatto (G-code conflicts). Usa arrange o "
                  "controlla layout_check."),
    (r"outside|out of (the )?(plate|bed)|exceed", "Uno o piu' oggetti escono dal piatto o superano l'altezza massima."),
    (r"empty|no object|nothing to", "Piatto vuoto o oggetti non stampabili."),
    (r"floating|suspended", "Parti fluttuanti senza supporto."),
    (r"compatib|preset|config", "Profili incompatibili o impostazioni non valide."),
    (r"not found|cannot open|invalid", "File non trovato o non valido."),
]


def probable_cause(slice_rep: dict, layout: dict | None) -> list[str]:
    text = " ".join(str(slice_rep.get(k) or "") for k in ("cli_message", "stderr_tail", "stdout_tail"))
    text += " " + " ".join(slice_rep.get("slicer_warnings") or [])
    out = [msg for pat, msg in _CAUSES if re.search(pat, text, re.I)]
    for p in (layout or {}).get("problems", []):
        out.append(p["message"])
    if slice_rep.get("cli_returncode") == -999:
        out.append("Timeout dello slicing (slice_timeout_s in config.json).")
    return out or ["Causa non riconosciuta: vedi cli_message e stderr_tail."]


def analyze_project_overhangs(project: Path, cfg: dict) -> dict:
    """Sbalzi per piatto e per oggetto: regioni {z, area_mm2, tipo, xy_bbox, span_mm}."""
    try:
        P = bp.Project(project)
    except Exception as e:
        return {"error": str(e)}
    crit, warn = float(cfg["overhang_critical_angle_deg"]), float(cfg["overhang_warning_angle_deg"])
    max_span = float(cfg["bridge_ok_span_mm"])
    plates, overall = [], []
    for pl in P.plates():
        objs_rep = []
        for o in P.objects():
            if o["plate"] != pl["index"]:
                continue
            try:
                regs = bp.overhang_regions(P.world_mesh(o), crit, warn, origin=P.plate_origin(o["plate"]))
            except Exception as e:
                objs_rep.append({"name": o["name"], "error": str(e)})
                continue
            issues, level = [], 0  # 0 ok, 1 da_verificare, 2 attenzione
            for r in regs:
                xy = r["xy_bbox"]
                where = f"a z={r['z']} mm, XY {xy[0]}..{xy[2]} x {xy[1]}..{xy[3]}"
                if r["tipo"] == "ponte" and r["span_mm"] is not None and r["span_mm"] > max_span:
                    lv = 1 if r["span_mm"] <= 2 * max_span else 2
                    issues.append(f"ponte lungo {r['span_mm']} mm {where} (soglia {max_span:g} mm"
                                  f"{': di solito stampabile, verifica' if lv == 1 else ''})")
                    level = max(level, lv)
                elif r["tipo"] == "sbalzo" and r["area_mm2"] > 4:
                    issues.append(f"sbalzo senza appoggio su lati opposti di {r['area_mm2']} mm2 {where}")
                    level = 2
                elif r["tipo"] == "inclinata" and r.get("critica") and r["area_mm2"] > 25:
                    issues.append(f"superficie inclinata sotto {crit:g} gradi di {r['area_mm2']} mm2 {where}")
                    level = max(level, 1)
            counts: dict[str, int] = {}
            for r in regs:
                counts[r["tipo"]] = counts.get(r["tipo"], 0) + 1
            verdict = ("ok", "da_verificare", "attenzione")[level]
            overall.append(verdict)
            objs_rep.append({"name": o["name"], "verdict": verdict, "issues": issues[:10], "counts": counts,
                             "regions": regs[:20], "regions_total": len(regs)})
        plates.append({"plate": pl["index"], "name": pl["name"], "objects": objs_rep})
    return {"verdict": next((v for v in ("attenzione", "da_verificare") if v in overall), "ok"),
            "bridge_ok_span_mm": max_span, "critical_angle_deg": crit, "plates": plates,
            "legend": {"ponte": "soffitto orizzontale appoggiato su due lati opposti (span_mm = luce libera)",
                       "sbalzo": "soffitto orizzontale senza appoggio su lati opposti",
                       "inclinata": f"faccia inclinata verso il basso (critica se sotto {crit:g} gradi)"}}


def run_pipeline(path: str, edit=None, *, run_slice: bool = True, reload_gui: bool = False,
                 confirm_close_unsaved: bool = False, auto_arrange: bool = True, normalize: bool = True,
                 machine: str = "", process: str = "", filament: str = "", extra: dict | None = None) -> dict:
    """Pipeline comune: copia di lavoro -> modifica (edit(Project, report) -> bool) -> controllo layout
    (con ridisposizione automatica) -> export CLI (miniature) -> slicing -> backup e salvataggio -> GUI."""
    cfg = load_config()
    src = _resolve(path)
    report: dict[str, Any] = {"input": str(src)}
    if extra:
        report.update(extra)
    work = _new_workdir(_slug(src.stem)[:30])
    ok = False
    saved = False
    try:
        proj, project_dst, init = _prepare_project(src, work, machine, process, filament)
        if init:
            report["initialized_from_cad"] = init
        changed = bool(init)
        if edit is not None:
            P = bp.Project(proj)
            if edit(P, report, work):
                changed = True
                P.drop_stale_gcode()
                P.save(proj)
        # controllo sovrapposizioni / uscita dal piatto (prima dello slicing)
        P = bp.Project(proj)
        layout = P.layout_check()
        if not layout["ok"] and auto_arrange and any(p["type"] in ("overlap", "outside_plate") for p in layout["problems"]):
            plates = sorted({p["plate"] for p in layout["problems"] if p["type"] in ("overlap", "outside_plate")})
            arr = {"plates": plates, "result": [P.arrange(pl, float(cfg["arrange_spacing_mm"])) for pl in plates],
                   "problems_before": [p["message"] for p in layout["problems"]]}
            P.drop_stale_gcode()
            P.save(proj)
            changed = True
            report["auto_arrange"] = arr
            layout = bp.Project(proj).layout_check()
        report["layout_check"] = layout
        if changed and normalize:
            report["normalize"] = cli_normalize(proj, work)
        ps = read_project_settings(proj) or {}
        report["profiles_in_project"] = {"printer": ps.get("printer_settings_id"),
                                         "process": ps.get("print_settings_id"),
                                         "filament": ps.get("filament_settings_id"),
                                         "filament_colour": ps.get("filament_colour")}
        sliced_dst = _sliced_path_for(project_dst)
        sliced_tmp = None
        if run_slice:
            sr = cli_slice(proj, sliced_dst.name, work)
            report["slice"] = sr
            if sr.get("ok"):
                sliced_tmp = Path(sr.pop("sliced_file_tmp"))
            else:
                report["slice_failure"] = {"probable_causes": probable_cause(sr, layout)}
        report["overhang_analysis"] = analyze_project_overhangs(proj, cfg)

        close_rep = gui_close_matching([project_dst, sliced_dst], confirm_close_unsaved) if reload_gui else None
        if changed:
            if project_dst.exists():
                report["backup"] = _backup(project_dst)
            _replace_with_retry(proj, project_dst)
            saved = True
        report["project_file"] = str(project_dst)
        report["project_saved"] = saved
        if sliced_tmp:
            _replace_with_retry(sliced_tmp, sliced_dst)
            report["sliced_file"] = str(sliced_dst)
        if run_slice and not sliced_tmp:
            report["slice_failure"]["message"] = (
                ("Slicing fallito, MA il progetto e' stato salvato con le modifiche in " + str(project_dst))
                if saved else "Slicing fallito; il progetto non e' stato modificato.") + \
                (" Il vecchio .gcode.3mf (se esiste) NON e' aggiornato." if sliced_dst.exists() else "")
        if reload_gui:
            report["gui"] = gui_reload(project_dst, sliced_dst if sliced_tmp else None, close_rep)
            report["gui_summary"] = report["gui"].get("summary")
            report["unsaved_changes_found"] = bool(close_rep and close_rep.get("unsaved_changes_found"))
            if close_rep and close_rep.get("warnings"):
                report["warnings"] = close_rep["warnings"]
            if close_rep and close_rep.get("needs_confirmation"):
                report["needs_confirmation"] = True
        ok = bool(not run_slice or sliced_tmp)
        report["ok"] = ok
        return report
    finally:
        if ok:
            shutil.rmtree(work, ignore_errors=True)
        else:
            report["work_dir_kept_for_debug"] = str(work)
            report.setdefault("project_saved", saved)
        try:
            for o in sorted(WORK_DIR.glob("*"))[:-10]:
                shutil.rmtree(o, ignore_errors=True)
        except Exception:
            pass


def process_3mf(threemf_path: str, settings_patch: dict | str | None, rotate_z_deg: float = 0.0,
                run_slice: bool = True, reload_gui: bool = False, machine: str = "", process: str = "",
                filament: str = "", confirm_close_unsaved: bool = False, filament_profile: str = "",
                filament_colour: str = "") -> dict:
    if isinstance(settings_patch, str):
        settings_patch = json.loads(settings_patch) if settings_patch.strip() else {}
    patch = dict(settings_patch or {})

    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        changed = False
        if filament_profile:
            report["filament_profile"] = apply_filament_profile(P.settings, filament_profile)
            changed = True
        if filament_colour:
            n = len(P.settings.get("filament_settings_id") or [1])
            P.settings["filament_colour"] = [filament_colour] * max(n, 1)
            report["filament_colour"] = filament_colour
            changed = True
        if patch:
            report["patch"] = apply_patch(P.settings, patch)
            changed = changed or bool(report["patch"]["changed"])
        if rotate_z_deg:
            for o in P.objects():
                P.rotate_world(o, rz=float(rotate_z_deg))
            report["rotation"] = {"deg_z": rotate_z_deg, "objects": [o["name"] for o in P.objects()]}
            changed = True
        return changed

    return run_pipeline(threemf_path, edit, run_slice=run_slice or bool(rotate_z_deg), reload_gui=reload_gui,
                        confirm_close_unsaved=confirm_close_unsaved, machine=machine, process=process,
                        filament=filament)


_FIL_META = {"name", "inherits", "from", "type", "setting_id", "instantiation", "include", "version",
             "description", "compatible_printers", "compatible_printers_condition", "compatible_prints",
             "compatible_prints_condition", "filament_settings_id", "filament_id"}


def apply_filament_profile(ps: dict, name: str) -> dict:
    """Applica al progetto tutti i valori di un profilo filamento (sistema o utente), senza
    toccare i default globali."""
    idx = get_index()
    if name not in idx.entries:
        idx = get_index(refresh=True)
    if idx.kind_of(name) != "filament":
        sug = [e["name"] for e in idx.list("filament", name.split("@")[0].strip()[:12])][:8]
        raise KeyError(f"Profilo filamento '{name}' non trovato. Simili: {sug}")
    flat = idx.flatten(name)
    n = len(ps.get("filament_settings_id") or [1]) or 1
    changed = []
    for k, v in flat.items():
        if k in _FIL_META or k not in ps:
            continue
        nv = v if isinstance(v, list) else normalize_value(ps[k], v)
        if isinstance(ps[k], list) and isinstance(nv, list) and len(nv) == 1 and n > 1:
            nv = nv * n
        if ps[k] != nv:
            ps[k] = nv
            changed.append(k)
    ps["filament_settings_id"] = [name] * n
    if flat.get("filament_id"):
        ps["filament_ids"] = [flat["filament_id"]] * n
    ig = ps.get("inherits_group")
    if isinstance(ig, list) and len(ig) >= 2:
        anc = idx.nearest_system_ancestor(name) if idx.entries[name]["source"] == "user" else ""
        for i in range(1, len(ig) - 1):
            ig[i] = anc
    return {"profile": name, "keys_changed": len(changed),
            "nozzle_temperature": ps.get("nozzle_temperature"), "textured_plate_temp": ps.get("textured_plate_temp")}


def default_cad_patch(seam_position: str = "back", infill_percent: int = 18, bed_temp: int = 60,
                      nozzle_temp: int = 215) -> tuple[dict, list[str]]:
    notes = []
    if not 15 <= infill_percent <= 20:
        notes.append(f"infill_percent={infill_percent} fuori dal range consigliato 15-20%")
    if bed_temp and not 55 <= bed_temp <= 60:
        notes.append(f"bed_temp={bed_temp} fuori dal range consigliato 55-60 C")
    if nozzle_temp and not 205 <= nozzle_temp <= 230:
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
    if not bed_temp:  # 0 = mantieni le temperature del profilo filamento
        for k in ("textured_plate_temp", "textured_plate_temp_initial_layer"):
            patch.pop(k)
    if not nozzle_temp:
        for k in ("nozzle_temperature", "nozzle_temperature_initial_layer"):
            patch.pop(k)
    return patch, notes


# --------------------------------------------------------------------------------------
# Server MCP
# --------------------------------------------------------------------------------------
mcp = FastMCP(
    "bambu-live-mcp",
    instructions=(
        "Strumenti per configurare, slicare e mostrare dal vivo file .3mf in Bambu Studio (Windows). "
        "Impostazioni: inspect_3mf_settings per le chiavi, apply_settings_and_preview con settings_patch "
        "{chiave: valore}, apply_default_cad_preset per il preset standard (accetta filament_profile e "
        "filament_colour). Oggetti e piatti: list_objects, replace_meshes (aggiorna la geometria dal CAD "
        "mantenendo le impostazioni), merge_projects, add_plate, assign_to_plate, transform_object, "
        "lay_on_face, arrange. Cucitura: seam_report (dal .gcode.3mf) e paint_seam. Ogni modifica controlla "
        "sovrapposizioni/uscita dal piatto e ridispone da sola se serve. La GUI NON viene ricaricata di "
        "default: usa reload_gui alla fine di piu' modifiche. Riferisci sempre all'utente warnings, "
        "layout_check, slice_failure e overhang_analysis.verdict. Nomi di file/oggetti/piatti: esatti; "
        "le coordinate dei box sono in mm sul piatto (origine nell'angolo del piatto)."
    ),
)


def _truncate(v: Any, n: int = 160) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... (+{len(v) - n} caratteri)"
    if isinstance(v, list):
        return [_truncate(x, n) for x in v]
    return v


async def _run(fn, *a, **kw):
    def go():
        with _LOCK:
            return fn(*a, **kw)
    return await anyio.to_thread.run_sync(go)


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
                out["contains_gcode"] = any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in z.namelist())
        return out
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def apply_settings_and_preview(threemf_path: str, settings_patch: dict, rotate_z_deg: float = 0.0,
                                     run_slice: bool = True, reload_gui: bool = False,
                                     confirm_close_unsaved: bool = False) -> dict:
    """Applica settings_patch (qualsiasi chiave di project_settings.config, es.
    {"wall_loops": 4, "sparse_infill_density": "20%", "nozzle_temperature": 215}) al .3mf.
    - .3mf grezzo dal CAD (o .stl/.step): viene prima inizializzato con i profili base e centrato.
    - rotate_z_deg: ruota ogni oggetto attorno al proprio centro; se poi si sovrappongono o escono
      dal piatto vengono ridisposti automaticamente (vedi auto_arrange nella risposta).
    - run_slice: slicing headless -> <nome>.gcode.3mf, stime per piatto, avvisi, analisi sbalzi.
      Se lo slicing fallisce la risposta dice se il progetto e' stato comunque salvato e la causa probabile.
    - reload_gui (default false): chiude SOLO la finestra di Bambu Studio con questo file (su "Save"
      sceglie "No") e la riapre. Le altre finestre non vengono toccate. Con needs_confirmation=true
      chiedi all'utente e solo se conferma rilancia con confirm_close_unsaved=true.
    Riferisci all'utente gui_summary, warnings e layout_check. Backup in .bambu_mcp_backup."""
    return await _run(process_3mf, threemf_path, settings_patch, rotate_z_deg, run_slice, reload_gui,
                      confirm_close_unsaved=confirm_close_unsaved)


@mcp.tool()
async def apply_default_cad_preset(threemf_path: str, seam_position: str = "back", infill_percent: int = 18,
                                   bed_temp: int = 60, nozzle_temp: int = 215, rotate_z_deg: float = 0.0,
                                   filament_profile: str = "", filament_colour: str = "",
                                   reload_gui: bool = False, confirm_close_unsaved: bool = False) -> dict:
    """Preset rapido per pezzi da CAD: layer 0.2 mm (anche primo layer), Textured PEI Plate a
    bed_temp (55-60 C), ugello nozzle_temp, Arachne con 3 pareti, 5 strati superiori / 4 inferiori,
    gyroid al infill_percent (15-20%), supporti OFF (con verifica sbalzi), cucitura seam_position
    ('back' o 'aligned'), brim OFF, compensazione piede d'elefante 0.15 mm.
    filament_profile: applica anche un profilo filamento (es. 'SUNLU PLA Matte @BBL A1' o un preset
    utente) solo a questo progetto, senza cambiare i default globali; con bed_temp=0 / nozzle_temp=0
    restano le temperature del profilo. filament_colour: es. '#000000'.
    rotate_z_deg ruota i pezzi sull'asse Z (ridisposti se si sovrappongono). Poi slicing.
    reload_gui di default false (usa il tool reload_gui alla fine)."""
    def run() -> dict:
        patch, notes = default_cad_patch(seam_position, infill_percent, bed_temp, nozzle_temp)
        rep = process_3mf(threemf_path, patch, rotate_z_deg, True, reload_gui,
                          confirm_close_unsaved=confirm_close_unsaved, filament_profile=filament_profile,
                          filament_colour=filament_colour)
        if notes:
            rep["preset_notes"] = notes
        return rep
    return await _run(run)


@mcp.tool()
async def list_objects(project_3mf: str) -> dict:
    """Piatti e oggetti del progetto: nome, piatto, ingombro sul piatto (mm), dimensioni, rotazione Z,
    triangoli, piu' il controllo di sovrapposizioni / uscita dal piatto."""
    def run() -> dict:
        P = bp.Project(_resolve(project_3mf))
        return {**P.summary(), "layout_check": P.layout_check()}
    return await _run(run)


@mcp.tool()
async def replace_meshes(project_3mf: str, cad_3mf: str, match: str = "name", name_map: dict | None = None,
                         run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Aggiorna la geometria del progetto con una nuova esportazione dal CAD mantenendo TUTTO il resto:
    project_settings, piatto, posizione XY, rotazione e impostazioni per oggetto.
    Gli oggetti vengono abbinati per nome (match='name': esatto, poi ignorando maiuscole/spazi/_);
    name_map {nome_nel_progetto: nome_nel_cad} per abbinamenti espliciti. La nuova mesh viene
    registrata su quella vecchia (le parti invariate restano ferme) e il pezzo riappoggiato al piatto.
    Riporta abbinati, nuovi (solo nel CAD) e spariti (solo nel progetto) con suggerimenti."""
    if match != "name":
        raise ValueError("match supportato: 'name'")

    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        cad_path = _resolve(cad_3mf)
        cad = bp.cad_objects(cad_path)
        nm = dict(name_map or {})
        by_norm = {bp._norm_name(k): k for k in cad}
        used, matched, missing = set(), [], []
        for o in P.objects():
            cname = nm.get(o["name"])
            if cname is None:
                cname = o["name"] if o["name"] in cad else by_norm.get(bp._norm_name(o["name"]))
            if not cname or cname not in cad:
                missing.append({"name": o["name"], "suggestions": difflib.get_close_matches(
                    o["name"], [k for k in cad if k not in used], n=3, cutoff=0.3)})
                continue
            if len(o["components"]) != 1:
                missing.append({"name": o["name"], "error": "oggetto multi-parte: sostituzione non supportata"})
                continue
            comp = o["components"][0]
            old_mesh = P.mesh(comp["path"], comp["objectid"])
            old_obj = old_mesh.transformed(comp["transform"])
            new = cad[cname]
            off, hits = bp.registration_offset(old_obj, new)
            if hits < max(5, 0.05 * len(new.verts)):  # nessuna parte in comune: allinea i centri
                olo, ohi = old_obj.bbox()
                nlo, nhi = new.bbox()
                off = tuple((olo[i] + ohi[i]) / 2 - (nlo[i] + nhi[i]) / 2 for i in range(3))
                mode = "centro"
            else:
                mode = f"registrata ({hits} vertici coincidenti)"
            new_obj = new.transformed(bp.translation(*off))
            inv = bp.invert(comp["transform"])
            new_local = new_obj.transformed(inv)
            painted = old_mesh.has_paint()
            P.set_mesh(comp["path"], comp["objectid"], new_local)
            lo_b, hi_b = old_obj.bbox()
            P.drop_to_bed(o)
            lo_a, hi_a = new_obj.bbox()
            ms = P._ms_object(o["id"])
            if ms is not None:
                for m in ms.findall("metadata"):
                    if m.get("face_count") is not None:
                        m.set("face_count", str(len(new.tris)))
                for part in ms.findall("part"):
                    st = part.find("mesh_stat")
                    if st is not None:
                        st.set("face_count", str(len(new.tris)))
            used.add(cname)
            entry = {"name": o["name"], "cad_name": cname, "plate": o["plate"], "alignment": mode,
                     "triangles": [len(old_mesh.tris), len(new.tris)],
                     "size_before_mm": [round(hi_b[i] - lo_b[i], 2) for i in range(3)],
                     "size_after_mm": [round(hi_a[i] - lo_a[i], 2) for i in range(3)]}
            if painted:
                entry["warning"] = "la vecchia mesh aveva verniciature (cucitura/supporti/colore): perse"
            matched.append(entry)
        report["replace"] = {"cad_file": str(cad_path), "matched": matched, "missing_in_cad": missing,
                             "new_in_cad": [k for k in cad if k not in used]}
        return bool(matched)

    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def merge_projects(target_3mf: str, source_3mf: str, as_new_plate: bool = True, plate_name: str = "",
                         target_plate: int = 1, run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Unisce gli oggetti di source_3mf (progetto Bambu o .3mf/.stl grezzo dal CAD) nel progetto target,
    con le loro impostazioni per oggetto. as_new_plate=True: ogni piatto della sorgente diventa un nuovo
    piatto (plate_name per il nome); altrimenti finiscono sul piatto target_plate e vengono ridisposti.
    ID, UUID, origini dei piatti, filament_sequence e miniature vengono calcolati dal tool.
    Le impostazioni di stampa restano quelle del target."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        sp = open_as_project(_resolve(source_3mf), work)
        S = bp.Project(sp)
        added = P.merge_from(S, as_new_plate=as_new_plate, plate_name=plate_name, target_plate=target_plate)
        if not as_new_plate:
            P.arrange(target_plate, float(load_config()["arrange_spacing_mm"]))
        report["merged"] = {"source": str(_resolve(source_3mf)), "added": added,
                            "plates": [{"index": p["index"], "name": p["name"]} for p in P.plates()]}
        return True
    return await _run(run_pipeline, target_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def add_plate(project_3mf: str, name: str = "", run_slice: bool = False, reload_gui: bool = False) -> dict:
    """Aggiunge un piatto vuoto (con nome). Le origini dei piatti vengono ricalcolate dal tool."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        idx = P.add_plate(name)
        report["plate_added"] = {"index": idx, "name": name,
                                 "plates": [{"index": p["index"], "name": p["name"]} for p in P.plates()]}
        return True
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def assign_to_plate(project_3mf: str, object_name: str, plate: int, plate_name: str = "",
                          arrange: bool = True, run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Sposta un oggetto su un piatto (plate = numero; se plate e' il numero del piatto successivo
    all'ultimo, il piatto viene creato con plate_name). arrange=True ridispone il piatto di arrivo."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        plates = [p["index"] for p in P.plates()]
        if plate == max(plates) + 1:
            P.add_plate(plate_name)
        elif plate_name:
            P.rename_plate(plate, plate_name)
        o = P.find(object_name)
        frm = o["plate"]
        P.assign_to_plate(o, plate)
        if arrange:
            P.arrange(plate, float(load_config()["arrange_spacing_mm"]))
        report["moved"] = {"name": o["name"], "from_plate": frm, "to_plate": plate}
        return True
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def transform_object(project_3mf: str, object_name: str, rotate_x: float = 0, rotate_y: float = 0,
                           rotate_z: float = 0, run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Ruota un oggetto (gradi, assi del piatto, nell'ordine X, Y, Z) attorno al proprio centro e lo
    riappoggia al piatto. Es. rotate_x=180 capovolge un coperchio. Se poi si sovrappone o esce dal
    piatto il piatto viene ridisposto automaticamente."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        o = P.find(object_name)
        P.rotate_world(o, rotate_x, rotate_y, rotate_z)
        report["transformed"] = {"name": o["name"], "rotate": [rotate_x, rotate_y, rotate_z],
                                 "bbox_on_plate": P.footprint(o)["bbox"]}
        return True
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def lay_on_face(project_3mf: str, object_name: str, face: str | list[float] = "largest_flat",
                      run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Appoggia l'oggetto sul piatto con una faccia: face='largest_flat' sceglie la faccia piana esterna
    piu' grande; oppure una normale [x, y, z] in coordinate del piatto (es. [0, 0, 1] = la faccia oggi
    rivolta verso l'alto finisce sul piatto)."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        o = P.find(object_name)
        wm = P.world_mesh(o)
        if isinstance(face, str):
            if face != "largest_flat":
                raise ValueError("face: 'largest_flat' oppure una normale [x, y, z]")
            n, area = _largest_outer_face(wm)
        else:
            n, area = tuple(float(x) for x in face), None
        R = bp.rotation_between(n, (0, 0, -1))
        lo, hi = wm.bbox()
        c = tuple((lo[i] + hi[i]) / 2 for i in range(3))
        P.set_transform(o, bp.compose(o["transform"], bp.about_point(R, c)))
        P.drop_to_bed(o)
        report["laid"] = {"name": o["name"], "face_normal": [round(x, 4) for x in n],
                          "face_area_mm2": round(area, 1) if area else None,
                          "bbox_on_plate": P.footprint(o)["bbox"]}
        return True
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


def _largest_outer_face(mesh: bp.Mesh) -> tuple[tuple, float]:
    groups: dict[tuple, list] = {}
    V = mesh.verts
    for a, b, c in mesh.tris:
        pa, pb, pc = V[a], V[b], V[c]
        n = bp._cross((pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]), (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]))
        ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        if ln < 1e-9:
            continue
        u = (round(n[0] / ln, 3), round(n[1] / ln, 3), round(n[2] / ln, 3))
        d = round(u[0] * pa[0] + u[1] * pa[1] + u[2] * pa[2], 1)
        g = groups.setdefault((u, d), [0.0])
        g[0] += ln / 2
    best = None
    for (u, d), (area,) in sorted(groups.items(), key=lambda kv: -kv[1][0]):
        ext = max(u[0] * v[0] + u[1] * v[1] + u[2] * v[2] for v in V)
        if ext <= d + 0.1:  # faccia esterna (nessun punto oltre il suo piano)
            best = (u, area)
            break
    if not best:
        raise ValueError("nessuna faccia piana esterna trovata")
    return best


@mcp.tool()
async def arrange(project_3mf: str, plate: int = 0, spacing_mm: float = 6.0, run_slice: bool = True,
                  reload_gui: bool = False) -> dict:
    """Dispone gli oggetti sul piatto (plate=0: tutti i piatti) senza sovrapposizioni, con spacing_mm
    di distanza, centrati sul piatto. Non ruota i pezzi."""
    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        report["arrange"] = P.arrange(plate or None, float(spacing_mm))
        return True
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def paint_seam(project_3mf: str, object_name: str, box: list[float], mode: str = "block",
                     run_slice: bool = True, reload_gui: bool = False) -> dict:
    """Vernicia la cucitura di un oggetto: mode 'enforce' (metti la cucitura qui), 'block' (mai qui) o
    'clear'. box = [xmin, ymin, zmin, xmax, ymax, zmax] in mm sul piatto dell'oggetto (stesse coordinate
    di list_objects.bbox_on_plate e di seam_report). Vengono marcati i triangoli il cui baricentro cade
    nel box (le mesh CAD hanno triangoli grandi: usa box generosi). Verifica poi con seam_report."""
    if mode not in ("enforce", "block", "clear"):
        raise ValueError("mode: enforce | block | clear")

    def edit(P: bp.Project, report: dict, work: Path) -> bool:
        o = P.find(object_name)
        if len(o["components"]) != 1:
            raise ValueError("oggetto multi-parte non supportato")
        comp = o["components"][0]
        mesh = P.mesh(comp["path"], comp["objectid"])
        ox, oy = P.plate_origin(o["plate"])
        world_box = [box[0] + ox, box[1] + oy, box[2], box[3] + ox, box[4] + oy, box[5]]
        n = bp.paint_seam(mesh, bp.compose(comp["transform"], o["transform"]), world_box, mode)
        P.set_mesh(comp["path"], comp["objectid"], mesh)
        report["seam_paint"] = {"name": o["name"], "mode": mode, "box": box, "triangles": n}
        if n == 0:
            report["seam_paint"]["warning"] = "nessun triangolo nel box: controlla le coordinate con list_objects"
        return n > 0
    return await _run(run_pipeline, project_3mf, edit, run_slice=run_slice, reload_gui=reload_gui)


@mcp.tool()
async def seam_report(gcode_3mf: str, band_mm: float = 5.0, avoid_box: list[float] | None = None,
                      avoid_plate: int = 0) -> dict:
    """Dove cade la cucitura, letto dal G-code del .gcode.3mf: per piatto, oggetto e fascia di altezza
    (band_mm) la posizione XY tipica (mediana, mm sul piatto), la dispersione e il lato dell'oggetto.
    avoid_box [xmin, ymin, zmin, xmax, ymax, zmax] (mm sul piatto, piatto avoid_plate o tutti):
    conta le cuciture che cadono nella zona da evitare."""
    def run() -> dict:
        path = _resolve(gcode_3mf)
        if not path.name.lower().endswith(".gcode.3mf"):
            sliced = _sliced_path_for(path)
            if sliced.exists():
                path = sliced
        P = bp.Project(path)
        fps = {o["id"]: (o, P.footprint(o)) for o in P.objects()}
        out = {"file": str(path), "plates": []}
        with zipfile.ZipFile(path) as z:
            for name in sorted(n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)):
                pl = int(re.search(r"plate_(\d+)", name).group(1))
                with z.open(name) as f:
                    pts = bp.seam_points(line.decode("utf-8", "replace") for line in f)
                objs = [(o, fp) for o, fp in fps.values() if o["plate"] == pl]
                per: dict[str, list] = {}
                for x, y, zz in pts:
                    best = None
                    for o, fp in objs:
                        h = fp["hull"]
                        dist = _dist_to_hull(h, x, y)
                        if best is None or dist < best[0]:
                            best = (dist, o["name"], fp)
                    if best and best[0] <= 2.0:
                        per.setdefault(best[1], []).append((x, y, zz))
                plate_rep = {"plate": pl, "seams_total": len(pts), "objects": []}
                for oname, lst in per.items():
                    fp = next(fp for o, fp in objs if o["name"] == oname)
                    bands = {}
                    for x, y, zz in lst:
                        bands.setdefault(int(zz // band_mm), []).append((x, y))
                    brep = []
                    for k in sorted(bands):
                        clusters = _seam_clusters(bands[k], 2.0)
                        brep.append({"z_from": round(k * band_mm, 2), "z_to": round((k + 1) * band_mm, 2),
                                     "count": len(bands[k]),
                                     "positions": [{"xy": [round(cx, 2), round(cy, 2)], "count": n,
                                                    "side": _side(fp["bbox"], cx, cy)}
                                                   for cx, cy, n in clusters[:6]],
                                     "other_positions": max(0, len(clusters) - 6)})
                    orep = {"name": oname, "seams": len(lst), "bbox_on_plate": fp["bbox"], "bands": brep}
                    if avoid_box and (not avoid_plate or avoid_plate == pl):
                        b = avoid_box
                        inside = [p for p in lst if b[0] <= p[0] <= b[3] and b[1] <= p[1] <= b[4] and b[2] <= p[2] <= b[5]]
                        orep["in_avoid_box"] = len(inside)
                        orep["in_avoid_box_examples"] = [[round(v, 2) for v in p] for p in inside[:5]]
                    plate_rep["objects"].append(orep)
                out["plates"].append(plate_rep)
        if avoid_box:
            tot = sum(o.get("in_avoid_box", 0) for p in out["plates"] for o in p["objects"])
            out["avoid_box_ok"] = tot == 0
            out["avoid_box_seams"] = tot
        return out
    return await _run(run)


def _seam_clusters(pts: list[tuple], radius: float) -> list[tuple[float, float, int]]:
    """Raggruppa le cuciture vicine (ogni foro/isola ha la sua): [(x, y, conteggio)] dal piu' frequente."""
    cl: list[list] = []
    for x, y in pts:
        for c in cl:
            if math.hypot(x - c[0] / c[2], y - c[1] / c[2]) <= radius:
                c[0] += x
                c[1] += y
                c[2] += 1
                break
        else:
            cl.append([x, y, 1])
    return sorted(((c[0] / c[2], c[1] / c[2], c[2]) for c in cl), key=lambda t: -t[2])


def _dist_to_hull(h: list[tuple], x: float, y: float) -> float:
    if len(h) < 3:
        return min((math.hypot(x - a, y - b) for a, b in h), default=1e9)
    inside = True
    best = 1e9
    for i in range(len(h)):
        (x1, y1), (x2, y2) = h[i], h[(i + 1) % len(h)]
        if (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1) < 0:
            inside = False
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / ((dx * dx + dy * dy) or 1)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return 0.0 if inside else best


def _side(bb: list[float], x: float, y: float) -> str:
    cx, cy = (bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2
    hw, hd = max((bb[3] - bb[0]) / 2, 1e-6), max((bb[4] - bb[1]) / 2, 1e-6)
    u, v = (x - cx) / hw, (y - cy) / hd
    parts = []
    if v > 0.5:
        parts.append("retro (+Y)")
    elif v < -0.5:
        parts.append("fronte (-Y)")
    if u > 0.5:
        parts.append("destra (+X)")
    elif u < -0.5:
        parts.append("sinistra (-X)")
    return " / ".join(parts) or "centro"


@mcp.tool()
async def reload_gui(project_3mf: str, confirm_close_unsaved: bool = False) -> dict:
    """Ricarica Bambu Studio una volta sola (es. alla fine di piu' modifiche): chiude solo la finestra
    con questo progetto (regole di chiusura sicura) e riapre il .gcode.3mf se e' piu' recente del
    progetto, altrimenti il progetto."""
    def run() -> dict:
        project = _resolve(project_3mf)
        if project.name.lower().endswith(".gcode.3mf"):
            sliced = project
            stem = project.name[: -len(".gcode.3mf")]
            project = project.with_name(stem + ".3mf")
        else:
            sliced = _sliced_path_for(project)
        fresh = sliced.exists() and (not project.exists() or sliced.stat().st_mtime >= project.stat().st_mtime - 1)
        close_rep = gui_close_matching([project, sliced], confirm_close_unsaved)
        rep = gui_reload(project if project.exists() else sliced, sliced if fresh else None, close_rep)
        if not fresh and sliced.exists():
            rep["note"] = "Il .gcode.3mf e' piu' vecchio del progetto: apro il progetto (rilancia lo slicing per l'anteprima)."
        rep["warnings"] = close_rep.get("warnings")
        rep["needs_confirmation"] = close_rep.get("needs_confirmation")
        return rep
    return await _run(run)


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
    """Cambia i profili base usati per inizializzare i FUTURI .3mf grezzi (es. un'altra stampante:
    machine='Bambu Lab P1S 0.4 nozzle', process='0.20mm Standard @BBL X1C', filament='Bambu PLA Basic @BBL X1C').
    Campi vuoti = invariati. Per cambiare filamento a un solo progetto usa apply_default_cad_preset(filament_profile=...)."""
    def run() -> dict:
        names = _profile_names(machine, process, filament)
        tdir = ensure_template(names, force=True)
        cfg = load_config()
        cfg["profiles"] = names
        save_config(cfg)
        return {"ok": True, "profiles": names, "template_dir": str(tdir)}
    return await _run(run)


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
