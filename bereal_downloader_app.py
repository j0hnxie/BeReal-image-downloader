#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import textwrap
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, ttk

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
APP_MIN_WIDTH = 980
APP_MIN_HEIGHT = 700

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

GALLERY_MAX_COLUMNS = 6
GALLERY_MEDIUM_COLUMNS = 5
GALLERY_MIN_COLUMNS = 4
GALLERY_FIVE_COLUMN_BREAKPOINT = 930
GALLERY_SIX_COLUMN_BREAKPOINT = 1100
CARD_BG_DEFAULT = "#e9ecef"
CARD_BG_SELECTED = "#cfe8ff"
CARD_BG_MISSING = "#ffe9e9"
META_UI_BG = "#000000"
META_UI_FG = "#ffffff"
PREVIEW_TEXT_FG = "#000000"
CARD_HIGHLIGHT_SELECTED = "#111111"
CARD_HIGHLIGHT_MISSING = "#c86b6b"
GRID_THUMB_MIN_WIDTH = 96
GRID_THUMB_MAX_WIDTH = 158
GRID_THUMB_HEIGHT_RATIO = 1.35
GALLERY_INITIAL_BATCH = 72
GALLERY_BATCH_SIZE = 48
GALLERY_PREFETCH_VIEWPORTS = 1.75
PREVIEW_NAV_DEBOUNCE_MS = 45
BEREAL_INSET_ASPECT = 0.75
BEREAL_INSET_HEIGHT_RATIO = 0.416
BEREAL_INSET_MARGIN_RATIO = 0.028


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
            return self._compose(base=front, inset=back, canvas_reference=back)

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
    def _compose(
        base: "Image.Image",
        inset: "Image.Image",
        canvas_reference: Optional["Image.Image"] = None,
    ) -> "Image.Image":
        if ImageDraw is None:
            raise RuntimeError("Pillow ImageDraw support is required for BeReal composition.")

        output_reference = canvas_reference or base
        composed = ImageOps.fit(
            base.copy(),
            output_reference.size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        reference_side = min(composed.width, composed.height)
        margin = max(13, int(reference_side * BEREAL_INSET_MARGIN_RATIO))
        inset_target_h = max(176, int(reference_side * BEREAL_INSET_HEIGHT_RATIO))
        inset_target_w = max(110, int(inset_target_h * BEREAL_INSET_ASPECT))
        max_inset_w = max(96, composed.width - (margin * 2) - 4)
        max_inset_h = max(120, composed.height - (margin * 2) - 4)
        scale = min(1.0, max_inset_w / inset_target_w, max_inset_h / inset_target_h)
        inset_target_w = max(96, int(inset_target_w * scale))
        inset_target_h = max(120, int(inset_target_h * scale))
        inset_copy = inset.copy()
        inset_copy = ImageOps.fit(
            inset_copy,
            (inset_target_w, inset_target_h),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

        border = max(2, int(reference_side * 0.004))
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
        self.root.minsize(APP_MIN_WIDTH, APP_MIN_HEIGHT)

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
        self.status_var = tk.StringVar(value="Select an export folder and click Load Data.")
        self.selection_status_var = tk.StringVar(value="Selected: 0")

        self.table_item_by_photo_key: Dict[str, str] = {}
        self.suppress_table_select_event = False

        self.selected_photo_keys: set[str] = set()
        self.selection_anchor_index: Optional[int] = None
        self.selection_focus_index: Optional[int] = None

        self.scroller_container: Optional[ttk.Frame] = None
        self.scroller_grid_frame: Optional[ttk.Frame] = None
        self.scroller_detail_frame: Optional[ttk.Frame] = None
        self.scroller_detail_image_label: Optional[tk.Canvas] = None
        self.scroller_detail_title_var = tk.StringVar(value="")
        self.scroller_detail_info_var = tk.StringVar(value="")
        self.scroller_detail_mode: bool = False
        self.scroller_detail_index: Optional[int] = None
        self.scroller_detail_canvas_items: Dict[str, int] = {}
        self.scroller_detail_meta_overlay: Optional[tk.Frame] = None
        self.scroller_detail_meta_label: Optional[tk.Label] = None
        self.scroller_detail_meta_visible: bool = False
        self.scroller_detail_meta_after_id: Optional[str] = None
        self.scroller_detail_image_bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self.scroller_detail_drag_start: Optional[Tuple[int, int]] = None
        self.notebook: Optional[ttk.Notebook] = None
        self.table_tab: Optional[ttk.Frame] = None
        self.scroller_tab: Optional[ttk.Frame] = None
        self.scroller_active: bool = False
        self.gallery_canvas: Optional[tk.Canvas] = None
        self.gallery_inner: Optional[tk.Frame] = None
        self.gallery_scrollbar: Optional[ttk.Scrollbar] = None
        self.gallery_window_id: Optional[int] = None
        self.gallery_footer: Optional[ttk.Frame] = None
        self.gallery_footer_label_var = tk.StringVar(value="")
        self.gallery_footer_progress: Optional[ttk.Progressbar] = None
        self.gallery_loading_overlay: Optional[ttk.Frame] = None
        self.gallery_loading_overlay_progress: Optional[ttk.Progressbar] = None
        self.gallery_initial_loading_active: bool = False
        self.gallery_cards: List[Dict] = []
        self.gallery_card_by_key: Dict[str, Dict] = {}
        self.gallery_column_count: int = GALLERY_MAX_COLUMNS
        self.gallery_rendered_count: int = 0
        self.gallery_thumbnail_refs: Dict[Tuple[str, str], "ImageTk.PhotoImage"] = {}
        self.card_meta_visible_keys: set[str] = set()
        self.gallery_batch_after_id: Optional[str] = None
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
        self.download_progress_cancel_var = tk.StringVar(value="Cancel Download")
        self.download_queue: Optional[queue.Queue] = None
        self.download_poll_after_id: Optional[str] = None
        self.download_state: Optional[Dict[str, object]] = None
        self.download_cancel_event = threading.Event()
        self.download_cancel_button: Optional[ttk.Button] = None

        self._configure_app_identity()
        self._build_ui()
        self._configure_row_tags()
        self._configure_focus_management()
        self._configure_keyboard_shortcuts()
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

        if self._running_in_macos_app_bundle():
            return

        try:
            self.app_icon_photo = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.app_icon_photo)
        except Exception:
            self.app_icon_photo = None

    def _running_in_macos_app_bundle(self) -> bool:
        if sys.platform != "darwin":
            return False
        return any(parent.suffix == ".app" for parent in Path(__file__).resolve().parents)

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
        if self._running_in_macos_app_bundle():
            return
        if self.app_icon_photo is None:
            return
        try:
            win.iconphoto(True, self.app_icon_photo)
        except Exception:
            pass

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill=tk.BOTH, expand=True)

        path_frame = ttk.Frame(outer)
        path_frame.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(path_frame, text="Export folder:").pack(side=tk.LEFT)
        path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        ttk.Button(path_frame, text="Browse", command=self.on_browse).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(path_frame, text="Load Data", command=self.on_load_data).pack(side=tk.LEFT)

        mode_frame = ttk.LabelFrame(outer, text="Download mode", padding=6)
        mode_frame.pack(fill=tk.X, pady=(0, 4))

        for mode, label in MODE_LABELS.items():
            ttk.Radiobutton(
                mode_frame,
                text=label,
                value=mode,
                variable=self.mode_var,
                command=self.on_export_mode_changed,
            ).pack(side=tk.LEFT, padx=(0, 18))

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill=tk.X, pady=(0, 4))

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
        status.pack(fill=tk.X, pady=(4, 0))

    def _is_scroller_tab_active(self) -> bool:
        if self.notebook is None or self.scroller_tab is None:
            return False
        return self.notebook.select() == str(self.scroller_tab)

    def on_notebook_tab_changed(self, _event: tk.Event) -> None:
        self.scroller_active = self._is_scroller_tab_active()
        if self.scroller_active:
            if self.scroller_needs_refresh:
                if self.scroller_detail_mode:
                    self.render_scroller_detail()
                else:
                    self.refresh_scroller()
            else:
                self.refresh_gallery_selection_styles()
                if not self.scroller_detail_mode:
                    self._schedule_thumbnail_request(1)
                    self._schedule_gallery_batch_load()
                else:
                    self.render_scroller_detail()
        else:
            self._cancel_thumbnail_loading()
            self._cancel_gallery_batch_load()

    def request_scroller_refresh(self) -> None:
        self.scroller_needs_refresh = True
        if self.scroller_active and not self.scroller_detail_mode:
            self.refresh_scroller()

    def _show_scroller_grid(self) -> None:
        self.scroller_detail_mode = False
        self.scroller_detail_index = None
        if self.scroller_detail_frame is not None:
            self.scroller_detail_frame.pack_forget()
        if self.scroller_grid_frame is not None:
            self.scroller_grid_frame.pack(fill=tk.BOTH, expand=True)
        if self.gallery_canvas is not None:
            self.gallery_canvas.focus_set()
            self._schedule_thumbnail_request(1)
            self._schedule_gallery_batch_load()
        self._update_gallery_initial_loading_visibility()

    def _show_gallery_loading_footer(self, text: str = "Loading more photos...") -> None:
        if self.gallery_footer is None:
            return
        self.gallery_footer_label_var.set(text)
        if not self.gallery_footer.winfo_manager():
            self.gallery_footer.pack(fill=tk.X, pady=(4, 0))
        if self.gallery_footer_progress is not None:
            self.gallery_footer_progress.start(10)

    def _hide_gallery_loading_footer(self) -> None:
        if self.gallery_footer_progress is not None:
            self.gallery_footer_progress.stop()
        if self.gallery_footer is not None and self.gallery_footer.winfo_manager():
            self.gallery_footer.pack_forget()
        self.gallery_footer_label_var.set("")

    def _show_gallery_initial_loading(self) -> None:
        self.gallery_initial_loading_active = False
        if self.gallery_loading_overlay_progress is not None:
            self.gallery_loading_overlay_progress.stop()
        if self.gallery_loading_overlay is not None:
            self.gallery_loading_overlay.place_forget()

    def _hide_gallery_initial_loading(self) -> None:
        self.gallery_initial_loading_active = False
        if self.gallery_loading_overlay_progress is not None:
            self.gallery_loading_overlay_progress.stop()
        if self.gallery_loading_overlay is not None:
            self.gallery_loading_overlay.place_forget()

    def _visible_gallery_cards(self) -> List[Dict]:
        if not self.gallery_cards:
            return []
        indices = self._visible_card_indices()
        if indices:
            return [self.gallery_cards[idx] for idx in indices if 0 <= idx < len(self.gallery_cards)]
        fallback_count = min(len(self.gallery_cards), max(1, self.gallery_column_count) * 2)
        return self.gallery_cards[:fallback_count]

    def _update_gallery_initial_loading_visibility(self) -> None:
        if not self.photos or not self.scroller_active or self.scroller_detail_mode:
            self._hide_gallery_initial_loading()
            return
        if any(bool(card.get("has_thumbnail")) for card in self._visible_gallery_cards()):
            self._hide_gallery_initial_loading()
        else:
            self._show_gallery_initial_loading()

    def _cancel_gallery_batch_load(self) -> None:
        if self.gallery_batch_after_id is not None:
            try:
                self.root.after_cancel(self.gallery_batch_after_id)
            except Exception:
                pass
        self.gallery_batch_after_id = None
        self._hide_gallery_loading_footer()

    def _ensure_gallery_cards_rendered(self, target_count: int) -> None:
        if self.gallery_inner is None:
            return
        target_count = max(0, min(len(self.photos), target_count))
        if target_count <= self.gallery_rendered_count:
            return

        for idx in range(self.gallery_rendered_count, target_count):
            photo = self.photos[idx]
            card = self._create_gallery_card(idx, photo)
            self.gallery_cards.append(card)
            self.gallery_card_by_key[photo.key] = card
            self._place_card(card)

        self.gallery_rendered_count = target_count
        self.update_gallery_scrollregion()

    def _gallery_needs_more_cards(self) -> bool:
        if (
            self.gallery_canvas is None
            or self.gallery_inner is None
            or self.gallery_rendered_count >= len(self.photos)
        ):
            return False
        content_height = max(1, self.gallery_inner.winfo_height())
        view_bottom = self.gallery_canvas.canvasy(self.gallery_canvas.winfo_height())
        threshold = max(500, int(self.gallery_canvas.winfo_height() * GALLERY_PREFETCH_VIEWPORTS))
        return (content_height - view_bottom) <= threshold

    def _schedule_gallery_batch_load(self, delay_ms: int = 1) -> None:
        if (
            not self.scroller_active
            or self.scroller_detail_mode
            or self.gallery_rendered_count >= len(self.photos)
            or not self._gallery_needs_more_cards()
        ):
            return
        if self.gallery_batch_after_id is not None:
            return
        self._show_gallery_loading_footer()
        self.gallery_batch_after_id = self.root.after(delay_ms, self._run_gallery_batch_load)

    def _run_gallery_batch_load(self) -> None:
        self.gallery_batch_after_id = None
        if not self.scroller_active or self.scroller_detail_mode:
            self._hide_gallery_loading_footer()
            return

        next_count = min(len(self.photos), self.gallery_rendered_count + GALLERY_BATCH_SIZE)
        self._ensure_gallery_cards_rendered(next_count)
        self._hide_gallery_loading_footer()
        self.refresh_gallery_selection_styles()
        self._update_gallery_initial_loading_visibility()
        self._schedule_thumbnail_request(1)
        if self._gallery_needs_more_cards():
            self._schedule_gallery_batch_load()

    def open_scroller_detail(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.photos):
            return
        self.scroller_detail_mode = True
        self.scroller_detail_index = idx
        self._hide_gallery_initial_loading()
        if self.scroller_grid_frame is not None:
            self.scroller_grid_frame.pack_forget()
        if self.scroller_detail_frame is not None:
            self.scroller_detail_frame.pack(fill=tk.BOTH, expand=True)
        self.render_scroller_detail()

    def exit_scroller_detail(self) -> None:
        if self.scroller_needs_refresh:
            self.refresh_scroller()
            return
        self._show_scroller_grid()
        if self.selection_focus_index is not None:
            self._ensure_scroller_index_visible(self.selection_focus_index)

    def render_scroller_detail(self) -> None:
        if (
            not self.scroller_detail_mode
            or self.scroller_detail_index is None
            or self.scroller_detail_index < 0
            or self.scroller_detail_index >= len(self.photos)
            or self.scroller_detail_image_label is None
        ):
            return

        photo = self.photos[self.scroller_detail_index]
        old_keys = set(self.selected_photo_keys)
        self.selected_photo_keys = {photo.key}
        self.selection_anchor_index = self.scroller_detail_index
        self.selection_focus_index = self.scroller_detail_index
        self._refresh_gallery_selection_for_keys(old_keys ^ self.selected_photo_keys)
        self.sync_table_selection_from_model()
        self.update_selection_status()
        mode = self.mode_var.get()
        holder = self.scroller_detail_image_label
        canvas_w = max(720, int(holder.winfo_width() or self.root.winfo_width() * 0.88))
        canvas_h = max(520, int(holder.winfo_height() or self.root.winfo_height() * 0.82))
        max_w = max(720, int(canvas_w * 0.92))
        max_h = max(520, int(canvas_h * 0.92))
        items = self.scroller_detail_canvas_items

        holder.configure(width=canvas_w, height=canvas_h)

        try:
            img = self._render_preview_image(photo, mode, max_w, max_h)
            photo_img = ImageTk.PhotoImage(img)
            image_w = photo_img.width()
            image_h = photo_img.height()
            x0 = int((canvas_w - image_w) / 2)
            y0 = int((canvas_h - image_h) / 2)
            x1 = x0 + image_w
            y1 = y0 + image_h
            self.scroller_detail_image_bounds = (x0, y0, x1, y1)
            holder.itemconfigure(items["image"], image=photo_img, state="normal")
            holder.coords(items["image"], canvas_w / 2, canvas_h / 2)
            holder.itemconfigure(items["placeholder"], state="hidden")
            holder.itemconfigure(items["text"], text="", state="hidden")
            holder.image = photo_img
        except Exception:
            x0 = int(canvas_w * 0.12)
            y0 = int(canvas_h * 0.08)
            x1 = int(canvas_w * 0.88)
            y1 = int(canvas_h * 0.92)
            self.scroller_detail_image_bounds = (x0, y0, x1, y1)
            holder.itemconfigure(items["image"], image="", state="hidden")
            holder.itemconfigure(items["placeholder"], state="normal")
            holder.itemconfigure(items["text"], text="Preview unavailable", state="normal")
            holder.image = None

        holder.coords(items["placeholder"], *self.scroller_detail_image_bounds)
        holder.coords(items["text"], canvas_w / 2, canvas_h / 2)
        self._position_scroller_detail_controls()
        self.update_scroller_detail_metadata_visibility()

        self.scroller_detail_title_var.set(
            f"{self.scroller_detail_index + 1} of {len(self.photos)}  |  {MODE_LABELS.get(mode, mode)}"
        )
        self.scroller_detail_info_var.set(self._format_time(photo.taken_time))
        holder.focus_set()

    def _position_scroller_detail_controls(self) -> None:
        if self.scroller_detail_image_label is None or not self.scroller_detail_canvas_items:
            return
        canvas = self.scroller_detail_image_label
        items = self.scroller_detail_canvas_items
        x0, y0, x1, y1 = self.scroller_detail_image_bounds
        image_w = max(1, x1 - x0)
        image_h = max(1, y1 - y0)

        meta_radius = 13
        meta_cx = x1 - 18
        meta_cy = y0 + 18
        canvas.coords(items["meta_button_oval"], meta_cx - meta_radius, meta_cy - meta_radius, meta_cx + meta_radius, meta_cy + meta_radius)
        canvas.coords(items["meta_button_text"], meta_cx, meta_cy)

        arrow_radius = max(18, min(28, int(min(image_w, image_h) * 0.035)))
        arrow_margin = max(20, int(image_w * 0.045))
        arrow_cy = (y0 + y1) / 2
        left_cx = x0 + arrow_margin
        right_cx = x1 - arrow_margin
        canvas.coords(
            items["left_arrow_oval"],
            left_cx - arrow_radius,
            arrow_cy - arrow_radius,
            left_cx + arrow_radius,
            arrow_cy + arrow_radius,
        )
        canvas.coords(items["left_arrow_text"], left_cx, arrow_cy - 1)
        canvas.coords(
            items["right_arrow_oval"],
            right_cx - arrow_radius,
            arrow_cy - arrow_radius,
            right_cx + arrow_radius,
            arrow_cy + arrow_radius,
        )
        canvas.coords(items["right_arrow_text"], right_cx, arrow_cy - 1)

        at_first = self.scroller_detail_index in (None, 0)
        at_last = self.scroller_detail_index is None or self.scroller_detail_index >= (len(self.photos) - 1)
        for item in (items["left_arrow_oval"], items["left_arrow_text"]):
            canvas.itemconfigure(item, state="hidden" if at_first else "normal")
        for item in (items["right_arrow_oval"], items["right_arrow_text"]):
            canvas.itemconfigure(item, state="hidden" if at_last else "normal")
        canvas.itemconfigure(items["meta_button_oval"], fill="#000000", outline="#000000", state="normal")
        canvas.itemconfigure(items["meta_button_text"], fill="#ffffff", state="normal")

    def _detail_meta_button_hit(self, item_id: int) -> bool:
        items = self.scroller_detail_canvas_items
        return item_id in {items.get("meta_button_oval"), items.get("meta_button_text")}

    def _detail_arrow_hit(self, item_id: int) -> bool:
        items = self.scroller_detail_canvas_items
        return item_id in {
            items.get("left_arrow_oval"),
            items.get("left_arrow_text"),
            items.get("right_arrow_oval"),
            items.get("right_arrow_text"),
        }

    def on_scroller_detail_press(self, event: tk.Event) -> None:
        self.scroller_detail_drag_start = (event.x, event.y)

    def on_scroller_detail_release(self, event: tk.Event) -> str:
        if self.scroller_detail_image_label is None:
            return "break"
        current_items = self.scroller_detail_image_label.find_withtag("current")
        if current_items:
            current_item = current_items[0]
            if self._detail_meta_button_hit(current_item) or self._detail_arrow_hit(current_item):
                self.scroller_detail_drag_start = None
                return "break"

        start = self.scroller_detail_drag_start
        self.scroller_detail_drag_start = None
        if start is None:
            return "break"

        dx = event.x - start[0]
        if abs(dx) >= 70:
            if dx < 0:
                self.show_next_scroller_detail()
            else:
                self.show_previous_scroller_detail()
            return "break"

        canvas_w = max(1, int(self.scroller_detail_image_label.winfo_width() or self.scroller_detail_image_label.cget("width")))
        left_zone = canvas_w * 0.28
        right_zone = canvas_w * 0.72
        if event.x <= left_zone:
            self.show_previous_scroller_detail()
        elif event.x >= right_zone:
            self.show_next_scroller_detail()
        return "break"

    def on_scroller_detail_prev_click(self) -> str:
        self.show_previous_scroller_detail()
        return "break"

    def on_scroller_detail_next_click(self) -> str:
        self.show_next_scroller_detail()
        return "break"

    def on_scroller_detail_meta_button(self) -> str:
        if self.scroller_detail_index is None:
            return "break"
        self.show_card_metadata(self.photos[self.scroller_detail_index].key)
        return "break"

    def _render_scroller_detail_metadata(self) -> None:
        self.scroller_detail_meta_after_id = None
        if (
            self.scroller_detail_index is None
            or self.scroller_detail_meta_label is None
            or not self.scroller_detail_meta_visible
        ):
            return
        photo = self.photos[self.scroller_detail_index]
        if not (self.show_all_metadata_var.get() or photo.key in self.card_meta_visible_keys):
            return
        self.scroller_detail_meta_label.configure(text=self._format_card_metadata(photo))

    def update_scroller_detail_metadata_visibility(self) -> None:
        if (
            self.scroller_detail_image_label is None
            or self.scroller_detail_meta_overlay is None
            or self.scroller_detail_meta_label is None
            or self.scroller_detail_index is None
        ):
            return

        canvas = self.scroller_detail_image_label
        items = self.scroller_detail_canvas_items
        photo = self.photos[self.scroller_detail_index]
        show = self.show_all_metadata_var.get() or (photo.key in self.card_meta_visible_keys)
        x0, y0, x1, y1 = self.scroller_detail_image_bounds
        overlay_w = max(260, int((x1 - x0) * 0.72))
        overlay_h = max(180, int((y1 - y0) * 0.6))
        font_size = max(12, min(15, int((x1 - x0) / 42)))
        wraplength = max(240, overlay_w - 36)
        self.scroller_detail_meta_label.configure(font=("Helvetica", font_size, "bold"), wraplength=wraplength)

        if self.scroller_detail_meta_after_id is not None:
            try:
                self.root.after_cancel(self.scroller_detail_meta_after_id)
            except Exception:
                pass
            self.scroller_detail_meta_after_id = None

        if show:
            self.scroller_detail_meta_overlay.place(
                x=int((x0 + x1 - overlay_w) / 2),
                y=int((y0 + y1 - overlay_h) / 2),
                width=overlay_w,
                height=overlay_h,
            )
            self.scroller_detail_meta_visible = True
            self.scroller_detail_meta_label.configure(text="Loading metadata...")
            self.scroller_detail_meta_after_id = self.root.after(16, self._render_scroller_detail_metadata)
            canvas.itemconfigure(items["meta_button_text"], text="×")
        else:
            self.scroller_detail_meta_overlay.place_forget()
            self.scroller_detail_meta_visible = False
            self.scroller_detail_meta_label.configure(text="")
            canvas.itemconfigure(items["meta_button_text"], text="i")

    def show_previous_scroller_detail(self) -> None:
        if self.scroller_detail_index is None:
            return
        self.scroller_detail_index = max(0, self.scroller_detail_index - 1)
        self.render_scroller_detail()

    def show_next_scroller_detail(self) -> None:
        if self.scroller_detail_index is None:
            return
        self.scroller_detail_index = min(len(self.photos) - 1, self.scroller_detail_index + 1)
        self.render_scroller_detail()

    def on_scroller_detail_left(self, _event: tk.Event) -> str:
        self.show_previous_scroller_detail()
        return "break"

    def on_scroller_detail_right(self, _event: tk.Event) -> str:
        self.show_next_scroller_detail()
        return "break"

    def on_scroller_detail_escape(self, _event: tk.Event) -> str:
        self.exit_scroller_detail()
        return "break"

    def on_scroller_detail_configure(self, _event: tk.Event) -> None:
        if self.scroller_detail_mode:
            self.root.after_idle(self.render_scroller_detail)

    def _clear_scroller_widgets(self) -> None:
        if self.gallery_inner is None:
            return
        self._cancel_thumbnail_loading()
        self._cancel_gallery_batch_load()
        for card in self.gallery_cards:
            self._cancel_card_metadata_job(card)
        self.gallery_thumbnail_refs.clear()
        self.gallery_cards.clear()
        self.gallery_card_by_key.clear()
        self.gallery_rendered_count = 0
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.update_gallery_scrollregion()

    def _build_scroller_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, padding=(6, 4, 6, 2))
        top.pack(fill=tk.X)

        ttk.Label(
            top,
            text="All photos. Click any photo to open it. Use Left/Right to move, and Esc or Back To Grid to return.",
        ).pack(side=tk.LEFT)
        right_controls = ttk.Frame(top)
        right_controls.pack(side=tk.RIGHT)
        ttk.Checkbutton(
            right_controls,
            text="Show metadata on all cards",
            variable=self.show_all_metadata_var,
            command=self.on_toggle_all_metadata,
        ).pack(side=tk.LEFT)

        self.scroller_container = ttk.Frame(parent, padding=(6, 2, 6, 4))
        self.scroller_container.pack(fill=tk.BOTH, expand=True)

        self.scroller_grid_frame = ttk.Frame(self.scroller_container)
        self.scroller_grid_frame.pack(fill=tk.BOTH, expand=True)

        self.gallery_canvas = tk.Canvas(
            self.scroller_grid_frame,
            highlightthickness=0,
            bd=0,
            bg=self._theme_background(),
        )
        self.gallery_scrollbar = ttk.Scrollbar(
            self.scroller_grid_frame, orient=tk.VERTICAL, command=self.gallery_canvas.yview
        )
        self.gallery_canvas.configure(yscrollcommand=self.gallery_scrollbar.set)

        self.gallery_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.gallery_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self.scroller_grid_frame)
        footer_label = ttk.Label(footer, textvariable=self.gallery_footer_label_var, anchor="center")
        footer_label.pack(side=tk.LEFT, padx=(0, 10))
        footer_progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
        footer_progress.pack(side=tk.LEFT)
        self.gallery_footer = footer
        self.gallery_footer_progress = footer_progress

        loading_overlay = ttk.Frame(self.scroller_grid_frame, padding=(18, 14, 18, 14))
        loading_label = ttk.Label(loading_overlay, text="Loading previews...", anchor="center")
        loading_label.pack()
        loading_progress = ttk.Progressbar(loading_overlay, mode="indeterminate", length=180)
        loading_progress.pack(pady=(10, 0))
        self.gallery_loading_overlay = loading_overlay
        self.gallery_loading_overlay_progress = loading_progress

        self.gallery_inner = tk.Frame(self.gallery_canvas, bg=self._theme_background(), bd=0, highlightthickness=0)
        self.gallery_window_id = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        for c in range(GALLERY_MAX_COLUMNS):
            self.gallery_inner.columnconfigure(c, weight=1 if c < self.gallery_column_count else 0)

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

        detail = ttk.Frame(self.scroller_container)
        nav = ttk.Frame(detail, padding=(4, 4, 4, 8))
        nav.pack(fill=tk.X)
        ttk.Button(nav, text="All Photos", command=self.exit_scroller_detail).pack(side=tk.LEFT)
        ttk.Label(nav, textvariable=self.scroller_detail_title_var, anchor="center").pack(side=tk.LEFT, expand=True)

        image_holder = tk.Frame(detail, bg="#111111", bd=0, highlightthickness=0)
        image_holder.pack(fill=tk.BOTH, expand=True)
        detail_image = tk.Canvas(image_holder, bg="#111111", bd=0, highlightthickness=0)
        detail_image.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        detail_image.bind("<Configure>", self.on_scroller_detail_configure)
        detail_image.bind("<Left>", self.on_scroller_detail_left)
        detail_image.bind("<Right>", self.on_scroller_detail_right)
        detail_image.bind("<Escape>", self.on_scroller_detail_escape)
        detail_image.bind("<ButtonPress-1>", self.on_scroller_detail_press)
        detail_image.bind("<ButtonRelease-1>", self.on_scroller_detail_release)

        detail_canvas_items = {
            "image": detail_image.create_image(0, 0, anchor="center"),
            "placeholder": detail_image.create_rectangle(0, 0, 0, 0, fill="#1d1d1d", outline="#333333", width=1),
            "text": detail_image.create_text(
                0,
                0,
                text="Loading preview...",
                fill="#ffffff",
                font=("Helvetica", 16, "bold"),
                justify="center",
            ),
            "meta_button_oval": detail_image.create_oval(0, 0, 0, 0, fill="#000000", outline="#000000", width=1),
            "meta_button_text": detail_image.create_text(0, 0, text="i", fill="#ffffff", font=("Helvetica", 12, "bold")),
            "left_arrow_oval": detail_image.create_oval(0, 0, 0, 0, fill="#000000", outline="#000000", width=1),
            "left_arrow_text": detail_image.create_text(0, 0, text="‹", fill="#ffffff", font=("Helvetica", 26, "bold")),
            "right_arrow_oval": detail_image.create_oval(0, 0, 0, 0, fill="#000000", outline="#000000", width=1),
            "right_arrow_text": detail_image.create_text(0, 0, text="›", fill="#ffffff", font=("Helvetica", 26, "bold")),
        }
        detail_image.tag_bind(
            detail_canvas_items["meta_button_oval"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_meta_button(),
        )
        detail_image.tag_bind(
            detail_canvas_items["meta_button_text"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_meta_button(),
        )
        detail_image.tag_bind(
            detail_canvas_items["left_arrow_oval"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_prev_click(),
        )
        detail_image.tag_bind(
            detail_canvas_items["left_arrow_text"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_prev_click(),
        )
        detail_image.tag_bind(
            detail_canvas_items["right_arrow_oval"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_next_click(),
        )
        detail_image.tag_bind(
            detail_canvas_items["right_arrow_text"],
            "<Button-1>",
            lambda _e: self.on_scroller_detail_next_click(),
        )

        detail_meta_overlay = tk.Frame(detail_image, bg=META_UI_BG, bd=0, highlightthickness=0)
        detail_meta_label = tk.Label(
            detail_meta_overlay,
            anchor="center",
            justify="center",
            bg=META_UI_BG,
            fg=META_UI_FG,
            font=("Helvetica", 14, "bold"),
        )
        detail_meta_label.pack(fill=tk.BOTH, expand=True, padx=18, pady=18)
        for widget in (detail_meta_overlay, detail_meta_label):
            widget.bind("<Left>", self.on_scroller_detail_left)
            widget.bind("<Right>", self.on_scroller_detail_right)
            widget.bind("<Escape>", self.on_scroller_detail_escape)

        detail_info = tk.Label(
            detail,
            textvariable=self.scroller_detail_info_var,
            bg="#111111",
            fg="#ffffff",
            font=("Helvetica", 11, "bold"),
        )
        detail_info.pack(fill=tk.X, pady=(0, 8))
        detail_info.bind("<Left>", self.on_scroller_detail_left)
        detail_info.bind("<Right>", self.on_scroller_detail_right)
        detail_info.bind("<Escape>", self.on_scroller_detail_escape)

        detail.bind("<Left>", self.on_scroller_detail_left)
        detail.bind("<Right>", self.on_scroller_detail_right)
        detail.bind("<Escape>", self.on_scroller_detail_escape)
        self.scroller_detail_frame = detail
        self.scroller_detail_image_label = detail_image
        self.scroller_detail_canvas_items = detail_canvas_items
        self.scroller_detail_meta_overlay = detail_meta_overlay
        self.scroller_detail_meta_label = detail_meta_label

    def _theme_background(self) -> str:
        style = ttk.Style()
        for style_name in ("TFrame", "TNotebook", "TLabel"):
            bg = style.lookup(style_name, "background")
            if bg:
                return bg
        return CARD_BG_DEFAULT

    def _configure_focus_management(self) -> None:
        self.root.bind_all("<Escape>", self.on_escape_unfocus, add="+")
        self.root.bind_all("<Button-1>", self.on_global_pointer_unfocus, add="+")

    def _configure_keyboard_shortcuts(self) -> None:
        bindings = [
            ("<Command-KeyPress-l>", self.on_shortcut_load_data),
            ("<Command-KeyPress-o>", self.on_shortcut_browse),
            ("<Command-KeyPress-L>", self.on_shortcut_load_data),
            ("<Command-KeyPress-O>", self.on_shortcut_browse),
            ("<Command-Shift-KeyPress-bracketleft>", self.on_shortcut_prev_tab),
            ("<Command-KeyPress-braceleft>", self.on_shortcut_prev_tab),
            ("<Command-Shift-KeyPress-bracketright>", self.on_shortcut_next_tab),
            ("<Command-KeyPress-braceright>", self.on_shortcut_next_tab),
            ("<Command-KeyPress-1>", self.on_shortcut_mode_front_only),
            ("<Command-KeyPress-2>", self.on_shortcut_mode_back_only),
            ("<Command-KeyPress-3>", self.on_shortcut_mode_bereal_front),
            ("<Command-KeyPress-4>", self.on_shortcut_mode_bereal_back),
            ("<Command-KeyPress-s>", self.on_shortcut_toggle_skip),
            ("<Command-KeyPress-S>", self.on_shortcut_toggle_skip),
            ("<Command-KeyPress-d>", self.on_shortcut_download_selected),
            ("<Command-KeyPress-D>", self.on_shortcut_download_all),
            ("<Command-KeyPress-i>", self.on_shortcut_toggle_metadata),
            ("<Command-KeyPress-I>", self.on_shortcut_toggle_metadata),
        ]
        for sequence, handler in bindings:
            self.root.bind_all(sequence, handler, add="+")

    def _is_text_input_widget(self, widget: tk.Widget) -> bool:
        return widget.winfo_class() in {"Entry", "TEntry", "Spinbox", "TSpinbox", "Text"}

    def _focus_primary_surface(self) -> None:
        if self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.focus_set()
            return
        if self.scroller_active and self.scroller_detail_mode and self.scroller_detail_image_label is not None:
            self.scroller_detail_image_label.focus_set()
            return
        if self.scroller_active and self.gallery_canvas is not None:
            self.gallery_canvas.focus_set()
            return
        if self.table is not None:
            self.table.focus_set()
            return
        self.root.focus_set()

    def on_escape_unfocus(self, _event: tk.Event) -> str:
        self._focus_primary_surface()
        return "break"

    def on_global_pointer_unfocus(self, event: tk.Event) -> None:
        widget = event.widget
        if widget is None or self._is_text_input_widget(widget):
            return
        self.root.after_idle(self._focus_primary_surface)

    def _is_scroller_shortcut_available(self) -> bool:
        return self._is_scroller_tab_active()

    def _select_notebook_tab(self, target_tab: Optional[ttk.Frame]) -> None:
        if self.notebook is None or target_tab is None:
            return
        self.notebook.select(target_tab)
        self.on_notebook_tab_changed(None)
        self._focus_primary_surface()

    def on_shortcut_load_data(self, _event: tk.Event) -> str:
        self.on_load_data()
        return "break"

    def on_shortcut_browse(self, _event: tk.Event) -> str:
        self.on_browse()
        return "break"

    def on_shortcut_prev_tab(self, _event: tk.Event) -> str:
        if self._is_scroller_tab_active():
            self._select_notebook_tab(self.table_tab)
        else:
            self._select_notebook_tab(self.scroller_tab)
        return "break"

    def on_shortcut_next_tab(self, _event: tk.Event) -> str:
        if self._is_scroller_tab_active():
            self._select_notebook_tab(self.table_tab)
        else:
            self._select_notebook_tab(self.scroller_tab)
        return "break"

    def _set_mode_shortcut(self, mode: str) -> None:
        self.mode_var.set(mode)
        self.on_export_mode_changed()

    def on_shortcut_mode_front_only(self, _event: tk.Event) -> str:
        self._set_mode_shortcut(MODE_FRONT_ONLY)
        return "break"

    def on_shortcut_mode_back_only(self, _event: tk.Event) -> str:
        self._set_mode_shortcut(MODE_BACK_ONLY)
        return "break"

    def on_shortcut_mode_bereal_front(self, _event: tk.Event) -> str:
        self._set_mode_shortcut(MODE_BEREAL_FRONT_TL)
        return "break"

    def on_shortcut_mode_bereal_back(self, _event: tk.Event) -> str:
        self._set_mode_shortcut(MODE_BEREAL_BACK_TL)
        return "break"

    def on_shortcut_toggle_skip(self, _event: tk.Event) -> str:
        self.skip_existing_var.set(not self.skip_existing_var.get())
        return "break"

    def on_shortcut_download_selected(self, _event: tk.Event) -> str:
        self.on_download_selected()
        return "break"

    def on_shortcut_download_all(self, _event: tk.Event) -> str:
        self.on_download_all()
        return "break"

    def on_shortcut_toggle_metadata(self, _event: tk.Event) -> str:
        if not self._is_scroller_shortcut_available():
            return "break"
        self.show_all_metadata_var.set(not self.show_all_metadata_var.get())
        self.on_toggle_all_metadata()
        return "break"

    def _configure_row_tags(self) -> None:
        self.table.tag_configure("missing", background="#ffe9e9")

    def on_export_mode_changed(self) -> None:
        self.refresh_table()
        self.last_target_preview_width = 0
        self.gallery_thumbnail_refs.clear()
        if self.scroller_active and self.scroller_detail_mode:
            self.scroller_needs_refresh = True
            self.render_scroller_detail()
            return
        self.request_scroller_refresh()

    def refresh_scroller(self) -> None:
        if self.gallery_inner is None:
            return

        self._show_scroller_grid()
        self._clear_scroller_widgets()
        self._ensure_gallery_cards_rendered(min(len(self.photos), GALLERY_INITIAL_BATCH))
        self._apply_gallery_column_layout(force=True)
        self._update_gallery_initial_loading_visibility()

        self.last_target_preview_width = self._current_target_preview_width()
        self.update_gallery_scrollregion()
        if self.scroller_active:
            self._schedule_thumbnail_request(1)
            self._schedule_gallery_batch_load()
        self.refresh_gallery_selection_styles()
        self.update_selection_status()
        self.scroller_needs_refresh = False

    def _create_gallery_card(self, idx: int, photo: MemoryPhoto) -> Dict:
        assert self.gallery_inner is not None
        initial_width, initial_height = self._thumbnail_canvas_size()

        frame = tk.Frame(
            self.gallery_inner,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0,
            padx=0,
            pady=0,
            bg=self._theme_background(),
        )

        image_canvas = tk.Canvas(
            frame,
            bd=0,
            highlightthickness=0,
            bg=self._theme_background(),
            width=initial_width,
            height=initial_height,
        )
        image_canvas.pack(anchor="center")
        canvas_image_item = image_canvas.create_image(0, 0, anchor="nw")
        placeholder_rect = image_canvas.create_rectangle(
            0,
            0,
            0,
            0,
            fill="#d7dbe0",
            outline="#c6ccd4",
            width=1,
            stipple="gray50",
            state="hidden",
        )
        selection_overlay = image_canvas.create_rectangle(
            0,
            0,
            0,
            0,
            fill="",
            outline="#9cc7f8",
            width=3,
            state="hidden",
        )
        canvas_text_item = image_canvas.create_text(
            180,
            260,
            text="",
            fill=PREVIEW_TEXT_FG,
            font=("Helvetica", 14, "bold"),
            width=300,
            justify="center",
        )
        meta_button_oval = image_canvas.create_oval(0, 0, 0, 0, fill="#000000", outline="#000000", width=1)
        meta_button_text = image_canvas.create_text(
            0,
            0,
            text="i",
            fill="#ffffff",
            font=("Helvetica", 11, "bold"),
        )
        image_canvas.tag_bind(
            meta_button_oval,
            "<Button-1>",
            lambda _e, k=photo.key: self._on_meta_button_click(k),
        )
        image_canvas.tag_bind(
            meta_button_text,
            "<Button-1>",
            lambda _e, k=photo.key: self._on_meta_button_click(k),
        )

        meta_overlay = tk.Frame(image_canvas, bg=META_UI_BG, bd=0, highlightthickness=0)
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
            "image_canvas": image_canvas,
            "canvas_image_item": canvas_image_item,
            "placeholder_rect": placeholder_rect,
            "selection_overlay": selection_overlay,
            "canvas_text_item": canvas_text_item,
            "meta_button_oval": meta_button_oval,
            "meta_button_text": meta_button_text,
            "meta_overlay": meta_overlay,
            "meta_label": meta_label,
            "meta_visible": False,
            "meta_after_id": None,
            "has_thumbnail": False,
        }

        for widget in (frame, image_canvas, meta_overlay, meta_label):
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
        self._set_card_canvas_image(card, None, "Loading preview...")
        card["meta_label"].configure(text="", fg=META_UI_FG, bg=META_UI_BG)

    def _set_meta_button_symbol(self, card: Dict, symbol: str) -> None:
        card["image_canvas"].itemconfigure(card["meta_button_text"], text=symbol)

    def _on_meta_button_click(self, photo_key: str) -> str:
        self.show_card_metadata(photo_key)
        return "break"

    def _set_card_canvas_image(
        self,
        card: Dict,
        image_obj: Optional["ImageTk.PhotoImage"],
        text: str = "",
    ) -> None:
        canvas = card["image_canvas"]
        width, height = self._thumbnail_canvas_size()
        image_x = 0
        image_y = 0
        if image_obj is not None:
            image_x = int((width - image_obj.width()) / 2)
            image_y = int((height - image_obj.height()) / 2)

        canvas.configure(width=width, height=height)
        canvas.coords(card["canvas_image_item"], image_x, image_y)
        canvas.coords(card["placeholder_rect"], 0, 0, width, height)
        canvas.coords(card["selection_overlay"], 1, 1, max(1, width - 1), max(1, height - 1))
        canvas.coords(card["canvas_text_item"], width / 2, height / 2)
        canvas.itemconfigure(card["canvas_text_item"], width=max(72, width - 16))

        if image_obj is not None:
            canvas.itemconfigure(card["canvas_image_item"], image=image_obj, state="normal")
            canvas.itemconfigure(card["placeholder_rect"], state="hidden")
            canvas.itemconfigure(card["canvas_text_item"], text="", state="hidden")
        else:
            canvas.itemconfigure(card["canvas_image_item"], image="", state="hidden")
            canvas.itemconfigure(card["placeholder_rect"], state="normal")
            canvas.itemconfigure(card["canvas_text_item"], text=text, fill=PREVIEW_TEXT_FG, state="normal")

        canvas.image = image_obj
        card["has_thumbnail"] = image_obj is not None
        canvas.tag_raise(card["placeholder_rect"])
        canvas.tag_raise(card["canvas_image_item"])
        canvas.tag_raise(card["selection_overlay"])
        canvas.tag_raise(card["canvas_text_item"])
        canvas.tag_raise(card["meta_button_oval"])
        canvas.tag_raise(card["meta_button_text"])
        self._position_meta_button(card)

    def _position_meta_button(self, card: Dict) -> None:
        canvas = card["image_canvas"]
        width = int(canvas.winfo_width())
        if width <= 1:
            width = int(canvas.cget("width"))
        width = max(1, width)
        cx = width - 18
        cy = 18
        radius = 11
        canvas.coords(card["meta_button_oval"], cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.coords(card["meta_button_text"], cx, cy)

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
        layout = self._grid_metadata_layout(card)
        card["meta_label"].configure(text=self._format_thumbnail_metadata(photo, layout))

    def _format_thumbnail_metadata(self, photo: MemoryPhoto, layout: Optional[Dict[str, float]] = None) -> str:
        lines = [self._format_time_thumbnail(photo.taken_time)]
        if photo.caption:
            caption = photo.caption.strip()
            if layout is not None:
                font_size = max(8.0, float(layout["font_size"]))
                approx_chars_per_line = max(12, int(float(layout["wraplength"]) / max(6.0, font_size * 0.62)))
                max_chars = max(18, min(56, int(approx_chars_per_line)))
                caption = self._wrap_thumbnail_caption(caption, max_chars, max_lines=2)
            elif len(caption) > 84:
                caption = caption[:81].rstrip() + "..."
            lines.append(caption)
        return "\n".join(lines)

    @staticmethod
    def _wrap_thumbnail_caption(caption: str, max_chars_per_line: int, max_lines: int = 2) -> str:
        normalized = " ".join(caption.split())
        if not normalized:
            return ""

        wrapped = textwrap.wrap(
            normalized,
            width=max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if not wrapped:
            return textwrap.shorten(normalized, width=max_chars_per_line, placeholder="...")
        if len(wrapped) <= max_lines:
            return "\n".join(wrapped)

        visible = wrapped[: max_lines - 1]
        tail_source = " ".join(wrapped[max_lines - 1 :])
        tail_line = textwrap.shorten(tail_source, width=max_chars_per_line, placeholder="...")
        visible.append(tail_line)
        return "\n".join(visible)

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
        columns = max(1, self.gallery_column_count)
        row = idx // columns
        col = idx % columns
        card["frame"].grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

    def on_toggle_all_metadata(self) -> None:
        self.card_meta_visible_keys.clear()
        if self.scroller_detail_mode:
            self.update_scroller_detail_metadata_visibility()
            self.root.after_idle(self._refresh_all_gallery_metadata_visibility)
        else:
            self._refresh_all_gallery_metadata_visibility()
            self.update_scroller_detail_metadata_visibility()
        self.update_gallery_scrollregion()

    def _refresh_all_gallery_metadata_visibility(self) -> None:
        for card in self.gallery_cards:
            self.update_card_metadata_visibility(card)

    def _grid_metadata_layout(self, card: Dict) -> Dict[str, float]:
        canvas = card["image_canvas"]
        width = max(1, int(canvas.winfo_width() or canvas.cget("width")))
        font_size = max(8, min(11, int(width / 17)))
        wraplength = max(92, width - 18)
        relwidth = 0.9 if width < 120 else 0.86
        relheight = 0.68 if width < 120 else 0.62
        rely = 0.66 if width < 120 else 0.64
        return {
            "font_size": font_size,
            "wraplength": wraplength,
            "relwidth": relwidth,
            "relheight": relheight,
            "rely": rely,
            "padding": max(4, min(8, int(width / 22))),
        }

    def update_card_metadata_visibility(self, card: Dict) -> None:
        photo: MemoryPhoto = card["photo"]
        show = self.show_all_metadata_var.get() or (photo.key in self.card_meta_visible_keys)
        layout = self._grid_metadata_layout(card)
        card["meta_label"].configure(
            wraplength=int(layout["wraplength"]),
            font=("Helvetica", int(layout["font_size"]), "bold"),
            padx=int(layout["padding"]),
            pady=int(layout["padding"]),
        )

        if show:
            if not card["meta_visible"]:
                card["meta_overlay"].place(
                    relx=0.5,
                    rely=layout["rely"],
                    anchor="center",
                    relwidth=layout["relwidth"],
                    relheight=layout["relheight"],
                )
                card["meta_visible"] = True
            else:
                card["meta_overlay"].place_configure(
                    rely=layout["rely"],
                    relwidth=layout["relwidth"],
                    relheight=layout["relheight"],
                )
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
        if (
            self.scroller_detail_mode
            and self.scroller_detail_index is not None
            and 0 <= self.scroller_detail_index < len(self.photos)
            and self.photos[self.scroller_detail_index].key == photo_key
        ):
            self.update_scroller_detail_metadata_visibility()
        self.update_gallery_scrollregion()

    def update_gallery_scrollregion(self) -> None:
        if self.gallery_canvas is None:
            return
        self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all"))

    def _gallery_column_target(self) -> int:
        if self.gallery_canvas is None:
            return self.gallery_column_count
        canvas_w = int(self.gallery_canvas.winfo_width() or 0)
        if canvas_w <= 1:
            return self.gallery_column_count
        if canvas_w >= GALLERY_SIX_COLUMN_BREAKPOINT:
            return GALLERY_MAX_COLUMNS
        if canvas_w >= GALLERY_FIVE_COLUMN_BREAKPOINT:
            return GALLERY_MEDIUM_COLUMNS
        return GALLERY_MIN_COLUMNS

    def _apply_gallery_column_layout(self, force: bool = False) -> None:
        if self.gallery_inner is None:
            return
        new_count = self._gallery_column_target()
        if not force and new_count == self.gallery_column_count:
            return
        self.gallery_column_count = new_count
        for c in range(GALLERY_MAX_COLUMNS):
            self.gallery_inner.columnconfigure(c, weight=1 if c < self.gallery_column_count else 0)
        for card in self.gallery_cards:
            self._place_card(card)
        self.update_gallery_scrollregion()

    def _current_target_preview_width(self) -> int:
        if self.gallery_canvas is None:
            return 128
        canvas_w = self.gallery_canvas.winfo_width()
        if canvas_w <= 1:
            return 128
        columns = max(1, self.gallery_column_count)
        gutter = 12 + ((columns - 1) * 12)
        thumb_w = int((canvas_w - gutter) / columns)
        return max(GRID_THUMB_MIN_WIDTH, min(GRID_THUMB_MAX_WIDTH, thumb_w))

    def _thumbnail_canvas_height(self, width: Optional[int] = None) -> int:
        if width is None:
            width = self._current_target_preview_width()
        return max(132, int(width * GRID_THUMB_HEIGHT_RATIO))

    def _thumbnail_canvas_size(self) -> Tuple[int, int]:
        width = self._current_target_preview_width()
        return width, self._thumbnail_canvas_height(width)

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
            self._set_card_canvas_image(card, None, "Loading preview...")
        self._update_gallery_initial_loading_visibility()

    def on_gallery_inner_configure(self, _event: tk.Event) -> None:
        self.update_gallery_scrollregion()
        self._update_gallery_initial_loading_visibility()

    def on_gallery_canvas_configure(self, event: tk.Event) -> None:
        if self.gallery_window_id is not None and self.gallery_canvas is not None:
            self.gallery_canvas.itemconfigure(self.gallery_window_id, width=event.width)
        self._apply_gallery_column_layout()
        self.update_gallery_scrollregion()
        self._handle_preview_width_change()
        self._schedule_thumbnail_request()
        self._schedule_gallery_batch_load()
        self._update_gallery_initial_loading_visibility()

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
            self._schedule_thumbnail_request(60)
            self._schedule_gallery_batch_load()
            self._update_gallery_initial_loading_visibility()

    def on_gallery_item_click(self, idx: int, event: tk.Event, action: str = "single") -> None:
        if idx < 0 or idx >= len(self.photos):
            return

        card = self.gallery_cards[idx]
        if event.widget is card["image_canvas"]:
            current_items = event.widget.find_withtag("current")
            if current_items and current_items[0] in {card["meta_button_oval"], card["meta_button_text"]}:
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
        if action == "single" and not shift_down:
            self.open_scroller_detail(idx)

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
        if self.gallery_canvas is None or idx < 0 or idx >= len(self.photos):
            return
        self._ensure_gallery_cards_rendered(idx + 1)
        if idx >= len(self.gallery_cards):
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
        self._update_gallery_initial_loading_visibility()

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

        bg = self._theme_background()
        card["frame"].configure(
            bg=bg,
            highlightbackground=CARD_HIGHLIGHT_MISSING if missing else bg,
            highlightcolor=CARD_HIGHLIGHT_MISSING if missing else bg,
            highlightthickness=2 if missing else 0,
        )
        card["image_canvas"].configure(bg=bg)
        card["meta_overlay"].configure(bg=META_UI_BG)
        card["meta_label"].configure(bg=META_UI_BG, fg=META_UI_FG)
        card["image_canvas"].itemconfigure(
            card["selection_overlay"],
            fill="",
            outline="#9cc7f8",
            width=3,
            state="normal" if selected else "hidden",
        )
        card["image_canvas"].itemconfigure(card["canvas_text_item"], fill=PREVIEW_TEXT_FG)
        card["image_canvas"].itemconfigure(card["meta_button_oval"], fill="#000000", outline="#000000")
        card["image_canvas"].itemconfigure(card["meta_button_text"], fill="#ffffff")

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
        if self.scroller_detail_mode:
            self._hide_gallery_initial_loading()
            return
        if not self.scroller_active or not self.gallery_cards or self.gallery_canvas is None:
            self._hide_gallery_initial_loading()
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
                self._set_card_canvas_image(card, self.gallery_thumbnail_refs[key])
                continue
            self.thumbnail_job_queue.append(idx)
            self.thumbnail_job_set.add(idx)

        if self.thumbnail_job_after_id is None and self.thumbnail_job_queue:
            self.thumbnail_job_after_id = self.root.after(1, self._process_thumbnail_batch)
        self._update_gallery_initial_loading_visibility()
        self._schedule_gallery_batch_load()

    def _visible_card_indices(self) -> List[int]:
        if self.gallery_canvas is None:
            return []
        y0, y1 = self.gallery_canvas.yview()
        total = max(1, len(self.gallery_cards))
        columns = max(1, self.gallery_column_count)
        rows = (total + columns - 1) // columns
        first_row = max(0, int(y0 * rows) - 2)
        last_row = min(rows - 1, int(y1 * rows) + 2)
        indices: List[int] = []
        for row in range(first_row, last_row + 1):
            start = row * columns
            end = min(total, start + columns)
            indices.extend(range(start, end))
        return indices

    def _process_thumbnail_batch(self) -> None:
        if not self.thumbnail_job_queue:
            self.thumbnail_job_after_id = None
            self._update_gallery_initial_loading_visibility()
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
                self._set_card_canvas_image(card, image_obj)
            else:
                self._set_card_canvas_image(card, None, "Preview unavailable")

        if self.thumbnail_job_queue:
            self.thumbnail_job_after_id = self.root.after(10, self._process_thumbnail_batch)
        else:
            self.thumbnail_job_after_id = None
        self._update_gallery_initial_loading_visibility()

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
        self._update_gallery_initial_loading_visibility()

    def _build_thumbnail(self, photo: MemoryPhoto, mode: str) -> Optional["ImageTk.PhotoImage"]:
        try:
            target_w = self._current_target_preview_width()
            target_h = self._thumbnail_canvas_height(target_w)
            source_side = min(720, max(320, int(max(target_w, target_h) * 2.1)))
            img = self._render_preview_image(
                photo,
                mode,
                target_w,
                target_h,
                source_max_side=source_side,
                resample=Image.Resampling.BILINEAR,
            )
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
        chosen = {"value": default or buttons[0]}

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        self._set_window_icon(win)

        body = ttk.Frame(win, padding=20)
        body.pack(fill=tk.BOTH, expand=True)

        title_label = ttk.Label(
            body,
            text=title,
            font=("TkDefaultFont", 14, "bold"),
            anchor="w",
            justify=tk.LEFT,
        )
        title_label.pack(fill=tk.X, pady=(0, 6))

        message_label = ttk.Label(
            body,
            text=message,
            font=("TkDefaultFont", 12),
            wraplength=500,
            justify=tk.LEFT,
            anchor="w",
        )
        message_label.pack(fill=tk.X, pady=(12, 6))

        if detail:
            detail_label = ttk.Label(
                body,
                text=detail,
                font=("TkDefaultFont", 12),
                wraplength=500,
                justify=tk.LEFT,
                anchor="w",
            )
            detail_label.pack(fill=tk.X, pady=(2, 10))

        button_row = ttk.Frame(body)
        button_row.pack(fill=tk.X, pady=(8, 0))

        def close_with(value: str) -> None:
            chosen["value"] = value
            win.destroy()

        for label in buttons:
            btn = self._create_modal_button(
                button_row,
                text=label,
                command=lambda value=label: close_with(value),
                default=(label == (default or buttons[0])),
            )
            btn.pack(side=tk.RIGHT, padx=(8, 0))

        win.protocol("WM_DELETE_WINDOW", lambda: close_with(default or buttons[0]))
        win.bind("<Return>", lambda _event: close_with(default or buttons[0]))
        if len(buttons) > 1:
            win.bind("<Escape>", lambda _event: close_with(buttons[-1]))

        self._center_window_over_root(win)
        win.deiconify()
        win.grab_set()
        self._raise_preview_window(win)
        win.focus_force()
        self.root.wait_window(win)
        return str(chosen["value"])

    def _create_modal_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        default: bool = False,
    ) -> ttk.Button:
        button = ttk.Button(
            parent,
            text=text,
            command=command,
        )
        if default:
            try:
                button.state(["focus"])
            except Exception:
                pass

        def set_active(active: bool) -> None:
            try:
                if active:
                    button.state(["active"])
                else:
                    button.state(["!active"])
            except Exception:
                pass

        button.bind("<Enter>", lambda _e: set_active(True))
        button.bind("<Leave>", lambda _e: set_active(False))
        return button

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
        self.download_cancel_event.clear()
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("Downloading BeReals")
        win.transient(self.root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._request_cancel_download)
        self._set_window_icon(win)

        body = ttk.Frame(win, padding=20)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text="Downloading BeReals",
            font=("TkDefaultFont", 14, "bold"),
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(0, 6))

        self.download_progress_title_var.set(f"Preparing {total} export(s) in {mode_label}")
        ttk.Label(
            body,
            textvariable=self.download_progress_title_var,
            font=("TkDefaultFont", 12),
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(12, 8))

        progress = ttk.Progressbar(body, mode="determinate", maximum=max(1, total), length=440)
        progress.pack(fill=tk.X)
        self.download_progress_bar = progress

        self.download_progress_detail_var.set("Starting...")
        ttk.Label(
            body,
            textvariable=self.download_progress_detail_var,
            font=("TkDefaultFont", 12),
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(10, 6))

        self.download_progress_counts_var.set("Success: 0    Skipped: 0    Failed: 0")
        ttk.Label(
            body,
            textvariable=self.download_progress_counts_var,
            font=("TkDefaultFont", 12),
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X)

        button_row = ttk.Frame(body)
        button_row.pack(fill=tk.X, pady=(14, 0))
        cancel_button = ttk.Button(
            button_row,
            textvariable=self.download_progress_cancel_var,
            command=self._request_cancel_download,
        )
        cancel_button.pack(side=tk.RIGHT)
        self.download_cancel_button = cancel_button

        self._center_window_over_root(win)
        win.deiconify()
        win.grab_set()
        self._raise_preview_window(win)
        self.download_progress_window = win

    def _request_cancel_download(self) -> None:
        if self.download_state is None or self.download_cancel_event.is_set():
            return
        self.download_cancel_event.set()
        self.download_progress_detail_var.set("Cancel requested. Finishing the current file...")
        self.download_progress_cancel_var.set("Canceling...")
        if self.download_cancel_button is not None:
            self.download_cancel_button.state(["disabled"])

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
        self.download_cancel_button = None
        self.download_progress_cancel_var.set("Cancel Download")

    def _render_preview_image(
        self,
        photo: MemoryPhoto,
        mode: str,
        max_w: int,
        max_h: int,
        source_max_side: Optional[int] = None,
        resample: Optional[int] = None,
    ) -> "Image.Image":
        if source_max_side is None:
            source_max_side = min(2200, max(900, int(max(max_w, max_h) * 1.25)))
        if resample is None:
            resample = Image.Resampling.LANCZOS
        downloaded_output: Optional[Path] = None
        if mode in {MODE_FRONT_ONLY, MODE_BACK_ONLY}:
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
                inset = self._open_preview_image(photo.front_path, max(240, int(source_max_side * 0.52)))
                img = ImageExporter._compose(base=base, inset=inset)
            elif mode == MODE_BEREAL_BACK_TL:
                if not photo.front_path.exists() or not photo.back_path.exists():
                    raise FileNotFoundError("Front or back image file is missing.")
                base = self._open_preview_image(photo.front_path, source_max_side)
                inset = self._open_preview_image(photo.back_path, max(240, int(source_max_side * 0.52)))
                reference = self._open_preview_image(photo.back_path, source_max_side)
                img = ImageExporter._compose(base=base, inset=inset, canvas_reference=reference)
            else:
                if not photo.front_path.exists():
                    raise FileNotFoundError("Front image file is missing.")
                img = self._open_preview_image(photo.front_path, source_max_side)

        if img.width > max_w or img.height > max_h:
            scale = min(max_w / img.width, max_h / img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), resample)
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
                "Confirm Overwrite",
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
            if self.download_cancel_event.is_set():
                event_queue.put({"type": "canceled", "index": i - 1, "total": total})
                break

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
            if event_type == "canceled":
                self._finish_download_run(canceled=True)
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

    def _finish_download_run(self, canceled: bool = False) -> None:
        if self.download_poll_after_id is not None:
            try:
                self.root.after_cancel(self.download_poll_after_id)
            except Exception:
                pass
        self.download_poll_after_id = None

        state = self.download_state or {}
        self.download_queue = None
        self.download_state = None
        self.download_cancel_event.clear()

        self.history.save()
        self.refresh_table()
        self.request_scroller_refresh()
        self._close_download_progress()

        summary = (
            f"Done. Success: {int(state.get('succeeded', 0))}, "
            f"Skipped: {int(state.get('skipped', 0))}, "
            f"Failed: {int(state.get('failed', 0))}"
        )
        if canceled:
            summary = "Canceled. " + summary
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
    def _format_time_thumbnail(value: str) -> str:
        if not value:
            return "Unknown capture time"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
            hour = dt.strftime("%I").lstrip("0") or "0"
            return f"{dt.strftime('%b %d, %Y')} {hour}:{dt.strftime('%M %p')}"
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
