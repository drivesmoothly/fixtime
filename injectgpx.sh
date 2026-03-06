#!/bin/bash

# ==============================================================================
# Script: injectgpx
# Purpose: Interpolates GPS coordinates from a GPX tracklog into orphaned photos.
# ==============================================================================

if [ "$#" -lt 2 ]; then
    echo "Usage: injectgpx /path/to/track.gpx /path/to/photos_directory [--dry-run]"
    exit 1
fi

GPX_FILE="$1"
DIRECTORY="$2"
DRY_RUN=0
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="injectgpx-${TIMESTAMP}.log"

# Check for dry-run anywhere in the arguments
for arg in "$@"; do
    if [ "$arg" == "--dry-run" ]; then
        DRY_RUN=1
    fi
done

if [ ! -f "$GPX_FILE" ]; then
    echo "CRITICAL ERROR: GPX file '$GPX_FILE' does not exist."
    exit 1
fi

if [ ! -d "$DIRECTORY" ]; then
    echo "CRITICAL ERROR: Directory '$DIRECTORY' does not exist."
    exit 1
fi

# Define the core logic in a function so we can pipe its entire output to tee/less
run_injection() {
    COMMAND_LINE="$0 $@"

    echo ""
    echo "=================================================="
    echo " injectgpx - Forensic GPX Interpolation"
    echo "=================================================="
    echo " 📂 Target Folder : $DIRECTORY"
    echo " 🗺️  GPX Track     : $GPX_FILE"
    echo " 💻 Command       : $COMMAND_LINE"
    echo " 📝 Log File      : $LOG_FILE"
    echo "=================================================="

    if [ $DRY_RUN -eq 1 ]; then
        echo " ⚠️  [DRY RUN ACTIVE] No files will be modified."
    fi
    echo ""

    # ExifTool Command Array
    CMD=(exiftool -if 'not $GPSLatitude' -api GeoMaxIntSecs=7200 -geotag "$GPX_FILE" "-Geotime<SubSecDateTimeOriginal" -overwrite_original -ext cr3 -ext jpg "$DIRECTORY")

    if [ $DRY_RUN -eq 1 ]; then
        echo "  [DRY RUN] Command that would execute:"
        echo "  ${CMD[*]}"
        echo ""
        echo "SUCCESS: Dry run complete. Run without --dry-run to apply changes."
    else
        if "${CMD[@]}"; then
            echo ""
            echo "SUCCESS: GPX coordinates successfully injected into orphaned frames."
        else
            echo ""
            echo "CRITICAL ERROR: ExifTool encountered a read/write issue."
            return 1
        fi
    fi
}

# Execute the function, pipe to log, and view in less
# 2>&1 ensures errors are also logged
run_injection "$@" 2>&1 | tee "$LOG_FILE" | less -R
