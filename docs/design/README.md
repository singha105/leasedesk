# Visual identity

LeaseDesk's images look like an ukiyo-e woodblock print set inside an Art Nouveau frame: flat Prussian-blue waves, a red seal and gold kasumi mist, held by whiplash-curve corners.

| Name | Hex | Use |
| - | - | - |
| washi | `#EDE3CC` | paper, backgrounds |
| sumi | `#23201C` | ink, text |
| bero-ai | `#1C3F66` | Prussian blue, waves, headings |
| hanada | `#7E9DB5` | pale indigo, secondary waves |
| shu | `#B23A2B` | seal red, sun, step markers |
| kin | `#A9843A` | gold lines, frames |

Type: Bigelow Rules for the wordmark and agent names, Alegreya for text, Shippori Mincho B1 (800) for the seal kanji 鍵 ("key"). All three are Google Fonts.

Rules for new images:

- One bold element per image: the wave in the hero and the social card, keymaster's red halo in the agent panels.
- Draw original art in these styles; don't copy a specific print. The wave's foam ends in Art Nouveau curls, not claws.
- No all-caps eyebrow labels, no middle-dot meta strings, no arrow suffixes.

## Files

| Path | What it is |
| - | - |
| `src/art.js` | Shared drawing: the wave, seigaiha, kasumi, the sun, the seal and the frame |
| `src/common.css` | Fonts (from `build/fonts/`) and the palette |
| `src/hero.html` | `docs/media/hero.png`, 1280 x 440 at 2x |
| `src/social.html` | `docs/media/social.png`, 1280 x 640, the GitHub social preview |
| `src/agents.html` | `docs/media/agents.png`, the four agent panels |
| `src/print.html` | `docs/media/print-*.png`, each scenario screenshot matted with a caption |
| `src/card.html` | `cards/title.png` and `cards/end.png`, the GIF's first and last frames |
| `captures/` | Crops of the live Discord runs on 2026-11-07. No ids, tokens or account numbers |

## Rebuilding

```bash
scripts/design/render.sh
```

It downloads the fonts into `build/fonts/` once (they are not committed), renders every page with headless Chrome at 2x and quantizes the PNGs with Pillow. Name targets to render only some: `hero`, `social`, `agents`, `prints`, `cards`. Set `CHROME` if Chrome or Chromium isn't on the path, and `PYTHON` if `python3` has no Pillow.

```bash
python3 scripts/design/make_gif.py
```

It builds `docs/media/leasedesk.gif` from `captures/`, `cards/` and the scenario screenshots in `docs/media/`, with the fonts from `build/fonts/`.

The social preview is uploaded by hand: repository Settings, Social preview, then `docs/media/social.png`.
