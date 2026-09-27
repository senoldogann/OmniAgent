#!/bin/sh
# Yerel Apple Silicon uygulama paketini ve simgesini tekrar üretir.
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

uv run --python 3.11 --frozen python packaging/create_icon.py build/OmniAgent.icns
uv run --python 3.11 --frozen --with pyinstaller==6.22.3 pyinstaller \
  --noconfirm --clean \
  --distpath dist \
  --workpath build/pyinstaller \
  packaging/OmniAgent.spec

plutil -lint dist/OmniAgent.app/Contents/Info.plist
codesign --verify --deep --strict --verbose=2 dist/OmniAgent.app
dist/OmniAgent.app/Contents/MacOS/OmniAgent --bundle-check
echo "Hazır: $PROJECT_ROOT/dist/OmniAgent.app"
