#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

PIL_IMPORT_ERROR: Optional[Exception] = None
IMAGETK_IMPORT_ERROR: Optional[Exception] = None

try:
    from PIL import Image, ImageDraw, ImageOps
except Exception as exc:  # pragma: no cover - runtime environment dependent
    Image = None
    ImageDraw = None
    ImageOps = None
    PIL_IMPORT_ERROR = exc

try:
    from PIL import ImageTk
except Exception as exc:  # pragma: no cover - runtime environment dependent
    ImageTk = None
    IMAGETK_IMPORT_ERROR = exc

APP_TITLE = "BeReal Image Downloader"
APP_WIDTH = 1300
APP_HEIGHT = 760

MODE_FRONT_ONLY = "front_only"
MODE_BACK_ONLY = "back_only"
MODE_BEREAL_FRONT_TL = "bereal_front_top_left"
MODE_BEREAL_BACK_TL = "bereal_back_top_left"

MODE_LABELS = {
    MODE_FRONT_ONLY: "Front only",
    MODE_BACK_ONLY: "Back only",
    MODE_BEREAL_FRONT_TL: "BeReal style (front top-left)",
    MODE_BEREAL_BACK_TL: "BeReal style (back top-left)",
}
MODE_FILENAME_LABELS = {
    MODE_FRONT_ONLY: "Front Only",
    MODE_BACK_ONLY: "Back Only",
    MODE_BEREAL_FRONT_TL: "BeReal Front Top Left",
    MODE_BEREAL_BACK_TL: "BeReal Back Top Left",
}

GALLERY_MAX_COLUMNS = 1
CARD_BG_DEFAULT = "#e9ecef"
CARD_BG_SELECTED = "#cfe8ff"
CARD_BG_MISSING = "#ffe9e9"
META_UI_BG = "#000000"
META_UI_FG = "#ffffff"
PREVIEW_ZOOM_MIN = 0.6
PREVIEW_ZOOM_MAX = 1.8
PREVIEW_ZOOM_DEFAULT = 0.8
PREVIEW_NAV_DEBOUNCE_MS = 45


@dataclass
class MemoryPhoto:
    key: str
    taken_time: str
    bereal_moment: str
    bereal_date: str
    is_late: bool
    caption: str
    location: Optional[Dict[str, float]]
    front_path: Path
    back_path: Path


class ExportDataLoader:
    def find_export_dir(self, base_path: Path) -> Path:
        base_path = base_path.expanduser().resolve()

        if self._looks_like_export_dir(base_path):
            return base_path

        for child in base_path.iterdir():
            if child.is_dir() and self._looks_like_export_dir(child):
                return child

        raise FileNotFoundError(
            "Could not find a BeReal export directory containing memories.json and Photos/."
        )

    @staticmethod
    def _looks_like_export_dir(path: Path) -> bool:
        return (path / "memories.json").exists() and (path / "Photos").is_dir()

    def load_memories(self, export_dir: Path) -> List[MemoryPhoto]:
        memories_path = export_dir / "memories.json"
        with memories_path.open("r", encoding="utf-8") as f:
            rows = json.load(f)

        photos: List[MemoryPhoto] = []
        collision_counts: Dict[str, int] = {}

        for row in rows:
            front_raw = (row.get("frontImage") or {}).get("path")
            back_raw = (row.get("backImage") or {}).get("path")
            if not front_raw or not back_raw:
                continue

            front_abs = self.resolve_media_path(export_dir, front_raw)
            back_abs = self.resolve_media_path(export_dir, back_raw)

            taken_time = row.get("takenTime") or ""
            base_key_input = "|".join([taken_time, front_raw, back_raw])
            base_key = hashlib.sha1(base_key_input.encode("utf-8")).hexdigest()[:16]

            count = collision_counts.get(base_key, 0)
            collision_counts[base_key] = count + 1
            key = base_key if count == 0 else f"{base_key}-{count}"

            photos.append(
                MemoryPhoto(
                    key=key,
                    taken_time=taken_time,
                    bereal_moment=row.get("berealMoment") or "",
                    bereal_date=row.get("date") or "",
                    is_late=bool(row.get("isLate")),
                    caption=(row.get("caption") or "").strip(),
                    location=row.get("location"),
                    front_path=front_abs,
                    back_path=back_abs,
                )
            )

        photos.sort(key=lambda p: p.taken_time, reverse=True)
        return photos

    @staticmethod
    def resolve_media_path(export_dir: Path, raw_path: str) -> Path:
        clean = raw_path.lstrip("/")
        parts = Path(clean).parts

        candidates: List[Path] = []

        if parts and parts[0] == "Photos":
            # Most paths are Photos/<user_id>/<bucket>/<filename>
            if len(parts) >= 3 and parts[1].startswith("u"):
                candidates.append(export_dir / "Photos" / Path(*parts[2:]))
            candidates.append(export_dir / Path(*parts))

        candidates.append(export_dir / clean)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return candidates[0]


class HistoryStore:
    def __init__(self, history_path: Optional[Path] = None) -> None:
        self.history_path = history_path or self._default_history_path()
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()
        self._dirty = False

    @staticmethod
    def _default_history_path() -> Path:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "BeRealDownloader"
        elif os.name == "nt":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) / "BeRealDownloader" if appdata else Path.home() / "BeRealDownloader"
        else:
            base = Path.home() / ".local" / "share" / "bereal-downloader"
        return base / "history.json"

    def _load(self) -> Dict:
        if not self.history_path.exists():
            return {"version": 1, "entries": {}}

        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "entries" not in data:
                return {"version": 1, "entries": {}}
            return data
        except Exception:
            return {"version": 1, "entries": {}}

    def save(self) -> None:
        if not self._dirty:
            return
        tmp_path = self.history_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp_path.replace(self.history_path)
        self._dirty = False

    def _get_entry(self, photo_key: str, mode: str) -> Optional[Dict]:
        entry = self._data.get("entries", {}).get(photo_key, {}).get(mode)
        return entry if isinstance(entry, dict) else None

    def _entry_metadata_exists(self, entry: Dict) -> bool:
        metadata_raw = entry.get("metadataPath")
        return isinstance(metadata_raw, str) and bool(metadata_raw) and Path(metadata_raw).exists()

    def _entry_output_exists(self, entry: Dict) -> bool:
        output_raw = entry.get("outputPath")
        return isinstance(output_raw, str) and bool(output_raw) and Path(output_raw).exists()

    def _prune_mode(self, photo_key: str, mode: str) -> None:
        entries = self._data.get("entries", {})
        photo_entry = entries.get(photo_key)
        if not isinstance(photo_entry, dict) or mode not in photo_entry:
            return
        del photo_entry[mode]
        if not photo_entry:
            entries.pop(photo_key, None)
        self._dirty = True

    def has_mode(self, photo_key: str, mode: str) -> bool:
        entry = self._get_entry(photo_key, mode)
        if entry is None:
            return False
        if not self._entry_metadata_exists(entry):
            self._prune_mode(photo_key, mode)
            return False
        return True

    def downloaded_modes(self, photo_key: str) -> List[str]:
        photo_entry = self._data.get("entries", {}).get(photo_key, {})
        if not isinstance(photo_entry, dict):
            return []

        modes: List[str] = []
        stale_modes: List[str] = []
        for mode, entry in photo_entry.items():
            if not isinstance(entry, dict) or not self._entry_metadata_exists(entry):
                stale_modes.append(mode)
                continue
            modes.append(mode)

        for mode in stale_modes:
            self._prune_mode(photo_key, mode)

        return sorted(modes)

    def get_output_path(self, photo_key: str, mode: str) -> Optional[Path]:
        entry = self._get_entry(photo_key, mode)
        if entry is None:
            return None
        if not self._entry_metadata_exists(entry):
            self._prune_mode(photo_key, mode)
            return None
        output_raw = entry.get("outputPath")
        if not isinstance(output_raw, str) or not output_raw:
            return None
        output_path = Path(output_raw)
        if not output_path.exists():
            return None
        return output_path

    def get_metadata_path(self, photo_key: str, mode: str) -> Optional[Path]:
        entry = self._get_entry(photo_key, mode)
        if entry is None:
            return None
        metadata_raw = entry.get("metadataPath")
        if not isinstance(metadata_raw, str) or not metadata_raw:
            return None
        metadata_path = Path(metadata_raw)
        if not metadata_path.exists():
            self._prune_mode(photo_key, mode)
            return None
        return metadata_path

    def mark_download(self, photo_key: str, mode: str, output_path: Path, sidecar_path: Path) -> None:
        entries = self._data.setdefault("entries", {})
        record = entries.setdefault(photo_key, {})
        record[mode] = {
            "downloadedAt": datetime.now(timezone.utc).isoformat(),
            "outputPath": str(output_path),
            "metadataPath": str(sidecar_path),
        }
        self._dirty = True


class ImageExporter:
    def __init__(self, downloads_root: Optional[Path] = None) -> None:
        self.downloads_root = downloads_root or (Path.home() / "Downloads" / "BeReal-Exports")

    def export_photo(
        self,
        photo: MemoryPhoto,
        mode: str,
        overwrite_path: Optional[Path] = None,
        overwrite_metadata_path: Optional[Path] = None,
    ) -> Tuple[Path, Path]:
        if Image is None or ImageOps is None:
            raise RuntimeError("Pillow is not installed. Run: pip install -r requirements.txt")

        if not photo.front_path.exists():
            raise FileNotFoundError(f"Front image not found: {photo.front_path}")
        if not photo.back_path.exists():
            raise FileNotFoundError(f"Back image not found: {photo.back_path}")

        output_img = self.render_output_image(photo, mode)

        output_path = overwrite_path or self._build_output_path(photo, mode)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path = overwrite_metadata_path or self._build_metadata_path(photo, mode)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)

        exif = Image.Exif()
        exif_dt = self._to_exif_datetime(photo.taken_time)
        if exif_dt:
            # DateTime, DateTimeOriginal, DateTimeDigitized
            exif[306] = exif_dt
            exif[36867] = exif_dt
            exif[36868] = exif_dt
        gps_ifd = self._build_gps_ifd(photo.location, photo.taken_time)
        if gps_ifd:
            exif[34853] = gps_ifd

        output_img.save(output_path, format="JPEG", quality=95, exif=exif)

        metadata = {
            "mode": mode,
            "modeLabel": MODE_LABELS.get(mode, mode),
            "takenTime": photo.taken_time,
            "berealMoment": photo.bereal_moment,
            "berealDate": photo.bereal_date,
            "isLate": photo.is_late,
            "caption": photo.caption,
            "location": photo.location,
            "frontSourcePath": str(photo.front_path),
            "backSourcePath": str(photo.back_path),
            "outputPath": str(output_path),
            "exportedAt": datetime.now(timezone.utc).isoformat(),
        }

        with sidecar_path.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        taken_epoch = self._to_epoch(photo.taken_time)
        if taken_epoch is not None:
            os.utime(output_path, (taken_epoch, taken_epoch))
            os.utime(sidecar_path, (taken_epoch, taken_epoch))

        return output_path, sidecar_path

    def render_output_image(self, photo: MemoryPhoto, mode: str) -> "Image.Image":
        if Image is None or ImageOps is None:
            raise RuntimeError("Pillow is not installed. Run: pip install -r requirements.txt")

        front = self._load_image(photo.front_path)
        back = self._load_image(photo.back_path)

        if mode == MODE_FRONT_ONLY:
            return front
        if mode == MODE_BACK_ONLY:
            return back
        if mode == MODE_BEREAL_FRONT_TL:
            return self._compose(base=back, inset=front)
        if mode == MODE_BEREAL_BACK_TL:
            return self._compose(base=front, inset=back)

        raise ValueError(f"Unsupported mode: {mode}")

    def planned_relative_path(self, photo: MemoryPhoto, mode: str) -> Path:
        return Path(self.planned_filename(photo, mode))

    def planned_metadata_relative_path(self, photo: MemoryPhoto, mode: str) -> Path:
        taken_dt = self._parse_iso(photo.taken_time) or datetime.now(timezone.utc)
        local_dt = taken_dt.astimezone()
        year_dir = local_dt.strftime("%Y")
        day_dir = local_dt.strftime("%Y-%m-%d")
        filename = self.planned_filename(photo, mode)
        return Path(year_dir) / day_dir / f"{Path(filename).stem}.json"

    def planned_filename(self, photo: MemoryPhoto, mode: str) -> str:
        taken_dt = self._parse_iso(photo.taken_time) or datetime.now(timezone.utc)
        local_dt = taken_dt.astimezone()
        stamp = local_dt.strftime("%Y-%m-%d %H.%M.%S")
        mode_label = MODE_FILENAME_LABELS.get(mode, mode).replace("/", "-")
        return f"{stamp} - {mode_label}.jpg"

    @staticmethod
    def _load_image(path: Path) -> "Image.Image":
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")

    @staticmethod
    def _compose(base: "Image.Image", inset: "Image.Image") -> "Image.Image":
        if ImageDraw is None:
            raise RuntimeError("Pillow ImageDraw support is required for BeReal composition.")

        composed = base.copy()
        inset_target_w = max(140, int(composed.width * 0.315))
        inset_target_h = max(140, int(composed.height * 0.315))
        inset_copy = inset.copy()
        inset_copy = ImageOps.fit(
            inset_copy,
            (inset_target_w, inset_target_h),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

        border = max(2, int(composed.width * 0.004))
        radius = max(12, int(min(inset_target_w, inset_target_h) * 0.12))
        framed_w = inset_target_w + border * 2
        framed_h = inset_target_h + border * 2

        framed = Image.new("RGBA", (framed_w, framed_h), (0, 0, 0, 0))
        border_mask = Image.new("L", (framed_w, framed_h), 0)
        ImageDraw.Draw(border_mask).rounded_rectangle(
            (0, 0, framed_w - 1, framed_h - 1),
            radius=radius + border,
            fill=255,
        )
        framed.paste((0, 0, 0, 255), (0, 0), border_mask)

        inset_mask = Image.new("L", (inset_target_w, inset_target_h), 0)
        ImageDraw.Draw(inset_mask).rounded_rectangle(
            (0, 0, inset_target_w - 1, inset_target_h - 1),
            radius=radius,
            fill=255,
        )
        inset_rgba = inset_copy.convert("RGBA")
        framed.paste(inset_rgba, (border, border), inset_mask)

        margin = max(10, int(composed.width * 0.028))
        composed_rgba = composed.convert("RGBA")
        composed_rgba.alpha_composite(framed, dest=(margin, margin))
        composed = composed_rgba.convert("RGB")
        return composed

    def _build_output_path(self, photo: MemoryPhoto, mode: str) -> Path:
        relative = self.planned_relative_path(photo, mode)
        base_path = self.downloads_root / relative
        if not base_path.exists():
            return base_path

        stem = base_path.stem
        suffix = 2
        while True:
            candidate = base_path.with_name(f"{stem} ({suffix}).jpg")
            if not candidate.exists():
                return candidate
            suffix += 1

    def _build_metadata_path(self, photo: MemoryPhoto, mode: str) -> Path:
        relative = self.planned_metadata_relative_path(photo, mode)
        base_path = self.downloads_root / relative
        if not base_path.exists():
            return base_path

        stem = base_path.stem
        suffix = 2
        while True:
            candidate = base_path.with_name(f"{stem} ({suffix}).json")
            if not candidate.exists():
                return candidate
            suffix += 1

    @staticmethod
    def _parse_iso(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _to_epoch(self, value: str) -> Optional[float]:
        dt = self._parse_iso(value)
        return dt.timestamp() if dt else None

    def _to_exif_datetime(self, value: str) -> Optional[str]:
        dt = self._parse_iso(value)
        if not dt:
            return None
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y:%m:%d %H:%M:%S")

    def _build_gps_ifd(
        self, location: Optional[Dict[str, float]], taken_time: str
    ) -> Optional[Dict[int, object]]:
        if not location:
            return None

        lat = location.get("latitude")
        lon = location.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None

        gps_ifd: Dict[int, object] = {
            1: "N" if lat >= 0 else "S",
            2: self._decimal_to_dms(abs(float(lat))),
            3: "E" if lon >= 0 else "W",
            4: self._decimal_to_dms(abs(float(lon))),
            27: "WGS-84",
        }

        dt = self._parse_iso(taken_time)
        if dt:
            utc_dt = dt.astimezone(timezone.utc)
            gps_ifd[7] = (
                float(utc_dt.hour),
                float(utc_dt.minute),
                float(utc_dt.second + utc_dt.microsecond / 1_000_000),
            )
            gps_ifd[29] = utc_dt.strftime("%Y:%m:%d")

        return gps_ifd

    @staticmethod
    def _decimal_to_dms(value: float) -> Tuple[float, float, float]:
        degrees = int(value)
        minutes_total = (value - degrees) * 60
        minutes = int(minutes_total)
        seconds = (minutes_total - minutes) * 60
        return (float(degrees), float(minutes), float(seconds))


class BeRealDownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")

        self.loader = ExportDataLoader()
        self.history = HistoryStore()
        self.exporter = ImageExporter()

        self.photos: List[MemoryPhoto] = []
        self.photo_by_item: Dict[str, MemoryPhoto] = {}
        self.photo_index_by_key: Dict[str, int] = {}
        self.export_dir: Optional[Path] = None

        self.path_var = tk.StringVar(value=str(Path.cwd()))
        self.mode_var = tk.StringVar(value=MODE_BEREAL_FRONT_TL)
        self.show_all_metadata_var = tk.BooleanVar(value=False)
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.preview_zoom = PREVIEW_ZOOM_DEFAULT
        self.zoom_label_var = tk.StringVar(value=f"{int(round(PREVIEW_ZOOM_DEFAULT * 100))}%")
        self.status_var = tk.StringVar(value="Select an export folder and click Load Data.")
        self.selection_status_var = tk.StringVar(value="Selected: 0")

        self.table_item_by_photo_key: Dict[str, str] = {}
        self.suppress_table_select_event = False

        self.selected_photo_keys: set[str] = set()
        self.selection_anchor_index: Optional[int] = None
        self.selection_focus_index: Optional[int] = None

        self.scroller_container: Optional[ttk.Frame] = None
        self.notebook: Optional[ttk.Notebook] = None
        self.table_tab: Optional[ttk.Frame] = None
        self.scroller_tab: Optional[ttk.Frame] = None
        self.scroller_active: bool = False
        self.gallery_canvas: Optional[tk.Canvas] = None
        self.gallery_inner: Optional[ttk.Frame] = None
        self.gallery_scrollbar: Optional[ttk.Scrollbar] = None
        self.gallery_window_id: Optional[int] = None
        self.gallery_cards: List[Dict] = []
        self.gallery_card_by_key: Dict[str, Dict] = {}
        self.gallery_thumbnail_refs: Dict[Tuple[str, str], "ImageTk.PhotoImage"] = {}
        self.card_meta_visible_keys: set[str] = set()
        self.thumbnail_job_queue = deque()
        self.thumbnail_job_set: set[int] = set()
        self.thumbnail_job_after_id: Optional[str] = None
        self.thumbnail_request_after_id: Optional[str] = None
        self.table_selection_after_id: Optional[str] = None
        self.table_selection_pending_items: Optional[Tuple[str, ...]] = None
        self.last_target_preview_width: int = 0
        self.scroller_needs_refresh: bool = True
        self.preview_window: Optional[tk.Toplevel] = None
        self.preview_signature: Optional[Tuple[str, str]] = None
        self.preview_image_label: Optional[tk.Label] = None
        self.preview_info_label: Optional[tk.Label] = None
        self.preview_nav_after_id: Optional[str] = None
        self.preview_nav_pending_index: Optional[int] = None
        self.app_icon_photo: Optional[tk.PhotoImage] = None
        self.download_progress_window: Optional[tk.Toplevel] = None
        self.download_progress_bar: Optional[ttk.Progressbar] = None
        self.download_progress_title_var = tk.StringVar(value="")
        self.download_progress_detail_var = tk.StringVar(value="")
        self.download_progress_counts_var = tk.StringVar(value="")
        self.download_queue: Optional[queue.Queue] = None
        self.download_poll_after_id: Optional[str] = None
        self.download_state: Optional[Dict[str, object]] = None

        self._configure_app_identity()
        self._build_ui()
        self._configure_row_tags()
        self.root.after(0, self._focus_main_window_on_start)

    def _configure_app_identity(self) -> None:
        try:
            self.root.iconname(APP_TITLE)
        except Exception:
            pass
        try:
            self.root.tk.call("tk", "appname", APP_TITLE)
        except Exception:
            pass

        icon_path = self._resolve_app_asset_path("icon.png")
        if icon_path is None:
            return

        try:
            self.app_icon_photo = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.app_icon_photo)
        except Exception:
            self.app_icon_photo = None

    def _resolve_app_asset_path(self, name: str) -> Optional[Path]:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            script_dir / name,
            script_dir.parent / name,
            Path.cwd() / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _set_window_icon(self, win: tk.Toplevel) -> None:
        if self.app_icon_photo is None:
            return
        try:
            win.iconphoto(True, self.app_icon_photo)
        except Exception:
            pass

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        path_frame = ttk.Frame(outer)
        path_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(path_frame, text="Export folder:").pack(side=tk.LEFT)
        path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        ttk.Button(path_frame, text="Browse", command=self.on_browse).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(path_frame, text="Load Data", command=self.on_load_data).pack(side=tk.LEFT)

        mode_frame = ttk.LabelFrame(outer, text="Download mode", padding=8)
        mode_frame.pack(fill=tk.X, pady=(0, 8))

        for mode, label in MODE_LABELS.items():
            ttk.Radiobutton(
                mode_frame,
                text=label,
                value=mode,
                variable=self.mode_var,
                command=self.on_export_mode_changed,
            ).pack(side=tk.LEFT, padx=(0, 18))

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Checkbutton(
            action_frame,
            text="Skip already downloaded entries for selected mode",
            variable=self.skip_existing_var,
        ).pack(side=tk.LEFT)
        ttk.Label(action_frame, textvariable=self.selection_status_var).pack(side=tk.LEFT, padx=(14, 0))

        ttk.Button(action_frame, text="Download Selected", command=self.on_download_selected).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(action_frame, text="Download All", command=self.on_download_all).pack(side=tk.RIGHT)
        ttk.Button(action_frame, text="Open Output Folder", command=self.on_open_output).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook = notebook

        table_tab = ttk.Frame(notebook)
        notebook.add(table_tab, text="Selection Table")
        self.table_tab = table_tab

        scroller_tab = ttk.Frame(notebook)
        notebook.add(scroller_tab, text="Scroller")
        self.scroller_tab = scroller_tab
        notebook.bind("<<NotebookTabChanged>>", self.on_notebook_tab_changed)

        table_frame = ttk.Frame(table_tab)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "taken_time",
            "is_late",
            "caption",
            "location",
            "files",
            "selected_mode",
            "downloaded_modes",
        )

        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.table.heading("taken_time", text="Taken Time")
        self.table.heading("is_late", text="Late")
        self.table.heading("caption", text="Caption")
        self.table.heading("location", text="Location")
        self.table.heading("files", text="Source Files")
        self.table.heading("selected_mode", text="Selected Mode Downloaded")
        self.table.heading("downloaded_modes", text="All Downloaded Modes")

        self.table.column("taken_time", width=190, anchor="w")
        self.table.column("is_late", width=60, anchor="center")
        self.table.column("caption", width=300, anchor="w")
        self.table.column("location", width=170, anchor="w")
        self.table.column("files", width=120, anchor="center")
        self.table.column("selected_mode", width=185, anchor="center")
        self.table.column("downloaded_modes", width=270, anchor="w")

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.table.xview)
        self.table.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.table.bind("<<TreeviewSelect>>", self.on_table_selection_changed)
        self.table.bind("<Double-1>", self.on_table_double_click)
        self.table.bind("<space>", self.on_space_toggle_preview)
        self.table.bind("<Up>", self.on_table_arrow_key)
        self.table.bind("<Down>", self.on_table_arrow_key)
        self.table.bind("<Command-a>", self.on_select_all_shortcut)
        self.table.bind("<Control-a>", self.on_select_all_shortcut)

        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        self._build_scroller_tab(scroller_tab)
        self.scroller_active = self._is_scroller_tab_active()

        status = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        status.pack(fill=tk.X, pady=(8, 0))

    def _is_scroller_tab_active(self) -> bool:
        if self.notebook is None or self.scroller_tab is None:
            return False
        return self.notebook.select() == str(self.scroller_tab)

    def on_notebook_tab_changed(self, _event: tk.Event) -> None:
        self.scroller_active = self._is_scroller_tab_active()
        if self.scroller_active:
            if self.scroller_needs_refresh:
                self.refresh_scroller()
            else:
                self.refresh_gallery_selection_styles()
                self._schedule_thumbnail_request(1)
        else:
            self._cancel_thumbnail_loading()

    def request_scroller_refresh(self) -> None:
        self.scroller_needs_refresh = True
        if self.scroller_active:
            self.refresh_scroller()

    def _clear_scroller_widgets(self) -> None:
        if self.gallery_inner is None:
            return
        self._cancel_thumbnail_loading()
        for card in self.gallery_cards:
            self._cancel_card_metadata_job(card)
        self.gallery_thumbnail_refs.clear()
        self.gallery_cards.clear()
        self.gallery_card_by_key.clear()
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.update_gallery_scrollregion()

    def _build_scroller_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, padding=(8, 8, 8, 4))
        top.pack(fill=tk.X)

        ttk.Label(
            top,
            text="Image preview scroller (1 per row). Click to select, Shift+Click for range. Use the i button for metadata.",
        ).pack(side=tk.LEFT)
        right_controls = ttk.Frame(top)
        right_controls.pack(side=tk.RIGHT)
        ttk.Button(right_controls, text="-", width=3, command=self.on_zoom_out).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(right_controls, textvariable=self.zoom_label_var, width=6, anchor="center").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(right_controls, text="+", width=3, command=self.on_zoom_in).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            right_controls,
            text="Show metadata on all cards",
            variable=self.show_all_metadata_var,
            command=self.on_toggle_all_metadata,
        ).pack(side=tk.LEFT)

        self.scroller_container = ttk.Frame(parent, padding=(8, 4, 8, 8))
        self.scroller_container.pack(fill=tk.BOTH, expand=True)

        self.gallery_canvas = tk.Canvas(self.scroller_container, highlightthickness=0)
        self.gallery_scrollbar = ttk.Scrollbar(
            self.scroller_container, orient=tk.VERTICAL, command=self.gallery_canvas.yview
        )
        self.gallery_canvas.configure(yscrollcommand=self.gallery_scrollbar.set)

        self.gallery_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.gallery_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.gallery_inner = ttk.Frame(self.gallery_canvas)
        self.gallery_window_id = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        for c in range(GALLERY_MAX_COLUMNS):
            self.gallery_inner.columnconfigure(c, weight=1)

        self.gallery_inner.bind("<Configure>", self.on_gallery_inner_configure)
        self.gallery_canvas.bind("<Configure>", self.on_gallery_canvas_configure)
        self.gallery_canvas.bind("<MouseWheel>", self.on_gallery_mouse_wheel)
        self.gallery_canvas.bind("<Button-4>", self.on_gallery_mouse_wheel)
        self.gallery_canvas.bind("<Button-5>", self.on_gallery_mouse_wheel)
        self.gallery_canvas.bind("<space>", self.on_space_toggle_preview)
        self.gallery_canvas.bind("<Up>", self.on_scroller_arrow_key)
        self.gallery_canvas.bind("<Down>", self.on_scroller_arrow_key)
        self.gallery_canvas.bind("<Command-a>", self.on_select_all_shortcut)
        self.gallery_canvas.bind("<Control-a>", self.on_select_all_shortcut)

    def _configure_row_tags(self) -> None:
        self.table.tag_configure("missing", background="#ffe9e9")

    def on_export_mode_changed(self) -> None:
        self.refresh_table()
        self.request_scroller_refresh()

    def _set_preview_zoom(self, new_zoom: float) -> None:
        clamped = max(PREVIEW_ZOOM_MIN, min(PREVIEW_ZOOM_MAX, new_zoom))
        clamped = round(clamped, 2)
        if abs(clamped - self.preview_zoom) < 0.005:
            return
        self.preview_zoom = clamped
        self.zoom_label_var.set(f"{int(round(self.preview_zoom * 100))}%")
        self.last_target_preview_width = 0
        self._invalidate_preview_cache_for_resize()
        for card in self.gallery_cards:
            self.update_card_metadata_visibility(card)
        self._schedule_thumbnail_request(1)

    def on_zoom_in(self) -> None:
        self._set_preview_zoom(self.preview_zoom * 1.15)

    def on_zoom_out(self) -> None:
        self._set_preview_zoom(self.preview_zoom / 1.15)

    def refresh_scroller(self) -> None:
        if self.gallery_inner is None:
            return

        self._clear_scroller_widgets()

        for idx, photo in enumerate(self.photos):
            card = self._create_gallery_card(idx, photo)
            self.gallery_cards.append(card)
            self.gallery_card_by_key[photo.key] = card
            self._place_card(card)

        self.last_target_preview_width = self._current_target_preview_width()
        self.update_gallery_scrollregion()
        if self.scroller_active:
            self._schedule_thumbnail_request(1)
        self.refresh_gallery_selection_styles()
        self.update_selection_status()
        self.scroller_needs_refresh = False

    def _create_gallery_card(self, idx: int, photo: MemoryPhoto) -> Dict:
        assert self.gallery_inner is not None

        frame = tk.Frame(
            self.gallery_inner,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0,
            padx=0,
            pady=0,
            cursor="hand2",
        )

        image_label = tk.Label(
            frame,
            text="",
            anchor="center",
            justify="center",
            bd=0,
            highlightthickness=0,
            fg="#111111",
        )
        image_label.pack(anchor="center")

        meta_button = tk.Canvas(
            image_label,
            width=24,
            height=24,
            bd=0,
            highlightthickness=0,
            bg=CARD_BG_DEFAULT,
            cursor="hand2",
        )
        meta_button_oval = meta_button.create_oval(1, 1, 23, 23, fill="#ffffff", outline="#ffffff", width=1)
        meta_button_text = meta_button.create_text(
            12,
            12,
            text="i",
            fill="#000000",
            font=("Helvetica", 11, "bold"),
        )
        meta_button.bind("<Button-1>", lambda _e, k=photo.key: self.show_card_metadata(k))
        meta_button.place(relx=1.0, x=-6, y=6, anchor="ne")

        meta_overlay = tk.Frame(image_label, bg=META_UI_BG, bd=0, highlightthickness=0)
        meta_label = tk.Label(
            meta_overlay,
            anchor="center",
            justify="center",
            wraplength=230,
            bg=META_UI_BG,
            fg=META_UI_FG,
            font=("Helvetica", 13, "bold"),
        )
        meta_label.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        card = {
            "index": idx,
            "photo": photo,
            "frame": frame,
            "image_label": image_label,
            "meta_button": meta_button,
            "meta_button_oval": meta_button_oval,
            "meta_button_text": meta_button_text,
            "meta_overlay": meta_overlay,
            "meta_label": meta_label,
            "meta_visible": False,
            "meta_after_id": None,
        }

        for widget in (frame, image_label, meta_overlay, meta_label):
            widget.bind("<Button-1>", lambda e, i=idx: self.on_gallery_item_click(i, e))
            widget.bind("<Shift-Button-1>", lambda e, i=idx: self.on_gallery_item_click(i, e))
            widget.bind("<Command-Button-1>", lambda e, i=idx: self.on_gallery_item_click(i, e, "toggle"))
            widget.bind("<Control-Button-1>", lambda e, i=idx: self.on_gallery_item_click(i, e, "toggle"))
            widget.bind("<MouseWheel>", self.on_gallery_mouse_wheel)
            widget.bind("<Button-4>", self.on_gallery_mouse_wheel)
            widget.bind("<Button-5>", self.on_gallery_mouse_wheel)

        self._populate_card_labels(card)
        self.update_card_metadata_visibility(card)
        self._apply_gallery_card_style(card)
        return card

    def _populate_card_labels(self, card: Dict) -> None:
        card["image_label"].configure(text="Loading preview...", image="")
        card["meta_label"].configure(text="", fg=META_UI_FG, bg=META_UI_BG)

    def _set_meta_button_symbol(self, card: Dict, symbol: str) -> None:
        card["meta_button"].itemconfigure(card["meta_button_text"], text=symbol)

    def _cancel_card_metadata_job(self, card: Dict) -> None:
        after_id = card.get("meta_after_id")
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            card["meta_after_id"] = None

    def _render_card_metadata(self, card: Dict) -> None:
        card["meta_after_id"] = None
        photo: MemoryPhoto = card["photo"]
        show = self.show_all_metadata_var.get() or (photo.key in self.card_meta_visible_keys)
        if not show or not card["meta_visible"]:
            return
        card["meta_label"].configure(text=self._format_card_metadata(photo))

    def _format_card_metadata(self, photo: MemoryPhoto) -> str:
        mode = self.mode_var.get()
        late = "Late" if photo.is_late else "On time"
        downloaded = "Already downloaded" if self.history.has_mode(photo.key, mode) else "Not downloaded yet"
        rel = self.exporter.planned_relative_path(photo, mode)

        lines = [
            "Taken",
            self._format_time_human(photo.taken_time),
            "",
            "Status",
            f"{late} | {downloaded}",
            "",
            "Export mode",
            MODE_LABELS.get(mode, mode),
            "",
            "File",
            Path(rel).name,
        ]
        if photo.caption:
            lines.extend(["", "Caption", f'"{photo.caption}"'])
        if photo.location:
            lines.extend(["", "Location", self._format_location(photo.location)])
        return "\n".join(lines)

    def _place_card(self, card: Dict) -> None:
        if self.gallery_inner is None:
            return
        idx = card["index"]
        row = idx // GALLERY_MAX_COLUMNS
        col = idx % GALLERY_MAX_COLUMNS
        card["frame"].grid(row=row, column=col, sticky="ew", padx=0, pady=8)

    def on_toggle_all_metadata(self) -> None:
        for card in self.gallery_cards:
            self.update_card_metadata_visibility(card)
        self.update_gallery_scrollregion()

    def update_card_metadata_visibility(self, card: Dict) -> None:
        photo: MemoryPhoto = card["photo"]
        show = self.show_all_metadata_var.get() or (photo.key in self.card_meta_visible_keys)
        card["meta_label"].configure(wraplength=max(260, self._current_target_preview_width() - 80))

        if show:
            if not card["meta_visible"]:
                card["meta_overlay"].place(
                    relx=0.5,
                    rely=0.5,
                    anchor="center",
                    relwidth=0.92,
                    relheight=0.82,
                )
                card["meta_visible"] = True
            self._set_meta_button_symbol(card, "×")
            self._cancel_card_metadata_job(card)
            card["meta_label"].configure(text="Loading metadata...")
            card["meta_after_id"] = self.root.after(16, lambda c=card: self._render_card_metadata(c))
        else:
            self._cancel_card_metadata_job(card)
            if card["meta_visible"]:
                card["meta_overlay"].place_forget()
                card["meta_visible"] = False
            self._set_meta_button_symbol(card, "i")

    def show_card_metadata(self, photo_key: str) -> None:
        if photo_key in self.card_meta_visible_keys:
            self.card_meta_visible_keys.remove(photo_key)
        else:
            self.card_meta_visible_keys.add(photo_key)

        card = self.gallery_card_by_key.get(photo_key)
        if card is not None:
            self.update_card_metadata_visibility(card)
        self.update_gallery_scrollregion()

    def update_gallery_scrollregion(self) -> None:
        if self.gallery_canvas is None:
            return
        self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all"))

    def _current_target_preview_width(self) -> int:
        if self.gallery_canvas is None:
            return int(760 * self.preview_zoom)
        canvas_w = self.gallery_canvas.winfo_width()
        if canvas_w <= 1:
            return int(760 * self.preview_zoom)
        # One card per row, intentionally smaller than full width.
        base_w = min(980, max(560, canvas_w - 220))
        zoomed = int(base_w * self.preview_zoom)
        return min(1700, max(360, zoomed))

    def _handle_preview_width_change(self) -> None:
        new_width = self._current_target_preview_width()
        if self.last_target_preview_width == 0:
            self.last_target_preview_width = new_width
            return

        # Avoid expensive cache rebuild for tiny resize deltas.
        if abs(new_width - self.last_target_preview_width) < 24:
            return

        self.last_target_preview_width = new_width
        self._invalidate_preview_cache_for_resize()

    def _invalidate_preview_cache_for_resize(self) -> None:
        self._cancel_thumbnail_loading()
        self.gallery_thumbnail_refs.clear()
        for card in self.gallery_cards:
            card["image_label"].configure(image="", text="Loading preview...")
            card["image_label"].image = None

    def on_gallery_inner_configure(self, _event: tk.Event) -> None:
        self.update_gallery_scrollregion()

    def on_gallery_canvas_configure(self, event: tk.Event) -> None:
        if self.gallery_window_id is not None and self.gallery_canvas is not None:
            self.gallery_canvas.itemconfigure(self.gallery_window_id, width=event.width)
        self.update_gallery_scrollregion()
        self._handle_preview_width_change()
        self._schedule_thumbnail_request()

    def on_gallery_mouse_wheel(self, event: tk.Event) -> None:
        if self.gallery_canvas is None:
            return

        step = 0
        if hasattr(event, "delta") and event.delta:
            if sys.platform == "darwin":
                step = int(-event.delta)
                if step == 0:
                    step = -1 if event.delta > 0 else 1
                step = max(-3, min(3, step))
            else:
                step = int(-event.delta / 120)
                if step == 0:
                    step = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1

        if step != 0:
            self.gallery_canvas.yview_scroll(step, "units")
            self._schedule_thumbnail_request(120)

    def on_gallery_item_click(self, idx: int, event: tk.Event, action: str = "single") -> None:
        if idx < 0 or idx >= len(self.photos):
            return

        if self.gallery_canvas is not None:
            self.gallery_canvas.focus_set()

        old_keys = set(self.selected_photo_keys)
        shift_down = action == "range" or bool(event.state & 0x0001)

        if shift_down and self.selection_anchor_index is not None:
            start = min(self.selection_anchor_index, idx)
            end = max(self.selection_anchor_index, idx)
            self.selected_photo_keys = {self.photos[i].key for i in range(start, end + 1)}
            self.selection_focus_index = idx
        elif action == "toggle":
            clicked_key = self.photos[idx].key
            if clicked_key in self.selected_photo_keys:
                self.selected_photo_keys.remove(clicked_key)
            else:
                self.selected_photo_keys.add(clicked_key)
            self.selection_anchor_index = idx
            self.selection_focus_index = idx
        else:
            clicked_key = self.photos[idx].key
            if clicked_key in self.selected_photo_keys and len(self.selected_photo_keys) == 1:
                self.selected_photo_keys.clear()
                self.selection_anchor_index = None
                self.selection_focus_index = None
            else:
                self.selected_photo_keys = {clicked_key}
                self.selection_anchor_index = idx
                self.selection_focus_index = idx

        self._refresh_gallery_selection_for_keys(old_keys ^ self.selected_photo_keys)
        self.sync_table_selection_from_model()
        self.update_selection_status()

    def _move_selection_by_arrow(self, direction: int, extend: bool) -> Optional[int]:
        if not self.photos:
            return None

        current_idx = self.selection_focus_index
        if current_idx is None:
            if self.selected_photo_keys:
                indices = [self.photo_index_by_key.get(k, 0) for k in self.selected_photo_keys]
                current_idx = min(indices) if direction < 0 else max(indices)
            else:
                current_idx = 0 if direction > 0 else len(self.photos) - 1

        new_idx = max(0, min(len(self.photos) - 1, current_idx + direction))
        old_keys = set(self.selected_photo_keys)

        if extend:
            if self.selection_anchor_index is None:
                self.selection_anchor_index = current_idx
            anchor = self.selection_anchor_index
            start = min(anchor, new_idx)
            end = max(anchor, new_idx)
            self.selected_photo_keys = {self.photos[i].key for i in range(start, end + 1)}
            self.selection_focus_index = new_idx
        else:
            self.selected_photo_keys = {self.photos[new_idx].key}
            self.selection_anchor_index = new_idx
            self.selection_focus_index = new_idx

        self._refresh_gallery_selection_for_keys(old_keys ^ self.selected_photo_keys)
        self.sync_table_selection_from_model()
        self.update_selection_status()
        if self.scroller_active:
            self._ensure_scroller_index_visible(new_idx)
        return new_idx

    def _get_primary_selected_photo(self) -> Optional[MemoryPhoto]:
        if self.selection_focus_index is not None and 0 <= self.selection_focus_index < len(self.photos):
            return self.photos[self.selection_focus_index]

        if self.selected_photo_keys:
            first_key = min(
                self.selected_photo_keys,
                key=lambda k: self.photo_index_by_key.get(k, len(self.photos) + 1),
            )
            return next((p for p in self.photos if p.key == first_key), None)

        selected_items = self.table.selection()
        if selected_items:
            return self.photo_by_item.get(selected_items[0])
        return None

    def on_space_toggle_preview(self, _event: tk.Event) -> str:
        photo = self._get_primary_selected_photo()

        if photo is None:
            if self.preview_window is not None and self.preview_window.winfo_exists():
                self._close_preview_window()
            return "break"

        if self.preview_window is not None and self.preview_window.winfo_exists():
            self._close_preview_window()
        else:
            self.open_photo_preview_window(photo)
        return "break"

    def on_select_all_shortcut(self, _event: tk.Event) -> str:
        if not self.photos:
            return "break"
        old_keys = set(self.selected_photo_keys)
        self.selected_photo_keys = {photo.key for photo in self.photos}
        self.selection_anchor_index = 0
        self.selection_focus_index = len(self.photos) - 1
        self._refresh_gallery_selection_for_keys(old_keys ^ self.selected_photo_keys)
        self.sync_table_selection_from_model()
        self.update_selection_status()
        return "break"

    def on_scroller_arrow_key(self, event: tk.Event) -> str:
        if self.gallery_canvas is not None:
            self.gallery_canvas.focus_set()

        direction = -1 if event.keysym.endswith("Up") else 1
        shift_down = bool(event.state & 0x0001)
        new_idx = self._move_selection_by_arrow(direction, shift_down)
        if (
            new_idx is not None
            and self.preview_window is not None
            and self.preview_window.winfo_exists()
        ):
            self.preview_nav_pending_index = new_idx
            if self.preview_nav_after_id is not None:
                try:
                    self.root.after_cancel(self.preview_nav_after_id)
                except Exception:
                    pass
            self.preview_nav_after_id = self.root.after(PREVIEW_NAV_DEBOUNCE_MS, self._open_pending_preview_from_nav)
        return "break"

    def _ensure_scroller_index_visible(self, idx: int) -> None:
        if self.gallery_canvas is None or idx < 0 or idx >= len(self.gallery_cards):
            return
        frame = self.gallery_cards[idx]["frame"]
        self.root.update_idletasks()
        card_top = frame.winfo_y()
        card_bottom = card_top + frame.winfo_height()
        view_top = self.gallery_canvas.canvasy(0)
        view_bottom = view_top + self.gallery_canvas.winfo_height()
        content_h = max(1, self.gallery_inner.winfo_height() if self.gallery_inner is not None else 1)

        if card_top < view_top:
            self.gallery_canvas.yview_moveto(max(0.0, min(1.0, card_top / content_h)))
        elif card_bottom > view_bottom:
            target = max(0.0, min(1.0, (card_bottom - self.gallery_canvas.winfo_height()) / content_h))
            self.gallery_canvas.yview_moveto(target)

        self._schedule_thumbnail_request(1)

    def on_table_arrow_key(self, event: tk.Event) -> str:
        direction = -1 if event.keysym.endswith("Up") else 1
        shift_down = bool(event.state & 0x0001)
        new_idx = self._move_selection_by_arrow(direction, shift_down)
        if (
            new_idx is not None
            and self.preview_window is not None
            and self.preview_window.winfo_exists()
        ):
            self.preview_nav_pending_index = new_idx
            if self.preview_nav_after_id is not None:
                try:
                    self.root.after_cancel(self.preview_nav_after_id)
                except Exception:
                    pass
            self.preview_nav_after_id = self.root.after(PREVIEW_NAV_DEBOUNCE_MS, self._open_pending_preview_from_nav)
        return "break"

    def refresh_gallery_selection_styles(self) -> None:
        if not self.scroller_active:
            return
        for card in self.gallery_cards:
            self._apply_gallery_card_style(card)

    def _refresh_gallery_selection_for_keys(self, photo_keys: set[str]) -> None:
        if not photo_keys or not self.scroller_active:
            return
        for key in photo_keys:
            card = self.gallery_card_by_key.get(key)
            if card is not None:
                self._apply_gallery_card_style(card)

    def _apply_gallery_card_style(self, card: Dict) -> None:
        photo: MemoryPhoto = card["photo"]
        selected = photo.key in self.selected_photo_keys
        missing = (not photo.front_path.exists()) or (not photo.back_path.exists())

        if selected:
            bg = CARD_BG_SELECTED
        elif missing:
            bg = CARD_BG_MISSING
        else:
            bg = CARD_BG_DEFAULT

        for widget in (card["frame"], card["image_label"]):
            widget.configure(bg=bg)
        card["meta_overlay"].configure(bg=META_UI_BG)
        card["meta_label"].configure(bg=META_UI_BG, fg=META_UI_FG)
        card["meta_button"].configure(bg=bg, highlightthickness=0)
        card["meta_button"].itemconfigure(card["meta_button_oval"], fill="#ffffff", outline="#ffffff")
        card["meta_button"].itemconfigure(card["meta_button_text"], fill="#000000")

    def _schedule_thumbnail_request(self, delay_ms: int = 110) -> None:
        if not self.scroller_active:
            return
        if self.thumbnail_request_after_id is not None:
            try:
                self.root.after_cancel(self.thumbnail_request_after_id)
            except Exception:
                pass
        self.thumbnail_request_after_id = self.root.after(delay_ms, self._run_thumbnail_request)

    def _run_thumbnail_request(self) -> None:
        self.thumbnail_request_after_id = None
        self.request_visible_thumbnail_loading()

    def request_visible_thumbnail_loading(self) -> None:
        if not self.scroller_active or not self.gallery_cards or self.gallery_canvas is None:
            return

        mode = self.mode_var.get()
        visible_indices = self._visible_card_indices()
        visible_set = set(visible_indices)

        if self.thumbnail_job_queue:
            self.thumbnail_job_queue = deque(idx for idx in self.thumbnail_job_queue if idx in visible_set)
            self.thumbnail_job_set = set(self.thumbnail_job_queue)

        for idx in visible_indices:
            if idx in self.thumbnail_job_set or idx < 0 or idx >= len(self.gallery_cards):
                continue
            card = self.gallery_cards[idx]
            key = (card["photo"].key, mode)
            if key in self.gallery_thumbnail_refs:
                card["image_label"].configure(image=self.gallery_thumbnail_refs[key], text="")
                card["image_label"].image = self.gallery_thumbnail_refs[key]
                continue
            self.thumbnail_job_queue.append(idx)
            self.thumbnail_job_set.add(idx)

        if self.thumbnail_job_after_id is None and self.thumbnail_job_queue:
            self.thumbnail_job_after_id = self.root.after(1, self._process_thumbnail_batch)

    def _visible_card_indices(self) -> List[int]:
        if self.gallery_canvas is None:
            return []
        y0, y1 = self.gallery_canvas.yview()
        total = max(1, len(self.gallery_cards))
        rows = (total + GALLERY_MAX_COLUMNS - 1) // GALLERY_MAX_COLUMNS
        first_row = max(0, int(y0 * rows) - 2)
        last_row = min(rows - 1, int(y1 * rows) + 2)
        indices: List[int] = []
        for row in range(first_row, last_row + 1):
            start = row * GALLERY_MAX_COLUMNS
            end = min(total, start + GALLERY_MAX_COLUMNS)
            indices.extend(range(start, end))
        return indices

    def _process_thumbnail_batch(self) -> None:
        if not self.thumbnail_job_queue:
            self.thumbnail_job_after_id = None
            return

        mode = self.mode_var.get()
        batch_size = 2
        for _ in range(batch_size):
            if not self.thumbnail_job_queue:
                break
            idx = self.thumbnail_job_queue.popleft()
            self.thumbnail_job_set.discard(idx)
            if idx < 0 or idx >= len(self.gallery_cards):
                continue

            card = self.gallery_cards[idx]
            photo: MemoryPhoto = card["photo"]
            key = (photo.key, mode)

            image_obj = self.gallery_thumbnail_refs.get(key)
            if image_obj is None:
                image_obj = self._build_thumbnail(photo, mode)
                if image_obj is not None:
                    self.gallery_thumbnail_refs[key] = image_obj

            if image_obj is not None:
                card["image_label"].configure(image=image_obj, text="")
                card["image_label"].image = image_obj
            else:
                card["image_label"].configure(image="", text="Preview unavailable")
                card["image_label"].image = None

        if self.thumbnail_job_queue:
            self.thumbnail_job_after_id = self.root.after(10, self._process_thumbnail_batch)
        else:
            self.thumbnail_job_after_id = None

    def _cancel_thumbnail_loading(self) -> None:
        self.thumbnail_job_queue.clear()
        self.thumbnail_job_set.clear()
        if self.thumbnail_request_after_id is not None:
            try:
                self.root.after_cancel(self.thumbnail_request_after_id)
            except Exception:
                pass
        self.thumbnail_request_after_id = None
        if self.thumbnail_job_after_id is not None:
            try:
                self.root.after_cancel(self.thumbnail_job_after_id)
            except Exception:
                pass
        self.thumbnail_job_after_id = None

    def _build_thumbnail(self, photo: MemoryPhoto, mode: str) -> Optional["ImageTk.PhotoImage"]:
        try:
            target_w = self._current_target_preview_width()
            target_h = max(900, int(target_w * 1.8))
            img = self._render_preview_image(photo, mode, target_w, target_h)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    @staticmethod
    def _open_preview_image(path: Path, max_side: int) -> "Image.Image":
        with Image.open(path) as source:
            try:
                source.draft("RGB", (max_side, max_side))
            except Exception:
                pass
            img = ImageOps.exif_transpose(source).convert("RGB")
        img.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
        return img

    def on_table_selection_changed(self, _event: tk.Event) -> None:
        if self.suppress_table_select_event:
            return
        self.table_selection_pending_items = tuple(self.table.selection())
        if self.table_selection_after_id is not None:
            try:
                self.root.after_cancel(self.table_selection_after_id)
            except Exception:
                pass
        self.table_selection_after_id = self.root.after(24, self._apply_table_selection_change)

    def _apply_table_selection_change(self) -> None:
        self.table_selection_after_id = None
        selected_items = self.table_selection_pending_items
        self.table_selection_pending_items = None
        if selected_items is None:
            selected_items = tuple(self.table.selection())

        old_keys = set(self.selected_photo_keys)
        new_keys: set[str] = set()
        for item in selected_items:
            photo = self.photo_by_item.get(item)
            if photo is not None:
                new_keys.add(photo.key)

        self.selected_photo_keys = new_keys
        if selected_items:
            first_photo = self.photo_by_item.get(selected_items[0])
            first_idx = self.photo_index_by_key.get(first_photo.key) if first_photo is not None else None
            if len(selected_items) == 1:
                self.selection_anchor_index = first_idx
            elif self.selection_anchor_index is None:
                self.selection_anchor_index = first_idx

            focus_item = self.table.focus()
            focus_photo = self.photo_by_item.get(focus_item) if focus_item else None
            if focus_photo is None:
                focus_photo = first_photo
            self.selection_focus_index = (
                self.photo_index_by_key.get(focus_photo.key) if focus_photo is not None else first_idx
            )
        else:
            self.selection_anchor_index = None
            self.selection_focus_index = None

        self._refresh_gallery_selection_for_keys(old_keys ^ new_keys)
        self.update_selection_status()

    def on_table_double_click(self, event: tk.Event) -> None:
        item = self.table.identify_row(event.y)
        if not item:
            selected = self.table.selection()
            item = selected[0] if selected else ""
        if not item:
            return

        photo = self.photo_by_item.get(item)
        if photo is None:
            return
        self.open_photo_preview_window(photo)

    def _close_preview_window(self) -> None:
        if self.preview_nav_after_id is not None:
            try:
                self.root.after_cancel(self.preview_nav_after_id)
            except Exception:
                pass
            self.preview_nav_after_id = None
        self.preview_nav_pending_index = None

        if self.preview_window is not None:
            try:
                if self.preview_window.winfo_exists():
                    self.preview_window.destroy()
            except Exception:
                pass
        self.preview_window = None
        self.preview_image_label = None
        self.preview_info_label = None
        self.preview_signature = None

    def on_preview_space_close(self, _event: tk.Event) -> str:
        self._close_preview_window()
        return "break"

    def on_preview_arrow_nav(self, event: tk.Event) -> str:
        direction = -1 if event.keysym.endswith("Up") else 1
        shift_down = bool(event.state & 0x0001)
        new_idx = self._move_selection_by_arrow(direction, shift_down)
        if new_idx is None or new_idx < 0 or new_idx >= len(self.photos):
            return "break"

        self.preview_nav_pending_index = new_idx
        if self.preview_nav_after_id is not None:
            try:
                self.root.after_cancel(self.preview_nav_after_id)
            except Exception:
                pass
        self.preview_nav_after_id = self.root.after(PREVIEW_NAV_DEBOUNCE_MS, self._open_pending_preview_from_nav)
        return "break"

    def _open_pending_preview_from_nav(self) -> None:
        self.preview_nav_after_id = None
        idx = self.preview_nav_pending_index
        self.preview_nav_pending_index = None
        if idx is None or idx < 0 or idx >= len(self.photos):
            return

        next_photo = self.photos[idx]
        mode = self.mode_var.get()
        if (
            self.preview_window is not None
            and self.preview_window.winfo_exists()
            and self.preview_signature == (next_photo.key, mode)
        ):
            return
        self.open_photo_preview_window(next_photo, show_errors=False)

    def _center_window_over_root(self, win: tk.Toplevel) -> None:
        self.root.update_idletasks()
        win.update_idletasks()

        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(1, self.root.winfo_width())
        root_h = max(1, self.root.winfo_height())

        win_w = max(1, win.winfo_reqwidth())
        win_h = max(1, win.winfo_reqheight())

        x = root_x + (root_w - win_w) // 2
        y = root_y + (root_h - win_h) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _show_modal_dialog(
        self,
        title: str,
        message: str,
        kind: str = "info",
        detail: str = "",
        buttons: Tuple[str, ...] = ("OK",),
        default: Optional[str] = None,
    ) -> str:
        colors = {
            "info": "#111111",
            "warning": "#8a4b00",
            "error": "#9f1111",
        }
        accent = colors.get(kind, "#111111")
        chosen = {"value": default or buttons[0]}

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#f5f5f5")
        self._set_window_icon(win)

        header = tk.Frame(win, bg=accent, height=8)
        header.pack(fill=tk.X)

        body = tk.Frame(win, bg="#f5f5f5", padx=22, pady=18)
        body.pack(fill=tk.BOTH, expand=True)

        title_label = tk.Label(
            body,
            text=title,
            bg="#f5f5f5",
            fg="#111111",
            font=("Helvetica", 16, "bold"),
            anchor="w",
            justify=tk.LEFT,
        )
        title_label.pack(fill=tk.X)

        message_label = tk.Label(
            body,
            text=message,
            bg="#f5f5f5",
            fg="#111111",
            font=("Helvetica", 12),
            wraplength=500,
            justify=tk.LEFT,
            anchor="w",
            pady=10,
        )
        message_label.pack(fill=tk.X)

        if detail:
            detail_label = tk.Label(
                body,
                text=detail,
                bg="#f5f5f5",
                fg="#444444",
                font=("Menlo", 10),
                wraplength=500,
                justify=tk.LEFT,
                anchor="w",
            )
            detail_label.pack(fill=tk.X, pady=(0, 8))

        button_row = tk.Frame(body, bg="#f5f5f5")
        button_row.pack(fill=tk.X, pady=(8, 0))

        def close_with(value: str) -> None:
            chosen["value"] = value
            win.destroy()

        for label in reversed(buttons):
            is_default = label == (default or buttons[0])
            btn = tk.Button(
                button_row,
                text=label,
                command=lambda value=label: close_with(value),
                relief=tk.FLAT,
                bd=0,
                padx=16,
                pady=8,
                font=("Helvetica", 11, "bold" if is_default else "normal"),
                bg="#111111" if is_default else "#e7e7e7",
                fg="#ffffff" if is_default else "#111111",
                activebackground="#222222" if is_default else "#d9d9d9",
                activeforeground="#ffffff" if is_default else "#111111",
                cursor="hand2",
            )
            btn.pack(side=tk.RIGHT, padx=(8, 0))
            if is_default:
                btn.focus_set()

        win.protocol("WM_DELETE_WINDOW", lambda: close_with(default or buttons[0]))
        win.bind("<Return>", lambda _event: close_with(default or buttons[0]))
        if len(buttons) > 1:
            win.bind("<Escape>", lambda _event: close_with(buttons[-1]))

        self._center_window_over_root(win)
        win.deiconify()
        win.grab_set()
        self._raise_preview_window(win)
        self.root.wait_window(win)
        return str(chosen["value"])

    def _show_info_dialog(self, title: str, message: str, detail: str = "") -> None:
        self._show_modal_dialog(title, message, kind="info", detail=detail)

    def _show_warning_dialog(self, title: str, message: str, detail: str = "") -> None:
        self._show_modal_dialog(title, message, kind="warning", detail=detail)

    def _show_error_dialog(self, title: str, message: str, detail: str = "") -> None:
        self._show_modal_dialog(title, message, kind="error", detail=detail)

    def _ask_confirm_dialog(self, title: str, message: str, detail: str = "") -> bool:
        choice = self._show_modal_dialog(
            title,
            message,
            kind="warning",
            detail=detail,
            buttons=("Continue", "Cancel"),
            default="Continue",
        )
        return choice == "Continue"

    def _open_download_progress(self, total: int, mode_label: str) -> None:
        self._close_download_progress()
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("Downloading BeReals")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#f5f5f5")
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        self._set_window_icon(win)

        body = tk.Frame(win, bg="#f5f5f5", padx=22, pady=18)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            body,
            text="Downloading BeReals",
            bg="#f5f5f5",
            fg="#111111",
            font=("Helvetica", 16, "bold"),
            anchor="w",
        ).pack(fill=tk.X)

        self.download_progress_title_var.set(f"Preparing {total} export(s) in {mode_label}")
        tk.Label(
            body,
            textvariable=self.download_progress_title_var,
            bg="#f5f5f5",
            fg="#111111",
            font=("Helvetica", 12),
            anchor="w",
            pady=10,
        ).pack(fill=tk.X)

        progress = ttk.Progressbar(body, mode="determinate", maximum=max(1, total), length=440)
        progress.pack(fill=tk.X)
        self.download_progress_bar = progress

        self.download_progress_detail_var.set("Starting...")
        tk.Label(
            body,
            textvariable=self.download_progress_detail_var,
            bg="#f5f5f5",
            fg="#333333",
            font=("Helvetica", 11),
            anchor="w",
            pady=10,
        ).pack(fill=tk.X)

        self.download_progress_counts_var.set("Success: 0    Skipped: 0    Failed: 0")
        tk.Label(
            body,
            textvariable=self.download_progress_counts_var,
            bg="#f5f5f5",
            fg="#555555",
            font=("Helvetica", 10),
            anchor="w",
        ).pack(fill=tk.X)

        self._center_window_over_root(win)
        win.deiconify()
        win.grab_set()
        self._raise_preview_window(win)
        self.download_progress_window = win

    def _close_download_progress(self) -> None:
        if self.download_progress_window is not None:
            try:
                if self.download_progress_window.winfo_exists():
                    self.download_progress_window.grab_release()
                    self.download_progress_window.destroy()
            except Exception:
                pass
        self.download_progress_window = None
        self.download_progress_bar = None

    def _render_preview_image(self, photo: MemoryPhoto, mode: str, max_w: int, max_h: int) -> "Image.Image":
        source_max_side = min(2200, max(900, int(max(max_w, max_h) * 1.25)))
        downloaded_output = self.history.get_output_path(photo.key, mode)
        if downloaded_output is not None and downloaded_output.exists():
            img = self._open_preview_image(downloaded_output, source_max_side)
        else:
            if mode == MODE_FRONT_ONLY:
                if not photo.front_path.exists():
                    raise FileNotFoundError("Front image file is missing.")
                img = self._open_preview_image(photo.front_path, source_max_side)
            elif mode == MODE_BACK_ONLY:
                if not photo.back_path.exists():
                    raise FileNotFoundError("Back image file is missing.")
                img = self._open_preview_image(photo.back_path, source_max_side)
            elif mode == MODE_BEREAL_FRONT_TL:
                if not photo.front_path.exists() or not photo.back_path.exists():
                    raise FileNotFoundError("Front or back image file is missing.")
                base = self._open_preview_image(photo.back_path, source_max_side)
                inset = self._open_preview_image(photo.front_path, max(520, int(source_max_side * 0.52)))
                img = ImageExporter._compose(base=base, inset=inset)
            elif mode == MODE_BEREAL_BACK_TL:
                if not photo.front_path.exists() or not photo.back_path.exists():
                    raise FileNotFoundError("Front or back image file is missing.")
                base = self._open_preview_image(photo.front_path, source_max_side)
                inset = self._open_preview_image(photo.back_path, max(520, int(source_max_side * 0.52)))
                img = ImageExporter._compose(base=base, inset=inset)
            else:
                if not photo.front_path.exists():
                    raise FileNotFoundError("Front image file is missing.")
                img = self._open_preview_image(photo.front_path, source_max_side)

        if img.width > max_w or img.height > max_h:
            scale = min(max_w / img.width, max_h / img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)
        return img

    def _raise_preview_window(self, win: tk.Toplevel) -> None:
        win.lift()
        try:
            win.attributes("-topmost", True)
            win.after(40, lambda w=win: w.attributes("-topmost", False) if w.winfo_exists() else None)
        except Exception:
            pass
        win.focus_force()

    def _focus_main_window_on_start(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(
                40,
                lambda: self.root.attributes("-topmost", False) if self.root.winfo_exists() else None,
            )
            self.root.focus_force()
        except Exception:
            pass

    def open_photo_preview_window(self, photo: MemoryPhoto, show_errors: bool = True) -> None:
        if self.preview_nav_after_id is not None:
            try:
                self.root.after_cancel(self.preview_nav_after_id)
            except Exception:
                pass
            self.preview_nav_after_id = None
        self.preview_nav_pending_index = None

        mode = self.mode_var.get()
        max_w = int(self.root.winfo_screenwidth() * 0.82)
        max_h = int(self.root.winfo_screenheight() * 0.82)

        try:
            img = self._render_preview_image(photo, mode, max_w, max_h)
        except Exception as exc:
            if show_errors:
                self._show_error_dialog("Preview failed", str(exc))
            return

        photo_img = ImageTk.PhotoImage(img)
        info = f"{self._format_time(photo.taken_time)}  |  {MODE_LABELS.get(mode, mode)}"

        if (
            self.preview_window is not None
            and self.preview_window.winfo_exists()
            and self.preview_image_label is not None
            and self.preview_info_label is not None
        ):
            self.preview_image_label.configure(image=photo_img)
            self.preview_image_label.image = photo_img
            self.preview_info_label.configure(text=info)
            self.preview_window.preview_photo = photo_img
            self.preview_signature = (photo.key, mode)
            self._raise_preview_window(self.preview_window)
            return

        self._close_preview_window()
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(f"Export Preview - {MODE_LABELS.get(mode, mode)}")
        win.configure(bg="#000000")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_preview_window)
        win.bind("<space>", self.on_preview_space_close)
        win.bind("<Up>", self.on_preview_arrow_nav)
        win.bind("<Down>", self.on_preview_arrow_nav)

        image_label = tk.Label(win, image=photo_img, bg="#000000", bd=0, highlightthickness=0)
        image_label.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))
        image_label.bind("<space>", self.on_preview_space_close)
        image_label.bind("<Up>", self.on_preview_arrow_nav)
        image_label.bind("<Down>", self.on_preview_arrow_nav)

        info_label = tk.Label(
            win,
            text=info,
            bg="#000000",
            fg="#ffffff",
            font=("Helvetica", 11, "bold"),
        )
        info_label.pack(fill=tk.X, padx=8, pady=(0, 8))
        info_label.bind("<space>", self.on_preview_space_close)
        info_label.bind("<Up>", self.on_preview_arrow_nav)
        info_label.bind("<Down>", self.on_preview_arrow_nav)

        self._center_window_over_root(win)
        win.deiconify()
        self._raise_preview_window(win)

        win.preview_photo = photo_img
        self.preview_window = win
        self.preview_image_label = image_label
        self.preview_info_label = info_label
        self.preview_signature = (photo.key, mode)

    def sync_table_selection_from_model(self) -> None:
        self.suppress_table_select_event = True
        try:
            current = set(self.table.selection())
            target = {
                self.table_item_by_photo_key[k]
                for k in self.selected_photo_keys
                if k in self.table_item_by_photo_key
            }
            to_remove = tuple(current - target)
            to_add = tuple(target - current)
            if to_remove:
                self.table.selection_remove(*to_remove)
            if to_add:
                self.table.selection_add(*to_add)
            if self.selected_photo_keys:
                focus_idx = self.selection_focus_index
                if focus_idx is not None and 0 <= focus_idx < len(self.photos):
                    focus_key = self.photos[focus_idx].key
                else:
                    focus_key = min(
                        self.selected_photo_keys,
                        key=lambda k: self.photo_index_by_key.get(k, len(self.photos) + 1),
                    )
                focus_item = self.table_item_by_photo_key.get(focus_key)
                if focus_item:
                    self.table.focus(focus_item)
                    self.table.see(focus_item)
        finally:
            self.suppress_table_select_event = False

    def update_selection_status(self) -> None:
        self.selection_status_var.set(f"Selected: {len(self.selected_photo_keys)}")

    def on_browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.cwd()))
        if selected:
            self.path_var.set(selected)

    def _show_imaging_dependency_error(self) -> None:
        if Image is None or ImageOps is None:
            detail = f"\n\nImport error:\n{PIL_IMPORT_ERROR}" if PIL_IMPORT_ERROR is not None else ""
            self._show_error_dialog(
                "Missing dependency",
                (
                    "The app could not import Pillow's core image modules.\n\n"
                    "If you are running from source, install dependencies with:\n\n"
                    "python3 -m pip install -r requirements.txt"
                ),
                detail=detail.strip(),
            )
            return

        detail = f"\n\nImport error:\n{IMAGETK_IMPORT_ERROR}" if IMAGETK_IMPORT_ERROR is not None else ""
        self._show_error_dialog(
            "Imaging setup problem",
            (
                "The app could import Pillow, but it could not load Pillow's Tk image bridge "
                "(`ImageTk`).\n\n"
                "If you are launching the installed app bundle, rebuild and reinstall it with:\n\n"
                "make app-bundle\nmake install-app"
            ),
            detail=detail.strip(),
        )

    def on_load_data(self) -> None:
        if Image is None or ImageOps is None or ImageTk is None:
            self._show_imaging_dependency_error()
            return

        try:
            base = Path(self.path_var.get()).expanduser()
            export_dir = self.loader.find_export_dir(base)
            photos = self.loader.load_memories(export_dir)
        except Exception as exc:
            self._show_error_dialog("Load failed", str(exc))
            return

        self.export_dir = export_dir
        self.photos = photos
        self.selected_photo_keys.clear()
        self.selection_anchor_index = None
        self.selection_focus_index = None
        self.card_meta_visible_keys.clear()
        self.show_all_metadata_var.set(False)

        self.refresh_table()
        self.request_scroller_refresh()
        self.update_selection_status()
        self.status_var.set(f"Loaded {len(self.photos)} memory entries from {export_dir}")

    def refresh_table(self) -> None:
        if self.table_selection_after_id is not None:
            try:
                self.root.after_cancel(self.table_selection_after_id)
            except Exception:
                pass
            self.table_selection_after_id = None
        self.table_selection_pending_items = None

        self.photo_by_item.clear()
        self.table_item_by_photo_key.clear()
        self.photo_index_by_key = {photo.key: idx for idx, photo in enumerate(self.photos)}
        for item in self.table.get_children():
            self.table.delete(item)

        mode = self.mode_var.get()

        for photo in self.photos:
            downloaded_modes = self.history.downloaded_modes(photo.key)
            downloaded_mode = "Yes" if mode in downloaded_modes else "No"

            tags = []
            if not photo.front_path.exists() or not photo.back_path.exists():
                tags.append("missing")

            loc = self._format_location(photo.location)
            files = "OK" if photo.front_path.exists() and photo.back_path.exists() else "Missing"

            item_id = self.table.insert(
                "",
                tk.END,
                values=(
                    self._format_time(photo.taken_time),
                    "Yes" if photo.is_late else "No",
                    self._truncate(photo.caption, 72),
                    loc,
                    files,
                    downloaded_mode,
                    ", ".join(MODE_LABELS[m] for m in downloaded_modes) if downloaded_modes else "",
                ),
                tags=tuple(tags),
            )
            self.photo_by_item[item_id] = photo
            self.table_item_by_photo_key[photo.key] = item_id

        self.sync_table_selection_from_model()

    def on_download_selected(self) -> None:
        if self.selected_photo_keys:
            photos = [p for p in self.photos if p.key in self.selected_photo_keys]
        else:
            selected_items = self.table.selection()
            photos = [self.photo_by_item[item] for item in selected_items if item in self.photo_by_item]

        if not photos:
            self._show_info_dialog("Nothing selected", "Select one or more rows in the table or scroller.")
            return

        self._download_photos(photos)

    def on_download_all(self) -> None:
        if not self.photos:
            self._show_info_dialog("No data", "Load export data first.")
            return
        self._download_photos(self.photos)

    def _download_photos(self, photos: List[MemoryPhoto]) -> None:
        if self.download_state is not None:
            return

        mode = self.mode_var.get()
        existing_outputs = {
            photo.key: self.history.get_output_path(photo.key, mode)
            for photo in photos
        }
        existing_metadata = {
            photo.key: self.history.get_metadata_path(photo.key, mode)
            for photo in photos
        }
        existing_count = sum(1 for path in existing_outputs.values() if path is not None)

        if existing_count and not self.skip_existing_var.get():
            mode_label = MODE_LABELS.get(mode, mode)
            should_overwrite = self._ask_confirm_dialog(
                "Confirm overwrite",
                f"{existing_count} selected export(s) already exist for {mode_label}.",
                detail="Overwrite the existing image and metadata files?",
            )
            if not should_overwrite:
                self.status_var.set("Overwrite canceled.")
                return

        total = len(photos)
        mode_label = MODE_LABELS.get(mode, mode)
        self._open_download_progress(total, mode_label)
        self.status_var.set(f"Exporting 0/{total}...")

        event_queue: queue.Queue = queue.Queue()
        self.download_queue = event_queue
        self.download_state = {
            "mode": mode,
            "total": total,
            "succeeded": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        worker = threading.Thread(
            target=self._download_worker,
            args=(
                photos,
                mode,
                bool(self.skip_existing_var.get()),
                existing_outputs,
                existing_metadata,
                event_queue,
            ),
            daemon=True,
        )
        worker.start()
        self.download_poll_after_id = self.root.after(30, self._poll_download_queue)

    def _download_worker(
        self,
        photos: List[MemoryPhoto],
        mode: str,
        skip_existing: bool,
        existing_outputs: Dict[str, Optional[Path]],
        existing_metadata: Dict[str, Optional[Path]],
        event_queue: "queue.Queue[Dict[str, object]]",
    ) -> None:
        total = len(photos)
        for i, photo in enumerate(photos, start=1):
            existing_output_path = existing_outputs.get(photo.key)
            if skip_existing and existing_output_path is not None:
                event_queue.put({"type": "skipped", "index": i, "total": total, "photo": photo})
                continue

            try:
                out_path, sidecar_path = self.exporter.export_photo(
                    photo,
                    mode,
                    overwrite_path=existing_output_path,
                    overwrite_metadata_path=existing_metadata.get(photo.key),
                )
                event_queue.put(
                    {
                        "type": "success",
                        "index": i,
                        "total": total,
                        "photo": photo,
                        "output_path": out_path,
                        "sidecar_path": sidecar_path,
                    }
                )
            except Exception as exc:
                event_queue.put(
                    {
                        "type": "failed",
                        "index": i,
                        "total": total,
                        "photo": photo,
                        "error": str(exc),
                    }
                )

        event_queue.put({"type": "done"})

    def _poll_download_queue(self) -> None:
        if self.download_queue is None or self.download_state is None:
            return

        while True:
            try:
                event = self.download_queue.get_nowait()
            except queue.Empty:
                break

            event_type = str(event.get("type"))
            if event_type == "done":
                self._finish_download_run()
                return

            index = int(event.get("index", 0))
            total = int(event.get("total", 0))
            photo = event.get("photo")
            photo_time = self._format_time(photo.taken_time) if isinstance(photo, MemoryPhoto) else ""

            if event_type == "success":
                output_path = event.get("output_path")
                sidecar_path = event.get("sidecar_path")
                if isinstance(photo, MemoryPhoto) and isinstance(output_path, Path) and isinstance(sidecar_path, Path):
                    self.history.mark_download(photo.key, str(self.download_state["mode"]), output_path, sidecar_path)
                self.download_state["succeeded"] = int(self.download_state["succeeded"]) + 1
                action = "Saved"
            elif event_type == "skipped":
                self.download_state["skipped"] = int(self.download_state["skipped"]) + 1
                action = "Skipped"
            else:
                self.download_state["failed"] = int(self.download_state["failed"]) + 1
                error = str(event.get("error", "Unknown error"))
                errors = self.download_state["errors"]
                if isinstance(errors, list):
                    errors.append(f"{photo_time or 'Unknown time'}: {error}")
                action = "Failed"

            if self.download_progress_bar is not None:
                self.download_progress_bar.configure(value=index)
            self.download_progress_title_var.set(
                f"Exporting {index}/{total} in {MODE_LABELS.get(str(self.download_state['mode']), str(self.download_state['mode']))}"
            )
            self.download_progress_detail_var.set(f"{action}: {photo_time or 'Unknown capture time'}")
            self.download_progress_counts_var.set(
                "Success: "
                f"{self.download_state['succeeded']}    "
                f"Skipped: {self.download_state['skipped']}    "
                f"Failed: {self.download_state['failed']}"
            )
            self.status_var.set(f"Exporting {index}/{total}...")

        self.download_poll_after_id = self.root.after(30, self._poll_download_queue)

    def _finish_download_run(self) -> None:
        if self.download_poll_after_id is not None:
            try:
                self.root.after_cancel(self.download_poll_after_id)
            except Exception:
                pass
        self.download_poll_after_id = None

        state = self.download_state or {}
        self.download_queue = None
        self.download_state = None

        self.history.save()
        self.refresh_table()
        self.request_scroller_refresh()
        self._close_download_progress()

        summary = (
            f"Done. Success: {int(state.get('succeeded', 0))}, "
            f"Skipped: {int(state.get('skipped', 0))}, "
            f"Failed: {int(state.get('failed', 0))}"
        )
        self.status_var.set(summary)

        errors = state.get("errors", [])
        if isinstance(errors, list) and errors:
            preview = "\n".join(errors[:10])
            more = "" if len(errors) <= 10 else f"\n...and {len(errors) - 10} more"
            self._show_warning_dialog("Completed with errors", summary, detail=f"{preview}{more}".strip())
        else:
            self._show_info_dialog("Completed", summary)

    def on_open_output(self) -> None:
        output_dir = self.exporter.downloads_root
        output_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform == "darwin":
            subprocess.run(["open", str(output_dir)], check=False)
        elif os.name == "nt":
            os.startfile(output_dir)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(output_dir)], check=False)

    @staticmethod
    def _format_time(value: str) -> str:
        if not value:
            return ""
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value

    @staticmethod
    def _format_time_human(value: str) -> str:
        if not value:
            return "Unknown capture time"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
            hour = dt.strftime("%I").lstrip("0") or "0"
            return f"{dt.strftime('%B %d, %Y')} at {hour}:{dt.strftime('%M:%S %p')}"
        except ValueError:
            return value

    @staticmethod
    def _format_location(location: Optional[Dict[str, float]]) -> str:
        if not location:
            return ""
        lat = location.get("latitude")
        lon = location.get("longitude")
        if lat is None or lon is None:
            return ""
        return f"{lat:.5f}, {lon:.5f}"

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."


def main() -> None:
    root = tk.Tk()
    app = BeRealDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
