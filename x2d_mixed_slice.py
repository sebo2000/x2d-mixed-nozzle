"""Slice a Bambu Studio project for an X2D with a different nozzle diameter on each side.

usage:
  python3 -I x2d_mixed_slice.py <project.3mf> --filaments 0.4,0.2[,...] [--main 0.2] [--aux 0.4]
                                [--layer-height H] [--first-layer H] [--prime-tower keep|on|off]
                                [--plate N] [--out file.gcode.3mf] [--work dir] [--cli path] [--profiles dir]

Prepare the project in Bambu Studio as usual on an X2D printer preset: models, filament per object/part,
painting, a filament change at a layer (layer slider), "Sparse infill filament", support filaments.
Save it. Then run this script with the nozzle diameter of each filament slot, in slot order. Each
filament prints only with the hotend of its diameter, so a filament slot is tied to one side.

The project is re-sliced with the installed BambuStudio CLI in multi-nozzle-diameter mode (Bambu Studio's
GUI does not offer the mode on the X2D). On the way the script:
  - moves a project made on another stock X2D variant onto "Bambu Lab X2D 0.4 nozzle" and its 0.20mm
    process (the mode slices against the 0.4 variant), keeping the project's prime tower setting;
  - turns filament changes at a layer (layer slider ticks, which the mode ignores) into height-range
    modifiers with the same filament from that layer up;
  - keeps the project's first layer height (the mode would change it) unless --first-layer is given;
  - reloads each filament from the system preset for its diameter
    (e.g. "Bambu PLA Basic @BBL X2D 0.2 nozzle"); changes to a filament in the project are listed, not kept.
The output is a sliced-only 3MF with the two send workarounds described in README.md:
no model inside (opens as a sliced file, no re-slice prompt) and the plate nozzle_diameters set to
"MAIN,AUX" with the mode flag off (so Bambu Studio's Print sends the right diameters). The script then
checks every used filament: right side, line widths that fit its nozzle. A project saved by an older
Bambu Studio can fail that check when it has layer filament changes; open and save it in the current one. Open the output in Bambu Studio
(File > Open Project) and press Print.

Layer heights: the layer grid follows the 0.2 nozzle, which prints half the project's layer height; a 0.4
prints the project's layer height. --layer-height sets the project's layer height before slicing, e.g.
0.16 for 0.08 mm layers on the 0.2.
"""
import argparse
import collections
import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent.parent
CLI_CANDIDATES = ["/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
                  "/Applications/BambuStudio 2.app/Contents/MacOS/BambuStudio",
                  r"C:\Program Files\Bambu Studio\bambu-studio.exe",
                  shutil.which("bambu-studio") or ""]
DEFAULT_CLI = next((c for c in CLI_CANDIDATES if c and Path(c).is_file()), None)
PROFILES = None  # set in main(): the installed Bambu Studio's BBL profiles, else this repository's


def find_profiles(cli):
    """The BBL system presets that ship with the CLI (same version), else this repository's copy."""
    exe = Path(cli).resolve()
    for p in (exe.parent.parent / "Resources" / "profiles" / "BBL",  # macOS app bundle
              exe.parent / "resources" / "profiles" / "BBL",           # Windows / Linux
              REPO / "resources" / "profiles" / "BBL"):
        if (p / "filament").is_dir() and (p / "machine").is_dir():
            return p
    die("no BBL profiles found; pass --profiles <folder with machine/, process/, filament/>")
SUFFIX = re.compile(r" \d\.\d nozzle$")
TICKS_FILE = "Metadata/custom_gcode_per_layer.xml"
RANGES_FILE = "Metadata/layer_config_ranges.xml"
TOOL_CHANGE = "2"  # CustomGCode::Type::ToolChange (CustomGCode.hpp)
RANGE_TOP = 1000.0  # a range end above any object
print = functools.partial(print, flush=True)  # keep our lines in order with the helper scripts' output


def fmt(d):
    return f"{float(d):.1f}"


def num(v):
    return f"{float(v):.3f}".rstrip("0").rstrip(".")


def die(msg):
    sys.exit("error: " + msg)


def flatten(kind, name, target, override=None):
    cmd = [sys.executable, "-I", str(TOOLS / "flatten.py"), str(PROFILES), kind, name, str(target)]
    if override:
        cmd.append(str(override))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return json.loads(Path(target).read_text())


def filament_index():
    index = {}
    for p in (PROFILES / "filament").rglob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if "name" in d:
            index[d["name"]] = d
    return index


def resolved_compatible_printers(index, name):
    while name:
        d = index[name]
        if "compatible_printers" in d:
            return d["compatible_printers"]
        name = d.get("inherits", "")
    return []


def pick_system_filament(index, system_name, diameter):
    base = SUFFIX.sub("", system_name)
    target = f"Bambu Lab X2D {diameter} nozzle"
    for candidate in (f"{base} {diameter} nozzle", base):
        if candidate in index and target in resolved_compatible_printers(index, candidate):
            return candidate
    return None


REBASE_PRINTER = "Bambu Lab X2D 0.4 nozzle"
REBASE_PROCESS = "0.20mm Standard @BBL X2D"
REBASE_SKIP = {"name", "type", "from", "version", "setting_id", "printer_settings_id", "print_settings_id",
               "inherits", "instantiation", "compatible_printers", "compatible_printers_condition"}


def rebase_to_x2d_04(settings, work):
    """Move a project made on another X2D variant (e.g. 0.2 nozzle) onto the 0.4 printer and process.

    The mode slices against the 0.4 variant. Only a stock system printer can be swapped automatically,
    because a user printer preset (custom G-code) would be lost. The process is replaced by the stock
    0.20mm Standard; per-object settings in the model are kept. Done here and not with the CLI's own
    printer switch, which failed on a painted test project without a reason in the log.
    """
    printer = settings.get("printer_settings_id", "")
    groups = settings.get("inherits_group") or []
    diffs = settings.get("different_settings_to_system") or []
    if not re.fullmatch(r"Bambu Lab X2D \d\.\d nozzle", printer) or (groups and groups[-1]) or (diffs and diffs[-1]):
        die(f"the project uses the printer preset {printer!r} (X2D {settings.get('printer_variant')} nozzle). "
            f"Select '{REBASE_PRINTER}' or a user preset based on it, with a 0.4 process, and save again")
    process = settings.get("print_settings_id", "")
    print(f"note: project made on {printer!r} / {process!r}; switching to {REBASE_PRINTER!r} / {REBASE_PROCESS!r}")
    if diffs and diffs[0]:
        print(f"note: process changes not carried over: {diffs[0]}")
    for kind, name in (("machine", REBASE_PRINTER), ("process", REBASE_PROCESS)):
        for key, value in flatten(kind, name, work / f"rebase_{kind}.json").items():
            if key not in REBASE_SKIP:
                settings[key] = value
    settings["printer_settings_id"] = REBASE_PRINTER
    settings["print_settings_id"] = REBASE_PROCESS
    settings["print_compatible_printers"] = [REBASE_PRINTER]


# ---- filament changes at a layer -> height ranges -------------------------------------------------------

def model_object_ids(model_xml):
    """Object ids of 3D/3dmodel.model in file order; RANGES_FILE numbers objects 1.. in this order."""
    root = ET.fromstring(model_xml)
    return [o.get("id") for o in root.iter() if o.tag.endswith("}object") or o.tag == "object"]


def plate_objects(model_settings):
    """{plate id: set of object ids} and {object id: own layer height or None} from model_settings.config."""
    root = ET.fromstring(model_settings)
    plates = {}
    for plate in root.findall("plate"):
        pid = next((m.get("value") for m in plate.findall("metadata") if m.get("key") == "plater_id"), None)
        plates[pid] = {m.get("value") for inst in plate.findall("model_instance")
                       for m in inst.findall("metadata") if m.get("key") == "object_id"}
    heights = {}
    for obj in root.findall("object"):
        lh = next((m.get("value") for m in obj.findall("metadata") if m.get("key") == "layer_height"), None)
        heights[obj.get("id")] = lh
    return plates, heights


def ticks_to_ranges(files, settings):
    """Replace layer-slider filament changes with height ranges. Returns {name: new bytes} for the archive.

    A tick at Z changes the filament after the layer that ends at Z, so the range starts at Z and runs to
    the next tick. Each range carries the object's layer height: a range without one crashes the CLI, and
    one with another height puts the coarse nozzle on the fine grid.
    """
    if TICKS_FILE not in files:
        return {}
    ticks_root = ET.fromstring(files[TICKS_FILE])
    per_plate = {}
    for plate in ticks_root.findall("plate"):
        info = plate.find("plate_info")
        changes = sorted((float(t.get("top_z")), t.get("extruder")) for t in plate.findall("layer")
                         if t.get("type") == TOOL_CHANGE)
        if changes:
            mode = plate.find("mode")
            if mode is not None and mode.get("value") not in ("MultiAsSingle", "SingleExtruder"):
                die(f"plate {info.get('id')}: layer filament changes in mode {mode.get('value')!r} are not supported")
            per_plate[info.get("id")] = changes
    if not per_plate:
        return {}

    order = model_object_ids(files["3D/3dmodel.model"])
    plates, own_heights = plate_objects(files["Metadata/model_settings.config"])
    existing = ET.fromstring(files[RANGES_FILE]) if RANGES_FILE in files else ET.Element("objects")
    taken = {o.get("id") for o in existing.findall("object")}
    by_object = {}
    for pid, changes in per_plate.items():
        for oid in plates.get(pid, ()):
            if oid in by_object and by_object[oid] != changes:
                die(f"object {oid} is on plates with different layer filament changes")
            by_object[oid] = changes
    for oid, changes in sorted(by_object.items()):
        if oid not in order:
            die(f"object {oid} of model_settings.config not found in 3D/3dmodel.model")
        index = str(order.index(oid) + 1)
        if index in taken:
            die(f"object {oid} has both height ranges and layer filament changes; use one of them")
        lh = own_heights.get(oid) or settings["layer_height"]
        obj = ET.SubElement(existing, "object", id=index)
        for (z, extruder), nxt in zip(changes, changes[1:] + [(RANGE_TOP, None)]):
            rng = ET.SubElement(obj, "range", min_z=num(z), max_z=num(nxt[0]))
            ET.SubElement(rng, "option", opt_key="extruder").text = extruder
            ET.SubElement(rng, "option", opt_key="layer_height").text = num(lh)
        steps = ", ".join(f"filament {e} above {num(z)} mm" for z, e in changes)
        print(f"layer filament changes -> height ranges, object {oid}: {steps} (layer height {num(lh)})")
    for obj in existing.findall("object"):
        for rng in obj.findall("range"):
            if not any(o.get("opt_key") == "layer_height" for o in rng.findall("option")):
                die(f"a height range of object #{obj.get('id')} has no layer height; set one in Bambu Studio")

    text = files[TICKS_FILE].decode()
    text = re.sub(r'\s*<layer [^>]*type="' + TOOL_CHANGE + r'"[^>]*/>', "", text)
    ET.indent(existing, " ")
    return {TICKS_FILE: text.encode(),
            RANGES_FILE: b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(existing) + b"\n"}


# ---- G-code checks --------------------------------------------------------------------------------------

def analyse_gcode(text):
    """Per tool: line widths (counted per segment) and printed Zs; plus the first layer Z and tool order."""
    tool, z, width = None, None, None
    widths = collections.defaultdict(collections.Counter)
    zs = collections.defaultdict(set)
    order = []
    for line in text.splitlines():
        if line.startswith("T"):
            m = re.match(r"T(\d+)\b", line)
            if m and int(m.group(1)) < 100:
                tool = int(m.group(1))
                order.append(tool)
        elif line.startswith(";"):
            m = re.match(r";\s*Z_HEIGHT:\s*([\d.]+)", line)
            if m:
                z = round(float(m.group(1)), 3)
            m = re.match(r";\s*LINE_WIDTH:\s*([\d.]+)", line)
            if m:
                width = round(float(m.group(1)), 3)
        elif line.startswith("G1 ") and " E" in line and z is not None:
            e = re.search(r" E([\d.]+)", line)
            if e and float(e.group(1)) > 0 and width:
                widths[tool][width] += 1
                zs[tool].add(z)
    first = min((min(v) for v in zs.values() if v), default=None)
    return widths, zs, first, order


def layer_step(zs):
    v = sorted(zs)
    steps = collections.Counter(round(b - a, 3) for a, b in zip(v, v[1:]))
    return steps.most_common(1)[0][0] if steps else None


def check_plate(name, text, used, wanted, main_d, aux_d):
    """Print side, width and layers per used filament; return the problems found."""
    problems = []
    fmap = re.search(r"^; filament_map = (.*)$", text, re.M)
    sides = [int(x) for x in fmap.group(1).split(",")] if fmap else []
    widths, zs, first, order = analyse_gcode(text)
    # The first filament is loaded by the start G-code, without a T command.
    if None in widths:
        rest = [f for f in used if not order or f != order[0]]
        start = rest[0] if len(rest) == 1 else None
        if start is not None:
            widths[start] += widths.pop(None)
            zs[start] |= zs.pop(None)
    print(f"  {name}: first layer {first} mm")
    for f in sorted(used):
        side = sides[f] if f < len(sides) else 0
        side_d = (main_d, aux_d)[side - 1] if side in (1, 2) else "?"
        line = f"    filament {f + 1} ({wanted[f]}): {'Main' if side == 1 else 'Auxiliary' if side == 2 else '?'} {side_d}"
        if side_d != wanted[f]:
            problems.append(f"filament {f + 1} is on the {side_d} hotend, selected {wanted[f]}")
        if widths.get(f):
            typical = sum(w * c for w, c in widths[f].items()) / sum(widths[f].values())
            step = layer_step(zs[f])
            line += f", lines {typical:.2f} mm avg, layers {step} mm, {len(zs[f])} layers"
            ratio = typical / float(wanted[f])
            if not 0.75 <= ratio <= 1.7:
                problems.append(f"filament {f + 1}: lines {typical:.2f} mm do not fit a {wanted[f]} nozzle")
        print(line)
    return problems, first


# ---- main ----------------------------------------------------------------------------------------------

def write_patched(project, patched, settings, replace):
    with zipfile.ZipFile(project) as zin, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if re.fullmatch(r"Metadata/plate_\d+\.gcode(\.md5)?", item.filename) or item.filename in replace:
                continue
            data = zin.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps(settings, indent=4).encode()
            elif item.filename == "Metadata/model_settings.config":
                text = data.decode()
                text = re.sub(r'(key="filament_map_mode" value=")[^"]*"', r'\1Auto For Flush"', text)
                text = re.sub(r'\s*<metadata key="filament_(?:maps|volume_maps|nozzle_maps)" value="[^"]*"/>', "", text)
                data = text.encode()
            zout.writestr(item, data)
        for name, data in replace.items():
            zout.writestr(name, data)


def slice_once(args, work, patched, files, wanted):
    out_dir = work / "out"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    cmd = [args.cli, "--slice", str(args.plate), "--load-filaments", ";".join(files),
           "--filament-diameter-map", ";".join(wanted), "--min-save", "--export-3mf", "sliced.3mf",
           "--outputdir", str(out_dir), str(patched)]
    with open(work / "cli.log", "w") as log:
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=work).returncode
    result = json.loads((out_dir / "result.json").read_text()) if (out_dir / "result.json").exists() else {}
    if result.get("return_code") != 0 or not (out_dir / "sliced.3mf").exists():
        print((work / "cli.log").read_text()[-3000:])
        reason = result.get("error_string") or (f"the CLI crashed (exit {rc})" if rc < 0 or rc > 128 else "no result")
        die(f"slicing failed: {reason} (log: {work / 'cli.log'})")
    with zipfile.ZipFile(out_dir / "sliced.3mf") as z:
        first_plate = sorted(n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))[0]
        _, _, first, _ = analyse_gcode(z.read(first_plate).decode(errors="replace"))
    return out_dir / "sliced.3mf", first


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--filaments", required=True, help="nozzle diameter per filament slot, e.g. 0.4,0.2,0.4")
    ap.add_argument("--main", default="0.2", help="hotend on Main (Direct Drive), default 0.2")
    ap.add_argument("--aux", default="0.4", help="hotend on Auxiliary (Bowden), default 0.4")
    ap.add_argument("--layer-height", help="project layer height before slicing; the 0.2 prints half of it")
    ap.add_argument("--first-layer", help="first layer height to print (default: the project's)")
    ap.add_argument("--plate", type=int, default=0, help="plate to slice (1-based), default 0 = all")
    ap.add_argument("--prime-tower", choices=("keep", "on", "off"), default="keep",
                    help="prime tower: keep the project's setting (default), or force it on/off")
    ap.add_argument("--out", help="output .gcode.3mf (default: next to the project)")
    ap.add_argument("--work", help="work directory (default: a new temporary one)")
    ap.add_argument("--cli", default=DEFAULT_CLI, help=f"Bambu Studio executable (default: {DEFAULT_CLI})")
    ap.add_argument("--profiles", help="BBL system profiles folder (default: the installed Bambu Studio's)")
    args = ap.parse_args()
    if not args.cli or not Path(args.cli).is_file():
        die("Bambu Studio not found; pass --cli <path to the BambuStudio / bambu-studio.exe executable>")
    global PROFILES
    PROFILES = Path(args.profiles).resolve() if args.profiles else find_profiles(args.cli)
    print(f"Bambu Studio: {args.cli}\nprofiles: {PROFILES}")

    project = Path(args.project).resolve()
    main_d, aux_d = fmt(args.main), fmt(args.aux)
    if main_d == aux_d:
        die("--main and --aux must differ")
    wanted = [fmt(x) for x in args.filaments.split(",")]
    for i, d in enumerate(wanted, 1):
        if d not in (main_d, aux_d):
            die(f"filament {i}: {d} is neither the Main ({main_d}) nor the Auxiliary ({aux_d}) hotend")
    out = Path(args.out).resolve() if args.out else project.with_name(f"{project.stem.removesuffix('.gcode')}_X2D_Main{main_d}_Aux{aux_d}.gcode.3mf")
    work = Path(args.work).resolve() if args.work else Path(tempfile.mkdtemp(prefix="x2d_mixed_"))
    work.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(project) as z:
        archive = {n: z.read(n) for n in z.namelist()
                   if n in ("Metadata/project_settings.config", "Metadata/model_settings.config",
                            "3D/3dmodel.model", TICKS_FILE, RANGES_FILE)}
    settings = json.loads(archive["Metadata/project_settings.config"])
    if settings.get("printer_model") != "Bambu Lab X2D":
        die(f"the project's printer is {settings.get('printer_model')!r}, not Bambu Lab X2D")
    tower_before = settings.get("enable_prime_tower")
    if fmt(settings.get("printer_variant", "0")) != "0.4":
        rebase_to_x2d_04(settings, work)
        settings["enable_prime_tower"] = tower_before  # the project's choice, not the stock process's
    if args.prime_tower != "keep":
        settings["enable_prime_tower"] = "1" if args.prime_tower == "on" else "0"
    if args.layer_height:
        settings["layer_height"] = num(args.layer_height)
    target_first = float(args.first_layer or settings["initial_layer_print_height"])
    print(f"prime tower: {'on' if settings.get('enable_prime_tower') == '1' else 'off'}; "
          f"layer height {settings['layer_height']} (0.2: half), first layer {num(target_first)}")
    replace = ticks_to_ranges(archive, settings)

    names = settings["filament_settings_id"]
    if len(wanted) != len(names):
        die(f"the project has {len(names)} filament slots ({', '.join(names)}); give {len(names)} diameters")
    colours = settings.get("filament_colour", [])
    inherits = settings.get("inherits_group", [])
    changed = settings.get("different_settings_to_system", [])
    has_groups = len(inherits) == len(names) + 2

    index = filament_index()
    files = []
    print(f"Main (Direct Drive) {main_d} mm, Auxiliary (Bowden) {aux_d} mm")
    for i, (name, d) in enumerate(zip(names, wanted)):
        parent = inherits[i + 1] if has_groups else ""
        system_name = parent or name
        if system_name not in index:
            die(f"filament {i + 1} {name!r} is not based on a Bambu system preset")
        chosen = pick_system_filament(index, system_name, d)
        if chosen is None:
            die(f"filament {i + 1}: no system preset of {SUFFIX.sub('', system_name)!r} for a {d} mm X2D nozzle")
        notes = []
        if parent:
            notes.append(f"user preset {name!r} -> its system preset")
        if has_groups and i + 1 < len(changed) and changed[i + 1]:
            notes.append(f"project changes not carried over: {changed[i + 1]}")
        side = "Main" if d == main_d else "Auxiliary"
        print(f"  filament {i + 1}: {chosen} ({side} {d})" + ("; " + "; ".join(notes) if notes else ""))
        override = work / f"ov_f{i + 1}.json"
        colour = colours[i] if i < len(colours) and colours[i] else "#FFFFFF"
        override.write_text(json.dumps({"filament_colour": [colour]}))
        flatten("filament", chosen, work / f"f{i + 1}.json", override)
        files.append(str(work / f"f{i + 1}.json"))

    # The project, switched to multi-diameter mode with the real hotends and automatic grouping.
    settings.update({
        "multi_nozzle_diameter_mode": "1",
        "nozzle_diameter": [main_d, aux_d],
        "extruder_nozzle_diameter": [main_d, aux_d],
        "filament_diameter_map": wanted,
        "filament_map_mode": "Auto For Flush",
    })
    # The mode recomputes the first layer on the 0.2 grid: printed = set / 2 + c, c depending on the
    # layer height and the first nozzle. One trial slice measures c, the second slice hits the target.
    patched = work / "project_mixed.3mf"
    settings["initial_layer_print_height"] = num(target_first)
    for attempt in (1, 2):
        write_patched(project, patched, settings, replace)
        print("slicing ..." if attempt == 1 else
              f"slicing again with first layer set to {settings['initial_layer_print_height']} ...")
        sliced, first = slice_once(args, work, patched, files, wanted)
        if first is None or abs(first - target_first) < 0.004 or attempt == 2:
            break
        offset = first - float(settings["initial_layer_print_height"]) / 2
        settings["initial_layer_print_height"] = num(round(2 * (target_first - offset), 3))
    if first is not None and abs(first - target_first) >= 0.004:
        print(f"warning: first layer is {first} mm, not {num(target_first)} mm")

    subprocess.run([sys.executable, "-I", str(TOOLS / "fix_plate_diameters.py"), str(sliced),
                    str(out), f"{main_d},{aux_d}", "--as-dual"], check=True)

    # Each used filament must be on the side whose hotend has its diameter, with lines that fit it.
    problems = []
    print("check:")
    with zipfile.ZipFile(out) as z:
        info = ET.fromstring(z.read("Metadata/slice_info.config"))
        for plate in info.findall("plate"):
            pid = next(m.get("value") for m in plate.findall("metadata") if m.get("key") == "index")
            used = {int(f.get("id")) - 1 for f in plate.findall("filament")}
            name = f"Metadata/plate_{pid}.gcode"
            if name in z.namelist():
                p, _ = check_plate(name, z.read(name).decode(errors="replace"), used, wanted, main_d, aux_d)
                problems += [f"plate {pid}: {x}" for x in p]
    if problems:
        print("\nPROBLEMS - do not print this file:\n  " + "\n  ".join(problems))
        print("If the project was saved by an older Bambu Studio, open and save it in the current one and retry.")
        sys.exit(1)
    print(f"\ndone: {out}\nOpen it in Bambu Studio (File > Open Project) and press Print.")


if __name__ == "__main__":
    main()
