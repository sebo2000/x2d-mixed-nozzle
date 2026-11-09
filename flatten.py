#!/usr/bin/env python3
"""Flatten a BBL system preset chain (inherits + include) plus user overrides into one full JSON for the CLI.

usage: flatten.py <profiles/BBL dir> <kind: machine|process|filament> <system name> <out.json> [override.json]
"""
import json
import sys
from pathlib import Path

base, kind, name, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
override = json.loads(Path(sys.argv[5]).read_text()) if len(sys.argv) > 5 else {}

index = {}
for p in (base / kind).rglob("*.json"):
    try:
        d = json.loads(p.read_text())
    except Exception:
        continue
    if "name" in d:
        index[d["name"]] = d


def resolve(n):
    d = index[n]
    merged = resolve(d["inherits"]) if d.get("inherits") else {}
    for inc in d.get("include", []):
        merged.update({k: v for k, v in index[inc].items() if k not in ("name", "type", "from", "instantiation")})
    merged.update(d)
    return merged


cfg = resolve(name)
for k in ("include", "inherits", "instantiation"):
    cfg.pop(k, None)
cfg.update({k: v for k, v in override.items() if k not in ("inherits", "from", "version", "name", "printer_settings_id", "filament_settings_id")})
cfg["from"] = "system"
cfg["type"] = kind
cfg.setdefault("version", "02.09.01.70")
if kind == "machine":
    cfg["printer_settings_id"] = cfg["name"]
elif kind == "process":
    cfg["print_settings_id"] = cfg["name"]
else:
    cfg["filament_settings_id"] = [cfg["name"]]
out.write_text(json.dumps(cfg, indent=1))
print("wrote", out, len(cfg), "keys")
