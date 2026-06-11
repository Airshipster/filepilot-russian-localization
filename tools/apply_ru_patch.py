import argparse
import hashlib
import json
import os
import shutil
import struct
from datetime import datetime
from pathlib import Path


INTERNAL_PREFIXES = (
    "HK_",
    "Tooltip_",
    "Setting_",
    "Panel_",
    "Button_",
    "CmdWindow_",
)

UNSAFE_COMMAND_VALUES = {
    "Options",
    "Open File Explorer",
    "Open Console^command,prompt,terminal",
    "Toggle Expand Folder^unfold,reveal,deep,hierarchy,directory",
    "Toggle File Extension^set,show,hide",
    "Toggle Hidden Files^set,show,hide",
    "Toggle System Files^set,show,hide",
    "Toggle Highlight Recents^set,show,hide,highlight",
    "Toggle Sort Recents^show,hide,highlight",
}


def align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def parse_pe(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    num = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    section_alignment = struct.unpack_from("<I", data, opt + 32)[0]
    file_alignment = struct.unpack_from("<I", data, opt + 36)[0]
    size_image_off = opt + 56
    sec_off = opt + opt_size
    sections = []
    for index in range(num):
        off = sec_off + index * 40
        name = data[off : off + 8].rstrip(b"\0").decode("ascii", "ignore")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append(
            {
                "name": name,
                "header": off,
                "vsize": vsize,
                "vaddr": vaddr,
                "rawsize": rawsize,
                "rawptr": rawptr,
            }
        )
    return {
        "pe": pe,
        "num": num,
        "num_off": pe + 6,
        "opt": opt,
        "sec_off": sec_off,
        "image_base": image_base,
        "section_alignment": section_alignment,
        "file_alignment": file_alignment,
        "size_image_off": size_image_off,
        "sections": sections,
    }


def off_to_rva(meta, file_offset):
    for sec in meta["sections"]:
        if sec["rawptr"] <= file_offset < sec["rawptr"] + sec["rawsize"]:
            return sec["vaddr"] + (file_offset - sec["rawptr"])
    return None


def find_rip_refs(data, meta, target_offsets):
    text = next(sec for sec in meta["sections"] if sec["name"] == ".text")
    targets = {}
    for source, file_offset in target_offsets.items():
        rva = off_to_rva(meta, file_offset)
        if rva is not None:
            va = meta["image_base"] + rva
            if va not in targets or len(source) > len(targets[va]):
                targets[va] = source

    refs = {source: [] for source in target_offsets}
    for pos in range(text["rawptr"], text["rawptr"] + text["rawsize"] - 7):
        disp = struct.unpack_from("<i", data, pos)[0]
        for add in (4, 5, 6, 7):
            next_rva = text["vaddr"] + ((pos + add) - text["rawptr"])
            source = targets.get(meta["image_base"] + next_rva + disp)
            if source:
                refs[source].append((pos, add))
                break
    return refs


def find_length_patch(data, pos, original_len):
    old32 = struct.pack("<I", original_len)
    for start in range(pos + 4, min(len(data) - 8, pos + 96)):
        if data[start : start + 5] == b"\x48\xc7\x44\x24\x38" and data[start + 5 : start + 9] == old32:
            return ("imm32", start + 5)
        if data[start : start + 4] == b"\xc7\x44\x24\x38" and data[start + 4 : start + 8] == old32:
            return ("imm32", start + 4)

    old8 = original_len & 0xFF
    for start in range(max(0, pos - 48), pos + 24):
        if data[start : start + 3] in (b"\x44\x8d\x4b", b"\x44\x8d\x43") and data[start + 3] == old8:
            return ("imm8", start + 3)
    return None


def add_section(data, meta, section_name, payload):
    data = bytearray(data)
    header_off = meta["sec_off"] + meta["num"] * 40
    first_raw = min(sec["rawptr"] for sec in meta["sections"] if sec["rawptr"])
    if header_off + 40 > first_raw:
        raise RuntimeError("No room for a new PE section header.")

    new_raw_ptr = align(len(data), meta["file_alignment"])
    if len(data) < new_raw_ptr:
        data.extend(b"\0" * (new_raw_ptr - len(data)))

    new_raw_size = align(len(payload), meta["file_alignment"])
    last = max(meta["sections"], key=lambda sec: sec["vaddr"])
    new_vaddr = align(last["vaddr"] + max(last["vsize"], last["rawsize"]), meta["section_alignment"])
    data.extend(payload)
    data.extend(b"\0" * (new_raw_size - len(payload)))

    header = (
        section_name.encode("ascii")[:8].ljust(8, b"\0")
        + struct.pack("<IIIIIIHHI", len(payload), new_vaddr, new_raw_size, new_raw_ptr, 0, 0, 0, 0, 0x40000040)
    )
    data[header_off : header_off + 40] = header
    struct.pack_into("<H", data, meta["num_off"], meta["num"] + 1)
    struct.pack_into("<I", data, meta["size_image_off"], align(new_vaddr + len(payload), meta["section_alignment"]))
    return data, new_vaddr


def looks_like_internal_id(value):
    return any(value.startswith(prefix) for prefix in INTERNAL_PREFIXES)


def build_translations(language_path, strings_path):
    lang = json.loads(language_path.read_text(encoding="utf-8"))
    extracted = json.loads(strings_path.read_text(encoding="utf-8"))
    translated = lang.get("strings", {})
    translations = {}

    for key, english in extracted.items():
        russian = translated.get(key)
        if not russian or russian == english:
            continue
        if looks_like_internal_id(english) or looks_like_internal_id(russian):
            continue
        if english in UNSAFE_COMMAND_VALUES:
            continue

        russian = russian.split("^", 1)[0]
        if russian:
            translations[english] = russian

    return translations


def patch_file(exe_path, language_path, strings_path, backup_root, force=False):
    exe_path = exe_path.expanduser().resolve()
    if not exe_path.exists():
        raise FileNotFoundError(f"File Pilot executable was not found: {exe_path}")

    original = exe_path.read_bytes()
    meta = parse_pe(original)
    if any(sec["name"] == ".rulang" for sec in meta["sections"]) and not force:
        raise RuntimeError("This executable already contains a .rulang section. Restore the backup or use --force.")

    translations = build_translations(language_path, strings_path)
    source_offsets = {}
    for source in sorted(translations, key=len, reverse=True):
        off = original.find(source.encode("utf-8"))
        if off != -1:
            source_offsets[source] = off

    refs = find_rip_refs(original, meta, source_offsets)
    payload = bytearray()
    locations = {}
    for source, target in translations.items():
        if refs.get(source):
            locations[source] = len(payload)
            payload.extend(target.encode("utf-8") + b"\0")

    patched, new_vaddr = add_section(original, meta, ".rulang", payload)
    text = next(sec for sec in meta["sections"] if sec["name"] == ".text")
    applied = []
    skipped = []

    for source, rel in locations.items():
        target = translations[source]
        target_len = len(target.encode("utf-8"))
        old_len = len(source.encode("utf-8"))
        new_va = meta["image_base"] + new_vaddr + rel
        for pos, add in refs[source]:
            length_patch = find_length_patch(original, pos, old_len)
            if not length_patch:
                skipped.append({"source": source, "target": target, "offset": pos, "reason": "length-not-found"})
                continue

            kind, len_off = length_patch
            next_rva = text["vaddr"] + ((pos + add) - text["rawptr"])
            disp = new_va - (meta["image_base"] + next_rva)
            if not -(2**31) <= disp < 2**31:
                skipped.append({"source": source, "target": target, "offset": pos, "reason": "disp-out-of-range"})
                continue
            if kind == "imm8" and target_len > 255:
                skipped.append({"source": source, "target": target, "offset": pos, "reason": "imm8-too-small"})
                continue

            struct.pack_into("<i", patched, pos, disp)
            if kind == "imm32":
                struct.pack_into("<I", patched, len_off, target_len)
            else:
                patched[len_off] = target_len
            applied.append(
                {
                    "source": source,
                    "target": target,
                    "refOffset": pos,
                    "lengthOffset": len_off,
                    "oldLength": old_len,
                    "newLength": target_len,
                }
            )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"filepilot-ru-backup-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_exe = backup_dir / exe_path.name
    shutil.copy2(exe_path, backup_exe)
    exe_path.write_bytes(patched)

    report = {
        "appExe": str(exe_path),
        "backupExe": str(backup_exe),
        "originalSha256": hashlib.sha256(original).hexdigest(),
        "patchedSha256": hashlib.sha256(patched).hexdigest(),
        "appliedCount": len(applied),
        "skippedCount": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }
    report_path = backup_dir / "patch-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    repo_root = Path(__file__).resolve().parents[1]
    default_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Voidstar" / "FilePilot" / "FPilot.exe"

    parser = argparse.ArgumentParser(description="Apply the unofficial Russian localization patch to File Pilot.")
    parser.add_argument("--exe", type=Path, default=default_exe, help="Path to FPilot.exe.")
    parser.add_argument("--language", type=Path, default=repo_root / "FilePilot.ru-RU.language.json")
    parser.add_argument("--strings", type=Path, default=repo_root / "data" / "extracted-filepilot-strings.json")
    parser.add_argument("--backup-dir", type=Path, default=repo_root / "backups")
    parser.add_argument("--force", action="store_true", help="Patch even if a .rulang section already exists.")
    args = parser.parse_args()

    report = patch_file(args.exe, args.language, args.strings, args.backup_dir, args.force)
    print(json.dumps({"applied": report["appliedCount"], "skipped": report["skippedCount"], "backup": report["backupExe"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
