# Forensic Metadata Pipeline: fixtime & injectgpx

This two-step pipeline perfectly synchronizes camera RAW files (.CR3, .JPG) with smartphone GPS tracklogs, preserving original metadata as hidden forensic backups.

---

## Step 1: fixtime (Timezone & Drift Alignment)

Because camera internal clocks drift and do not natively record timezones, combining naive photos with absolute UTC GPX tracklogs results in mapping errors. fixtime calculates the exact sub-second clock drift, applies a uniform temporal shift, and tags the correct timezone offset so the files are "GPX-Ready."

### Usage Syntax

**Automated (GPS Auto-Detect)**
Requires at least one Key Frame (a photo with native GPS data) in the folder to calculate the consensus timezone and atomic drift automatically.

    fixtime /path/to/photos [--dry-run]

**Manual Override ("Day 0" / No GPS)**
Used when no photos in the folder contain GPS data. You must manually define the camera's original timezone, the target timezone, and the known clock error.

    fixtime /path/to/photos --current-timezone='<offset>' --target-timezone='<offset>' --drift='<seconds>' [--dry-run]

### Examples & Tips

* **Standard Auto Run:**

    fixtime '/Volumes/Media/Photos/2026-01-20'

* **Manual Shift (e.g., Camera set to Munich, traveling to GMT-8):**
  *Note: Use the `=` sign for negative values to prevent shell parsing errors in macOS/zsh.*

    fixtime '/Volumes/Media/Photos/2026-01-20' --current-timezone='+01:00' --target-timezone='-08:00' --drift='-26'

* **Safety Check:**

    fixtime '/Volumes/Media/Photos/2026-01-20' --dry-run

### Forensic Features
* **Idempotency:** Re-running the script will not double-shift already corrected files.
* **Non-Destructive Math:** Uses relative shifts (-AllDates-=) to preserve exact sub-second intervals between high-speed burst frames.
* **XMP Data Vault:** Backs up the original, unshifted timestamp to a custom, Lightroom-proof namespace (XMP-tzshifter:OriginalCameraTime) and tags the source as Camera, Manual, or Orphan.

---

## Step 2: injectgpx (Coordinate Interpolation)

Once fixtime has aligned your photo timestamps to the absolute UTC timeline, injectgpx reads those corrected timestamps and interpolates the exact coordinates from your .gpx tracklog.

### Usage Syntax

    injectgpx /path/to/track.gpx /path/to/photos_directory [--dry-run]

### Examples

* **Standard Injection:**

    injectgpx '/Volumes/Media/Logs/munich_walk.gpx' '/Volumes/Media/Photos/2026-01-20'

* **Safety Check:**

    injectgpx '/Volumes/Media/Logs/munich_walk.gpx' '/Volumes/Media/Photos/2026-01-20' --dry-run

### Safety Features
* **The Bouncer:** Strictly ignores any photo that already has native GPS coordinates (-if 'not $GPSLatitude'). Ground-truth data recorded by the camera is never overwritten.
* **The Bridge:** Automatically interpolates missing locations across gaps in your tracklog of up to 2 hours (-api GeoMaxIntSecs=7200).
* **Precision Match:** Maps the GPX pings directly to the sub-second timestamps (-Geotime<SubSecDateTimeOriginal) established during the fixtime sync.
