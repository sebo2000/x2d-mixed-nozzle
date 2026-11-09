"""Copy a sliced 3MF, setting every plate's nozzle_diameters in Metadata/slice_info.config.

usage: fix_plate_diameters.py <in.3mf> <out.gcode.3mf> <"d1,d2"> [--as-dual]

Works around a Bambu Studio bug (see README.md): in multi-nozzle-diameter mode the slicer
writes the first filament's diameter for every extruder.

--as-dual also sets multi_nozzle_diameter_mode to 0 in project_settings.config and slice_info.config.
The G-code is unchanged. Needed for sending through Bambu Studio: Print re-exports the 3MF, and with the
flag on that export rewrites nozzle_diameters to the first filament's diameter again (bbs_3mf.cpp:9102).
With the flag off it writes the per-extruder nozzle_diameter list, i.e. "d1,d2".

Every other file is copied unchanged, so the G-code and its MD5 stay valid (checked for every plate).
Prints the summary a printer test needs.
"""
import hashlib
import json
import re
import sys
import zipfile

src, dst, value = sys.argv[1], sys.argv[2], sys.argv[3]
as_dual = "--as-dual" in sys.argv[4:]
pattern = re.compile(rb'key="nozzle_diameters" value="[^"]*"')
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "Metadata/slice_info.config":
            data, count = pattern.subn(b'key="nozzle_diameters" value="' + value.encode() + b'"', data)
            if count < 1:
                sys.exit("no nozzle_diameters in slice_info.config (was the project sliced?)")
            if as_dual:
                data = data.replace(b'key="multi_nozzle_diameter_mode" value="1"',
                                    b'key="multi_nozzle_diameter_mode" value="0"')
        elif as_dual and item.filename == "Metadata/project_settings.config":
            settings = json.loads(data)
            if settings.get("nozzle_diameter") != value.split(","):
                sys.exit(f"project nozzle_diameter {settings.get('nozzle_diameter')} does not match {value}")
            settings["multi_nozzle_diameter_mode"] = "0"
            data = json.dumps(settings, indent=4).encode()
        zout.writestr(item, data)

with zipfile.ZipFile(dst) as z:
    assert z.testzip() is None
    info = z.read("Metadata/slice_info.config").decode()
    plates = sorted(n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
    print("wrote", dst)
    for name in plates:
        gcode = z.read(name)
        md5_ok = hashlib.md5(gcode).hexdigest().upper() == z.read(name + ".md5").decode().strip().upper()
        text = gcode.decode(errors="replace")
        time = re.search(r"; total estimated time: (.*)", text)
        print(f"{name}: md5 ok: {md5_ok} | time: {time.group(1) if time else '?'}"
              f" | nozzle changes: {len(re.findall(r'^M620 S[0-9]+A', text, re.M))}")
        for line in text.splitlines():
            if line.startswith(("; filament_retraction_length =", "; retraction_length =", "; filament_map =")):
                print("  " + line)
for line in info.splitlines():
    if "nozzle_diameters" in line or "<nozzle " in line or "multi_nozzle_diameter_mode" in line:
        print(line.strip())
