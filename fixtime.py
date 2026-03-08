#!/usr/bin/env python3
import subprocess
import json
import os
import sys
import argparse
import time
import tempfile
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from typing import Optional, List, Dict, Tuple, Set, Any
from dataclasses import dataclass, field
from contextlib import contextmanager

# --- CONFIGURATION ---
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

# --- DATACLASSES ---

@dataclass
class PhotoPair:
    base_name: str
    xmp_path: Optional[str] = None
    raw_data: dict = field(default_factory=dict)
    xmp_data: dict = field(default_factory=dict)

@dataclass
class CorrectionStrategy:
    offset_str: str          # e.g. "+10:00"
    drift_seconds: int       # Total shift needed (e.g. -3635)
    timezone_name: str       # e.g. "Australia/Brisbane"
    is_manual: bool
    drift_distribution: List[Tuple[int, int]] = field(default_factory=list)
    total_gps_files: int = 0

@dataclass
class PipelineStats:
    total_images: int = 0
    total_xmps: int = 0
    key_frames: int = 0      # Has GPS
    orphans: int = 0         # No GPS
    shifted: int = 0         # Needs update
    already_processed: int = 0 # Idempotency lock
    perfectly_aligned: int = 0 # Time is correct, no update needed
    missing_xmp: int = 0     # Skipped because sidecar missing
    missing_data: int = 0    # No DateTimeOriginal in RAW
    gpx_injected: int = 0

# --- CONTEXT MANAGERS ---

@contextmanager
def temporary_argfile(lines: List[str]):
    fd, path = tempfile.mkstemp(suffix='.txt', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write(f"{line}\n")
        yield path
    finally:
        if os.path.exists(path):
            try: os.remove(path)
            except OSError: pass

@contextmanager
def exiftool_config():
    fd, path = tempfile.mkstemp(suffix='.config', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(EXIFTOOL_CONFIG_CONTENT)
        yield path
    finally:
        if os.path.exists(path):
            try: os.remove(path)
            except OSError: pass

# --- UTILS ---

def check_dependencies() -> None:
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ CRITICAL ERROR: ExifTool is not installed or not in PATH.")
        sys.exit(1)

def parse_offset(offset_str: Optional[str]) -> Optional[int]:
    if not offset_str or len(offset_str) < 5: return None
    try:
        sign = 1 if offset_str[0] == '+' else -1
        parts = offset_str[1:].replace('Z', '').split(':')
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        return sign * (h * 3600 + m * 60)
    except (ValueError, IndexError, AttributeError):
        return None

def format_shift(seconds: int) -> str:
    sign = "+=" if seconds >= 0 else "-="
    s = abs(int(seconds))
    return f"{sign}{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

def format_duration(seconds: float) -> str:
    if seconds < 60: return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    return f"{minutes}m {seconds % 60:.2f}s"

def decompose_drift(total_seconds: int) -> Tuple[int, int]:
    """
    Splits total drift into (Timezone_Jump, Actual_Drift).
    Heuristic: Rounds to nearest 30 mins (1800s) to guess the timezone error.
    """
    # Round to nearest 30 minutes (1800 seconds)
    tz_jump = round(total_seconds / 1800) * 1800
    actual_drift = total_seconds - tz_jump
    return tz_jump, actual_drift

def format_smart_time(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    s = abs(seconds)
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{sign}{h}h {m:02d}m"
    elif s >= 60:
        m = s // 60
        sec = s % 60
        return f"{sign}{m}m {sec:02d}s"
    else:
        return f"{sign}{s}s"

# --- CORE LOGIC ---

def scan_directory(target_dir: str) -> Tuple[List[str], Dict[str, str]]:
    raws, xmps, seen =[], {}, set()
    for f in sorted(os.listdir(target_dir)):
        if f.startswith('.'): continue
        path = os.path.join(target_dir, f)
        base = os.path.splitext(f)[0].lower()
        if f.lower().endswith('.xmp'):
            xmps[base] = path
        elif f.lower().endswith(('.cr3', '.jpg', '.dng', '.arw')) and base not in seen:
            seen.add(base)
            raws.append(path)
    return raws, xmps

def get_metadata(files: List[str], config: str) -> List[dict]:
    print(f"Scanning metadata for {len(files)} files...")
    with temporary_argfile(files) as argfile:
        cmd =[
            "exiftool", "-config", config, "-json", "-c", "%+.6f", "-q", "-q",
            "-DateTimeOriginal", "-GPSDateTime", "-GPSLatitude", "-GPSLongitude",
            "-OffsetTimeOriginal", "-OriginalCameraTime", "-@", argfile
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout) if res.stdout.strip() else[]
        except subprocess.CalledProcessError as e:
            print(f"❌ EXIFTOOL ERROR: {e.stderr.strip()}")
            return[]

def correlate(raw_data: List[dict], xmp_map: Dict[str, str]) -> Dict[str, PhotoPair]:
    pairs = {}
    for d in raw_data:
        src = d.get("SourceFile")
        if not src: continue
        base = os.path.splitext(os.path.basename(src))[0].lower()
        if base not in pairs:
            pairs[base] = PhotoPair(base, xmp_map.get(base))

        if src.lower().endswith('.xmp'): pairs[base].xmp_data = d
        else: pairs[base].raw_data = d
    return pairs

def analyze_drift(pairs: Dict[str, PhotoPair], args: argparse.Namespace) -> CorrectionStrategy:
    if args.target_timezone:
        if args.current_timezone and parse_offset(args.current_timezone) is None:
             sys.exit("❌ ERROR: Invalid --current-timezone format.")
        return CorrectionStrategy(
            offset_str=args.target_timezone,
            drift_seconds=args.drift if args.drift is not None else 0,
            timezone_name="Manual Override",
            is_manual=True
        )

    tf = TimezoneFinder()
    drifts, offsets = [],[]
    consensus_tz = "Unknown"

    for p in pairs.values():
        d = p.raw_data
        if "GPSLatitude" in d and "GPSDateTime" in d:
            try:
                # 1. True UTC Time from GPS
                utc_dt = datetime.strptime(d["GPSDateTime"][:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)

                # 2. Camera's Naive Time
                cam_dt = datetime.strptime(d["DateTimeOriginal"][:19], "%Y:%m:%d %H:%M:%S")

                # 3. Find Target Timezone and Local Offset
                lat, lon = float(d["GPSLatitude"]), float(d["GPSLongitude"])
                tz_name = tf.timezone_at(lat=lat, lng=lon)
                if not tz_name: continue

                local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
                off_sec = int(local_dt.utcoffset().total_seconds()) # type: ignore
                sign = "+" if off_sec >= 0 else "-"
                off_str = f"{sign}{abs(off_sec)//3600:02d}:{(abs(off_sec)%3600)//60:02d}"

                # 4. Determine Camera's assumed Offset
                cam_offset_str = d.get("OffsetTimeOriginal")
                cam_offset_sec = parse_offset(cam_offset_str)
                if cam_offset_sec is None:
                    # Fallback cleanly if no timezone tags exist on the photo
                    cam_offset_sec = parse_offset(args.current_timezone) if args.current_timezone else off_sec

                cam_tz = timezone(timedelta(seconds=cam_offset_sec))

                # 5. Make Camera Time Aware and convert to UTC
                cam_dt_aware = cam_dt.replace(tzinfo=cam_tz)

                # 6. PURE ATOMIC DRIFT: Difference in UTC reality vs UTC assumed by camera
                drift = round((utc_dt - cam_dt_aware).total_seconds())

                drifts.append(drift)
                offsets.append(off_str)
                consensus_tz = tz_name
            except (ValueError, KeyError, TypeError): continue

    if not drifts:
        sys.exit("❌ CRITICAL: No GPS data found. Use --target-timezone.")

    global_offset = Counter(offsets).most_common(1)[0][0]
    drift_counts = Counter(drifts)

    # --- SMART DRIFT LOGIC (Stale GPS Filter) ---
    max_freq = max(drift_counts.values())

    # 1. Identify all drifts that are statistically significant (at least 70% of max freq)
    # This prevents noise (1 or 2 files) from overriding the main clusters.
    threshold = max_freq * 0.7
    candidates =[d for d, c in drift_counts.items() if c >= threshold]

    # 2. Pick the algebraically LARGEST drift (least negative).
    # Rationale: Stale GPS timestamps are old (smaller), causing drift to be more negative.
    # The 'freshest' GPS data will always produce the largest drift value.
    calc_drift = max(candidates)

    return CorrectionStrategy(
        offset_str=global_offset,
        drift_seconds=args.drift if args.drift is not None else calc_drift,
        timezone_name=str(consensus_tz),
        is_manual=False,
        drift_distribution=drift_counts.most_common(10),
        total_gps_files=len(drifts)
    )

def plan_writes(pairs: Dict[str, PhotoPair], strat: CorrectionStrategy, args: argparse.Namespace) -> Tuple[List[str], PipelineStats, Set[str], List[str]]:
    stats = PipelineStats(total_images=len(pairs))
    cmds, orphan_xmps, file_logs = [], set(), []
    target_sec = parse_offset(strat.offset_str) or 0

    for p in pairs.values():
        try:
            if not p.raw_data or "DateTimeOriginal" not in p.raw_data:
                stats.missing_data += 1
                file_logs.append(f"   [SKIP]  {p.base_name.ljust(15)} | Missing DateTimeOriginal in RAW")
                continue

            if not p.xmp_path:
                stats.missing_xmp += 1
                if "GPSLatitude" not in p.raw_data: stats.orphans += 1
                file_logs.append(f"   [ERROR] {p.base_name.ljust(15)} | No XMP sidecar found")
                continue

            # Only count available XMPs
            stats.total_xmps += 1

            has_raw_gps = "GPSLatitude" in p.raw_data
            if has_raw_gps: stats.key_frames += 1
            else: stats.orphans += 1

            if not has_raw_gps: orphan_xmps.add(p.xmp_path)

            if any("OriginalCameraTime" in k for k in p.xmp_data.keys()):
                stats.already_processed += 1
                file_logs.append(f"   [LOCK]  {p.base_name.ljust(15)} | Idempotency lock active (Already processed)")
                continue

            # Math Logic
            curr_off_str = p.raw_data.get("OffsetTimeOriginal")
            curr_off_sec = parse_offset(curr_off_str)
            if curr_off_sec is None:
                curr_off_sec = parse_offset(args.current_timezone) if args.current_timezone else target_sec

            tz_shift = target_sec - (curr_off_sec or 0)
            total_shift = tz_shift + strat.drift_seconds

            if total_shift == 0 and curr_off_str == strat.offset_str:
                stats.perfectly_aligned += 1
                file_logs.append(f"   [OK]    {p.base_name.ljust(15)} | Perfectly aligned (Offset: {curr_off_str})")
                continue

            # Build ExifTool Command
            cmds.extend([
                "-overwrite_original",
                f"-OffsetTime={strat.offset_str}",
                f"-OffsetTimeOriginal={strat.offset_str}",
                f"-OffsetTimeDigitized={strat.offset_str}"
            ])

            src = "Camera" if has_raw_gps else ("Manual" if strat.is_manual else "Orphan")
            cmds.append(f"-XMP-tzshifter:LocationSource={src}")

            if total_shift != 0:
                cmds.append("-XMP-tzshifter:OriginalCameraTime<DateTimeOriginal")
                cmds.append(f"-AllDates{format_shift(total_shift)}")
                stats.shifted += 1
                file_logs.append(f"   [SHIFT] {p.base_name.ljust(15)} | Action: {format_shift(total_shift).ljust(12)} | New TZ: {strat.offset_str}")
            else:
                stats.perfectly_aligned += 1
                file_logs.append(f"   [OK]    {p.base_name.ljust(15)} | Time aligned | New TZ: {strat.offset_str}")

            cmds.append(p.xmp_path)
            cmds.append("-execute")

        except Exception as e:
            file_logs.append(f"   [FATAL] {p.base_name.ljust(15)} | Unhandled error calculating shift: {e}")
            print(f"❌ Error processing file {p.base_name}: {e}")

    return cmds, stats, orphan_xmps, file_logs

# --- UI ---

def print_summary(stats: PipelineStats, times: Dict[str, float], args: argparse.Namespace):
    print("\n==================================================")
    print(" Pipeline Execution Summary")
    print("==================================================")
    print(f" Total Photos        : {stats.total_images}")
    print(f" Total XMPs Found    : {stats.total_xmps}")
    print("--------------------------------------------------")
    print(" Phase 1 (Time Alignment):")
    print(f"   ✅ Shifted         : {stats.shifted}")
    print(f"   ✨ Already Aligned : {stats.perfectly_aligned}")
    print(f"   🔒 Already Locked  : {stats.already_processed} (Idempotent protection)")
    if stats.missing_xmp > 0:
        print(f"   ❌ NO XMP SIDECAR  : {stats.missing_xmp} (SKIPPED)")
        print("      👉 ACTION NEEDED: Select all in Lightroom -> Cmd+S")
    if stats.missing_data > 0:
        print(f"   ⚠️ Missing Exif    : {stats.missing_data}")
    print("--------------------------------------------------")
    if args.gpx:
        print(f" Phase 2 (GPX Injection):")
        print(f"   📍 Injected        : {stats.gpx_injected}")
    print("--------------------------------------------------")
    print(f" ⏱️  Dual Scan Phase   : {format_duration(times['scan'])}")
    print(f" ⏱️  Time Batch Write  : {format_duration(times['p1'])}")
    if args.gpx:
        print(f" ⏱️  GPX Batch Write   : {format_duration(times['p2'])}")
    print(f" ⏱️  Total Duration    : {format_duration(times['total'])}")
    print("==================================================\n")

def main():
    sys.stdout.reconfigure(line_buffering=True)
    t0 = time.time()
    times = {'scan': 0, 'p1': 0, 'p2': 0, 'total': 0}

    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--gpx", "-g")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-confirm", action="store_true")
    parser.add_argument("--target-timezone")
    parser.add_argument("--current-timezone")
    parser.add_argument("--drift", type=int)
    args = parser.parse_args()

    check_dependencies()
    target_dir = os.path.abspath(args.directory)
    if not os.path.isdir(target_dir): sys.exit("❌ Invalid Directory")

    print("\n==================================================")
    print(" Unified Forensic Metadata Pipeline")
    print("==================================================")
    print(f" 📂 Target : {target_dir}")
    if args.gpx: print(f" 🗺️  GPX    : {args.gpx}")

    # 1. DISCOVERY
    files, xmp_map = scan_directory(target_dir)
    print(f" 🔍 Inventory : {len(files)} images, {len(xmp_map)} sidecars")
    print("==================================================\n")

    if not files: sys.exit("❌ No image files found.")

    with exiftool_config() as cfg:
        # 2. SCAN
        t_scan = time.time()
        scan_list = files + list(xmp_map.values())
        raw_data = get_metadata(scan_list, cfg)
        pairs = correlate(raw_data, xmp_map)
        times['scan'] = time.time() - t_scan

        if not pairs: sys.exit("❌ No metadata extracted.")

        # 3. ANALYZE
        strat = analyze_drift(pairs, args)

        print("\n==================================================")
        print(" Phase 1: Master Timecode Sync Established")
        print("==================================================")
        print(f" 📍 Target Timezone : {strat.timezone_name} [{strat.offset_str}]")

        # SMART DRIFT DISPLAY
        tz_jump, actual_drift = decompose_drift(strat.drift_seconds)
        if abs(tz_jump) > 0:
            print(f" ⚠️  Timezone Error : {format_smart_time(tz_jump)} (Detected)")
            print(f" ⏱️  Atomic Drift   : {format_smart_time(actual_drift)}")
        else:
            print(f" ⏱️  Atomic Drift   : {format_smart_time(strat.drift_seconds)}")

        if not strat.is_manual and strat.drift_distribution:
            print("--------------------------------------------------")
            print(f" 📊 Drift Stats (Based on {strat.total_gps_files} GPS-tagged photos):")
            for drift, count in strat.drift_distribution:
                pct = (count / strat.total_gps_files) * 100
                mark = "⭐" if drift == strat.drift_seconds else ""

                # Format distribution lines nicely
                d_tz, d_act = decompose_drift(drift)
                if abs(d_tz) > 0:
                    drift_lbl = f"{format_smart_time(d_tz)} + {format_smart_time(d_act)}"
                else:
                    drift_lbl = f"{format_smart_time(drift)}"

                print(f"    {drift_lbl:>16} : {count:>4} ({pct:>5.1f}%) {mark}")
        print("==================================================")

        cmds, stats, orphans, file_logs = plan_writes(pairs, strat, args)

        # Print the newly generated File-by-File Log
        print("\n==================================================")
        print(" File-by-File Operation Log")
        print("==================================================")
        for log_entry in file_logs:
            print(log_entry)
        print("==================================================")

        # 5. CONFIRM
        if not args.no_confirm and cmds:
            print(f"\n⚠️  Proposed Changes:")
            print(f"   - Modify {stats.shifted} files")
            if stats.missing_xmp > 0:
                print(f"   - ❌ SKIP {stats.missing_xmp} files (Missing .xmp sidecars)")
            try:
                if input("   Proceed? (y/n): ").strip().lower() not in ['y', 'yes']: sys.exit(0)
            except KeyboardInterrupt: sys.exit(0)
        elif stats.missing_xmp > 0 and not cmds:
             print(f"\n❌ Cannot proceed: {stats.missing_xmp} photos are missing XMP sidecars.")
             print("   Please go to Lightroom -> Select All -> Metadata -> Save Metadata to File.")

        # 6. EXECUTE PHASE 1
        t_p1 = time.time()
        if args.dry_run:
            if cmds: print(f"\n[DRY RUN] Would update {stats.shifted} files.")
        elif cmds:
            print(f"\n⏳ Applying Phase 1 shifts...")
            with temporary_argfile(cmds) as arg:
                # -q suppresses "1 image files updated" noise
                # stdout=subprocess.DEVNULL ensures specific suppression
                subprocess.run(["exiftool", "-config", cfg, "-q", "-@", arg],
                               check=True, stdout=subprocess.DEVNULL)
        times['p1'] = time.time() - t_p1

        # 7. EXECUTE PHASE 2
        t_p2 = time.time()
        if args.gpx and orphans:
            valid_orphans = [o for o in orphans if o]
            if valid_orphans:
                print(f"\nPhase 2: Interpolating GPX for {len(valid_orphans)} orphans...")
                with temporary_argfile(valid_orphans) as arg:
                    cmd =["exiftool", "-config", cfg, "-if", "not $GPSLatitude",
                           "-api", "GeoMaxIntSecs=7200", "-geotag", args.gpx,
                           "-Geotime<DateTimeOriginal", "-XMP-tzshifter:LocationSource=Orphan",
                           "-overwrite_original", "-@", arg]
                    if args.dry_run:
                        print(f"  [DRY RUN] Would inject GPX into {len(valid_orphans)} files.")
                    else:
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        if m := re.search(r'(\d+)\s+image', res.stdout):
                            stats.gpx_injected = int(m.group(1))
            else:
                 print("\nPhase 2 Skipped: Orphan files exist but have no XMP sidecars to write to.")
        times['p2'] = time.time() - t_p2

    times['total'] = time.time() - t0
    print_summary(stats, times, args)

if __name__ == "__main__":
    main()
