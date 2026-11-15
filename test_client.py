"""Test end-to-end del server MCP via stdio.
Uso: venv\\Scripts\\python.exe test_client.py <file.3mf> [--no-gui] [--rotate GRADI]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--rotate", type=float, default=0.0)
    a = ap.parse_args()
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "bambu_live_mcp.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            res = await s.call_tool("inspect_3mf_settings", {"threemf_path": a.file, "filter_keyword": "seam,wall_loops"})
            print("INSPECT:", res.content[0].text[:1500])
            if a.no_gui:
                res = await s.call_tool("apply_settings_and_preview", {
                    "threemf_path": a.file, "settings_patch": {"wall_loops": 3},
                    "rotate_z_deg": a.rotate, "run_slice": True, "reload_gui": False})
            else:
                res = await s.call_tool("apply_default_cad_preset", {"threemf_path": a.file, "rotate_z_deg": a.rotate})
            txt = res.content[0].text
            try:
                d = json.loads(txt)
                d.get("patch", {}).pop("changed", None)
                print("RESULT:", json.dumps({k: d.get(k) for k in (
                    "ok", "project_file", "sliced_file", "backup", "rotation", "preset_notes",
                    "overhang_analysis", "gui")}, indent=1, ensure_ascii=False))
                sl = d.get("slice", {})
                print("SLICE:", json.dumps({k: sl.get(k) for k in (
                    "ok", "cli_message", "plates", "gcode_header", "slicer_warnings")}, indent=1, ensure_ascii=False))
            except Exception:
                print("RESULT RAW:", txt[:4000])


asyncio.run(main())
