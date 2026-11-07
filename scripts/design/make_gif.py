"""Build docs/media/leasedesk.gif from real Discord captures of the live runs.

Usage: python scripts/design/make_gif.py [out.gif] [frames_dir]
Needs Pillow. The font paths below are macOS paths.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "docs/design/captures"
MEDIA = ROOT / "docs/media"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else MEDIA / "leasedesk.gif"
FRAMES_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "build/gif-frames"

W, H, CAP, PAD = 1120, 630, 66, 16
BG = (49, 51, 56)
BAND = (30, 31, 34)
ACCENT = (88, 101, 242)
WHITE = (242, 243, 245)
MUTED = (181, 186, 193)

BOLD = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 25, index=1)
BIG = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 64, index=1)
MID = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 30, index=0)
SMALL = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 24, index=0)


def blob(tag: str) -> Image.Image:
    return Image.open(CAPTURES / f"{tag}.png").convert("RGB")


def media(name: str) -> Image.Image:
    return Image.open(MEDIA / name).convert("RGB")


def to_width(im: Image.Image, w: int = 1568) -> Image.Image:
    return im if im.width == w else im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)


def stack(parts: list[Image.Image], gap: int = 10) -> Image.Image:
    parts = [to_width(p) for p in parts]
    out = Image.new(
        "RGB", (parts[0].width, sum(p.height for p in parts) + gap * (len(parts) - 1)), BG
    )
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height + gap
    return out


def frame(content: Image.Image, step: str, caption: str) -> Image.Image:
    f = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(f)
    d.rectangle([0, 0, W, CAP], fill=BAND)
    x = PAD + 4
    if step:
        d.rounded_rectangle([x, 16, x + 36, 50], radius=8, fill=ACCENT)
        d.text((x + 18, 33), step, font=BOLD, fill=WHITE, anchor="mm")
        x += 50
    d.text((x, 33), caption, font=BOLD, fill=WHITE, anchor="lm")
    maxw, maxh = W - 2 * PAD, H - CAP - 2 * PAD
    s = min(maxw / content.width, maxh / content.height)
    c = content.resize((round(content.width * s), round(content.height * s)), Image.LANCZOS)
    f.paste(c, (PAD, CAP + PAD))
    return f


def card(
    title: str, lines: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]]
) -> Image.Image:
    f = Image.new("RGB", (W, H), BAND)
    d = ImageDraw.Draw(f)
    y = 200
    d.text((W // 2, y), title, font=BIG, fill=WHITE, anchor="mm")
    y += 80
    for text, font, color in lines:
        d.text((W // 2, y), text, font=font, fill=color, anchor="mm")
        y += 48
    d.rectangle([W // 2 - 60, 120, W // 2 + 60, 126], fill=ACCENT)
    return f


req = blob("01-request")
ImageDraw.Draw(req).rectangle(
    [0, 72, 80, req.height], fill=req.getpixel((req.width - 5, 5))
)  # hide the thread connector line
happy = to_width(media("happy-path.png"))
cut = round(330 * happy.width / 1456)

frames = [
    (
        card(
            "LeaseDesk",
            [
                ("Just-in-time AWS access, asked for in a Switch room", MID, WHITE),
                ("AI drafts and critiques. A person approves. AWS ends it on time.", SMALL, MUTED),
            ],
        ),
        3200,
    ),
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
    (
        card(
            "LeaseDesk",
            [
                ("github.com/singha105/leasedesk", MID, WHITE),
                ("Built on Switch  ·  Flip the Switch 2026  ·  Team TigerMan", SMALL, MUTED),
            ],
        ),
        3500,
    ),
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
