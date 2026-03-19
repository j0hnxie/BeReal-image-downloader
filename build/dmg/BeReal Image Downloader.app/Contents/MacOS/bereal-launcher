#!/bin/bash
set -euo pipefail

APP_DISPLAY_NAME="BeReal Image Downloader"
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES_DIR="$APP_ROOT/Resources"
VENV_DIR="$RESOURCES_DIR/venv"
PYTHON_BIN="$RESOURCES_DIR/venv/bin/python3"
APP_SCRIPT="$RESOURCES_DIR/app/bereal_downloader_app.py"
PIL_DYLIB_DIR="$(find "$VENV_DIR/lib" -maxdepth 4 -type d \( -name '.dylibs' -o -name 'pillow.libs' \) -print -quit 2>/dev/null || true)"
HOST_ARM64_CAPABLE="$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || echo 0)"
CURRENT_ARCH="$(/usr/bin/arch)"

show_alert() {
  /usr/bin/osascript -e "display alert \"BeReal Image Downloader\" message \"$1\" as critical"
}

if [[ ! -x "$PYTHON_BIN" ]]; then
  show_alert "The bundled Python runtime was not found. Rebuild the app with 'make app-bundle' and reinstall it."
  exit 1
fi

if [[ ! -f "$APP_SCRIPT" ]]; then
  show_alert "The application script is missing from the app bundle. Rebuild the app with 'make app-bundle'."
  exit 1
fi

if [[ "${BEREAL_NATIVE_REEXEC_DONE:-0}" != "1" && "$HOST_ARM64_CAPABLE" == "1" && "$CURRENT_ARCH" != "arm64" ]]; then
  export BEREAL_NATIVE_REEXEC_DONE=1
  exec /usr/bin/arch -arm64 /bin/bash "$0" "$@"
fi

unset PYTHONHOME
export PYTHONNOUSERSITE=1
if [[ -n "$PIL_DYLIB_DIR" && -d "$PIL_DYLIB_DIR" ]]; then
  export DYLD_FALLBACK_LIBRARY_PATH="$PIL_DYLIB_DIR${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi

cd "$HOME"
if [[ "$HOST_ARM64_CAPABLE" == "1" ]]; then
  exec /bin/bash -c 'exec -a "$0" /usr/bin/arch -arm64 "$1" "$2"' "$APP_DISPLAY_NAME" "$PYTHON_BIN" "$APP_SCRIPT"
fi
exec -a "$APP_DISPLAY_NAME" "$PYTHON_BIN" "$APP_SCRIPT"
