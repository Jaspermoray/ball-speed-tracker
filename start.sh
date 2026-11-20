#!/usr/bin/env bash
# Start the tracker. See ./start.sh --help
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Not set up yet. Run ./setup.sh first." >&2
  exit 1
fi
exec .venv/bin/python start.py "$@"
