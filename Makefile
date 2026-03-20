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
NATIVE_LAUNCHER := $(BUILD_DIR)/bereal-launcher

PYTHON ?= /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
VENV_DIR := .venv
PIP := $(VENV_DIR)/bin/pip
APP_PYTHON := $(VENV_DIR)/bin/python3
PIP_FLAGS := --disable-pip-version-check
MACOS_SDK := $(shell xcrun --sdk macosx --show-sdk-path)
CLANG := $(shell xcrun --find clang)
PYTHON_EMBED_CFLAGS := $(shell /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13-config --embed --cflags | sed 's/-arch x86_64//g')
PYTHON_EMBED_LDFLAGS := $(shell /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13-config --embed --ldflags)

.PHONY: help venv deps doctor run icon app-icon app-bundle install-app uninstall-app reinstall-app open-app dmg clean distclean

help:
	@echo "Available targets:"
	@echo "  make venv          - Create the local virtualenv"
	@echo "  make deps          - Install Python dependencies into .venv"
	@echo "  make doctor        - Check Python, tkinter, and Pillow"
	@echo "  make run           - Run the app from source"
	@echo "  make icon          - Build the macOS .icns file from icon.png"
	@echo "  make app-icon      - Build build/AppIcon.iconset and build/AppIcon.icns"
	@echo "  make launcher      - Build the native macOS launcher binary"
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

.PHONY: launcher

icon: deps $(ICNS_PATH)

app-icon: icon

$(ICNS_PATH): icon.png packaging/build_app_icon.py
	rm -rf $(ICONSET_DIR)
	$(APP_PYTHON) packaging/build_app_icon.py icon.png $(ICONSET_DIR)
	iconutil -c icns $(ICONSET_DIR) -o $(ICNS_PATH)

launcher: $(NATIVE_LAUNCHER)

$(NATIVE_LAUNCHER): packaging/native_launcher.m
	mkdir -p $(BUILD_DIR)
	$(CLANG) -arch arm64 -isysroot "$(MACOS_SDK)" \
		$(PYTHON_EMBED_CFLAGS) \
		-framework Foundation \
		-o $(NATIVE_LAUNCHER) \
		packaging/native_launcher.m \
		$(PYTHON_EMBED_LDFLAGS)

app-bundle: deps icon launcher
	rm -rf "$(APP_BUNDLE)"
	mkdir -p "$(APP_MACOS)" "$(APP_RESOURCES)/app"
	cp packaging/Info.plist "$(APP_CONTENTS)/Info.plist"
	printf 'APPL????' > "$(APP_CONTENTS)/PkgInfo"
	cp "$(NATIVE_LAUNCHER)" "$(APP_EXECUTABLE)"
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
