# Asset Management: Stateful Odometer Renaming Strategy

## 1. The "Rocket-Proof" Philosophy
Standard camera filenames (`R52M9999.CR3`) are non-unique relics that roll over every 10,000 frames. Timestamp-based renaming (`20260305_120000.CR3`) is "brittle" because correcting a clock drift later makes the filename a lie.

This strategy implements a **Central Registry Odometer**. By decoupling the filename from the clock and the manufacturer's counter, every file receives a strictly incremental, globally unique identity that is traceable for the next 20+ years.

---

## 2. Infrastructure: The Synology Master Registry
A central "Source of Truth" is maintained on the NAS to track the life-cycle of every camera body.

**Location:** `/Volumes/Media/Reference/Camera_Odometers/`
**Registry Files:** Named by hardware Serial Number (e.g., `[145502100267]_R5m2.tsv`)

### Registry Schema (Audit Trail)
Each import session appends rows to the TSV to ensure perfect traceability:
| Odometer | Original Filename | Camera Timestamp | SubSec | Registry Entry Date |
| :--- | :--- | :--- | :--- | :--- |
| `00114323` | `R52M6733.CR3` | `2026:01:31 18:08:04` | `25` | `2026:03:05 09:00:00` |
| `00114324` | `R52M6734.CR3` | `2026:01:31 18:08:04` | `58` | `2026:03:05 09:00:00` |

---

## 3. The `cr3_odometer.py` Logic
This script acts as the final gatekeeper before photos are moved into the master library.

1.  **Hardware Identification:** Script extracts `SerialNumber` and `Model` from the first RAW file.
2.  **State Loading:** Script opens the matching registry on the Synology and reads the last line to find the starting odometer value.
3.  **Sequential Processing:** Files are sorted by `DateTimeOriginal` + `SubSecTimeOriginal` to ensure high-speed bursts (30fps) are numbered in perfect chronological order.
4.  **Renaming:** Files are renamed to a permanent format: `[CameraModel]_[8-Digit-Odometer].[ext]`.
    * *Example:* `R5m2_00114323.CR3`
5.  **Audit Commit:** The script appends the session data back to the Synology registry and closes the file.

---

## 4. Key Advantages
* **Collision Immunity:** An 8-digit counter allows for 100 million unique photos per camera body.
* **Midnight/Burst Proof:** By using a stateful counter instead of a clock-string, frames taken at 30fps or across midnight boundaries never collide or scramble.
* **Lightroom Safety:** Renaming occurs *before* import, ensuring Lightroom's database tracks the permanent, unique ID from day one.
* **Traceability:** If a file is ever questioned, the registry allows you to trace `R5m2_00114323.CR3` back to its original manufacturer name and exact capture moment.

---

## 5. Implementation Roadmap
* [ ] Initialize `Reference/Camera_Odometers` folder on Synology.
* [ ] Create "Seed" files for existing R5 and R5m2 bodies.
* [ ] Develop `cr3_odometer.py` with multi-camera support.
