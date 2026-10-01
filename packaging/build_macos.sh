#!/bin/sh
# Yerel Apple Silicon uygulama paketini ve simgesini tekrar üretir.
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

# A certificate-backed identity keeps the designated requirement stable across
# updates. Never pick arbitrarily when multiple Developer ID identities exist.
if [ -z "${OMNIAGENT_CODESIGN_IDENTITY:-}" ]; then
  OMNIAGENT_CODESIGN_IDENTITY=$(uv run --python 3.11 --frozen python - <<'PY'
import re
import subprocess
import sys

result = subprocess.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
                        check=True, capture_output=True, text=True)
identities = re.findall(r'"(Developer ID Application:[^"\n]+)"', result.stdout)
if len(identities) > 1:
    sys.exit("Birden çok Developer ID var; OMNIAGENT_CODESIGN_IDENTITY ile kimliği seçin.")
if identities:
    print(identities[0])
else:
    print("UYARI: Developer ID yok; yerel geçici imza güncellemelerde izinleri korumayabilir.", file=sys.stderr)
PY
  )
fi
export OMNIAGENT_CODESIGN_IDENTITY

uv run --python 3.11 --frozen python packaging/create_icon.py build/OmniAgent.icns
uv run --python 3.11 --frozen --with pyinstaller==6.22.3 pyinstaller \
  --noconfirm --clean \
  --distpath dist \
  --workpath build/pyinstaller \
  packaging/OmniAgent.spec

plutil -lint dist/OmniAgent.app/Contents/Info.plist
codesign --verify --deep --strict --verbose=2 dist/OmniAgent.app
codesign -d -r- dist/OmniAgent.app
dist/OmniAgent.app/Contents/MacOS/OmniAgent --bundle-check
echo "Hazır: $PROJECT_ROOT/dist/OmniAgent.app"
