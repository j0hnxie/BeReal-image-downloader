APP_NAME := BeReal Image Downloader
APP_BUNDLE := dist/$(APP_NAME).app
APP_CONTENTS := $(APP_BUNDLE)/Contents
APP_MACOS := $(APP_CONTENTS)/MacOS
APP_RESOURCES := $(APP_CONTENTS)/Resources
APP_EXECUTABLE := $(APP_MACOS)/bereal-launcher
APP_INSTALL_PATH := /Applications/$(APP_NAME).app

BUILD_DIR := build
DIST_DIR := dist
ICONSET_DIR := $(BUILD_DIR)/AppIcon.iconset
ICNS_PATH := $(BUILD_DIR)/AppIcon.icns
DMG_STAGING_DIR := $(BUILD_DIR)/dmg
DMG_PATH := $(DIST_DIR)/$(APP_NAME).dmg

PYTHON ?= /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
VENV_DIR := .venv
PIP := $(VENV_DIR)/bin/pip
APP_PYTHON := $(VENV_DIR)/bin/python3
PIP_FLAGS := --disable-pip-version-check

.PHONY: help venv deps doctor run icon app-bundle install-app uninstall-app reinstall-app open-app dmg clean distclean

help:
	@echo "Available targets:"
	@echo "  make venv          - Create the local virtualenv"
	@echo "  make deps          - Install Python dependencies into .venv"
	@echo "  make doctor        - Check Python, tkinter, and Pillow"
	@echo "  make run           - Run the app from source"
	@echo "  make icon          - Build the macOS .icns file from icon.png"
	@echo "  make app-bundle    - Build dist/$(APP_NAME).app"
	@echo "  make install-app   - Install the app bundle into /Applications"
	@echo "  make uninstall-app - Remove the installed app from /Applications"
	@echo "  make reinstall-app - Reinstall the app into /Applications"
	@echo "  make open-app      - Open the built app bundle"
	@echo "  make dmg           - Build a DMG containing the app bundle"
	@echo "  make clean         - Remove build and dist artifacts"
	@echo "  make distclean     - Remove build artifacts and the local virtualenv"

$(VENV_DIR)/bin/python3:
	$(PYTHON) -m venv $(VENV_DIR)

venv: $(VENV_DIR)/bin/python3

deps: venv requirements.txt
	$(PIP) install $(PIP_FLAGS) -r requirements.txt

doctor: deps
	@$(APP_PYTHON) -c 'import sys; print("python:", sys.executable); import tkinter as tk; print("tkinter:", tk.TkVersion); import PIL; print("pillow:", PIL.__version__); print("doctor_ok")'

run: deps
	$(APP_PYTHON) bereal_downloader_app.py

icon: $(ICNS_PATH)

$(ICNS_PATH): icon.png
	rm -rf $(ICONSET_DIR)
	mkdir -p $(ICONSET_DIR)
	sips -z 16 16 icon.png --out $(ICONSET_DIR)/icon_16x16.png
	sips -z 32 32 icon.png --out $(ICONSET_DIR)/icon_16x16@2x.png
	sips -z 32 32 icon.png --out $(ICONSET_DIR)/icon_32x32.png
	sips -z 64 64 icon.png --out $(ICONSET_DIR)/icon_32x32@2x.png
	sips -z 128 128 icon.png --out $(ICONSET_DIR)/icon_128x128.png
	sips -z 256 256 icon.png --out $(ICONSET_DIR)/icon_128x128@2x.png
	sips -z 256 256 icon.png --out $(ICONSET_DIR)/icon_256x256.png
	sips -z 512 512 icon.png --out $(ICONSET_DIR)/icon_256x256@2x.png
	sips -z 512 512 icon.png --out $(ICONSET_DIR)/icon_512x512.png
	cp icon.png $(ICONSET_DIR)/icon_512x512@2x.png
	iconutil -c icns $(ICONSET_DIR) -o $(ICNS_PATH)

app-bundle: deps icon
	rm -rf "$(APP_BUNDLE)"
	mkdir -p "$(APP_MACOS)" "$(APP_RESOURCES)/app"
	cp packaging/Info.plist "$(APP_CONTENTS)/Info.plist"
	printf 'APPL????' > "$(APP_CONTENTS)/PkgInfo"
	cp packaging/launcher.sh "$(APP_EXECUTABLE)"
	chmod +x "$(APP_EXECUTABLE)"
	cp "$(ICNS_PATH)" "$(APP_RESOURCES)/AppIcon.icns"
	cp bereal_downloader_app.py requirements.txt README.md icon.png "$(APP_RESOURCES)/app/"
	ditto "$(VENV_DIR)" "$(APP_RESOURCES)/venv"

install-app: app-bundle
	rm -rf "$(APP_INSTALL_PATH)"
	ditto "$(APP_BUNDLE)" "$(APP_INSTALL_PATH)"

uninstall-app:
	rm -rf "$(APP_INSTALL_PATH)"

reinstall-app: uninstall-app install-app

open-app: app-bundle
	open "$(APP_BUNDLE)"

dmg: app-bundle
	rm -rf "$(DMG_STAGING_DIR)"
	mkdir -p "$(DMG_STAGING_DIR)"
	rm -f "$(DMG_PATH)"
	ditto "$(APP_BUNDLE)" "$(DMG_STAGING_DIR)/$(APP_NAME).app"
	hdiutil create -volname "$(APP_NAME)" -srcfolder "$(DMG_STAGING_DIR)" -ov -format UDZO "$(DMG_PATH)"

clean:
	rm -rf "$(BUILD_DIR)" "$(DIST_DIR)"

distclean: clean
	rm -rf "$(VENV_DIR)"
