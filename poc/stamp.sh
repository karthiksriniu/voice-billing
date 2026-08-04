#!/bin/bash
# Stamp the build into the page and bust the asset cache, so "are you on the new build?"
# stops being a question anybody has to answer by guessing. Run before every deploy.
set -euo pipefail
cd "$(dirname "$0")"
V="$(git rev-parse --short HEAD 2>/dev/null || date +%s)-$(date +%H%M)"
python3 - "$V" <<'PY'
import re, sys, pathlib
v = sys.argv[1]
p = pathlib.Path("public/index.html"); s = p.read_text()
s = re.sub(r'(src|href)="/(app|wake|i18n)\.js(\?v=[^"]*)?"', rf'\1="/\2.js?v={v}"', s)
s = re.sub(r'href="/(style|kiowa)\.css(\?v=[^"]*)?"', rf'href="/\1.css?v={v}"', s)
s = re.sub(r'<script>window\.BOLO_BUILD=.*?</script>\n?', '', s)
s = s.replace('<script src="/i18n.js', f'<script>window.BOLO_BUILD="{v}"</script>\n<script src="/i18n.js')
p.write_text(s)
print(f"stamped {v}")
PY
