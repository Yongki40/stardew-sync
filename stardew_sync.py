#!/usr/bin/env python3
"""
Stardew Valley save sync: PC (Steam) <-> Android, over ADB with a USB cable.

Compares every farm on both sides, keeps the one with more in-game progress,
backs up whatever is about to be overwritten, then copies it across.

Usage:
    python stardew_sync.py                 # same as "sync"
    python stardew_sync.py devices         # check the USB connection
    python stardew_sync.py status          # show farms on both sides
    python stardew_sync.py sync [--dry-run] [--yes] [--farm NAME]
    python stardew_sync.py push NAME       # force PC -> Android
    python stardew_sync.py pull NAME       # force Android -> PC

Only needs Python 3.8+ and Google's adb (Android SDK platform-tools).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

PKG = "com.chucklefish.stardewvalley"
ANDROID_SAVE_DIRS = [
    f"/sdcard/Android/data/{PKG}/files/Saves",
    "/sdcard/StardewValley/Saves",  # very old Android builds
]
KEEP_BACKUPS = 10
SEASON_NAMES = ["Spring", "Summer", "Fall", "Winter"]
SEASON_INDEX = {s.lower(): i for i, s in enumerate(SEASON_NAMES)}

PC_TO_ANDROID = "PC -> Android"
ANDROID_TO_PC = "Android -> PC"
IN_SYNC = "in sync"

NO_DEVICE_HINT = (
    "No phone found over USB.\n"
    "  1. Plug the phone in with a data-capable USB cable.\n"
    "  2. On the phone: Settings > Developer options > USB debugging = ON.\n"
    "  3. Set the USB mode to 'File transfer' if it is on 'Charging only'.\n"
    "  4. Still nothing? Install your phone maker's USB driver (Samsung, Xiaomi, ...)."
)
STATE_HINTS = {
    "unauthorized": "Unlock the phone and tap 'Allow' on the 'Allow USB debugging?' "
                    "prompt (tick 'Always allow from this computer').",
    "offline": "Unplug and replug the cable. If it stays offline, run 'adb kill-server'.",
    "no": "No permission to access the device (Linux: add a udev rule for the phone).",
    "recovery": "The phone is in recovery mode. Boot it normally.",
    "sideload": "The phone is in sideload mode. Boot it normally.",
}


class SyncError(Exception):
    pass


def q(s):
    """Quote a string for the Android shell."""
    return shlex.quote(s)


# --------------------------------------------------------------------------- paths & config

def app_dir():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "stardew-sync"


def default_pc_saves():
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "StardewValley" / "Saves"
    return Path.home() / ".config" / "StardewValley" / "Saves"  # Linux and macOS


def load_config():
    path = app_dir() / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- adb

def find_adb(cfg, override=None):
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [
        override,
        cfg.get("adb_path"),
        str(Path(__file__).resolve().parent / exe),
        str(Path(__file__).resolve().parent / "platform-tools" / exe),
        shutil.which("adb"),
    ]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / exe))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise SyncError(
        "adb was not found.\n"
        "  Download 'SDK Platform-Tools for Windows' from\n"
        "  https://developer.android.com/tools/releases/platform-tools\n"
        "  then either unzip it so 'platform-tools' sits next to this script,\n"
        "  add it to PATH, or run once with: --adb C:\\path\\to\\adb.exe"
    )


class Adb:
    def __init__(self, exe):
        self.exe = exe
        self.serial = None

    def run(self, *args, check=True, binary=False, timeout=600):
        cmd = [self.exe]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SyncError(f"adb timed out: {' '.join(args)}")
        except OSError as e:
            raise SyncError(f"Could not run adb ({self.exe}): {e}")
        out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace").strip()
        if check and p.returncode != 0:
            msg = err or (p.stdout.decode("utf-8", "replace").strip())
            if "no devices" in msg or "not found" in msg and "device" in msg:
                msg += "\n  The phone was disconnected. Check the USB cable."
            raise SyncError(f"adb {args[0]} failed: {msg}")
        return out

    def shell(self, script, check=True):
        return self.run("shell", script, check=check)


def list_usb_devices(adb):
    adb.run("start-server", check=False)
    out = adb.run("devices", "-l", check=False)
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        # Keep only USB devices: skip Wi-Fi (ip:port / mDNS) and emulators.
        if ":" in serial or "._adb" in serial or serial.startswith("emulator-"):
            continue
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
        devices.append((serial, state, model.replace("_", " ")))
    return devices


def choose_device(adb, cfg, want_serial=None):
    devices = list_usb_devices(adb)
    if not devices:
        raise SyncError(NO_DEVICE_HINT)
    if want_serial:
        devices = [d for d in devices if d[0] == want_serial]
        if not devices:
            raise SyncError(f"Device '{want_serial}' is not connected over USB.")
    ready = [d for d in devices if d[1] == "device"]
    if not ready:
        lines = [f"  {s} ({m or 'unknown'}): {st}. {STATE_HINTS.get(st, '')}" for s, st, m in devices]
        raise SyncError("The phone is connected but not ready:\n" + "\n".join(lines))

    if len(ready) == 1:
        chosen = ready[0]
    elif cfg.get("serial") in [d[0] for d in ready]:
        chosen = next(d for d in ready if d[0] == cfg["serial"])
    else:
        print("Several phones are connected:")
        for i, (s, _, m) in enumerate(ready, 1):
            print(f"  {i}. {m or 'unknown'} [{s}]")
        while True:
            pick = input(f"Which one? [1-{len(ready)}]: ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(ready):
                chosen = ready[int(pick) - 1]
                break
        cfg["serial"] = chosen[0]
        save_config(cfg)
    adb.serial = chosen[0]
    print(f"Phone: {chosen[2] or 'Android device'} [{chosen[0]}]")
    return chosen


def find_android_saves(adb, cfg):
    candidates = ANDROID_SAVE_DIRS
    if cfg.get("android_saves") in candidates:
        candidates = [cfg["android_saves"]] + [c for c in candidates if c != cfg["android_saves"]]
    for d in candidates:
        if "YES" in adb.shell(f"[ -d {q(d)} ] && echo YES", check=False):
            if cfg.get("android_saves") != d:
                cfg["android_saves"] = d
                save_config(cfg)
            return d
    # No saves on the phone yet. Use the modern location (created on first push).
    return ANDROID_SAVE_DIRS[0]


# --------------------------------------------------------------------------- save files

def parse_save_info(data):
    """Read progress fields from a SaveGameInfo file. Returns a dict, or None if unreadable."""
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        root = ET.fromstring(data)
        fields = {}
        for el in root:  # direct children of <Farmer> only (items also have <name>)
            fields.setdefault(el.tag.rsplit("}", 1)[-1], (el.text or "").strip())

        season_raw = fields.get("seasonForSaveGame", "0")
        season = int(season_raw) if season_raw.lstrip("-").isdigit() else SEASON_INDEX[season_raw.lower()]
        year = int(fields["yearForSaveGame"])
        day = int(fields["dayOfMonthForSaveGame"])
        ms = int(fields.get("millisecondsPlayed") or 0)
        total_days = (year - 1) * 112 + season * 28 + day
        return {
            "name": fields.get("name", "?"),
            "farm": fields.get("farmName", "?"),
            "money": int(fields.get("money") or 0),
            "ms": ms,
            "year": year,
            "season": season,
            "day": day,
            "version": fields.get("gameVersion", ""),
            "key": (total_days, ms),
        }
    except (ET.ParseError, KeyError, ValueError, IndexError):
        return None


def describe(side):
    if side is None:
        return "(not on this device)"
    info = side["info"]
    if info is None:
        return f"(SaveGameInfo unreadable, file time {fmt_time(side['mtime'])})"
    h, m = divmod(info["ms"] // 60000, 60)
    season = SEASON_NAMES[info["season"]] if 0 <= info["season"] < 4 else "?"
    ver = f", v{info['version']}" if info["version"] else ""
    return f"Year {info['year']} {season} {info['day']}, {info['money']:,}g, played {h}h{m:02d}m{ver}"


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def is_farm_dir(p):
    return (p / p.name).is_file() and (p / "SaveGameInfo").is_file()


def list_pc_farms(pc_saves):
    if not pc_saves.is_dir():
        return []
    return sorted(p.name for p in pc_saves.iterdir() if p.is_dir() and is_farm_dir(p))


def list_android_farms(adb, saves_dir):
    script = (
        f"cd {q(saves_dir)} 2>/dev/null || exit 0; "
        'for d in *; do [ -f "$d/$d" ] && [ -f "$d/SaveGameInfo" ] && echo "$d"; done'
    )
    return sorted(line.strip() for line in adb.shell(script, check=False).splitlines() if line.strip())


def read_pc_side(pc_saves, folder):
    d = pc_saves / folder
    mtime = max((d / folder).stat().st_mtime, (d / "SaveGameInfo").stat().st_mtime)
    return {"info": parse_save_info((d / "SaveGameInfo").read_bytes()), "mtime": mtime}


def read_android_side(adb, saves_dir, folder):
    base = f"{saves_dir}/{folder}"
    data = adb.run("exec-out", f"cat {q(base + '/SaveGameInfo')}", binary=True)
    out = adb.shell(f"stat -c %Y {q(base + '/' + folder)} {q(base + '/SaveGameInfo')}", check=False)
    times = [int(t) for t in out.split() if t.isdigit()]
    return {"info": parse_save_info(data), "mtime": max(times) if times else 0}


def decide(pc, android):
    """Return (direction, reason)."""
    if pc and not android:
        return PC_TO_ANDROID, "only on PC"
    if android and not pc:
        return ANDROID_TO_PC, "only on Android"
    if pc["info"] and android["info"]:
        if pc["info"]["key"] > android["info"]["key"]:
            return PC_TO_ANDROID, "PC has more progress"
        if android["info"]["key"] > pc["info"]["key"]:
            return ANDROID_TO_PC, "Android has more progress"
        return IN_SYNC, "same day and play time"
    # Fallback when a SaveGameInfo could not be parsed.
    if pc["mtime"] > android["mtime"]:
        return PC_TO_ANDROID, "PC file is newer (could not read progress, used file time)"
    if android["mtime"] > pc["mtime"]:
        return ANDROID_TO_PC, "Android file is newer (could not read progress, used file time)"
    return IN_SYNC, "same file time"


# --------------------------------------------------------------------------- backups & copying

def zip_dir(src_dir, zip_path, arc_root):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(src_dir).rglob("*")):
            if f.is_file():
                z.write(f, Path(arc_root) / f.relative_to(src_dir))


def prune_backups(folder, side):
    files = sorted((app_dir() / "backups").glob(f"{folder}_{side}_*.zip"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def backup_name(folder, side):
    return app_dir() / "backups" / f"{folder}_{side}_{datetime.now():%Y%m%d-%H%M%S}.zip"


def backup_pc(pc_saves, folder):
    dest = backup_name(folder, "pc")
    zip_dir(pc_saves / folder, dest, folder)
    prune_backups(folder, "pc")
    return dest


def backup_android(adb, saves_dir, folder):
    dest = backup_name(folder, "android")
    with tempfile.TemporaryDirectory() as tmp:
        adb.run("pull", f"{saves_dir}/{folder}", tmp)
        pulled = Path(tmp) / folder
        if not is_farm_dir(pulled):
            raise SyncError(f"Backup of the Android copy of {folder} looks incomplete. Nothing was changed.")
        zip_dir(pulled, dest, folder)
    prune_backups(folder, "android")
    return dest


def copy_android_to_pc(adb, saves_dir, pc_saves, folder):
    pc_saves.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sdv-sync-"))
    try:
        adb.run("pull", f"{saves_dir}/{folder}", str(tmp))
        pulled = tmp / folder
        if not is_farm_dir(pulled):
            raise SyncError("The files pulled from the phone are incomplete. PC save left unchanged.")
        dest = pc_saves / folder
        old = pc_saves / f".{folder}.sync_old"
        if old.exists():
            shutil.rmtree(old)
        if dest.exists():
            dest.rename(old)
        try:
            shutil.move(str(pulled), str(dest))
        except Exception:
            if old.exists() and not dest.exists():
                old.rename(dest)  # put the original back
            raise
        if old.exists():
            shutil.rmtree(old)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def copy_pc_to_android(adb, saves_dir, pc_saves, folder):
    src = pc_saves / folder
    dest = f"{saves_dir}/{folder}"
    tmp = f"{saves_dir}/.sync_tmp_{folder}"
    adb.shell(f"rm -rf {q(tmp)}; mkdir -p {q(saves_dir)}")
    adb.run("push", str(src), tmp)
    check = adb.shell(f"[ -f {q(tmp + '/' + folder)} ] && [ -f {q(tmp + '/SaveGameInfo')} ] && echo OK",
                      check=False)
    if "OK" not in check:
        adb.shell(f"rm -rf {q(tmp)}", check=False)
        raise SyncError("Upload to the phone looks incomplete. Android save left unchanged.")
    result = adb.shell(f"rm -rf {q(dest)} && mv {q(tmp)} {q(dest)} && echo OK", check=False)
    if "OK" not in result:
        raise SyncError(f"Could not move the uploaded save into place on the phone: {result.strip()}\n"
                        f"  The uploaded copy may still be at {tmp}; a backup is in {app_dir() / 'backups'}.")


# --------------------------------------------------------------------------- context

class Ctx:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config()
        if args.adb:
            self.cfg["adb_path"] = str(Path(args.adb).resolve())
            save_config(self.cfg)
        self.adb = Adb(find_adb(self.cfg, args.adb))
        self.pc_saves = Path(args.pc_saves) if args.pc_saves else default_pc_saves()
        choose_device(self.adb, self.cfg, args.serial)
        self.android_saves = find_android_saves(self.adb, self.cfg)
        print(f"PC saves:      {self.pc_saves}")
        print(f"Android saves: {self.android_saves}\n")

    def farms(self, wanted=None):
        """Return [(folder, pc_side, android_side)] for every farm, optionally filtered by name."""
        pc_list = list_pc_farms(self.pc_saves)
        an_list = list_android_farms(self.adb, self.android_saves)
        result = []
        for folder in sorted(set(pc_list) | set(an_list)):
            pc = read_pc_side(self.pc_saves, folder) if folder in pc_list else None
            an = read_android_side(self.adb, self.android_saves, folder) if folder in an_list else None
            result.append((folder, pc, an))
        if wanted:
            w = wanted.lower()

            def matches(folder, pc, an):
                names = {folder.lower()}
                for side in (pc, an):
                    if side and side["info"]:
                        names |= {side["info"]["name"].lower(), side["info"]["farm"].lower()}
                return w in names or folder.lower().startswith(w + "_")

            result = [f for f in result if matches(*f)]
            if not result:
                raise SyncError(f"No farm called '{wanted}'. Run 'status' to see the farm names.")
        return result


def games_running(ctx):
    running = []
    out = ctx.adb.shell(f"pidof {PKG}", check=False).strip()
    if out and re.fullmatch(r"[\d\s]+", out):
        running.append("Android")
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Stardew Valley.exe", "/NH"],
                                 capture_output=True, text=True, errors="replace").stdout
            if "stardew valley.exe" in out.lower():
                running.append("PC")
        elif shutil.which("pgrep"):
            if subprocess.run(["pgrep", "-x", "StardewValley"], capture_output=True).returncode == 0:
                running.append("PC")
    except OSError:
        pass
    return running


def ensure_games_closed(ctx):
    running = games_running(ctx)
    if running and not ctx.args.force:
        raise SyncError(
            f"Stardew Valley is running on: {', '.join(running)}.\n"
            "  Close the game (on Android, swipe it away from Recent apps) so it can't overwrite the sync,\n"
            "  then run again. Use --force to skip this check."
        )


def print_farm(folder, pc, an):
    print(f"Farm: {folder}")
    print(f"  PC:      {describe(pc)}")
    print(f"  Android: {describe(an)}")


def version_warning(direction, pc, an):
    src, dst = (pc, an) if direction == PC_TO_ANDROID else (an, pc)
    if src and dst and src["info"] and dst["info"]:
        sv, dv = version_tuple(src["info"]["version"]), version_tuple(dst["info"]["version"])
        if sv and dv and sv > dv:
            target = "Android" if direction == PC_TO_ANDROID else "PC"
            return (f"  ! This save is from game v{src['info']['version']}, the {target} copy was v"
                    f"{dst['info']['version']}. Make sure the {target} game is updated, or it may not load.")
    return None


def transfer(ctx, folder, direction):
    a = ctx.adb
    if direction == PC_TO_ANDROID:
        if "YES" in a.shell(f"[ -d {q(ctx.android_saves + '/' + folder)} ] && echo YES", check=False):
            print(f"  Backup of Android copy -> {backup_android(a, ctx.android_saves, folder)}")
        copy_pc_to_android(a, ctx.android_saves, ctx.pc_saves, folder)
    else:
        if (ctx.pc_saves / folder).exists():
            print(f"  Backup of PC copy -> {backup_pc(ctx.pc_saves, folder)}")
        copy_android_to_pc(a, ctx.android_saves, ctx.pc_saves, folder)
    print(f"  Done: {direction}.")


def confirm(prompt):
    """Return 'y', 'n' or 'q'."""
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("y", "yes"):
            return "y"
        if ans in ("", "n", "no", "s", "skip"):
            return "n"
        if ans in ("q", "quit"):
            return "q"


# --------------------------------------------------------------------------- commands

def cmd_devices(args):
    cfg = load_config()
    adb = Adb(find_adb(cfg, args.adb))
    devices = list_usb_devices(adb)
    if not devices:
        print(NO_DEVICE_HINT)
        return 1
    for serial, state, model in devices:
        line = f"{model or 'unknown'} [{serial}]: {state}"
        if state != "device":
            line += f"\n    {STATE_HINTS.get(state, '')}"
        else:
            line += "  (ready)"
        print(line)
    return 0 if any(d[1] == "device" for d in devices) else 1


def cmd_status(args):
    ctx = Ctx(args)
    farms = ctx.farms(args.farm)
    if not farms:
        print("No farms found on either device.")
        return 0
    for folder, pc, an in farms:
        print_farm(folder, pc, an)
        direction, reason = decide(pc, an)
        print(f"  => {direction} ({reason})\n")
    return 0


def cmd_sync(args):
    ctx = Ctx(args)
    if not args.dry_run:
        ensure_games_closed(ctx)
    farms = ctx.farms(args.farm)
    if not farms:
        print("No farms found on either device.")
        return 0
    copied = skipped = 0
    for folder, pc, an in farms:
        print_farm(folder, pc, an)
        direction, reason = decide(pc, an)
        if direction == IN_SYNC:
            print(f"  => Already in sync ({reason}).\n")
            continue
        print(f"  => {direction} ({reason})")
        warn = version_warning(direction, pc, an)
        if warn:
            print(warn)
        if args.dry_run:
            print("  (dry run, nothing copied)\n")
            continue
        if not args.yes:
            ans = confirm("  Copy it? [y]es / [N]o, skip / [q]uit: ")
            if ans == "q":
                print("Stopped.")
                break
            if ans == "n":
                print("  Skipped.\n")
                skipped += 1
                continue
        transfer(ctx, folder, direction)
        copied += 1
        print()
    if not args.dry_run:
        print(f"Finished: {copied} copied, {skipped} skipped.")
    return 0


def cmd_force(args, direction):
    ctx = Ctx(args)
    ensure_games_closed(ctx)
    farms = ctx.farms(args.name)
    if len(farms) > 1:
        raise SyncError("That name matches several farms: " + ", ".join(f[0] for f in farms)
                        + ". Use the full folder name.")
    folder, pc, an = farms[0]
    src = pc if direction == PC_TO_ANDROID else an
    if src is None:
        raise SyncError(f"{folder} does not exist on the {'PC' if direction == PC_TO_ANDROID else 'phone'}.")
    print_farm(folder, pc, an)
    print(f"  => {direction} (forced)")
    warn = version_warning(direction, pc, an)
    if warn:
        print(warn)
    if not args.yes and confirm("  Overwrite? [y]es / [N]o: ") != "y":
        print("Cancelled.")
        return 0
    transfer(ctx, folder, direction)
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--serial", help="USB serial of the phone (only needed if several are plugged in)")
    common.add_argument("--adb", help="Path to adb / adb.exe (remembered for next time)")
    common.add_argument("--pc-saves", help="PC save folder (default: %%APPDATA%%\\StardewValley\\Saves)")
    common.add_argument("--force", action="store_true", help="Skip the 'game is running' check")
    common.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")

    p = argparse.ArgumentParser(description="Sync Stardew Valley saves between PC (Steam) and Android over USB ADB.")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("devices", parents=[common], help="Check the USB connection to the phone")
    s = sub.add_parser("status", parents=[common], help="Show farms on both sides")
    s.add_argument("--farm", help="Only this farm (folder, farmer or farm name)")
    s = sub.add_parser("sync", parents=[common], help="Copy the save with more progress to the other side")
    s.add_argument("--farm", help="Only this farm (folder, farmer or farm name)")
    s.add_argument("--dry-run", action="store_true", help="Show what would happen, copy nothing")
    s = sub.add_parser("push", parents=[common], help="Force copy PC -> Android")
    s.add_argument("name", help="Farm folder, farmer or farm name")
    s = sub.add_parser("pull", parents=[common], help="Force copy Android -> PC")
    s.add_argument("name", help="Farm folder, farmer or farm name")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "sync")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "devices":
            return cmd_devices(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "sync":
            return cmd_sync(args)
        if args.command == "push":
            return cmd_force(args, PC_TO_ANDROID)
        if args.command == "pull":
            return cmd_force(args, ANDROID_TO_PC)
    except SyncError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
