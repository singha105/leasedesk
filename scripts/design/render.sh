#!/usr/bin/env bash
# Render the design pages in docs/design/src/ to docs/media/ with headless Chrome at 2x, then
# quantize them with Pillow so they stay small.
#
#   scripts/design/render.sh                 everything
#   scripts/design/render.sh hero agents     just those (hero, social, agents, prints)
#
# Needs curl, Chrome or Chromium (set CHROME to its path if it isn't found) and Python with
# Pillow (set PYTHON if python3 lacks it). The fonts are downloaded once into build/fonts/.
set -euo pipefail
cd "$(dirname "$0")/../.."

FONTS=build/fonts
OUT=build/design
MEDIA=docs/media
PY=${PYTHON:-python3}
mkdir -p "$FONTS" "$OUT"

fonts() {
  local base=https://raw.githubusercontent.com/google/fonts/main/ofl f name
  for f in bigelowrules/BigelowRules-Regular.ttf \
    "alegreya/Alegreya%5Bwght%5D.ttf" "alegreya/Alegreya-Italic%5Bwght%5D.ttf" \
    shipporiminchob1/ShipporiMinchoB1-ExtraBold.ttf; do
    name=$(basename "$f" | sed 's/%5B/[/; s/%5D/]/')
    [ -s "$FONTS/$name" ] || curl -fsSL -o "$FONTS/$name" "$base/$f"
  done
}

chrome() {
  local c
  for c in "${CHROME:-}" chromium chromium-browser google-chrome google-chrome-stable \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if [ -n "$c" ] && command -v "$c" >/dev/null 2>&1; then
      echo "$c"
      return
    fi
  done
  echo "no Chrome or Chromium found; set CHROME" >&2
  exit 1
}

# shot PAGE WIDTH HEIGHT NAME [QUERY]: screenshot docs/design/src/PAGE.html to build/design/NAME.png
shot() {
  "$(chrome)" --headless --disable-gpu --no-sandbox --hide-scrollbars --no-first-run \
    --force-device-scale-factor=2 --window-size="$2,$3" --virtual-time-budget=5000 \
    --screenshot="$OUT/$4.png" "file://$PWD/docs/design/src/$1.html${5:-}" >/dev/null 2>&1
}

# quantize SRC DST COLORS [WxH]: palette PNG, optionally resized first
quantize() {
  "$PY" - "$@" <<'EOF'
import sys
from PIL import Image
src, dst, colors = sys.argv[1], sys.argv[2], int(sys.argv[3])
im = Image.open(src).convert("RGB")
if len(sys.argv) > 4:
    w, h = map(int, sys.argv[4].split("x"))
    im = im.resize((w, h), Image.LANCZOS)
im = im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
im.save(dst, optimize=True)
print(f"{dst}: {im.width}x{im.height}, {colors} colours")
EOF
}

hero() { shot hero 1280 440 hero && quantize "$OUT/hero.png" "$MEDIA/hero.png" 96; }
social() { shot social 1280 640 social && quantize "$OUT/social.png" "$MEDIA/social.png" 96 1280x640; }
agents() { shot agents 1280 600 agents && quantize "$OUT/agents.png" "$MEDIA/agents.png" 128; }

prints() {
  local name caption height
  while IFS='|' read -r name caption; do
    # 40 px mat on top, the screenshot at 760 px wide, a 76 px caption strip
    height=$("$PY" -c "from PIL import Image; w, h = Image.open('$MEDIA/$name.png').size; print(40 + round(760 * h / w) + 76)")
    shot print 840 "$height" "print-$name" "?img=$name&caption=$("$PY" -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$caption")"
    quantize "$OUT/print-$name.png" "$MEDIA/print-$name.png" 256
  done <<'EOF'
happy-path|Approved in chat, active for five minutes, ended by AWS on time
block|sentinel blocks PassRole with Lambda and names the escalation path
escalation|A HIGH-risk lease, approved by the security group in its own room
freeze|One command revokes every active lease and blocks new ones
EOF
}

fonts
targets=("$@")
[ ${#targets[@]} -gt 0 ] || targets=(hero social agents prints)
for t in "${targets[@]}"; do
  case "$t" in
    hero | social | agents | prints | fonts) "$t" ;;
    *)
      echo "unknown target: $t" >&2
      exit 2
      ;;
  esac
done
