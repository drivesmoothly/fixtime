#!/usr/bin/env python3
import subprocess
import json
import os
import sys
import argparse
import time
import tempfile
import glob
import re
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder

# --- FORENSIC CONFIGURATION ---
EXIFTOOL_CONFIG_CONTENT = """
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        tzshifter => {
            SubDirectory => {
                TagTable => 'Image::ExifTool::UserDefined::tzshifter',
            },
        },
    },
);

%Image::ExifTool::UserDefined::tzshifter = (
    GROUPS        => { 0 => 'XMP', 1 => 'XMP-tzshifter', 2 => 'Image' },
    NAMESPACE     => { 'tzshifter' => 'http://ns.tzshifter.com/1.0/' },
    WRITABLE      => 'string',
    OriginalCameraTime => { Writable => 'string' },
    LocationSource     => { Writable => 'string' },
);
1;  #end
"""

def create_exiftool_config():
    try:
        fd, path = tempfile.mkstemp(suffix='.config', text=True)
        with os.fdopen(fd, 'w') as f:
            f.write(EXIFTOOL_CONFIG_CONTENT)
        return path
    except Exception as e:
        print(f"❌ CRITICAL ERROR: Failed to write temporary ExifTool config. {e}")
        sys.exit(1)

def check_dependencies():
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ CRITICAL ERROR: ExifTool is not installed or not in your system PATH.")
        sys.exit(1)

def get_bulk_exif_data(argfile_path, config_path):
    print("Scanning metadata (Reading RAW baselines + checking XMP states)...")
    cmd =[
        "exiftool", "-config", config_path, "-json", "-c", "%+.6f", "-q", "-q",
        "-DateTimeOriginal", "-GPSDateTime",
        "-GPSLatitude", "-GPSLongitude",
        "-OffsetTimeOriginal",
        "-OriginalCameraTime", # Checks the Forensic Vault
        "-@", argfile_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            return json.loads(result.stdout)
        return[]
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR reading metadata: ExifTool encountered an issue.")
        print(f"EXIFTOOL ERROR: {e.stderr.strip()}")
        return[]

def parse_offset_string_to_seconds(offset_str):
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
    sign = "+=" if total_seconds >= 0 else "-="
    secs = abs(int(total_seconds))
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{sign}{h}:{m:02d}:{s:02d}"

def get_gps_stats(data, tf):
    try:
        gps_utc_str = data["GPSDateTime"].replace("Z", "").strip()[:19]
        cam_time_str = data["DateTimeOriginal"][:19]

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
    sys.stdout.reconfigure(line_buffering=True)
    script_start_time = time.time()

    parser = argparse.ArgumentParser(description="Unified Idempotent Forensic Metadata Pipeline.")
    parser.add_argument("directory", help="Path to the folder containing your CR3 and XMP files")
    parser.add_argument("--gpx", "-g", help="Optional: Path to GPX tracklog file for Phase 2 coordinate injection")
    parser.add_argument("--dry-run", action="store_true", help="Run the math without modifying files")
    parser.add_argument("--no-confirm", action="store_true", help="Bypass manual verification pause for automation")
    parser.add_argument("--target-timezone", help="Manually specify TARGET timezone offset (e.g., -08:00)")
    parser.add_argument("--current-timezone", help="The timezone the camera clock was actually set to (e.g., +01:00)")
    parser.add_argument("--drift", type=int, default=0, help="Manually specify atomic drift in seconds")
    args = parser.parse_args()

    check_dependencies()

    target_dir = os.path.abspath(args.directory)
    if not os.path.isdir(target_dir):
        print(f"❌ ERROR: Directory '{target_dir}' does not exist.")
        sys.exit(1)

    gpx_path = None
    if args.gpx:
        gpx_path = os.path.abspath(args.gpx)
        if not os.path.isfile(gpx_path):
            print(f"❌ CRITICAL ERROR: GPX file '{gpx_path}' does not exist.")
            sys.exit(1)

    print("\n==================================================")
    print(" Unified Forensic Metadata Pipeline")
    print("==================================================")
    print(f" 📂 Target Folder : {target_dir}")
    if gpx_path:
        print(f" 🗺️  GPX Track     : {gpx_path}")
    print(f" 💻 Command       : {' '.join(sys.argv)}")
    print("==================================================\n")

    tf = TimezoneFinder()
    config_path = create_exiftool_config()

    try:
        valid_raw_files =[]
        xmp_map = {}
        seen = set()

        for filename in sorted(os.listdir(target_dir)):
            if filename.startswith('.'):
                continue

            lower_name = filename.lower()
            base = os.path.splitext(lower_name)[0]

            if lower_name.endswith('.xmp'):
                xmp_map[base] = os.path.join(target_dir, filename)
            elif lower_name.endswith(('.cr3', '.jpg')):
                if base not in seen:
                    seen.add(base)
                    valid_raw_files.append(os.path.join(target_dir, filename))

        xmp_count = len(xmp_map)
        if xmp_count == 0:
            print(f"❌ CRITICAL ERROR: No .xmp sidecar files found in {target_dir}.")
            print("Please go to Lightroom, select all photos, and press Cmd+S (Save Metadata to File) first.")
            sys.exit(1)

        if not valid_raw_files:
            print("❌ No valid CR3 or JPG files found to read baseline data from.")
            sys.exit(0)

        # Build combined argfile to scan both RAW (for baseline) and XMP (for idempotency checks)
        scan_files = valid_raw_files + list(xmp_map.values())
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_argfile:
            for file_path in scan_files:
                temp_argfile.write(f"{file_path}\n")
            argfile_path = temp_argfile.name

        scan_start_time = time.time()
        try:
            raw_data = get_bulk_exif_data(argfile_path, config_path)
        finally:
            os.remove(argfile_path)

        if not raw_data:
            print("No EXIF data extracted.")
            sys.exit(0)

        # Pair RAW and XMP data in RAM
        paired_data = {}
        for data in raw_data:
            path = data.get("SourceFile")
            if not path: continue
            base = os.path.splitext(os.path.basename(path))[0].lower()
            ext = os.path.splitext(path)[1].lower()

            if base not in paired_data:
                paired_data[base] = {'raw': {}, 'xmp': {}}

            if ext == '.xmp':
                paired_data[base]['xmp'] = data
            else:
                paired_data[base]['raw'] = data

        scan_duration = time.time() - scan_start_time

        manual_mode = False
        if args.target_timezone:
            global_offset = args.target_timezone
            global_drift = args.drift
            consensus_tz = "Manual Override"
            manual_mode = True

            if args.current_timezone and parse_offset_string_to_seconds(args.current_timezone) is None:
                print("❌ CRITICAL ERROR: Invalid --current-timezone format.")
                sys.exit(1)

            cam_info = f" (Camera assumed at {args.current_timezone})" if args.current_timezone else ""
            print(f"⚠️  Using MANUAL override: Target Timezone {global_offset}{cam_info}, Drift {global_drift}s")
        else:
            all_drifts = []
            all_offsets =[]
            consensus_tz = "Unknown"

            for data_pair in paired_data.values():
                raw_info = data_pair['raw']
                if "GPSLatitude" in raw_info and "GPSDateTime" in raw_info:
                    drift, offset, tz = get_gps_stats(raw_info, tf)
                    if drift is not None:
                        all_drifts.append(drift)
                        all_offsets.append(offset)
                        consensus_tz = tz

            if not all_drifts:
                print("❌ CRITICAL ERROR: No photos with GPS data found. Use --target-timezone to override.")
                sys.exit(1)

            offset_counts = Counter(all_offsets)
            global_offset = offset_counts.most_common(1)[0][0]
            drift_counts = Counter(all_drifts)
            max_frequency = max(drift_counts.values())

            if max_frequency >= 2:
                most_common_drifts =[d for d, c in drift_counts.items() if c == max_frequency]
                global_drift = max(most_common_drifts)
            else:
                global_drift = max(all_drifts)

        target_offset_sec = parse_offset_string_to_seconds(global_offset)

        print("\n==================================================")
        print(" Phase 1: Master Timecode Sync Established")
        print("==================================================")
        print(f" 📍 Target Timezone : {consensus_tz} [{global_offset}]")
        print(f" ⏱️ Atomic Drift    : {global_drift} seconds")
        print("==================================================")

         # --- BATCH TIME ALIGNMENT PREP ---
        stats = {"total": len(paired_data), "key_frames": 0, "orphans": 0, "shifted": 0, "tagged_only": 0, "skipped": 0, "errors": 0}
        process_start_time = time.time()

        write_instructions = []
        orphan_xmps =[]

        for base, data_pair in paired_data.items():
            raw_info = data_pair['raw']
            xmp_info = data_pair['xmp']
            xmp_path = xmp_map.get(base)

            if not xmp_path or not raw_info:
                stats["skipped"] += 1
                continue

            if "DateTimeOriginal" not in raw_info:
                stats["skipped"] += 1
                continue

            has_raw_gps = "GPSLatitude" in raw_info and "GPSDateTime" in raw_info

            if has_raw_gps:
                stats["key_frames"] += 1
            else:
                stats["orphans"] += 1

            # --- IDEMPOTENCY LOCK ---
            # If the XMP already contains our Forensic Vault, Phase 1 was already completed successfully.
            is_processed = any("OriginalCameraTime" in k for k in xmp_info.keys())

            if is_processed:
                stats["skipped"] += 1
                # Even if Phase 1 is skipped, we evaluate if Phase 2 GPX is still needed
                has_xmp_gps = any("GPSLatitude" in k for k in xmp_info.keys())
                if not has_raw_gps and not has_xmp_gps:
                    orphan_xmps.append(xmp_path)
                continue

            # Queue for GPX if no GPS found natively
            if not has_raw_gps:
                orphan_xmps.append(xmp_path)

            existing_offset_str = raw_info.get("OffsetTimeOriginal")
            existing_offset_sec = parse_offset_string_to_seconds(existing_offset_str)
            if existing_offset_sec is None:
                if args.current_timezone:
                    existing_offset_sec = parse_offset_string_to_seconds(args.current_timezone)
                else:
                    existing_offset_sec = target_offset_sec

            tz_shift_sec = target_offset_sec - existing_offset_sec
            total_shift_sec = tz_shift_sec + global_drift

            if total_shift_sec == 0 and existing_offset_str == global_offset:
                stats["skipped"] += 1
                continue

            shift_str = format_shift_string(total_shift_sec)

            write_instructions.append("-overwrite_original")
            write_instructions.append(f"-OffsetTime={global_offset}")
            write_instructions.append(f"-OffsetTimeOriginal={global_offset}")
            write_instructions.append(f"-OffsetTimeDigitized={global_offset}")

            if has_raw_gps:
                write_instructions.append("-XMP-tzshifter:LocationSource=Camera")
            elif manual_mode:
                write_instructions.append("-XMP-tzshifter:LocationSource=Manual")
            else:
                write_instructions.append("-XMP-tzshifter:LocationSource=Orphan")

            if total_shift_sec != 0:
                write_instructions.append("-XMP-tzshifter:OriginalCameraTime<DateTimeOriginal")
                write_instructions.append(f"-AllDates{shift_str}")
                stats["shifted"] += 1
            else:
                stats["tagged_only"] += 1

            write_instructions.append(xmp_path)
            write_instructions.append("-execute")

        if not args.no_confirm and write_instructions:
            print("\n⚠️  Please review the calculated timezone and drift above.")
            print(f"\n Would apply Phase 1 shifts to {stats['shifted']} files.")
            try:
                user_confirm = input("Do you want to apply this shift to the XMP files? (y/n): ").strip().lower()
                if user_confirm not in ['y', 'yes']:
                    print("\n🛑 Execution aborted by user. No files were modified.")
                    sys.exit(0)
            except KeyboardInterrupt:
                print("\n\n🛑 Execution aborted via keyboard interrupt. No files were modified.")
                sys.exit(0)

        # --- EXECUTE PHASE 1 (TIME) ---
        if args.dry_run:
            if write_instructions:
                print(f"\n  [DRY RUN] Would apply Phase 1 shifts to {stats['shifted']} files (Skipping {stats['skipped']} already processed).")
            else:
                print("\n✅ Phase 1: All files are already perfectly aligned (Idempotency locked).")
            process_duration = time.time() - process_start_time
        elif write_instructions:
            print(f"\n⏳ Applying timezone shifts to {stats['shifted']} XMP files...")
            with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as write_argfile:
                for item in write_instructions:
                    write_argfile.write(f"{item}\n")
                write_argfile_path = write_argfile.name

            try:
                write_cmd =["exiftool", "-config", config_path, "-@", write_argfile_path]
                subprocess.run(write_cmd, capture_output=True, text=True)
            finally:
                os.remove(write_argfile_path)
            process_duration = time.time() - process_start_time
        else:
            print("\n✅ Phase 1: No time shifts needed. All files perfectly aligned or previously processed.")
            process_duration = 0

        # --- PHASE 2: GPX INJECTION ---
        gpx_injected_count = 0
        gpx_duration = 0

        if gpx_path:
            print("\n==================================================")
            print(" Phase 2: Forensic GPX Interpolation")
            print("==================================================")

            if not orphan_xmps:
                print("✅ No orphaned frames detected. GPX injection skipped.")
            else:
                with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as gpx_argfile:
                    for orphan_xmp in set(orphan_xmps):
                        gpx_argfile.write(f"{orphan_xmp}\n")
                    gpx_argfile_path = gpx_argfile.name

                try:
                    gpx_cmd =[
                        "exiftool", "-config", config_path,
                        "-if", "not $GPSLatitude",
                        "-api", "GeoMaxIntSecs=7200",
                        "-geotag", gpx_path,
                        "-Geotime<DateTimeOriginal",
                        "-XMP-tzshifter:LocationSource=Orphan",
                        "-overwrite_original",
                        "-@", gpx_argfile_path
                    ]

                    if args.dry_run:
                        print(f"  [DRY RUN] Will attempt to inject GPX into {len(set(orphan_xmps))} orphaned files.")
                    else:
                        print(f"⏳ Interpolating GPX data for {len(set(orphan_xmps))} orphaned frames...")
                        gpx_start_time = time.time()

                        result = subprocess.run(gpx_cmd, capture_output=True, text=True)

                        updated_match = re.search(r'(\d+)\s+image files updated', result.stdout)
                        if updated_match:
                            gpx_injected_count = int(updated_match.group(1))
                            print(f"✅ SUCCESS: {gpx_injected_count} orphaned files successfully geotagged.")
                        else:
                            print("⚠️  No orphaned files required GPX injection, or limit (GeoMaxIntSecs) exceeded.")

                        gpx_duration = time.time() - gpx_start_time
                finally:
                    os.remove(gpx_argfile_path)

        total_duration = time.time() - script_start_time

        # --- SUMMARY ---
        print("\n==================================================")
        print(" Pipeline Execution Summary")
        print("==================================================")
        print(f" Total Photos Analyzed   : {stats['total']}")
        print(f"   - Key Frames (GPS)    : {stats['key_frames']}")
        print(f"   - Orphan Frames       : {stats['orphans']}")
        print("--------------------------------------------------")
        print(" Phase 1: Time Alignment (XMP)")
        print(f"   - Time Shifted        : {stats['shifted']}")
        print(f"   - Skipped (Perfect)   : {stats['skipped']}")
        if gpx_path:
            print("--------------------------------------------------")
            print(" Phase 2: GPX Injection (XMP)")
            print(f"   - Queued Orphans      : {len(set(orphan_xmps))}")
            if not args.dry_run:
                print(f"   - Geotagged Orphans   : {gpx_injected_count}")
        print("--------------------------------------------------")
        print(" Performance Metrics:")
        print(f"   - Dual Scan Phase     : {format_duration(scan_duration)}")
        print(f"   - Time Batch Write    : {format_duration(process_duration)}")
        if gpx_path:
            print(f"   - GPX Batch Write     : {format_duration(gpx_duration)}")
        print(f"   - Total Script Time   : {format_duration(total_duration)}")
        print("==================================================\n")

        if not args.dry_run:
            print("✅ FINISHED. Return to Lightroom, select all photos, and choose 'Read Metadata from File'.\n")

    finally:
        if os.path.exists(config_path):
            try:
                os.remove(config_path)
            except Exception:
                pass

if __name__ == "__main__":
    main()
