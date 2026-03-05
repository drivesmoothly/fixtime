# `fixtime` (tz_shifter.py) - Architecture & Documentation

## Overview
`fixtime` is Pass 1 of a two-step forensic metadata pipeline designed to perfectly synchronize camera RAW files (`.CR3`, `.JPG`) with smartphone GPS tracklogs.

Because camera internal clocks drift (seconds to minutes) and do not natively record timezones, combining camera photos with absolute UTC GPX tracklogs often results in massive geographical mapping errors. `fixtime` solves this by calculating the exact sub-second clock drift between the camera and atomic GPS satellites, applying a uniform temporal shift to the entire shoot, and tagging the correct timezone offset so the files are "GPX-Ready" for Pass 2 (`injectgpx`).

---

## What the Script Does (The Execution Flow)

1. **Pre-Scan (Deduplication):** Scans the target directory for `.cr3` and `.jpg` files, completely ignoring `.xmp` sidecars to preserve non-destructive Lightroom edits.
2. **Bulk Metadata Read:** Uses ExifTool to extract `DateTimeOriginal`, `GPSDateTime`, `GPSLatitude`, `GPSLongitude`, and `OffsetTimeOriginal` in a single, highly optimized batch read.
3. **Timezone Resolution:** Uses the `timezonefinder` library to reverse-geocode the GPS coordinates of the first available Key Frame into an exact timezone (e.g., `America/Los_Angeles`) and calculates the UTC offset (`-08:00`).
4. **Statistical Atomic Drift Calculation:** Compares the camera's naive clock (`DateTimeOriginal`) against the iPhone's atomic clock (`GPSDateTime`). It calculates this drift for *every* Key Frame and uses a statistical mode to lock onto the true sub-second drift delta.
5. **Memory Loop & Execution:** Calculates the required shift (Target Timezone Jump + Atomic Drift). If a shift is needed, it commands ExifTool to perform a relative mathematical shift on the timestamps, secures the forensic backups, and writes the correct `OffsetTime` tags file-by-file with robust error handling.

---

## Key Architectural Decisions (AI Context)

*Future agents: Read this section carefully before suggesting modifications to the script's core loops or ExifTool commands.*

### 1. The SMB Hallucination Bypass (The `ArgFile` Architecture)
**The Problem:** Running standard ExifTool directory scans (e.g., `exiftool -ext cr3 /path/`) over a macOS SMB network share on a folder with thousands of large RAW files caused the OS directory enumeration API to hallucinate. It would loop, duplicate file names, and crash ExifTool.
**The Solution:** The script completely strips macOS of enumeration duties during the ExifTool phase. Python reads the directory, strictly deduplicates the files using a `set()`, and writes the absolute paths to a temporary text file (`ArgFile`). ExifTool is then triggered using the `-@ argfile_path` flag. ExifTool addresses the files directly, bypassing the OS folder-scan bug entirely.

### 2. Statistical "Stale GPS" Filtering
**The Problem:** If the smartphone companion app loses GPS signal, it feeds the camera a "stale" location and a "stale" timestamp. If the script blindly trusted the first Key Frame it found, a 4-minute stale GPS fix would result in a permanent 4-minute mathematical error applied to the entire folder.
**The Solution:** The script calculates the drift delta for *all* Key Frames. Because stale fixes are older, they calculate as random mathematical outliers. The script uses a statistical mode (`Counter().most_common()`) to find the highest frequency of identical drifts. This automatically ignores the stale shadows and locks onto the authentic, live satellite pings.

### 3. Mathematical Reversibility (No Hardcoding)
**The Problem:** Overwriting timestamps with absolute values (`-DateTimeOriginal="2026:01:25 12:00:00"`) destroys the original millisecond intervals between high-speed camera bursts and is impossible to undo safely.
**The Solution:** The script only uses ExifTool's relative shift commands (e.g., `-AllDates-=0:0:26`). This preserves the exact micro-intervals between burst frames. It also acts as an automatic undo feature: if the script incorrectly shifts a folder by 9 hours, it can be perfectly reverted by simply running `-AllDates+=9:0:00`.

### 4. Idempotency
The script is strictly idempotent. Because it reads the *current* `OffsetTimeOriginal` tag before calculating the math, running the script multiple times on the same folder will not result in double-shifting. If a folder is already perfectly aligned to the target timezone and drift, the script recognizes the required shift is `0`, skips the ExifTool write phase, and prints a success state.

### 5. The Forensic Data Vault (Custom XMP Namespace)
**The Problem:** Modifying standard EXIF metadata carries a risk of permanent data loss, and standard fields (like `UserComment`) are often overwritten or corrupted by Lightroom syncing. Additionally, Pass 2 (`injectgpx`) mixes interpolated coordinates with authentic camera coordinates, making it impossible to separate ground-truth data from estimates later.
**The Solution:** The script relies on a custom ExifTool namespace to act as an indestructible, hidden vault inside the `.CR3` container. Lightroom cannot read or overwrite these tags.
* **`XMP-tzshifter:OriginalCameraTime`**: Stores the pure, unshifted timestamp strictly as a backup.
* **`XMP-tzshifter:LocationSource`**: Flags the file as `Camera` (ground-truth Bluetooth handshake) or `Orphan` (interpolated by the Pass 2 GPX script).

**Required Local Configuration (`~/.ExifTool_config`):**
To execute properly, the host machine must have this configuration file defined:
```perl
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        tzshifter => {
            SubDirectory => { TagTable => 'Image::ExifTool::UserDefined::tzshifter' },
        },
    },
);
%Image::ExifTool::UserDefined::tzshifter = (
    GROUPS        => { 0 => 'XMP', 1 => 'XMP-tzshifter', 2 => 'Image' },
    NAMESPACE     => { 'tzshifter' => '[http://ns.tzshifter.com/1.0/](http://ns.tzshifter.com/1.0/)' },
    WRITABLE      => 'string',
    OriginalCameraTime => { Writable => 'string' },
    LocationSource     => { Writable => 'string' },
);
1;  #end
