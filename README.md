# cue2pops.py (Python Version)

This project is a refactored and optimized Python implementation of **cue2pops**, designed to run cross-platform, including Android devices via Termux.

## Key Improvements

The original C codebase has been completely refactored into modern Python with a focus on code safety, maintainability, and clean execution.

* **Native Python Implementation:** No C compilation required—runs directly with Python 3.
* **Robust File Handling:** Powered by `pathlib` for safe cross-platform path resolution and automatic input/output directory creation.
* **Smart Resource Management:** Uses contextual file streaming to prevent memory leaks and optimize I/O performance.
* **Process Interruption Safety:** Supports graceful termination (`SIGINT`/`SIGTERM`) with atomic temporary file replacements to prevent corrupted output files.
* **Game Patching & Auto-Detection:** Retains built-in support for game signature identification, trainer injections, and compatibility fixes.

## Requirements

* Python 3.8 or higher
* Single-BIN format (`.cue` + single `.bin`). Multi-BIN dumps must be combined into a single BIN before conversion.

## Usage & Examples

### 1. Basic Conversion (Default)
Converts a `.cue` file and automatically generates a `.VCD` file in the same directory using the same base name:

```bash
python3 rcue2pops.py "Vigilante 8 - 2nd Offense (USA).cue"
```
## 2. Custom Output File Name
Specify a custom output path or filename (e.g., standard POPS IMAGE0.VCD format):
```bash
python3 rcue2pops.py "Vigilante 8 - 2nd Offense (USA).cue" IMAGE0.VCD
```

## 3. Debug & Header Validation
Use the --debug-validate flag to print header structure details (BCD markers, sector count, Lead-Out MSF):
```bash
python3 rcue2pops.py "Vigilante 8 - 2nd Offense (USA).cue" IMAGE0_PY.VCD --debug-validate
```

## 4. Advanced Patching Flags
Pass additional POPS arguments such as vmode to apply video fixes (e.g., PAL to NTSC conversion):
```bash
python3 rcue2pops.py "EuropeanGame.cue" IMAGE0.VCD vmode
```

