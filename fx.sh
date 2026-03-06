#!/bin/bash

# --- Forensic Metadata Pipeline: fixtime Wrapper ---

# 1. Automatically locate fixtime.py in the same folder as this wrapper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
FIXTIME_APP="$SCRIPT_DIR/fixtime.py"

if [ ! -f "$FIXTIME_APP" ]; then
    echo "❌ Error: 'fixtime.py' not found in $SCRIPT_DIR."
    exit 1
fi

# 2. Check for the directory argument
if [ "$#" -lt 1 ]; then
    echo "Usage: fx <directory> [options]"
    exit 1
fi

# 3. Setup variables
TARGET_DIR="$1"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="fixtime-${TIMESTAMP}.log"

# 4. Execute with full pass-through
echo "🚀 Executing fixtime on: $TARGET_DIR"
echo "📝 Log: $LOG_FILE"
echo "------------------------------------------------"

# Run the python script directly using the absolute path we found
python3 "$FIXTIME_APP" "$@" 2>&1 | tee "$LOG_FILE" | less -R
