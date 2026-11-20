#!/usr/bin/env bash
# One-time setup: Python environment + scrcpy (which includes adb).
# Safe to re-run; it skips anything already in place.
set -euo pipefail
cd "$(dirname "$0")"

SCRCPY_VERSION="5.0.1"   # the client speaks this exact protocol version
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mSetup failed:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- Python ------------------------------------------------------------------
say "1/3  Checking Python"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found. Install Python 3.10 or newer from https://www.python.org/downloads/"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ is required (found $("$PY" --version 2>&1))."
echo "using $("$PY" --version 2>&1)"

say "2/3  Installing Python packages into .venv"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -c 'import tracker, av, cv2, numpy; print(f"PyAV {av.__version__}, OpenCV {cv2.__version__}, NumPy {numpy.__version__}")'

# ---- scrcpy ------------------------------------------------------------------
say "3/3  Installing scrcpy $SCRCPY_VERSION (includes adb)"
DEST="tools/scrcpy"
if [ -x "$DEST/scrcpy" ] && "$DEST/scrcpy" --version 2>/dev/null | grep -q "scrcpy $SCRCPY_VERSION"; then
  echo "already installed in $DEST"
else
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  PLAT="macos-aarch64" ;;
    Darwin-x86_64) PLAT="macos-x86_64" ;;
    Linux-x86_64)  PLAT="linux-x86_64" ;;
    *) die "no prebuilt scrcpy for $(uname -s)-$(uname -m). Install scrcpy $SCRCPY_VERSION yourself and copy scrcpy, adb and scrcpy-server into $DEST/." ;;
  esac
  NAME="scrcpy-$PLAT-v$SCRCPY_VERSION"
  BASE="https://github.com/Genymobile/scrcpy/releases/download/v$SCRCPY_VERSION"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "downloading $NAME.tar.gz"
  curl -fsSL -o "$TMP/$NAME.tar.gz" "$BASE/$NAME.tar.gz"
  curl -fsSL -o "$TMP/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt"
  WANT="$(grep " $NAME.tar.gz\$" "$TMP/SHA256SUMS.txt" | cut -d' ' -f1)"
  if command -v shasum >/dev/null; then GOT="$(shasum -a 256 "$TMP/$NAME.tar.gz" | cut -d' ' -f1)";
  else GOT="$(sha256sum "$TMP/$NAME.tar.gz" | cut -d' ' -f1)"; fi
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || die "checksum mismatch for $NAME.tar.gz"
  echo "checksum OK"
  tar -xzf "$TMP/$NAME.tar.gz" -C "$TMP"
  mkdir -p tools
  rm -rf "$DEST"
  mv "$TMP/$NAME" "$DEST"
  "$DEST/scrcpy" --version | head -1
fi

say "Done."
cat <<'EOF'
Next:
  1. Prepare the phone (README: "Phone setup").
  2. Plug it in by USB and run:   ./start.sh
EOF
