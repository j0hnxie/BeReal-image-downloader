#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
except Exception:  # pragma: no cover - runtime environment dependent
    Image = None
    ImageOps = None
    ImageTk = None

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

BROWSER_FILENAME = "filename"
BROWSER_PREVIEW = "preview"


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
        tmp_path = self.history_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp_path.replace(self.history_path)

    def has_mode(self, photo_key: str, mode: str) -> bool:
        return mode in self._data.get("entries", {}).get(photo_key, {})

    def downloaded_modes(self, photo_key: str) -> List[str]:
        return sorted(self._data.get("entries", {}).get(photo_key, {}).keys())

    def mark_download(self, photo_key: str, mode: str, output_path: Path, sidecar_path: Path) -> None:
        entries = self._data.setdefault("entries", {})
        record = entries.setdefault(photo_key, {})
        record[mode] = {
            "downloadedAt": datetime.now(timezone.utc).isoformat(),
            "outputPath": str(output_path),
            "metadataPath": str(sidecar_path),
        }


class ImageExporter:
    def __init__(self, downloads_root: Optional[Path] = None) -> None:
        self.downloads_root = downloads_root or (Path.home() / "Downloads" / "BeReal-Exports")

    def export_photo(self, photo: MemoryPhoto, mode: str) -> Tuple[Path, Path]:
        if Image is None or ImageOps is None:
            raise RuntimeError("Pillow is not installed. Run: pip install -r requirements.txt")

        if not photo.front_path.exists():
            raise FileNotFoundError(f"Front image not found: {photo.front_path}")
        if not photo.back_path.exists():
            raise FileNotFoundError(f"Back image not found: {photo.back_path}")

        output_img = self.render_output_image(photo, mode)

        output_path = self._build_output_path(photo, mode)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path = output_path.with_suffix(".json")

        exif = Image.Exif()
        exif_dt = self._to_exif_datetime(photo.taken_time)
        if exif_dt:
            # DateTime, DateTimeOriginal, DateTimeDigitized
            exif[306] = exif_dt
            exif[36867] = exif_dt
            exif[36868] = exif_dt

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
        taken_dt = self._parse_iso(photo.taken_time) or datetime.now(timezone.utc)
        local_dt = taken_dt.astimezone()
        year_dir = local_dt.strftime("%Y")
        day_dir = local_dt.strftime("%Y-%m-%d")
        filename = self.planned_filename(photo, mode)
        return Path(year_dir) / day_dir / filename

    def planned_filename(self, photo: MemoryPhoto, mode: str) -> str:
        taken_dt = self._parse_iso(photo.taken_time) or datetime.now(timezone.utc)
        local_dt = taken_dt.astimezone()
        stamp = local_dt.strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{mode}.jpg"

    @staticmethod
    def _load_image(path: Path) -> "Image.Image":
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")

    @staticmethod
    def _compose(base: "Image.Image", inset: "Image.Image") -> "Image.Image":
        composed = base.copy()

        inset_target_w = max(140, int(composed.width * 0.28))
        inset_copy = inset.copy()
        inset_copy.thumbnail((inset_target_w, inset_target_w), Image.Resampling.LANCZOS)

        border = max(2, int(composed.width * 0.008))
        framed = Image.new(
            "RGB",
            (inset_copy.width + border * 2, inset_copy.height + border * 2),
            (255, 255, 255),
        )
        framed.paste(inset_copy, (border, border))

        margin = max(8, int(composed.width * 0.03))
        composed.paste(framed, (margin, margin))
        return composed

    def _build_output_path(self, photo: MemoryPhoto, mode: str) -> Path:
        relative = self.planned_relative_path(photo, mode)
        base_path = self.downloads_root / relative
        if not base_path.exists():
            return base_path

        stem = base_path.stem
        suffix = 2
        while True:
            candidate = base_path.with_name(f"{stem}_{suffix}.jpg")
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
        self.export_dir: Optional[Path] = None

        self.path_var = tk.StringVar(value=str(Path.cwd()))
        self.mode_var = tk.StringVar(value=MODE_BEREAL_FRONT_TL)
        self.browser_mode_var = tk.StringVar(value=BROWSER_FILENAME)
        self.preview_index_var = tk.IntVar(value=1)
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Select an export folder and click Load Data.")

        self.filename_listbox: Optional[tk.Listbox] = None
        self.preview_frame: Optional[ttk.Frame] = None
        self.filename_frame: Optional[ttk.Frame] = None
        self.preview_image_label: Optional[ttk.Label] = None
        self.preview_meta_var = tk.StringVar(value="")
        self.preview_filename_var = tk.StringVar(value="")
        self.preview_index_label_var = tk.StringVar(value="0 / 0")
        self.preview_scale: Optional[ttk.Scale] = None
        self.preview_photo_image = None
        self.preview_updating = False

        self._build_ui()
        self._configure_row_tags()

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

        ttk.Button(action_frame, text="Download Selected", command=self.on_download_selected).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(action_frame, text="Download All", command=self.on_download_all).pack(side=tk.RIGHT)
        ttk.Button(action_frame, text="Open Output Folder", command=self.on_open_output).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)

        table_tab = ttk.Frame(notebook)
        notebook.add(table_tab, text="Selection Table")

        scroller_tab = ttk.Frame(notebook)
        notebook.add(scroller_tab, text="Scroller")

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

        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        self._build_scroller_tab(scroller_tab)

        status = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        status.pack(fill=tk.X, pady=(8, 0))

    def _build_scroller_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, padding=(8, 8, 8, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Browse style:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            top,
            text="Filename list",
            value=BROWSER_FILENAME,
            variable=self.browser_mode_var,
            command=self.on_browser_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            top,
            text="Image preview",
            value=BROWSER_PREVIEW,
            variable=self.browser_mode_var,
            command=self.on_browser_mode_changed,
        ).pack(side=tk.LEFT)

        container = ttk.Frame(parent, padding=(8, 4, 8, 8))
        container.pack(fill=tk.BOTH, expand=True)

        self.filename_frame = ttk.Frame(container)
        self.preview_frame = ttk.Frame(container)

        self._build_filename_browser(self.filename_frame)
        self._build_preview_browser(self.preview_frame)

        self.on_browser_mode_changed()

    def _build_filename_browser(self, parent: ttk.Frame) -> None:
        parent.pack(fill=tk.BOTH, expand=True)
        list_frame = ttk.Frame(parent)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.filename_listbox = tk.Listbox(
            list_frame,
            activestyle="none",
            selectmode=tk.SINGLE,
            font=("Menlo", 11) if sys.platform == "darwin" else ("Courier", 10),
        )
        y_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.filename_listbox.yview)
        self.filename_listbox.configure(yscrollcommand=y_scroll.set)

        self.filename_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_preview_browser(self, parent: ttk.Frame) -> None:
        parent.pack(fill=tk.BOTH, expand=True)

        nav = ttk.Frame(parent)
        nav.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(nav, text="Previous", command=self.on_preview_previous).pack(side=tk.LEFT)
        ttk.Button(nav, text="Next", command=self.on_preview_next).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(nav, textvariable=self.preview_index_label_var).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(nav, text="Scroll bar:").pack(side=tk.LEFT, padx=(0, 8))

        self.preview_scale = ttk.Scale(nav, from_=1, to=1, command=self.on_preview_scale_changed)
        self.preview_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

        canvas_holder = ttk.Frame(parent)
        canvas_holder.pack(fill=tk.BOTH, expand=True)

        self.preview_image_label = ttk.Label(canvas_holder, anchor="center")
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        self.preview_image_label.bind("<MouseWheel>", self.on_preview_mouse_wheel)
        self.preview_image_label.bind("<Button-4>", self.on_preview_mouse_wheel)
        self.preview_image_label.bind("<Button-5>", self.on_preview_mouse_wheel)

        ttk.Label(parent, textvariable=self.preview_filename_var, anchor="w").pack(fill=tk.X, pady=(6, 0))
        ttk.Label(parent, textvariable=self.preview_meta_var, anchor="w").pack(fill=tk.X, pady=(2, 0))

    def _configure_row_tags(self) -> None:
        self.table.tag_configure("missing", background="#ffe9e9")
        self.table.tag_configure("downloaded_mode", background="#e9f8ee")

    def on_export_mode_changed(self) -> None:
        self.refresh_table()
        self.refresh_scroller()

    def on_browser_mode_changed(self) -> None:
        if self.filename_frame is None or self.preview_frame is None:
            return

        self.filename_frame.pack_forget()
        self.preview_frame.pack_forget()

        if self.browser_mode_var.get() == BROWSER_PREVIEW:
            self.preview_frame.pack(fill=tk.BOTH, expand=True)
            self.refresh_preview_panel()
        else:
            self.filename_frame.pack(fill=tk.BOTH, expand=True)

    def refresh_scroller(self) -> None:
        self.refresh_filename_browser()
        if self.preview_scale is not None:
            max_value = max(1, len(self.photos))
            self.preview_scale.configure(from_=1, to=max_value)
        if len(self.photos) == 0:
            self.preview_index_var.set(1)
        else:
            self.preview_index_var.set(min(max(self.preview_index_var.get(), 1), len(self.photos)))
        self.refresh_preview_panel()

    def refresh_filename_browser(self) -> None:
        if self.filename_listbox is None:
            return

        self.filename_listbox.delete(0, tk.END)
        mode = self.mode_var.get()

        for idx, photo in enumerate(self.photos, start=1):
            rel = self.exporter.planned_relative_path(photo, mode)
            downloaded = "downloaded" if self.history.has_mode(photo.key, mode) else "new"
            line = f"{idx:04d}  {rel}  [{downloaded}]"
            self.filename_listbox.insert(tk.END, line)

        if self.photos:
            self.filename_listbox.selection_set(0)
            self.filename_listbox.see(0)

    def on_preview_previous(self) -> None:
        self.set_preview_index(self.preview_index_var.get() - 1)

    def on_preview_next(self) -> None:
        self.set_preview_index(self.preview_index_var.get() + 1)

    def on_preview_scale_changed(self, value: str) -> None:
        if self.preview_updating:
            return
        try:
            index = int(float(value))
        except ValueError:
            return
        self.set_preview_index(index)

    def on_preview_mouse_wheel(self, event: tk.Event) -> None:
        delta = 0
        if hasattr(event, "delta") and event.delta:
            delta = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1

        if delta != 0:
            self.set_preview_index(self.preview_index_var.get() + delta)

    def set_preview_index(self, one_based_index: int) -> None:
        if not self.photos:
            self.preview_index_var.set(1)
            self.refresh_preview_panel()
            return

        clamped = min(max(one_based_index, 1), len(self.photos))
        self.preview_index_var.set(clamped)
        self.refresh_preview_panel()

    def refresh_preview_panel(self) -> None:
        total = len(self.photos)
        current = self.preview_index_var.get()

        if total == 0:
            self.preview_index_label_var.set("0 / 0")
            self.preview_filename_var.set("")
            self.preview_meta_var.set("No photos loaded")
            if self.preview_image_label is not None:
                self.preview_image_label.configure(image="", text="")
            return

        current = min(max(current, 1), total)
        self.preview_index_var.set(current)
        self.preview_index_label_var.set(f"{current} / {total}")

        if self.preview_scale is not None:
            self.preview_updating = True
            try:
                self.preview_scale.set(current)
            finally:
                self.preview_updating = False

        photo = self.photos[current - 1]
        mode = self.mode_var.get()
        rel = self.exporter.planned_relative_path(photo, mode)
        self.preview_filename_var.set(str(rel))

        late = "Late" if photo.is_late else "On time"
        downloaded = "Downloaded for this mode" if self.history.has_mode(photo.key, mode) else "Not downloaded"
        self.preview_meta_var.set(f"{self._format_time(photo.taken_time)} | {late} | {downloaded}")

        if self.preview_image_label is None:
            return

        if not photo.front_path.exists() or not photo.back_path.exists():
            self.preview_image_label.configure(image="", text="Source image missing")
            return

        try:
            preview = self.exporter.render_output_image(photo, mode)
            preview.thumbnail((900, 430), Image.Resampling.LANCZOS)
            self.preview_photo_image = ImageTk.PhotoImage(preview)
            self.preview_image_label.configure(image=self.preview_photo_image, text="")
        except Exception as exc:
            self.preview_image_label.configure(image="", text=f"Preview error: {exc}")

    def on_browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.cwd()))
        if selected:
            self.path_var.set(selected)

    def on_load_data(self) -> None:
        if Image is None or ImageOps is None or ImageTk is None:
            messagebox.showerror(
                "Missing dependency",
                "Pillow is required. Install it with:\n\npython3 -m pip install -r requirements.txt",
            )
            return

        try:
            base = Path(self.path_var.get()).expanduser()
            export_dir = self.loader.find_export_dir(base)
            photos = self.loader.load_memories(export_dir)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return

        self.export_dir = export_dir
        self.photos = photos

        self.refresh_table()
        self.refresh_scroller()
        self.status_var.set(f"Loaded {len(self.photos)} memory entries from {export_dir}")

    def refresh_table(self) -> None:
        self.photo_by_item.clear()
        for item in self.table.get_children():
            self.table.delete(item)

        mode = self.mode_var.get()

        for photo in self.photos:
            downloaded_modes = self.history.downloaded_modes(photo.key)
            downloaded_mode = "Yes" if mode in downloaded_modes else "No"

            tags = []
            if not photo.front_path.exists() or not photo.back_path.exists():
                tags.append("missing")
            elif downloaded_mode == "Yes":
                tags.append("downloaded_mode")

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

    def on_download_selected(self) -> None:
        selected_items = self.table.selection()
        if not selected_items:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return

        photos = [self.photo_by_item[item] for item in selected_items if item in self.photo_by_item]
        self._download_photos(photos)

    def on_download_all(self) -> None:
        if not self.photos:
            messagebox.showinfo("No data", "Load export data first.")
            return
        self._download_photos(self.photos)

    def _download_photos(self, photos: List[MemoryPhoto]) -> None:
        mode = self.mode_var.get()

        succeeded = 0
        skipped = 0
        failed = 0
        errors: List[str] = []

        total = len(photos)

        for i, photo in enumerate(photos, start=1):
            self.status_var.set(f"Exporting {i}/{total}...")
            self.root.update_idletasks()

            if self.skip_existing_var.get() and self.history.has_mode(photo.key, mode):
                skipped += 1
                continue

            try:
                out_path, sidecar_path = self.exporter.export_photo(photo, mode)
                self.history.mark_download(photo.key, mode, out_path, sidecar_path)
                succeeded += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{photo.taken_time}: {exc}")

        self.history.save()
        self.refresh_table()
        self.refresh_scroller()

        summary = f"Done. Success: {succeeded}, Skipped: {skipped}, Failed: {failed}"
        self.status_var.set(summary)

        if failed:
            preview = "\n".join(errors[:10])
            more = "" if len(errors) <= 10 else f"\n...and {len(errors) - 10} more"
            messagebox.showwarning("Completed with errors", f"{summary}\n\n{preview}{more}")
        else:
            messagebox.showinfo("Completed", summary)

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
