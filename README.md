# Stardew Valley Save Sync (PC ⇄ Android, USB)

Syncs Stardew Valley saves between the Steam version on Windows and the Android version, over a USB cable using ADB.

For each farm it:
1. reads the in-game date and play time on both sides,
2. picks the copy with **more progress**,
3. asks you to confirm,
4. **backs up** the copy that will be overwritten,
5. copies the newer save across.

## One-time setup

### On the PC (Windows)
1. Install **Python 3** from https://www.python.org/downloads/ and tick **"Add Python to PATH"** during install.
2. Download **SDK Platform-Tools for Windows** from https://developer.android.com/tools/releases/platform-tools
   and unzip it so the `platform-tools` folder sits **next to `stardew_sync.py`**:
   ```
   stardew-sync\
     stardew_sync.py
     sync.bat
     platform-tools\adb.exe   <- here
   ```
   (Or add `platform-tools` to PATH, or run once with `--adb C:\path\to\adb.exe`. The script remembers it.)

### On the phone
1. **Settings → About phone** → tap **Build number** 7 times to turn on Developer options.
   (Samsung: *About phone → Software information → Build number*.)
2. **Settings → Developer options → USB debugging = ON**.
3. Plug the phone into the PC. Unlock it and tap **Allow** on the *"Allow USB debugging?"* prompt
   (tick **Always allow from this computer**).
4. If the phone isn't found, set the USB mode to **File transfer**. If it still isn't found, install your phone maker's USB driver.

Check the connection:
```
python stardew_sync.py devices
```

## Everyday use

**Close Stardew Valley on both devices**, plug in the phone, then double-click **`sync.bat`**.

Or from a terminal:

| Command | What it does |
|---|---|
| `python stardew_sync.py` | Sync all farms (same as `sync`) |
| `python stardew_sync.py status` | Show every farm on both sides and which one is ahead |
| `python stardew_sync.py sync --dry-run` | Show what would be copied, change nothing |
| `python stardew_sync.py sync --yes` | Sync without asking |
| `python stardew_sync.py sync --farm Sunny` | Only one farm (folder, farmer or farm name) |
| `python stardew_sync.py push Sunny_123456789` | Force PC → Android |
| `python stardew_sync.py pull Sunny_123456789` | Force Android → PC |

Extra options: `--serial` (pick a phone when several are plugged in), `--adb PATH`, `--pc-saves PATH`, `--force` (skip the "game is running" check).

## Good to know

- **The game only saves when you sleep.** Progress since your last night in-game isn't in the save file.
- **Sync every time you switch devices.** Play on one device, sync, then play on the other.
- **Keep both games on the same version.** A save from a newer version (e.g. 1.6.15) won't load in an older game. The script warns you about this.
- **Backups** are stored in `%APPDATA%\stardew-sync\backups\` as zip files (the last 10 per farm and side).
  To restore one, unzip it into the Saves folder.
- Save locations:
  - PC: `%APPDATA%\StardewValley\Saves\`
  - Android: `/sdcard/Android/data/com.chucklefish.stardewvalley/files/Saves/`
- Only USB devices are used. Wi-Fi ADB devices and emulators are ignored.
