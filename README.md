# BeReal Desktop Downloader

Desktop app to export your BeReal images from this GDPR folder into your `Downloads` folder in these modes:

- Front camera only
- Back camera only
- BeReal style: front in top-left over back
- BeReal style: back in top-left over front

It also tracks what has already been downloaded per photo and per mode, so the UI can show prior exports.

## What it uses as source data

- `memories.json` for photo pairs and metadata (`takenTime`, `berealMoment`, `date`, `isLate`, caption, location)
- `Photos/...` for source image files

## Output location

Exports are written to:

- `~/Downloads/BeReal-Exports/<year>/<YYYY-MM-DD>/...jpg`

For each exported image, a sidecar metadata JSON file is also written next to it.

## Download history location

The app stores mode-specific download history at:

- macOS: `~/Library/Application Support/BeRealDownloader/history.json`
- Linux: `~/.local/share/bereal-downloader/history.json`
- Windows: `%APPDATA%/BeRealDownloader/history.json`

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bereal_downloader_app.py
```

## Usage

1. Launch the app.
2. Set the export folder path (the app can auto-detect the nested BeReal folder in this repo).
3. Click `Load Data`.
4. Pick a download mode.
5. Use `Download Selected` or `Download All`.
6. Rows marked as already downloaded for the selected mode are highlighted.
7. Use the `Scroller` tab to browse:
   - `Filename list`: scroll planned output filenames for the selected mode.
   - `Image preview`: scroll through rendered previews of exactly how each export will look for the selected mode.

## Metadata behavior

Each exported JPEG gets:

- EXIF datetime fields set from `takenTime`
- File timestamp (`mtime`) set from `takenTime`
- Sidecar JSON with BeReal metadata (date, prompt moment, lateness, caption, location, source paths)
