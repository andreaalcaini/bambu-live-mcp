"""
Modello di un progetto .3mf di Bambu Studio: oggetti, mesh, piatti, trasformazioni e geometria
(impronte, sovrapposizioni, disposizione, sbalzi, cucitura).

Convenzione 3MF: vettori riga, p' = p * M, con M affine 4x3 scritta come 12 numeri
"m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz".
"""
from __future__ import annotations

import copy
import difflib
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

MODEL_ENTRY = "3D/3dmodel.model"
MS_ENTRY = "Metadata/model_settings.config"
PS_ENTRY = "Metadata/project_settings.config"
RELS_ENTRY = "3D/_rels/3dmodel.model.rels"
FSEQ_ENTRY = "Metadata/filament_sequence.json"
PLATE_GAP = 0.2  # Bambu Studio: piatti distanziati di (1 + 1/5) x dimensione del piatto

_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')


def attrs(s: str) -> dict:
    return dict(_ATTR_RE.findall(s))


# ======================================================================================
# Matrici affini (vettori riga)
# ======================================================================================
IDENTITY = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0]


def parse_tf(s: str | None) -> list[float]:
    if not s:
        return list(IDENTITY)
    v = [float(x) for x in s.split()]
    return v if len(v) == 12 else list(IDENTITY)


def fmt_tf(m: list[float]) -> str:
    return " ".join(f"{(0.0 if abs(x) < 1e-12 else x):.9g}" for x in m)


def compose(a: list[float], b: list[float]) -> list[float]:
    """Prima a, poi b."""
    A = [a[0:3] + [0], a[3:6] + [0], a[6:9] + [0], a[9:12] + [1]]
    B = [b[0:3] + [0], b[3:6] + [0], b[6:9] + [0], b[9:12] + [1]]
    C = [[sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    return C[0][:3] + C[1][:3] + C[2][:3] + C[3][:3]


def invert(m: list[float]) -> list[float]:
    a, b, c, d, e, f, g, h, i = m[:9]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-15:
        raise ValueError("matrice non invertibile")
    inv = [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
           (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
           (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]
    tx, ty, tz = m[9:12]
    t = [-(tx * inv[0] + ty * inv[3] + tz * inv[6]),
         -(tx * inv[1] + ty * inv[4] + tz * inv[7]),
         -(tx * inv[2] + ty * inv[5] + tz * inv[8])]
    return inv + t


def apply(m: list[float], p: tuple) -> tuple[float, float, float]:
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def apply_dir(m: list[float], v: tuple) -> tuple[float, float, float]:
    x, y, z = v
    return (x * m[0] + y * m[3] + z * m[6], x * m[1] + y * m[4] + z * m[7], x * m[2] + y * m[5] + z * m[8])


def translation(tx: float, ty: float, tz: float) -> list[float]:
    return [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, tx, ty, tz]


def rot(axis: str, deg: float) -> list[float]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return [1.0, 0, 0, 0, c, s, 0, -s, c, 0, 0, 0]
    if axis == "y":
        return [c, 0, -s, 0, 1.0, 0, s, 0, c, 0, 0, 0]
    return [c, s, 0, -s, c, 0, 0, 0, 1.0, 0, 0, 0]


def rot_axis_angle(axis: tuple, ang: float) -> list[float]:
    x, y, z = _unit(axis)
    c, s, C = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    R = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
         [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
         [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]  # p' = R p (colonne)
    return [R[0][0], R[1][0], R[2][0], R[0][1], R[1][1], R[2][1], R[0][2], R[1][2], R[2][2], 0, 0, 0]


def rotation_between(a: tuple, b: tuple) -> list[float]:
    a, b = _unit(a), _unit(b)
    d = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
    if d > 1 - 1e-9:
        return list(IDENTITY)
    if d < -1 + 1e-9:
        perp = (1, 0, 0) if abs(a[0]) < 0.9 else (0, 1, 0)
        return rot_axis_angle(_cross(a, perp), math.pi)
    return rot_axis_angle(_cross(a, b), math.acos(d))


def about_point(m: list[float], c: tuple) -> list[float]:
    """Applica la parte lineare di m attorno al punto c."""
    return compose(compose(translation(-c[0], -c[1], -c[2]), m), translation(*c))


def _unit(v: tuple) -> tuple[float, float, float]:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a: tuple, b: tuple) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def is_identity(m: list[float], eps: float = 1e-9) -> bool:
    return all(abs(x - y) < eps for x, y in zip(m, IDENTITY))


def rotation_deg_z(m: list[float]) -> float:
    return round(math.degrees(math.atan2(m[1], m[0])), 2)


# ======================================================================================
# Mesh
# ======================================================================================
_VERT_FAST = re.compile(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"\s*/>')
_VERT_ANY = re.compile(r"<vertex\b([^>]*?)/?>")
_TRI_FAST = re.compile(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"([^>]*?)/>')
_TRI_ANY = re.compile(r"<triangle\b([^>]*?)/?>")


class Mesh:
    def __init__(self, verts: list[tuple], tris: list[tuple], tri_extra: list[str] | None = None):
        self.verts = verts
        self.tris = tris
        self.tri_extra = tri_extra or [""] * len(tris)

    @classmethod
    def parse(cls, body: str) -> "Mesh":
        verts = [(float(a), float(b), float(c)) for a, b, c in _VERT_FAST.findall(body)]
        if not verts:
            verts = [(float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0)))
                     for d in (attrs(x) for x in _VERT_ANY.findall(body))]
        tris, extra = [], []
        fast = _TRI_FAST.findall(body)
        if fast:
            for a, b, c, rest in fast:
                tris.append((int(a), int(b), int(c)))
                extra.append(rest.strip())
        else:
            for x in _TRI_ANY.findall(body):
                d = attrs(x)
                tris.append((int(d["v1"]), int(d["v2"]), int(d["v3"])))
                extra.append(" ".join(f'{k}="{v}"' for k, v in d.items() if k not in ("v1", "v2", "v3")))
        return cls(verts, tris, extra)

    def to_xml(self, indent: str = "    ") -> str:
        out = [f"{indent}<mesh>\n{indent} <vertices>\n"]
        out += [f'{indent}  <vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>\n' for x, y, z in self.verts]
        out.append(f"{indent} </vertices>\n{indent} <triangles>\n")
        for (a, b, c), ex in zip(self.tris, self.tri_extra):
            out.append(f'{indent}  <triangle v1="{a}" v2="{b}" v3="{c}"{(" " + ex) if ex else ""}/>\n')
        out.append(f"{indent} </triangles>\n{indent}</mesh>\n")
        return "".join(out)

    def transformed(self, m: list[float]) -> "Mesh":
        return Mesh([apply(m, v) for v in self.verts], list(self.tris), list(self.tri_extra))

    def bbox(self) -> tuple[list[float], list[float]]:
        xs, ys, zs = zip(*self.verts) if self.verts else ((0,), (0,), (0,))
        return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]

    def has_paint(self) -> bool:
        return any("paint_" in e for e in self.tri_extra)


def merge_meshes(meshes: list[Mesh]) -> Mesh:
    verts, tris, extra = [], [], []
    for m in meshes:
        off = len(verts)
        verts += m.verts
        tris += [(a + off, b + off, c + off) for a, b, c in m.tris]
        extra += m.tri_extra
    return Mesh(verts, tris, extra)


# ======================================================================================
# Geometria 2D
# ======================================================================================
def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cr(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cr(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def hulls_overlap(a: list[tuple], b: list[tuple], gap: float = 0.0) -> bool:
    """Separating Axis Theorem tra poligoni convessi; gap>0 richiede una distanza minima."""
    if len(a) < 3 or len(b) < 3:
        return False
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            ax, ay = y2 - y1, x1 - x2
            ln = math.hypot(ax, ay) or 1.0
            ax, ay = ax / ln, ay / ln
            pa = [x * ax + y * ay for x, y in a]
            pb = [x * ax + y * ay for x, y in b]
            if max(pa) + gap <= min(pb) or max(pb) + gap <= min(pa):
                return False
    return True


def hull_area(h: list[tuple]) -> float:
    return abs(sum(h[i][0] * h[(i + 1) % len(h)][1] - h[(i + 1) % len(h)][0] * h[i][1] for i in range(len(h)))) / 2


# ======================================================================================
# Progetto
# ======================================================================================
def _new_uuid() -> str:
    return str(uuid.uuid4())


class Project:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, bytes] = {}
        with zipfile.ZipFile(self.path) as z:
            for n in z.namelist():
                self.entries[n] = z.read(n)
        if MODEL_ENTRY not in self.entries:
            raise ValueError(f"{self.path.name}: 3D/3dmodel.model mancante")
        self.model = self.entries[MODEL_ENTRY].decode("utf-8")
        self._mesh_cache: dict[tuple[str, str], Mesh] = {}
        self._mesh_dirty: set[str] = set()
        self.ms_root = ET.fromstring(self.entries[MS_ENTRY]) if MS_ENTRY in self.entries else None
        self.settings = json.loads(self.entries[PS_ENTRY].decode("utf-8-sig")) if PS_ENTRY in self.entries else None

    # ---------------------------------------------------------------- salvataggio
    def save(self, path: str | Path | None = None) -> Path:
        out = Path(path or self.path)
        self.entries[MODEL_ENTRY] = self.model.encode("utf-8")
        for entry in self._mesh_dirty:
            self.entries[entry] = self._render_mesh_file(entry).encode("utf-8")
        self._mesh_dirty.clear()
        if self.ms_root is not None:
            self.entries[MS_ENTRY] = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
                                      + ET.tostring(self.ms_root, encoding="utf-8"))
        if self.settings is not None:
            self.entries[PS_ENTRY] = json.dumps(self.settings, indent=4, ensure_ascii=False).encode("utf-8")
        tmp = out.with_suffix(out.suffix + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in self.entries.items():
                z.writestr(n, data)
        tmp.replace(out)
        self.path = out
        return out

    def drop_stale_gcode(self) -> None:
        for n in list(self.entries):
            if re.fullmatch(r"Metadata/plate_\d+\.gcode(\.md5)?", n) or re.fullmatch(r"Metadata/plate_\d+\.json", n):
                del self.entries[n]

    # ---------------------------------------------------------------- oggetti
    def _root_objects(self) -> dict[str, dict]:
        res = {}
        for m in re.finditer(r"<object\b([^>]*)>(.*?)</object>", self.model, re.S):
            a = attrs(m.group(1))
            comps = []
            for c in re.finditer(r"<component\b([^>]*?)/?>", m.group(2)):
                ca = attrs(c.group(1))
                comps.append({"path": ca.get("p:path", "").lstrip("/") or MODEL_ENTRY,
                              "objectid": ca.get("objectid"), "transform": parse_tf(ca.get("transform"))})
            res[a.get("id")] = {"id": a.get("id"), "name_attr": a.get("name"), "components": comps,
                                "inline_mesh": "<mesh" in m.group(2)}
        return res

    def _items(self) -> list[dict]:
        build = re.search(r"<build\b.*?</build>", self.model, re.S)
        out = []
        for m in re.finditer(r"<item\b([^>]*?)/?>", build.group(0) if build else ""):
            a = attrs(m.group(1))
            out.append({"objectid": a.get("objectid"), "transform": parse_tf(a.get("transform")),
                        "printable": a.get("printable", "1") != "0"})
        return out

    def _ms_object(self, oid: str):
        if self.ms_root is None:
            return None
        return next((o for o in self.ms_root.findall("object") if o.get("id") == oid), None)

    @staticmethod
    def _meta(el, key: str) -> str | None:
        if el is None:
            return None
        m = next((x for x in el.findall("metadata") if x.get("key") == key), None)
        return m.get("value") if m is not None else None

    @staticmethod
    def _set_meta(el, key: str, value: str) -> None:
        m = next((x for x in el.findall("metadata") if x.get("key") == key), None)
        if m is None:
            m = ET.SubElement(el, "metadata", {"key": key, "value": value})
        m.set("value", value)

    def objects(self) -> list[dict]:
        roots = self._root_objects()
        plate_of = {oid: p["index"] for p in self.plates() for oid in p["object_ids"]}
        out = []
        for idx, it in enumerate(self._items()):
            ro = roots.get(it["objectid"], {})
            name = self._meta(self._ms_object(it["objectid"]), "name") or ro.get("name_attr") or f"Object_{it['objectid']}"
            out.append({"id": it["objectid"], "name": name, "item_index": idx, "transform": it["transform"],
                        "components": ro.get("components", []), "inline_mesh": ro.get("inline_mesh", False),
                        "plate": plate_of.get(it["objectid"], 1), "printable": it["printable"]})
        return out

    def find(self, name: str) -> dict:
        objs = self.objects()
        for o in objs:
            if o["name"] == name:
                return o
        key = _norm_name(name)
        hits = [o for o in objs if _norm_name(o["name"]) == key]
        if len(hits) == 1:
            return hits[0]
        sug = difflib.get_close_matches(name, [o["name"] for o in objs], n=3, cutoff=0.4)
        raise KeyError(f"Oggetto '{name}' non trovato. Oggetti: {[o['name'] for o in objs]}"
                       + (f". Forse: {sug}" if sug else ""))

    # ---------------------------------------------------------------- mesh
    def _mesh_objects_xml(self, entry: str) -> str:
        return self.model if entry == MODEL_ENTRY else self.entries[entry].decode("utf-8")

    def mesh(self, entry: str, objectid: str) -> Mesh:
        key = (entry, objectid)
        if key not in self._mesh_cache:
            xml = self._mesh_objects_xml(entry)
            m = re.search(rf'<object\b[^>]*\bid="{re.escape(objectid)}"[^>]*>(.*?)</object>', xml, re.S)
            if not m or "<mesh" not in m.group(1):
                raise KeyError(f"mesh {objectid} non trovata in {entry}")
            self._mesh_cache[key] = Mesh.parse(m.group(1))
        return self._mesh_cache[key]

    def set_mesh(self, entry: str, objectid: str, mesh: Mesh) -> None:
        if entry == MODEL_ENTRY:
            raise ValueError("mesh inline nel modello principale: non supportato")
        self._mesh_cache[(entry, objectid)] = mesh
        self._mesh_dirty.add(entry)

    def _render_mesh_file(self, entry: str) -> str:
        xml = self.entries[entry].decode("utf-8")

        def repl(m: re.Match) -> str:
            oid = attrs(m.group(1)).get("id")
            mesh = self._mesh_cache.get((entry, oid))
            if mesh is None:
                return m.group(0)
            return f"<object{m.group(1)}>\n{mesh.to_xml('   ')}  </object>"

        return re.sub(r"<object\b([^>]*)>(.*?)</object>", repl, xml, flags=re.S)

    def object_local_mesh(self, o: dict) -> Mesh:
        """Mesh nel sistema dell'oggetto (componenti applicati, senza la trasformazione dell'item)."""
        if o["inline_mesh"]:
            return self.mesh(MODEL_ENTRY, o["id"])
        return merge_meshes([self.mesh(c["path"], c["objectid"]).transformed(c["transform"]) for c in o["components"]])

    def world_mesh(self, o: dict) -> Mesh:
        return self.object_local_mesh(o).transformed(o["transform"])

    # ---------------------------------------------------------------- trasformazioni
    def set_transform(self, o: dict, tf: list[float]) -> None:
        count = -1

        def repl(m: re.Match) -> str:
            nonlocal count
            count += 1
            if count != o["item_index"]:
                return m.group(0)
            tag = m.group(0)
            new = f'transform="{fmt_tf(tf)}"'
            if 'transform="' in tag:
                return re.sub(r'transform="[^"]*"', new, tag)
            return tag.replace("<item", f"<item {new}", 1)

        build = re.search(r"<build\b.*?</build>", self.model, re.S)
        nb = re.sub(r"<item\b[^>]*?/?>", repl, build.group(0))
        self.model = self.model[:build.start()] + nb + self.model[build.end():]
        o["transform"] = tf

    def drop_to_bed(self, o: dict) -> float:
        zmin = self.world_mesh(o).bbox()[0][2]
        tf = list(o["transform"])
        tf[11] -= zmin
        self.set_transform(o, tf)
        return zmin

    def rotate_world(self, o: dict, rx: float = 0, ry: float = 0, rz: float = 0) -> None:
        R = compose(compose(rot("x", rx), rot("y", ry)), rot("z", rz))
        lo, hi = self.world_mesh(o).bbox()
        c = tuple((lo[i] + hi[i]) / 2 for i in range(3))
        self.set_transform(o, compose(o["transform"], about_point(R, c)))
        self.drop_to_bed(o)

    def translate(self, o: dict, dx: float, dy: float, dz: float = 0) -> None:
        tf = list(o["transform"])
        tf[9] += dx
        tf[10] += dy
        tf[11] += dz
        self.set_transform(o, tf)

    # ---------------------------------------------------------------- piatti
    def bed(self) -> dict:
        ps = self.settings or {}
        try:
            pts = [tuple(float(x) for x in p.split("x")) for p in ps.get("printable_area", [])]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x0, y0, w, d = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        except Exception:
            x0, y0, w, d = 0.0, 0.0, 256.0, 256.0
        try:
            h = float(ps.get("printable_height", "250"))
        except ValueError:
            h = 250.0
        return {"x0": x0, "y0": y0, "w": w, "d": d, "h": h}

    def plates(self) -> list[dict]:
        if self.ms_root is None:
            return [{"index": 1, "name": "", "object_ids": [it["objectid"] for it in self._items()]}]
        out = []
        for i, p in enumerate(self.ms_root.findall("plate")):
            idx = int(self._meta(p, "plater_id") or i + 1)
            ids = [self._meta(mi, "object_id") for mi in p.findall("model_instance")]
            out.append({"index": idx, "name": self._meta(p, "plater_name") or "", "object_ids": ids, "el": p})
        if not out:
            out = [{"index": 1, "name": "", "object_ids": [it["objectid"] for it in self._items()]}]
        return sorted(out, key=lambda p: p["index"])

    @staticmethod
    def plate_cols(count: int) -> int:
        v = math.sqrt(count)
        r = round(v)
        return int(r + 1 if v > r else r)

    def plate_origin(self, index: int, count: int | None = None) -> tuple[float, float]:
        count = count or len(self.plates())
        cols = self.plate_cols(count)
        b = self.bed()
        col, row = (index - 1) % cols, (index - 1) // cols
        return col * b["w"] * (1 + PLATE_GAP), -row * b["d"] * (1 + PLATE_GAP)

    def _relayout(self, old_count: int, new_count: int) -> None:
        if self.plate_cols(old_count) == self.plate_cols(new_count):
            return
        for o in self.objects():
            ox, oy = self.plate_origin(o["plate"], old_count)
            nx, ny = self.plate_origin(o["plate"], new_count)
            if (ox, oy) != (nx, ny):
                self.translate(o, nx - ox, ny - oy)

    def _ensure_ms(self) -> ET.Element:
        if self.ms_root is None:
            self.ms_root = ET.Element("config")
        return self.ms_root

    def add_plate(self, name: str = "") -> int:
        root = self._ensure_ms()
        plates = self.plates()
        old_count = len(root.findall("plate")) or 1
        if not root.findall("plate"):  # progetto senza blocchi <plate>: crea il piatto 1 esplicito
            self._make_plate_el(1, "", [o["id"] for o in self.objects()])
            plates = self.plates()
        idx = max(p["index"] for p in plates) + 1
        self._make_plate_el(idx, name, [])
        self._relayout(old_count, idx)
        self._sync_filament_sequence()
        return idx

    def _make_plate_el(self, idx: int, name: str, object_ids: list[str]) -> ET.Element:
        root = self._ensure_ms()
        first = root.find("plate")
        mode = self._meta(first, "filament_map_mode") if first is not None else None
        el = ET.Element("plate")
        for k, v in (("plater_id", str(idx)), ("plater_name", name), ("locked", "false"),
                     ("filament_map_mode", mode or "Auto For Flush"), ("gcode_file", ""),
                     ("thumbnail_file", ""), ("thumbnail_no_light_file", ""), ("top_file", ""), ("pick_file", "")):
            ET.SubElement(el, "metadata", {"key": k, "value": v})
        for oid in object_ids:
            self._add_instance(el, oid)
        children = list(root)
        last_plate = max((i for i, c in enumerate(children) if c.tag == "plate"), default=None)
        asm = next((i for i, c in enumerate(children) if c.tag == "assemble"), None)
        pos = last_plate + 1 if last_plate is not None else (asm if asm is not None else len(children))
        root.insert(pos, el)
        return el

    def _next_identify_id(self) -> int:
        ids = [int(self._meta(mi, "identify_id") or 0) for p in self._ensure_ms().findall("plate")
               for mi in p.findall("model_instance")]
        return max(ids + [0]) + 4

    def _add_instance(self, plate_el: ET.Element, oid: str) -> None:
        mi = ET.SubElement(plate_el, "model_instance")
        for k, v in (("object_id", oid), ("instance_id", "0"), ("identify_id", str(self._next_identify_id()))):
            ET.SubElement(mi, "metadata", {"key": k, "value": v})

    def rename_plate(self, index: int, name: str) -> None:
        p = next(p for p in self.plates() if p["index"] == index)
        self._set_meta(p["el"], "plater_name", name)

    def assign_to_plate(self, o: dict, index: int, keep_relative: bool = True) -> None:
        plates = self.plates()
        if index not in [p["index"] for p in plates]:
            raise ValueError(f"Piatto {index} inesistente (piatti: {[p['index'] for p in plates]})")
        src = o["plate"]
        for p in plates:
            el = p.get("el")
            if el is None:
                continue
            for mi in list(el.findall("model_instance")):
                if self._meta(mi, "object_id") == o["id"]:
                    el.remove(mi)
        target = next(p for p in self.plates() if p["index"] == index)
        self._add_instance(target["el"], o["id"])
        if keep_relative and src != index:
            sx, sy = self.plate_origin(src)
            tx, ty = self.plate_origin(index)
            self.translate(o, tx - sx, ty - sy)
        o["plate"] = index

    def _sync_filament_sequence(self) -> None:
        try:
            seq = json.loads(self.entries.get(FSEQ_ENTRY, b"{}").decode("utf-8") or "{}")
        except Exception:
            seq = {}
        empty = {"nozzle_sequence": [], "optimal_assignment": [], "sequence": []}
        new = {f"plate_{p['index']}": seq.get(f"plate_{p['index']}", copy.deepcopy(empty)) for p in self.plates()}
        self.entries[FSEQ_ENTRY] = json.dumps(new, separators=(",", ":")).encode("utf-8")

    # ---------------------------------------------------------------- unione progetti
    def merge_from(self, src: "Project", as_new_plate: bool = True, plate_name: str = "",
                   target_plate: int = 1) -> list[dict]:
        """Copia tutti gli oggetti di src (con impostazioni per oggetto) in questo progetto."""
        root_ids = [int(x) for x in re.findall(r'<object\b[^>]*\bid="(\d+)"', self.model)]
        mesh_ids = []
        for n, data in self.entries.items():
            if n.startswith("3D/Objects/") and n.endswith(".model"):
                mesh_ids += [int(x) for x in re.findall(rb'<object\b[^>]*\bid="(\d+)"', data)]
        next_id = max(root_ids + mesh_ids + [0]) + 1
        file_idx = max([int(m) for n in self.entries for m in re.findall(r"3D/Objects/object_(\d+)\.model$", n)] + [0]) + 1
        src_plates = src.plates()
        n_fil = len((self.settings or {}).get("filament_settings_id", ["x"])) or 1
        plate_map: dict[int, int] = {}
        old_count = len(self.plates())
        for sp in src_plates:
            if as_new_plate:
                plate_map[sp["index"]] = self.add_plate(plate_name if len(src_plates) == 1 or not sp["name"]
                                                        else sp["name"])
            else:
                plate_map[sp["index"]] = target_plate
        added = []
        new_files: dict[str, str] = {}
        for o in src.objects():
            new_root = str(next_id)
            next_id += 1
            comp_xml = []
            part_id_map = {}
            for c in o["components"] or [{"path": MODEL_ENTRY, "objectid": o["id"], "transform": list(IDENTITY)}]:
                mesh = src.mesh(c["path"], c["objectid"])
                if c["path"] not in new_files or c["path"] == MODEL_ENTRY:
                    new_files[c["path"]] = f"3D/Objects/object_{file_idx}.model"
                    file_idx += 1
                entry = new_files[c["path"]]
                new_mesh_id = str(next_id)
                next_id += 1
                part_id_map[c["objectid"]] = new_mesh_id
                self._append_mesh_object(entry, new_mesh_id, mesh)
                comp_xml.append(f'    <component p:path="/{entry}" objectid="{new_mesh_id}" p:UUID="{_new_uuid()}" '
                                f'transform="{fmt_tf(c["transform"])}"/>\n')
            obj_xml = (f'  <object id="{new_root}" p:UUID="{_new_uuid()}" type="model">\n   <components>\n'
                       + "".join(comp_xml) + "   </components>\n  </object>\n")
            self.model = self.model.replace("</resources>", obj_xml + " </resources>", 1)
            sx, sy = src.plate_origin(o["plate"])
            dst_plate = plate_map.get(o["plate"], target_plate)
            tx, ty = self.plate_origin(dst_plate)
            tf = list(o["transform"])
            tf[9] += tx - sx
            tf[10] += ty - sy
            item = f'  <item objectid="{new_root}" p:UUID="{_new_uuid()}" transform="{fmt_tf(tf)}" printable="1"/>\n'
            self.model = re.sub(r"(\s*)</build>", lambda m: "\n" + item + " </build>", self.model, count=1)
            # impostazioni per oggetto
            so = src._ms_object(o["id"])
            el = copy.deepcopy(so) if so is not None else ET.Element("object")
            el.set("id", new_root)
            if so is None:
                ET.SubElement(el, "metadata", {"key": "name", "value": o["name"]})
            for part in el.findall("part"):
                if part.get("id") in part_id_map:
                    part.set("id", part_id_map[part.get("id")])
            ext = self._meta(el, "extruder")
            if ext and ext.isdigit() and int(ext) > n_fil:
                self._set_meta(el, "extruder", "1")
            root = self._ensure_ms()
            first_plate = next((i for i, c in enumerate(list(root)) if c.tag == "plate"), len(list(root)))
            root.insert(first_plate, el)
            plate_el = next(p["el"] for p in self.plates() if p["index"] == dst_plate)
            self._add_instance(plate_el, new_root)
            added.append({"name": o["name"], "plate": dst_plate, "id": new_root})
        self._relayout(old_count, len(self.plates()))
        self._sync_filament_sequence()
        return added

    def _append_mesh_object(self, entry: str, oid: str, mesh: Mesh) -> None:
        if entry not in self.entries:
            self.entries[entry] = (
                '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
                'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
                'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
                ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n </resources>\n <build/>\n</model>\n'
            ).encode("utf-8")
            self._add_rel(entry)
        xml = self.entries[entry].decode("utf-8")
        obj = f'  <object id="{oid}" p:UUID="{_new_uuid()}" type="model">\n{mesh.to_xml("   ")}  </object>\n'
        self.entries[entry] = xml.replace(" </resources>", obj + " </resources>", 1).encode("utf-8")
        self._mesh_cache[(entry, oid)] = mesh

    def _add_rel(self, entry: str) -> None:
        rels = self.entries.get(RELS_ENTRY, (
            b'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships '
            b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n</Relationships>\n')).decode("utf-8")
        n = len(re.findall(r"<Relationship\b", rels)) + 1
        rel = (f' <Relationship Target="/{entry}" Id="rel-{n}" '
               f'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n')
        self.entries[RELS_ENTRY] = rels.replace("</Relationships>", rel + "</Relationships>", 1).encode("utf-8")

    # ---------------------------------------------------------------- impronte / controlli
    def footprint(self, o: dict) -> dict:
        wm = self.world_mesh(o)
        ox, oy = self.plate_origin(o["plate"])
        hull = convex_hull([(x - ox, y - oy) for x, y, _ in wm.verts])
        lo, hi = wm.bbox()
        return {"hull": hull, "bbox": [round(lo[0] - ox, 2), round(lo[1] - oy, 2), round(lo[2], 2),
                                       round(hi[0] - ox, 2), round(hi[1] - oy, 2), round(hi[2], 2)]}

    def layout_check(self, min_gap: float = 0.0) -> dict:
        b = self.bed()
        problems, per_plate = [], {}
        objs = [o for o in self.objects() if o["printable"]]
        fps = {o["id"]: self.footprint(o) for o in objs}
        for o in objs:
            per_plate.setdefault(o["plate"], []).append(o)
        for plate, lst in per_plate.items():
            for o in lst:
                bb = fps[o["id"]]["bbox"]
                if bb[0] < b["x0"] - 0.05 or bb[1] < b["y0"] - 0.05 or bb[3] > b["x0"] + b["w"] + 0.05 \
                        or bb[4] > b["y0"] + b["d"] + 0.05:
                    problems.append({"type": "outside_plate", "plate": plate, "objects": [o["name"]],
                                     "message": f"{o['name']} esce dal piatto {plate} (ingombro XY {bb[0]}..{bb[3]} x "
                                                f"{bb[1]}..{bb[4]} mm, piatto {b['w']:.0f}x{b['d']:.0f})"})
                if bb[5] > b["h"] + 0.05:
                    problems.append({"type": "too_tall", "plate": plate, "objects": [o["name"]],
                                     "message": f"{o['name']} e' alto {bb[5]} mm (massimo {b['h']:.0f})"})
                if abs(bb[2]) > 0.02:
                    problems.append({"type": "not_on_bed", "plate": plate, "objects": [o["name"]],
                                     "message": f"{o['name']} non appoggia sul piatto (z minima {bb[2]} mm)"})
            for i in range(len(lst)):
                for j in range(i + 1, len(lst)):
                    a, c = lst[i], lst[j]
                    if hulls_overlap(fps[a["id"]]["hull"], fps[c["id"]]["hull"], min_gap):
                        ba, bc = fps[a["id"]]["bbox"], fps[c["id"]]["bbox"]
                        problems.append({"type": "overlap", "plate": plate, "objects": [a["name"], c["name"]],
                                         "message": f"{a['name']} e {c['name']} si sovrappongono sul piatto {plate} "
                                                    f"(ingombri X {ba[0]}..{ba[3]} Y {ba[1]}..{ba[4]} e "
                                                    f"X {bc[0]}..{bc[3]} Y {bc[1]}..{bc[4]} mm)"})
        return {"ok": not problems, "problems": problems}

    def arrange(self, plate: int | None = None, spacing: float = 6.0) -> dict:
        b = self.bed()
        res = {"arranged": [], "not_fitting": []}
        plates = [plate] if plate else sorted({o["plate"] for o in self.objects()})
        for pl in plates:
            objs = [o for o in self.objects() if o["plate"] == pl and o["printable"]]
            if not objs:
                continue
            boxes = []
            for o in objs:
                bb = self.footprint(o)["bbox"]
                boxes.append((o, bb, bb[3] - bb[0], bb[4] - bb[1]))
            best = None
            for along_x in (True, False):
                layout = _shelf_pack(boxes, b["w"], b["d"], spacing, along_x)
                if best is None or len(layout["placed"]) > len(best["placed"]) or \
                        (len(layout["placed"]) == len(best["placed"]) and layout["area"] < best["area"]):
                    best = layout
            # centra il gruppo sul piatto
            if best["placed"]:
                xs = [p[1] for p in best["placed"]] + [p[1] + p[3] for p in best["placed"]]
                ys = [p[2] for p in best["placed"]] + [p[2] + p[4] for p in best["placed"]]
                cx = b["x0"] + (b["w"] - (max(xs) - min(xs))) / 2 - min(xs)
                cy = b["y0"] + (b["d"] - (max(ys) - min(ys))) / 2 - min(ys)
                for o, x, y, w, d, bb in best["placed"]:
                    self.translate(o, x + cx - bb[0], y + cy - bb[1])
                    res["arranged"].append({"name": o["name"], "plate": pl})
            res["not_fitting"] += [{"name": o["name"], "plate": pl} for o in best["rejected"]]
        return res

    # ---------------------------------------------------------------- riepilogo
    def summary(self) -> dict:
        plates = self.plates()
        out = {"bed": self.bed(), "plates": []}
        for p in plates:
            ox, oy = self.plate_origin(p["index"], len(plates))
            objs = []
            for o in self.objects():
                if o["plate"] != p["index"]:
                    continue
                fp = self.footprint(o)
                objs.append({"name": o["name"], "bbox_on_plate": fp["bbox"],
                             "size_mm": [round(fp["bbox"][3] - fp["bbox"][0], 2), round(fp["bbox"][4] - fp["bbox"][1], 2),
                                         round(fp["bbox"][5] - fp["bbox"][2], 2)],
                             "rotation_z_deg": rotation_deg_z(o["transform"]),
                             "triangles": sum(len(self.mesh(c["path"], c["objectid"]).tris) for c in o["components"])
                             if o["components"] else None})
            out["plates"].append({"index": p["index"], "name": p["name"], "origin": [ox, oy], "objects": objs})
        return out


def _norm_name(s: str) -> str:
    return re.sub(r"[\s_\-.]+", "", s or "").lower()


def _shelf_pack(boxes, W, D, gap, along_x):
    """Disposizione a ripiani. along_x: i ripiani crescono lungo X (righe), altrimenti lungo Y."""
    U, Vmax = (W, D) if along_x else (D, W)
    dims = [(o, bb, (w, d) if along_x else (d, w)) for o, bb, w, d in boxes]
    order = sorted(dims, key=lambda t: -t[2][1])
    placed, rejected = [], []
    u = v = shelf = 0.0
    for o, bb, (bu, bv) in order:
        if bu > U or bv > Vmax:
            rejected.append(o)
            continue
        if u > 0 and u + bu > U:
            u, v, shelf = 0.0, v + shelf + gap, 0.0
        if v + bv > Vmax:
            rejected.append(o)
            continue
        x, y = (u, v) if along_x else (v, u)
        w, d = (bu, bv) if along_x else (bv, bu)
        placed.append((o, x, y, w, d, bb))
        u += bu + gap
        shelf = max(shelf, bv)
    area = 0.0
    if placed:
        area = (max(p[1] + p[3] for p in placed) - min(p[1] for p in placed)) * \
               (max(p[2] + p[4] for p in placed) - min(p[2] for p in placed))
    return {"placed": placed, "rejected": rejected, "area": area}


# ======================================================================================
# Sorgenti CAD
# ======================================================================================
def cad_objects(path: str | Path) -> dict[str, Mesh]:
    """Oggetti di un .3mf (grezzo da CAD o progetto Bambu) in coordinate del CAD/piatto: {nome: Mesh}."""
    p = Project(path)
    out: dict[str, Mesh] = {}
    roots = p._root_objects()
    for o in p.objects():
        if o["inline_mesh"]:
            m = p.mesh(MODEL_ENTRY, o["id"])
        elif o["components"]:
            meshes = []
            for c in o["components"]:
                ro = roots.get(c["objectid"]) if c["path"] == MODEL_ENTRY else None
                if ro and ro["components"]:  # componenti annidati nel modello principale
                    for cc in ro["components"]:
                        meshes.append(p.mesh(cc["path"], cc["objectid"]).transformed(compose(cc["transform"], c["transform"])))
                else:
                    meshes.append(p.mesh(c["path"], c["objectid"]).transformed(c["transform"]))
            m = merge_meshes(meshes)
        else:
            continue
        name = o["name"]
        if p.ms_root is None and roots.get(o["id"], {}).get("name_attr"):
            name = roots[o["id"]]["name_attr"]
        base, k = name, 2
        while name in out:
            name = f"{base}_{k}"
            k += 1
        out[name] = m.transformed(o["transform"])
    return out


def registration_offset(old: Mesh, new: Mesh) -> tuple[tuple[float, float, float], int]:
    """Traslazione da applicare a 'new' per far coincidere le parti invariate con 'old'.
    Prova, per ogni asse, l'allineamento sul minimo, sul massimo e sul centro e sceglie
    la combinazione con piu' vertici coincidenti."""
    olo, ohi = old.bbox()
    nlo, nhi = new.bbox()
    cand = [[olo[i] - nlo[i], ohi[i] - nhi[i], (olo[i] + ohi[i]) / 2 - (nlo[i] + nhi[i]) / 2] for i in range(3)]
    oset = {(round(x, 2), round(y, 2), round(z, 2)) for x, y, z in old.verts}
    sample = new.verts if len(new.verts) <= 20000 else new.verts[:: len(new.verts) // 20000 + 1]
    best, best_n = (cand[0][2], cand[1][2], cand[2][2]), -1
    for dx in cand[0]:
        for dy in cand[1]:
            for dz in cand[2]:
                n = sum((round(x + dx, 2), round(y + dy, 2), round(z + dz, 2)) in oset for x, y, z in sample)
                if n > best_n:
                    best, best_n = (dx, dy, dz), n
    return best, best_n


# ======================================================================================
# Analisi degli sbalzi per oggetto
# ======================================================================================
def overhang_regions(mesh: Mesh, critical_deg: float = 30.0, warning_deg: float = 45.0,
                     horizontal_deg: float = 5.0, origin: tuple = (0.0, 0.0)) -> list[dict]:
    """Classifica le facce rivolte verso il basso (mesh in coordinate del piatto):
    'ponte' = soffitto orizzontale con appoggi su due lati opposti (span_mm = luce libera),
    'sbalzo' = soffitto orizzontale senza appoggi opposti, 'inclinata' = faccia inclinata oltre soglia."""
    V, T = mesh.verts, mesh.tris
    if not T:
        return []
    zmin = min(v[2] for v in V)
    cos_h = math.cos(math.radians(horizontal_deg))
    cos_c = math.cos(math.radians(critical_deg))
    cos_w = math.cos(math.radians(warning_deg))
    info = []
    for (a, b, c) in T:
        pa, pb, pc = V[a], V[b], V[c]
        u = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        w = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        n = _cross(u, w)
        ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        if ln == 0:
            info.append(None)
            continue
        down = -n[2] / ln
        on_bed = max(pa[2], pb[2], pc[2]) <= zmin + 0.3
        if down < cos_w or on_bed:
            info.append(None)
            continue
        kind = "horizontal" if down >= cos_h else ("inclined_critical" if down >= cos_c else "inclined")
        info.append((kind, ln / 2))
    edge_tris: dict[tuple[int, int], list[int]] = {}
    for ti, (a, b, c) in enumerate(T):
        for e in ((a, b), (b, c), (c, a)):
            edge_tris.setdefault((min(e), max(e)), []).append(ti)
    # regioni connesse per tipo
    parent = list(range(len(T)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e, ts in edge_tris.items():
        sel = [t for t in ts if info[t] is not None]
        for i in range(1, len(sel)):
            if info[sel[i]][0] == info[sel[0]][0]:
                ra, rb = find(sel[0]), find(sel[i])
                if ra != rb:
                    parent[ra] = rb
    regions: dict[int, list[int]] = {}
    for t, inf in enumerate(info):
        if inf is not None:
            regions.setdefault(find(t), []).append(t)
    out = []
    for tris in regions.values():
        kind = info[tris[0]][0]
        area = sum(info[t][1] for t in tris)
        if area < 0.5:
            continue
        pts = [V[i] for t in tris for i in T[t]]
        xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
        reg = {"z": round(min(zs) - zmin, 2), "area_mm2": round(area, 1),
               "xy_bbox": [round(min(xs) - origin[0], 1), round(min(ys) - origin[1], 1),
                           round(max(xs) - origin[0], 1), round(max(ys) - origin[1], 1)]}
        if kind == "horizontal":
            span = _bridge_span(tris, T, V, edge_tris, info)
            reg["tipo"] = "ponte" if span is not None else "sbalzo"
            reg["span_mm"] = round(span, 1) if span is not None else None
        else:
            reg["tipo"] = "inclinata"
            reg["critica"] = kind == "inclined_critical"
            reg["span_mm"] = None
        out.append(reg)
    return sorted(out, key=lambda r: -r["area_mm2"])


def _bridge_span(tris, T, V, edge_tris, info) -> float | None:
    """Luce minima tra appoggi opposti; None se non ci sono appoggi su due lati opposti."""
    tset = set(tris)
    anchors = []
    for t in tris:
        a, b, c = T[t]
        for e in ((a, b), (b, c), (c, a)):
            key = (min(e), max(e))
            others = [x for x in edge_tris.get(key, []) if x not in tset]
            if not others:
                continue
            ez = (V[e[0]][2] + V[e[1]][2]) / 2
            for ot in others:
                third = [i for i in T[ot] if i not in e]
                if third and V[third[0]][2] < ez - 0.05:  # parete che scende: appoggio
                    anchors.append(((V[e[0]][0] + V[e[1]][0]) / 2, (V[e[0]][1] + V[e[1]][1]) / 2))
                    anchors.append((V[e[0]][0], V[e[0]][1]))
                    anchors.append((V[e[1]][0], V[e[1]][1]))
                    break
    if len(anchors) < 2:
        return None
    pts = [(V[i][0], V[i][1]) for t in tris for i in T[t]]
    best = None
    for k in range(12):
        ang = math.pi * k / 12
        dx, dy = math.cos(ang), math.sin(ang)
        proj = [x * dx + y * dy for x, y in pts]
        lo, hi = min(proj), max(proj)
        extent = hi - lo
        if extent < 0.2:
            continue
        tol = max(0.3, 0.05 * extent)
        ap = [x * dx + y * dy for x, y in anchors]
        if any(p <= lo + tol for p in ap) and any(p >= hi - tol for p in ap):
            best = extent if best is None else min(best, extent)
    return best


# ======================================================================================
# Cucitura dal G-code
# ======================================================================================
_G1_RE = re.compile(r"^G[01]\b(.*)$")
_AX_RE = re.compile(r"([XYZEF])(-?\d*\.?\d+)")


def seam_points(gcode_lines) -> list[tuple[float, float, float]]:
    """Punto iniziale di ogni giro di parete esterna (; FEATURE: Outer wall)."""
    x = y = z = 0.0
    in_outer = False
    waiting = False
    pts = []
    for raw in gcode_lines:
        line = raw.strip()
        if line.startswith(";"):
            if line.startswith("; FEATURE:"):
                in_outer = "outer wall" in line.lower()
                waiting = in_outer
            elif line.startswith("; Z_HEIGHT:"):
                try:
                    z = float(line.split(":", 1)[1])
                except ValueError:
                    pass
            continue
        m = _G1_RE.match(line)
        if not m:
            continue
        ax = dict(_AX_RE.findall(m.group(1).split(";")[0]))
        extruding = "E" in ax and float(ax["E"]) > 0 and ("X" in ax or "Y" in ax)
        if in_outer and waiting and extruding:
            pts.append((x, y, z))
            waiting = False
        if "X" in ax:
            x = float(ax["X"])
        if "Y" in ax:
            y = float(ax["Y"])
        if "Z" in ax:
            z = float(ax["Z"])
        if in_outer and not extruding and ("X" in ax or "Y" in ax):
            waiting = True  # nuovo giro dopo uno spostamento
    return pts


def _tri_box_overlap(a: tuple, b: tuple, c: tuple, box: list[float]) -> bool:
    """Test di intersezione triangolo / box allineato agli assi (Separating Axis Theorem, 13 assi)."""
    cen = [(box[i] + box[i + 3]) / 2 for i in range(3)]
    h = [(box[i + 3] - box[i]) / 2 for i in range(3)]
    v = [[p[i] - cen[i] for i in range(3)] for p in (a, b, c)]
    e = [[v[1][i] - v[0][i] for i in range(3)], [v[2][i] - v[1][i] for i in range(3)],
         [v[0][i] - v[2][i] for i in range(3)]]
    axes = [(1, 0, 0), (0, 1, 0), (0, 0, 1), _cross(e[0], e[1])]
    for ei in e:
        for u in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            axes.append(_cross(u, ei))
    for ax in axes:
        if abs(ax[0]) + abs(ax[1]) + abs(ax[2]) < 1e-12:
            continue
        p = [vv[0] * ax[0] + vv[1] * ax[1] + vv[2] * ax[2] for vv in v]
        r = h[0] * abs(ax[0]) + h[1] * abs(ax[1]) + h[2] * abs(ax[2])
        if min(p) > r or max(p) < -r:
            return False
    return True


def paint_seam(mesh_local: Mesh, world_tf: list[float], box: list[float], mode: str) -> int:
    """Vernicia la cucitura su tutti i triangoli che intersecano il box (coordinate mondo)
    [xmin, ymin, zmin, xmax, ymax, zmax]. mode: 'enforce' | 'block' | 'clear'. Restituisce il numero di triangoli.
    La vernice copre triangoli interi: sulle mesh CAD (pochi triangoli grandi) l'area marcata puo' essere
    piu' ampia del box."""
    code = {"enforce": "4", "block": "8", "clear": None}[mode]
    n = 0
    for i, (a, b, c) in enumerate(mesh_local.tris):
        pa, pb, pc = (apply(world_tf, mesh_local.verts[k]) for k in (a, b, c))
        if _tri_box_overlap(pa, pb, pc, box):
            ex = re.sub(r'\s*paint_seam="[^"]*"', "", mesh_local.tri_extra[i]).strip()
            if code:
                ex = (ex + f' paint_seam="{code}"').strip()
            mesh_local.tri_extra[i] = ex
            n += 1
    return n
