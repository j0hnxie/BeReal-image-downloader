# BeReal Image Downloader

Desktop app for browsing a BeReal GDPR export and exporting images into a cleaner local photo library.

The app reads your BeReal export, lets you preview each post in multiple output formats, tracks what has already been exported, and writes images with capture time and GPS metadata embedded in the JPEG.

## What This App Does

- Loads a BeReal GDPR export folder that contains `memories.json` and `Photos/`
- Pairs front and back camera images for each memory
- Exports images in four modes:
  - `Front only`
  - `Back only`
  - `BeReal style (front top-left)`
  - `BeReal style (back top-left)`
- Generates previews in the app using the same render pipeline used for final export
- Lets you browse by table or image scroller
- Supports multi-selection, range selection, keyboard navigation, and preview shortcuts
- Tracks prior exports per photo and per mode
- Supports re-exporting and overwriting existing exports with confirmation

## Source Data Expected

The app expects a BeReal GDPR export directory with this structure:

```text
<export root>/
  memories.json
  Photos/
    ...
```

The app reads:

- `memories.json`
  - `takenTime`
  - `berealMoment`
  - `date`
  - `isLate`
  - `caption`
  - `location.latitude`
  - `location.longitude`
  - `frontImage.path`
  - `backImage.path`
- `Photos/...`
  - the original front/back image files referenced by `memories.json`

You can point the app either at the exact export folder or at a parent directory that contains it. The loader will try to auto-detect the correct nested export directory.

## Output Layout

Exports are written under:

```text
~/Downloads/BeReal-Exports
```

Current layout:

- Exported JPEG images are written into the top-level output folder directly
- Metadata JSON files are written into dated subfolders under the same root

Example:

```text
~/Downloads/BeReal-Exports/
  2026-03-19 18.15.44 - BeReal Front Top Left.jpg
  2026-03-19 18.15.44 - Front Only.jpg
  2026/
    2026-03-19/
      2026-03-19 18.15.44 - BeReal Front Top Left.json
      2026-03-19 18.15.44 - Front Only.json
```

Why it is structured this way:

- JPEGs stay in one folder, which makes drag-and-drop or copy/paste into other apps much easier
- Metadata remains organized by date
- Download history can validate against metadata files without requiring the image to stay in a nested date tree

## File Naming

Exported files use human-readable names:

```text
YYYY-MM-DD HH.MM.SS - <Mode Label>.jpg
```

Examples:

- `2026-03-19 18.15.44 - Front Only.jpg`
- `2026-03-19 18.15.44 - Back Only.jpg`
- `2026-03-19 18.15.44 - BeReal Front Top Left.jpg`
- `2026-03-19 18.15.44 - BeReal Back Top Left.jpg`

If a filename already exists and you are not overwriting it, the exporter appends ` (2)`, ` (3)`, and so on.

## Metadata Written to Exported JPEGs

Each exported JPEG includes:

- EXIF capture time
  - `DateTime`
  - `DateTimeOriginal`
  - `DateTimeDigitized`
- GPS EXIF metadata when the BeReal memory contains a location
  - latitude
  - longitude
  - GPS date
  - GPS time
- File modification time set to the BeReal capture time

Each export also gets a sidecar JSON file containing:

- export mode
- export mode label
- BeReal capture time
- BeReal moment
- BeReal date
- late/on-time status
- caption
- location
- source front image path
- source back image path
- exported output path
- exported timestamp

## Download History

The app stores export history separately from the output folder.

History file location:

- macOS: `~/Library/Application Support/BeRealDownloader/history.json`
- Linux: `~/.local/share/bereal-downloader/history.json`
- Windows: `%APPDATA%/BeRealDownloader/history.json`

History is tracked per:

- BeReal photo
- export mode

Important behavior:

- The app uses the recorded metadata JSON path to confirm an export still exists
- If the metadata file is gone, that export record is pruned automatically
- If the JPEG is gone but the metadata JSON remains, the mode still counts as downloaded in history, but preview reuse falls back to re-rendering from source images

## Image Composition

For BeReal-style exports, the app creates a composite image with:

- the base image filling the full frame
- the secondary image inset in the top-left
- rounded corners on the inset
- a thin black border around the inset
- no BeReal watermark

The same composition logic is used for:

- final exports
- scroller previews
- popup preview window

## Requirements

- macOS, Linux, or Windows
- Python 3.10+ recommended
- `tkinter`
- Pillow

`requirements.txt` currently contains:

```text
Pillow>=10.0.0
```

## Python Recommendation on macOS

Use a normal Python install with working `tkinter`, such as the Python.org installer.

Avoid:

- broken Conda `tkinter` builds
- old Xcode-bundled Python + Tk combinations

If you previously saw Tk startup crashes or macOS/Tk version errors, rebuild the virtual environment using your Python.org interpreter rather than Conda or Xcode Python.

Example:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python bereal_downloader_app.py
```

## Setup

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

From the project root:

```bash
source .venv/bin/activate
python bereal_downloader_app.py
```

## Make Targets

This repository now includes a [Makefile](/Users/johnxie/Documents/Personal/Projects/bereal-data/Makefile) for setup, validation, app bundling, installation, and DMG creation.

List available commands:

```bash
make help
```

Important targets:

- `make venv`
  - create the local virtual environment
- `make deps`
  - install Python dependencies into `.venv`
- `make doctor`
  - verify Python, `tkinter`, and Pillow
- `make run`
  - run the app from source
- `make icon`
  - build the macOS `.icns` icon from `icon.png`
- `make app-bundle`
  - build `dist/BeReal Image Downloader.app`
- `make install-app`
  - install the built app bundle into `/Applications`
- `make uninstall-app`
  - remove the installed app from `/Applications`
- `make reinstall-app`
  - uninstall and reinstall the app
- `make open-app`
  - open the locally built app bundle
- `make dmg`
  - build `dist/BeReal Image Downloader.dmg`
- `make clean`
  - remove generated build and dist artifacts
- `make distclean`
  - remove generated build artifacts and `.venv`

## macOS App Bundle and Installation

The Makefile builds a real macOS `.app` bundle with:

- your app code
- the generated `.icns` icon built from `icon.png`
- an app launcher script
- a copied version of the project virtual environment

Build the app bundle:

```bash
make app-bundle
```

This creates:

```text
dist/BeReal Image Downloader.app
```

Install it into `/Applications`:

```bash
make install-app
```

After that, you can launch it from:

- Finder
- Spotlight
- Launchpad
- `/Applications/BeReal Image Downloader.app`

Open the local built bundle without installing:

```bash
make open-app
```

Uninstall the installed app:

```bash
make uninstall-app
```

Reinstall after code changes:

```bash
make reinstall-app
```

## DMG Creation

Build a DMG containing the app bundle:

```bash
make dmg
```

This creates:

```text
dist/BeReal Image Downloader.dmg
```

## Recommended Packaging Workflow

For a normal local build/install cycle:

```bash
make doctor
make app-bundle
make install-app
```

For a distributable disk image:

```bash
make dmg
```

## Packaging Notes and Constraints

The macOS app bundle is a real `.app` package, but it is not a fully standalone native binary.

Current packaging model:

- the app bundle includes a copy of the project `.venv`
- that virtual environment is created from your local Python installation
- on this machine, that means the bundle expects a working Python.org 3.13 framework-based install

Practical implication:

- the app should work correctly on the machine where you built it
- if you want a fully self-contained app for redistribution to machines without that Python runtime, the next step would be packaging with a dedicated freezer such as PyInstaller or py2app

## App Icon

The app icon is generated from:

```text
icon.png
```

The Makefile converts it into a macOS `.icns` file automatically during `make icon`, `make app-bundle`, and `make dmg`.

## First Use

1. Launch the app.
2. Select or paste the BeReal GDPR export folder path.
3. Click `Load Data`.
4. Choose the export mode you want.
5. Browse the memories in either `Selection Table` or `Scroller`.
6. Download selected items or the full set.

## Main UI Areas

### Top Controls

- export folder path input
- `Browse`
- `Load Data`
- mode radio buttons
- `Skip existing`
- `Download Selected`
- `Download All`
- `Open Output Folder`

### Selection Table

Tabular browser for the full dataset.

Useful for:

- fast scanning by timestamp
- checking export status columns
- selecting many rows quickly

### Scroller

Vertical image browser using preview cards.

Useful for:

- seeing the export result before downloading
- visual selection
- inspecting metadata overlays

## Selection Behavior

Selection is shared between the table and scroller.

Supported interaction:

- click to select
- `Shift+Click` for range selection
- `Cmd+Click` on macOS or `Ctrl+Click` on other platforms to toggle items in the scroller
- `Shift+Up` and `Shift+Down` to expand or shrink selection by keyboard
- `Cmd+A` or `Ctrl+A` to select all

## Preview Behavior

You can preview the currently selected export result based on the active mode.

Supported behavior:

- double-click a row in `Selection Table` to open preview
- press `Space` in the table or scroller to open or close preview
- press `Space` in the preview window to close it
- use `Up` and `Down` while preview is open to move through items
- use `Shift+Up` and `Shift+Down` while preview is open to extend selection and update preview

The preview window:

- opens centered
- is raised above the main window
- reuses an existing preview window when navigating rapidly

## Scroller Features

- vertical scrolling
- one image per row
- zoom controls
- metadata overlay per image
- optional toggle to show metadata on all cards
- lazy loading for visible preview cards

Each scroller card includes:

- rendered image preview
- circular `i` button in the top-right
- metadata overlay rendered on top of the image

## Download Behavior

### Skip Existing On

If `Skip existing` is enabled:

- photos already exported for the selected mode are skipped
- no overwrite happens

### Skip Existing Off

If `Skip existing` is disabled:

- the app checks whether any selected exports already exist for the current mode
- if so, it asks once for confirmation before overwriting
- confirmed overwrites replace both the JPEG and the sidecar metadata JSON in place

## Already Exported Detection

The app remembers exports by photo key and export mode.

The UI can tell you:

- whether the selected mode has already been exported
- which modes have been exported for each photo

The app no longer visually highlights already-downloaded images in the scroller. Download status remains available through the table and metadata text.

## Performance Notes

The app is designed for large BeReal exports, but preview generation is still image-heavy.

Current performance strategy:

- the table and scroller are separated so image loading does not run while you are only using the table
- scroller previews are lazy-loaded for visible cards
- downloaded exports are reused as preview sources when available
- preview navigation is debounced to avoid rapid destroy/recreate behavior

What can still be slow:

- first-time composite preview generation for large images
- scrolling through many previously unseen items
- previewing modes that require compositing front and back images

## Troubleshooting

### App Crashes on Startup with `tkinter` Errors

Cause:

- broken Python/Tk environment

Fix:

- rebuild the virtual environment with a working Python.org interpreter
- avoid Conda or Xcode Python if they are pulling in broken Tk libraries

### `macOS 26 or later required` or Similar Tk Crash

Cause:

- mismatched Python and Tk libraries

Fix:

- recreate `.venv` from Python.org Python
- reinstall requirements

### `(base)` Keeps Appearing in the Shell

Cause:

- Conda base auto-activation

Fix:

```bash
conda config --set auto_activate_base false
```

Then open a fresh shell and activate only `.venv`.

### Deleted Export Still Shows as Downloaded

Expected behavior now:

- if the sidecar metadata JSON is gone, the history entry is pruned automatically
- if only the JPEG is gone but metadata remains, history still treats the export as present

If you want a deleted export to stop showing as downloaded, remove the corresponding metadata JSON too or re-export/overwrite it.

## Project Files

- [README.md](/Users/johnxie/Documents/Personal/Projects/bereal-data/README.md)
  - main documentation
- [bereal_downloader_app.py](/Users/johnxie/Documents/Personal/Projects/bereal-data/bereal_downloader_app.py)
  - full desktop application
- [requirements.txt](/Users/johnxie/Documents/Personal/Projects/bereal-data/requirements.txt)
  - Python dependency list

## Development Notes

Current implementation is a single-file Tkinter desktop app. There is no packaging, installer, or test suite in the repository yet.

If you change export behavior, also update:

- output layout documentation
- metadata behavior documentation
- history behavior documentation
- run instructions if the Python environment assumptions change
