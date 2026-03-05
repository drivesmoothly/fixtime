#!/usr/bin/env python3
import subprocess
import json
import os
import sys
import argparse
import time
import tempfile
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder

def check_dependencies():
    """Ensures ExifTool is installed and accessible."""
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("CRITICAL ERROR: ExifTool is not installed or not in your system PATH.")
        sys.exit(1)

def get_bulk_exif_data(argfile_path):
    """
    Asks ExifTool to scan a strictly controlled list of files to bypass SMB duplication bugs.
    Uses a single, highly optimized batch read for performance over network shares.
    """
    print("Scanning folder metadata (Full read mode for CR3 safety. This may take a few minutes over NAS)...")

    cmd = [
        "exiftool", "-json", "-c", "%+.6f", "-q", "-q",
        "-DateTimeOriginal", "-GPSDateTime",
        "-GPSLatitude", "-GPSLongitude",
        "-OffsetTimeOriginal",
        "-@", argfile_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            return json.loads(result.stdout)
        return []
    except subprocess.CalledProcessError as e:
        print(f"ERROR reading metadata: ExifTool encountered an issue.")
        print(f"EXIFTOOL ERROR: {e.stderr.strip()}")
        return []

def parse_offset_string_to_seconds(offset_str):
    """Converts an offset string like '+01:00' or '-08:00' to raw seconds."""
    if not offset_str or len(offset_str) < 5:
        return None
    try:
        sign = 1 if offset_str[0] == '+' else -1
        hours = int(offset_str[1:3])
        minutes = int(offset_str[4:6] if len(offset_str) >= 6 else 0)
        return sign * (hours * 3600 + minutes * 60)
    except:
        return None

def format_shift_string(total_seconds):
    """Converts raw seconds into an ExifTool shift string like '-=9:00:25'."""
    sign = "+=" if total_seconds >= 0 else "-="
    secs = abs(int(total_seconds))
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{sign}{h}:{m:02d}:{s:02d}"

def get_gps_stats(data, tf):
    """Calculates the exact atomic drift down to the second for a Key Frame."""
    try:
        gps_utc_str = data["GPSDateTime"].replace("Z", "").strip()[:19]
        cam_time_str = data["DateTimeOriginal"]

        utc_dt = datetime.strptime(gps_utc_str, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
        cam_dt = datetime.strptime(cam_time_str, "%Y:%m:%d %H:%M:%S")

        lat = float(data["GPSLatitude"])
        lon = float(data["GPSLongitude"])
        tz_name = tf.timezone_at(lat=lat, lng=lon)

        if not tz_name:
            return None, None, None

        local_tz = ZoneInfo(tz_name)
        true_local_dt = utc_dt.astimezone(local_tz)

        offset_seconds = int(true_local_dt.utcoffset().total_seconds())
        offset_hours = offset_seconds // 3600
        offset_minutes = (abs(offset_seconds) % 3600) // 60
        sign = "+" if offset_seconds >= 0 else "-"
        offset_str = f"{sign}{abs(offset_hours):02d}:{offset_minutes:02d}"

        true_local_dt_naive = true_local_dt.replace(tzinfo=None)
        drift_sec = round((true_local_dt_naive - cam_dt).total_seconds())

        return drift_sec, offset_str, tz_name

    except Exception:
        return None, None, None

def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    sec = seconds % 60
    return f"{minutes}m {sec:.2f}s"

def main():
    script_start_time = time.time()

    parser = argparse.ArgumentParser(description="Shift EXIF capture time with GPX-ready Orphan Memory, Atomic Sync, and Forensic Backups.")
    parser.add_argument("directory", help="Path to the folder containing your CR3 or JPG files")
    parser.add_argument("--dry-run", action="store_true", help="Run the math without modifying files")
    parser.add_argument("--target-timezone", help="Manually specify TARGET timezone offset (e.g., -08:00)")
    parser.add_argument("--current-timezone", help="The timezone the camera clock was actually set to (e.g., +01:00)")
    parser.add_argument("--drift", type=int, default=0, help="Manually specify atomic drift in seconds")
    args = parser.parse_args()

    check_dependencies()

    target_dir = os.path.abspath(args.directory)
    if not os.path.isdir(target_dir):
        print(f"ERROR: Directory '{target_dir}' does not exist.")
        sys.exit(1)

    print(f"Initializing Timezone database... (DRY_RUN is {'ON' if args.dry_run else 'OFF'})")
    tf = TimezoneFinder()

    # --- 1. PRE-SCAN (Deduplication) ---
    valid_files = []
    seen = set()
    for filename in sorted(os.listdir(target_dir)):
        if filename.startswith('.'):
            continue
        if filename.lower().endswith(('.cr3', '.jpg')) and filename not in seen:
            seen.add(filename)
            valid_files.append(os.path.join(target_dir, filename))

    if not valid_files:
        print("No valid CR3 or JPG files found.")
        sys.exit(0)

    # Use the ArgFile Architecture to bypass OS enumeration duties
    with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_argfile:
        for file_path in valid_files:
            temp_argfile.write(f"{file_path}\n")
        argfile_path = temp_argfile.name

    # --- 2. BULK METADATA READ ---
    scan_start_time = time.time()
    try:
        raw_data = get_bulk_exif_data(argfile_path)
    finally:
        os.remove(argfile_path)

    if not raw_data:
        print("No EXIF data extracted.")
        sys.exit(0)

    # Sort data immediately for consistent logging and chronological processing
    raw_data.sort(key=lambda x: x.get("SourceFile", ""))
    scan_duration = time.time() - scan_start_time

    # --- 3. OFFSET & ATOMIC DRIFT DETERMINATION ---
    manual_mode = False
    if args.target_timezone:
        # Manual Override: Bypasses GPS resolution if user provides direct target/drift
        global_offset = args.target_timezone
        global_drift = args.drift
        consensus_tz = "Manual Override"
        manual_mode = True

        # Robust Error Handling for the current-timezone argument
        if args.current_timezone and parse_offset_string_to_seconds(args.current_timezone) is None:
            print("CRITICAL ERROR: Invalid --current-timezone format. Please use formats like '+01:00' or '-08:00'.")
            sys.exit(1)

        cam_info = f" (Camera assumed at {args.current_timezone})" if args.current_timezone else ""
        print(f"⚠️  Using MANUAL override: Target Timezone {global_offset}{cam_info}, Drift {global_drift}s")
    else:
        # GPS Resolution: Establish ground-truth timezone and calculate atomic drift
        all_drifts = []
        all_offsets = []
        consensus_tz = "Unknown"

        for data in raw_data:
            if "GPSLatitude" in data and "GPSDateTime" in data:
                drift, offset, tz = get_gps_stats(data, tf)
                if drift is not None:
                    all_drifts.append(drift)
                    all_offsets.append(offset)
                    consensus_tz = tz

        if not all_drifts:
            print("CRITICAL ERROR: No photos with GPS data found. Cannot establish timeline. Use --target-timezone to override.")
            sys.exit(1)

        # Statistical "Stale GPS" Filtering: Use mode to lock onto true atomic drift
        offset_counts = Counter(all_offsets)
        global_offset = offset_counts.most_common(1)[0][0]

        drift_counts = Counter(all_drifts)
        max_frequency = max(drift_counts.values())

        if max_frequency >= 2:
            most_common_drifts = [d for d, c in drift_counts.items() if c == max_frequency]
            global_drift = max(most_common_drifts)
        else:
            global_drift = max(all_drifts)

    target_offset_sec = parse_offset_string_to_seconds(global_offset)

    print("\n==================================================")
    print(" Master Timecode Sync Established")
    print("==================================================")
    print(f" 📍 Target Timezone : {consensus_tz} [{global_offset}]")
    print(f" ⏱️ Atomic Drift    : {global_drift} seconds")
    print("==================================================")

    # --- 4. MEMORY LOOP & EXECUTION ---
    stats = {"total": len(raw_data), "key_frames": 0, "orphans": 0, "shifted": 0, "tagged_only": 0, "skipped": 0, "errors": 0}
    process_start_time = time.time()

    for data in raw_data:
        file_path = data.get("SourceFile")
        filename = os.path.basename(file_path)

        if "DateTimeOriginal" not in data:
            print(f"SKIPPING: {filename} (Corrupt or missing DateTimeOriginal)")
            stats["skipped"] += 1
            continue

        existing_offset_str = data.get("OffsetTimeOriginal")
        has_gps = "GPSLatitude" in data and "GPSDateTime" in data

        if has_gps:
            stats["key_frames"] += 1
            frame_type = f"KEY FRAME ({consensus_tz})"
        else:
            stats["orphans"] += 1
            frame_type = "ORPHAN FRAME"

        existing_offset_sec = parse_offset_string_to_seconds(existing_offset_str)

        # If the file has no offset, determine the baseline using the current-timezone logic
        if existing_offset_sec is None:
            if args.current_timezone:
                existing_offset_sec = parse_offset_string_to_seconds(args.current_timezone)
            else:
                existing_offset_sec = target_offset_sec

        # Calculate required shift: (Target Timezone Jump + Atomic Drift)
        tz_shift_sec = target_offset_sec - existing_offset_sec
        total_shift_sec = tz_shift_sec + global_drift

        # Idempotency: Do not double-shift if values are already perfect
        if total_shift_sec == 0 and existing_offset_str == global_offset:
            print(f"SKIPPING: {filename} [{frame_type}] (Time, Drift, and Offset already correct)")
            stats["skipped"] += 1
            continue

        shift_str = format_shift_string(total_shift_sec)
        print(f"\n--- PROCESSING: {filename} [{frame_type}] ---")

        update_cmd = [
            "exiftool", "-overwrite_original",
            f"-OffsetTime={global_offset}",
            f"-OffsetTimeOriginal={global_offset}",
            f"-OffsetTimeDigitized={global_offset}"
        ]

        # The Forensic Data Vault
        if has_gps:
            update_cmd.append("-XMP-tzshifter:LocationSource=Camera")
        elif manual_mode:
            update_cmd.append("-XMP-tzshifter:LocationSource=Manual")
        else:
            update_cmd.append("-XMP-tzshifter:LocationSource=Orphan")

        if total_shift_sec != 0:
            print(f"  ACTION: Shift clock by {shift_str} & Tag {global_offset}")
            update_cmd.append("-XMP-tzshifter:OriginalCameraTime<DateTimeOriginal")
            update_cmd.append(f"-AllDates{shift_str}")
        else:
            print(f"  ACTION: Clock drift is 0. Tagging offset {global_offset} only.")

        update_cmd.append(file_path)

        if args.dry_run:
            print(f"  [DRY RUN] {' '.join(update_cmd)}")
            if total_shift_sec != 0: stats["shifted"] += 1
            else: stats["tagged_only"] += 1
        else:
            result = subprocess.run(update_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print("  [SUCCESS] Metadata updated.")
                if total_shift_sec != 0: stats["shifted"] += 1
                else: stats["tagged_only"] += 1
            else:
                print(f"  [ERROR] ExifTool failed to modify {filename}.")
                print(f"  [EXIFTOOL MESSAGE] {result.stderr.strip()}")
                stats["errors"] += 1

    process_duration = time.time() - process_start_time
    total_duration = time.time() - script_start_time

    # --- 5. EXECUTION SUMMARY ---
    print("\n==================================================")
    print(" Execution Summary")
    print("==================================================")
    print(f" Total Valid Files Found : {stats['total']}")
    print(f"   - Key Frames (GPS)    : {stats['key_frames']}")
    print(f"   - Orphan Frames       : {stats['orphans']}")
    print("--------------------------------------------------")
    print(" Actions Taken:")
    print(f"   - Time Shifted        : {stats['shifted']}")
    print(f"   - Tagged Offset Only  : {stats['tagged_only']}")
    print(f"   - Skipped (Perfect)   : {stats['skipped']}")
    print(f"   - Errors              : {stats['errors']}")
    print("--------------------------------------------------")
    print(" Performance Metrics:")
    print(f"   - Metadata Scan Phase : {format_duration(scan_duration)}")
    print(f"   - File Write Phase    : {format_duration(process_duration)}")
    print(f"   - Total Script Time   : {format_duration(total_duration)}")
    print("==================================================\n")

if __name__ == "__main__":
    main()
