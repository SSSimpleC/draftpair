#!/bin/zsh
set -eu
cd "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/TeX/texbin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'from lxml import html; import markdown_it, pypdf' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python launch.py "$@"
