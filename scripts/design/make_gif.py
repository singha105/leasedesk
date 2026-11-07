"""Build docs/media/leasedesk.gif from real Discord captures of the live runs.

Usage: python scripts/design/make_gif.py [out.gif] [frames_dir]

Needs Pillow and the fonts in build/fonts/ (`scripts/design/render.sh fonts` downloads them).
Reads docs/design/captures/, the title and end cards in docs/design/cards/ (rendered by
`scripts/design/render.sh cards`) and the scenario screenshots in docs/media/.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "docs/design/captures"
CARDS = ROOT / "docs/design/cards"
MEDIA = ROOT / "docs/media"
FONTS = ROOT / "build/fonts"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else MEDIA / "leasedesk.gif"
FRAMES_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "build/gif-frames"

W, H, BAND, PAD = 1120, 630, 74, 22
WASHI = (237, 227, 204)
SUMI = (35, 32, 28)
AI = (28, 63, 102)
SHU = (178, 58, 43)
KIN = (169, 132, 58)


def font(name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    path = FONTS / name
    if not path.exists():
        sys.exit(f"{path} is missing: run scripts/design/render.sh fonts")
    f = ImageFont.truetype(str(path), size)
    if weight is not None:
        f.set_variation_by_axes([weight])
    return f


WORDMARK = font("BigelowRules-Regular.ttf", 40)
NUMERAL = font("Alegreya[wght].ttf", 30 * 4, 800)
KANJI = font("ShipporiMinchoB1-ExtraBold.ttf", 30 * 4)


def caption_font(text: str, room: int) -> ImageFont.FreeTypeFont:
    """Alegreya at 27 px, or smaller until the caption fits its room."""
    for size in range(27, 19, -1):
        f = font("Alegreya[wght].ttf", size, 560)
        if f.getlength(text) <= room:
            return f
    return f


def blob(tag: str) -> Image.Image:
    return Image.open(CAPTURES / f"{tag}.png").convert("RGB")


def media(name: str) -> Image.Image:
    return Image.open(MEDIA / name).convert("RGB")


def to_width(im: Image.Image, w: int = 1568) -> Image.Image:
    return im if im.width == w else im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)


def stack(parts: list[Image.Image], gap: int = 10) -> Image.Image:
    parts = [to_width(p) for p in parts]
    bg = parts[0].getpixel((parts[0].width - 5, 5))
    out = Image.new(
        "RGB", (parts[0].width, sum(p.height for p in parts) + gap * (len(parts) - 1)), bg
    )
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height + gap
    return out


def seal(mark: str, size: int = 44) -> Image.Image:
    """A vermilion seal with a washi numeral or kanji, drawn at 4x and scaled down."""
    k = 4
    s = size * k
    im = Image.new("RGBA", (s + 8 * k, s + 8 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    o = 4 * k
    d.rounded_rectangle([o, o, o + s, o + s], radius=4 * k, fill=SHU)
    inner = [o + 3 * k, o + 3 * k, o + s - 3 * k, o + s - 3 * k]
    d.rounded_rectangle(inner, radius=2 * k, outline=WASHI, width=2 * k)
    glyph = KANJI if not mark.isdigit() else NUMERAL
    d.text((o + s / 2, o + s / 2 + (0 if mark.isdigit() else 2 * k)), mark, WASHI, glyph, "mm")
    im = im.rotate(-3, resample=Image.BICUBIC)
    return im.resize((im.width // k, im.height // k), Image.LANCZOS)


def frame(content: Image.Image, step: str, caption: str) -> Image.Image:
    f = Image.new("RGB", (W, H), WASHI)
    d = ImageDraw.Draw(f)
    # the caption band: a seal, the caption in sumi, the wordmark, then a double gold rule
    mark = seal(step or "鍵")
    f.paste(mark, (PAD - 4, (BAND - mark.height) // 2 + 1), mark)
    wordmark_w = WORDMARK.getlength("LeaseDesk")
    x = PAD + mark.width + 10
    room = W - x - wordmark_w - PAD - 24
    d.text((x, BAND // 2 + 1), caption, SUMI, caption_font(caption, room), "lm")
    d.text((W - PAD, BAND // 2 + 2), "LeaseDesk", AI, WORDMARK, "rm")
    d.line([(PAD, BAND - 2), (W - PAD, BAND - 2)], KIN, 2)
    d.line([(PAD, BAND + 3), (W - PAD, BAND + 3)], KIN, 1)
    # the capture, matted like a print: a sumi keyline and a gold rule around it
    maxw, maxh = W - 2 * PAD - 16, H - BAND - 2 * PAD - 16
    s = min(maxw / content.width, maxh / content.height)
    c = content.resize((round(content.width * s), round(content.height * s)), Image.LANCZOS)
    cx = (W - c.width) // 2
    cy = BAND + PAD + (H - BAND - 2 * PAD - c.height) // 2
    f.paste(c, (cx, cy))
    d.rectangle([cx - 1, cy - 1, cx + c.width, cy + c.height], None, SUMI, 1)
    d.rectangle([cx - 8, cy - 8, cx + c.width + 7, cy + c.height + 7], None, KIN, 2)
    return f


def card(name: str) -> Image.Image:
    return Image.open(CARDS / f"{name}.png").convert("RGB").resize((W, H), Image.LANCZOS)


req = blob("01-request")
ImageDraw.Draw(req).rectangle(
    [0, 72, 80, req.height], fill=req.getpixel((req.width - 5, 5))
)  # hide the thread connector line
happy = to_width(media("happy-path.png"))
cut = round(330 * happy.width / 1456)

frames = [
    (card("title"), 3200),
    (frame(req, "1", "Ask in plain English in #access-desk"), 3500),
    (
        frame(
            blob("02-scribe-draft"), "2", "scribe (Claude Code) drafts the IAM policy as a lease"
        ),
        4500,
    ),
    (
        frame(
            stack(
                [
                    blob("03-sentinel-advisory"),
                    blob("04-keymaster-refusal"),
                    blob("05-ready-and-confirm"),
                ]
            ),
            "3",
            "sentinel checks it, keymaster enforces the rules, you confirm",
        ),
        6000,
    ),
    (
        frame(
            stack([blob("06-sentinel-verdict"), happy.crop((0, 0, happy.width, cut))]),
            "4",
            "sentinel scores it with rules (no LLM); a named person approves",
        ),
        5500,
    ),
    (
        frame(
            happy.crop((0, cut, happy.width, happy.height)),
            "5",
            "Time-boxed IAM policy: ACTIVE, red-teamed by skeptic, EXPIRED on time",
        ),
        6000,
    ),
    (
        frame(
            media("block.png"),
            "",
            "Ask for PassRole + Lambda: sentinel blocks it and names the path",
        ),
        6000,
    ),
    (
        frame(
            media("escalation.png"),
            "",
            "Touch a crown jewel: HIGH risk, approval moves to #security-review",
        ),
        5000,
    ),
    (
        frame(
            media("freeze.png"), "", "One command freezes the desk and revokes every active lease"
        ),
        4500,
    ),
    (card("end"), 3500),
]

FRAMES_DIR.mkdir(parents=True, exist_ok=True)
for i, (im, _) in enumerate(frames):
    im.save(FRAMES_DIR / f"frame{i:02d}.png")

pal = [
    im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    for im, _ in frames
]
pal[0].save(
    OUT,
    save_all=True,
    append_images=pal[1:],
    duration=[d for _, d in frames],
    loop=0,
    optimize=True,
    disposal=1,
)
print(
    OUT, OUT.stat().st_size, "bytes,", sum(d for _, d in frames) / 1000, "s,", len(frames), "frames"
)
