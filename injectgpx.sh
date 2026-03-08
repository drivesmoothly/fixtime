#!/bin/bash

# ==============================================================================
# Script: injectgpx (XMP Sidecar Edition)
# Purpose: Interpolates GPS coordinates from a GPX tracklog into XMP text files.
# ==============================================================================

# --- 1. Robust Error Handling & Validation ---
set -e

if ! command -v exiftool &> /dev/null; then
    echo "❌ CRITICAL ERROR: 'exiftool' is not installed or not in PATH."
    exit 1
fi

if [ "$#" -lt 2 ]; then
    echo "Usage: injectgpx /path/to/track.gpx /path/to/photos_directory [--dry-run]"
    exit 1
fi

GPX_FILE="$1"
DIRECTORY="$2"
DRY_RUN=0
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="injectgpx-${TIMESTAMP}.log"

for arg in "$@"; do
    if [ "$arg" == "--dry-run" ]; then
        DRY_RUN=1
    fi
done

if [ ! -f "$GPX_FILE" ]; then
    echo "❌ CRITICAL ERROR: GPX file '$GPX_FILE' does not exist."
    exit 1
fi

if [ ! -d "$DIRECTORY" ]; then
    echo "❌ CRITICAL ERROR: Directory '$DIRECTORY' does not exist."
    exit 1
fi

# Pre-flight Check: Ensure XMP files exist before doing any work
XMP_COUNT=$(find "$DIRECTORY" -maxdepth 1 -iname "*.xmp" | wc -l | tr -d ' ')
if [ "$XMP_COUNT" -eq 0 ]; then
    echo "❌ CRITICAL ERROR: No .xmp sidecars found in '$DIRECTORY'."
    echo "Please select the photos in Lightroom and press Cmd+S (Save Metadata to File) first."
    exit 1
fi

# --- 2. Forensic Configuration Generation ---
CONFIG_PATH="/tmp/tzshifter_gpx.config"
cat << 'EOF' > "$CONFIG_PATH"
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        tzshifter => { SubDirectory => { TagTable => 'Image::ExifTool::UserDefined::tzshifter' } },
    },
);
%Image::ExifTool::UserDefined::tzshifter = (
    GROUPS        => { 0 => 'XMP', 1 => 'XMP-tzshifter', 2 => 'Image' },
    NAMESPACE     => { 'tzshifter' => 'http://ns.tzshifter.com/1.0/' },
    WRITABLE      => 'string',
    OriginalCameraTime => { Writable => 'string' },
    LocationSource     => { Writable => 'string' },
);
1;
EOF

# --- 3. Core Execution Logic ---
run_injection() {
    echo ""
    echo "=================================================="
    echo " injectgpx - Forensic GPX Interpolation (XMP)"
    echo "=================================================="
    echo " 📂 Target Folder : $DIRECTORY"
    echo " 🗺️  GPX Track     : $GPX_FILE"
    echo " 📄 Target Files  : $XMP_COUNT XMP sidecars"
    echo " 📝 Log File      : $LOG_FILE"
    echo "=================================================="

    if [ $DRY_RUN -eq 1 ]; then
        echo " ⚠️  [DRY RUN ACTIVE] No text files will be modified."
    fi
    echo ""

    # ExifTool Command Array - Exclusively targets XMP
    CMD=(exiftool -config "$CONFIG_PATH" -if 'not $GPSLatitude' -api GeoMaxIntSecs=7200 -geotag "$GPX_FILE" "-Geotime<DateTimeOriginal" "-XMP-tzshifter:LocationSource=Orphan" -overwrite_original -ext xmp "$DIRECTORY")

    if [ $DRY_RUN -eq 1 ]; then
        echo "  [DRY RUN] Command that would execute:"
        echo "  ${CMD[*]}"
        echo ""
        echo "✅ SUCCESS: Dry run complete."
    else
        if "${CMD[@]}"; then
            echo ""
            echo "✅ SUCCESS: GPX text coordinates safely injected into XMP files."
            echo "Return to Lightroom, select all photos, and choose 'Read Metadata from File'."
        else
            echo ""
            echo "❌ CRITICAL ERROR: ExifTool encountered an issue."
            rm -f "$CONFIG_PATH"
            return 1
        fi
    fi

    rm -f "$CONFIG_PATH"
}

run_injection "$@" 2>&1 | tee "$LOG_FILE"
