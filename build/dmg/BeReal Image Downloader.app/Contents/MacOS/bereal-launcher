#!/bin/bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES_DIR="$APP_ROOT/Resources"
PYTHON_BIN="$RESOURCES_DIR/venv/bin/python3"
APP_SCRIPT="$RESOURCES_DIR/app/bereal_downloader_app.py"

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

cd "$HOME"
exec "$PYTHON_BIN" "$APP_SCRIPT"
