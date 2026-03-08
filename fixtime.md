# Technical Architecture: Forensic Metadata Pipeline

## 1. The Core Problem Space
Managing photo metadata across multiple cameras, international travel, and offline external GPS trackers introduces a highly complex set of failures that standard photo management tools (like Lightroom Classic) are ill-equipped to handle automatically.

The pipeline addresses three intertwined issues:
1. **Hardware Clock Drift:** Internal camera batteries lose accuracy. Over weeks or months, the camera's internal clock drifts seconds or minutes away from Coordinated Universal Time (UTC).
2. **Timezone Disconnects:** Photographers frequently cross borders and forget to update their camera's local timezone. A camera might be physically in Japan but stamping files with New York local time.
3. **The "Orphan" Body Problem:** Secondary cameras often lack built-in GPS modules. They rely on manual post-production geotagging, which requires mathematically perfect timestamps to align with a `.gpx` track log.

## 2. Architectural Philosophy



The absolute rule of this pipeline is **Non-Destructive Forensic Correction**.
RAW files (`.cr3`, `.arw`, etc.) are treated as read-only, immutable evidence. All corrections—time shifts, timezone offsets, and GPS injections—are written exclusively to the Extensible Metadata Platform (`.xmp`) sidecar files.

If a mistake is made, deleting the `.xmp` file and re-reading the RAW restores the photo to its original, factory-stamped state.

## 3. Phase 1: The Master Timecode Sync (Solving Drift & Timezones)

The pipeline does not guess the time difference; it mathematically derives it by treating the GPS satellite network as the singular source of absolute truth.

### Step 3.1: Dual-Scan Correlation
The script uses ExifTool to perform a high-speed batch read of the target directory. It correlates the unedited hardware data hiding inside the RAW file with the active data inside the `.xmp` sidecar, linking them into a `PhotoPair` object.

### Step 3.2: Atomic Drift Calculation
For every photo that contains hardware GPS data, the script performs the following calculation:
1. Extracts `GPSDateTime` (True UTC reality).
2. Extracts `DateTimeOriginal` (The camera's subjective local time).
3. Extracts `GPSLatitude` and `GPSLongitude`.
4. Uses the `timezonefinder` library to cross-reference the exact coordinates against a geographic database. This reveals the *actual* local timezone the photographer was standing in, completely bypassing the camera's internal menu setting.
5. Applies this true local offset to the camera's subjective time, converting it to UTC.
6. Calculates the difference between True UTC and Camera UTC. This value (in seconds) is the precise hardware clock drift.

### Step 3.3: Statistical Consensus (The "Stale GPS" Filter)
GPS receivers can momentarily lose satellite lock (e.g., when walking indoors), causing them to stamp a "stale" or old timestamp onto a newer photo.

To prevent these anomalies from skewing the correction, the script groups all calculated drifts in a folder. It ignores statistical noise and isolates the algebraically largest drift cluster (the most "fresh" GPS syncs). This establishes the master `CorrectionStrategy` for the entire batch.

## 4. Phase 2: GPX Interpolation
Once Phase 1 guarantees every `.xmp` file in the folder is perfectly aligned to atomic UTC time, Phase 2 handles the "Orphan" files (photos without GPS data).

Using ExifTool's `GeoMaxIntSecs` API, the script cross-references the mathematically corrected timestamp of the orphan photo against a user-provided `.gpx` track log. Because the time is now perfect, ExifTool can cleanly interpolate the exact latitude, longitude, and elevation from the track log and inject it into the XMP.

## 5. Safety & Idempotency

### The Double-Shift Hazard
If a script shifts a timestamp by -5 minutes, and the user accidentally runs the script again, the file would shift by -10 minutes, corrupting the timeline.

### The Solution: Custom XMP Namespaces
To prevent this, the pipeline injects a custom ExifTool namespace into the sidecar: `http://ns.tzshifter.com/1.0/`.
When an XMP is shifted, the script backs up the original, flawed camera time into a custom tag (`XMP-tzshifter:OriginalCameraTime`).

During the initial scan, if the script detects this tag in an XMP file, it engages a strict **Idempotency Lock**. It immediately flags the file as `[LOCK]` in the operation log and refuses to process it, rendering the script completely safe to run multiple times on the same directory.
