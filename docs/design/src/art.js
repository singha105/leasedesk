/* Shared drawing for the LeaseDesk design pages: the great wave, seigaiha, kasumi mist,
   the sun, the red seal and the Art Nouveau frame. A plain script, because Chrome will not
   load ES modules from file:// URLs. Every page calls these with its own coordinates. */

const PAL = {
  washi: "#EDE3CC",
  sumi: "#23201C",
  ai: "#1C3F66",
  hanada: "#7E9DB5",
  shu: "#B23A2B",
  kin: "#A9843A",
};

const SVGNS = "http://www.w3.org/2000/svg";

function E(parent, tag, attrs = {}, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

const fmt = (n) => (Math.round(n * 100) / 100).toString();
const poly = (pts) => "M" + pts.map(([x, y]) => `${fmt(x)} ${fmt(y)}`).join(" L") + " Z";
const line = (pts) => "M" + pts.map(([x, y]) => `${fmt(x)} ${fmt(y)}`).join(" L");

/* A small deterministic random, so every render is identical. */
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/* Catmull-Rom through [x, y, w] points, resampled to n points evenly spaced by arc length.
   Each sample carries its tangent (tx, ty), its right-hand normal (nx, ny) and s in [0, 1]. */
function track(points, n = 600) {
  const P = [points[0], ...points, points[points.length - 1]];
  const dense = [];
  for (let i = 1; i < P.length - 2; i++) {
    const [p0, p1, p2, p3] = [P[i - 1], P[i], P[i + 1], P[i + 2]];
    for (let k = 0; k < 40; k++) {
      const t = k / 40;
      const q = p1.map(
        (_, j) =>
          0.5 *
          (2 * p1[j] +
            (-p0[j] + p2[j]) * t +
            (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t * t +
            (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t * t * t),
      );
      dense.push(q);
    }
  }
  dense.push(points[points.length - 1]);
  const len = [0];
  for (let i = 1; i < dense.length; i++) {
    len.push(len[i - 1] + Math.hypot(dense[i][0] - dense[i - 1][0], dense[i][1] - dense[i - 1][1]));
  }
  const total = len[len.length - 1];
  const out = [];
  let j = 0;
  for (let i = 0; i < n; i++) {
    const target = (total * i) / (n - 1);
    while (j < len.length - 2 && len[j + 1] < target) j++;
    const f = (target - len[j]) / (len[j + 1] - len[j] || 1);
    out.push(dense[j].map((v, k) => v + (dense[j + 1][k] - v) * f));
  }
  return out.map((p, i) => {
    const a = out[Math.max(0, i - 1)];
    const b = out[Math.min(n - 1, i + 1)];
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const tx = (b[0] - a[0]) / d;
    const ty = (b[1] - a[1]) / d;
    return { x: p[0], y: p[1], w: p[2] ?? 0, tx, ty, nx: -ty, ny: tx, s: i / (n - 1) };
  });
}

/* Seigaiha: rows of concentric-ring scales, each row half a scale lower and offset by R.
   left(y) and right(y) bound the centres allowed on a row, so the sea can end in whole scales. */
function seigaiha(g, { x0, y0, x1, y1, r, left = () => -Infinity, right = () => Infinity, fill = PAL.hanada, ring = PAL.washi, edge = PAL.ai }) {
  const rows = Math.ceil((y1 - y0) / (r / 2)) + 2;
  for (let row = 0; row < rows; row++) {
    const y = y0 + (row * r) / 2;
    const off = row % 2 ? r : 0;
    for (let x = x0 - 2 * r + off; x <= x1 + 2 * r; x += 2 * r) {
      if (x < left(y) || x > right(y)) continue;
      E(g, "circle", { cx: x, cy: y, r, fill, stroke: edge, "stroke-width": r * 0.07 });
      for (const f of [0.78, 0.56, 0.34]) {
        E(g, "circle", { cx: x, cy: y, r: r * f, fill: "none", stroke: ring, "stroke-width": r * 0.075 });
      }
      E(g, "circle", { cx: x, cy: y, r: r * 0.13, fill: ring });
    }
  }
}

/* A suyari-gasumi mist band: a long rounded bar with a shorter bar stepped above it. */
function kasumi(g, { x, y, len, h, fill = PAL.kin, step = 0.45, flip = false, opacity = 1 }) {
  const grp = E(g, "g", { fill, opacity });
  E(grp, "rect", { x, y, width: len, height: h, rx: h / 2 });
  const sl = len * step;
  const sx = flip ? x + len - sl - h * 1.2 : x + h * 1.2;
  E(grp, "rect", { x: sx, y: y - h * 0.62, width: sl, height: h * 0.9, rx: h * 0.45 });
  return grp;
}

/* The great wave. spec.pts runs from the foot of the wave's back, over the crest, into the curl:
   [x, y, thickness]. The body is filled bero-ai with hanada flow lines; the washi foam lip breaks
   into fingers that curl like Art Nouveau tendrils (Euler spirals) instead of claws. */
function greatWave(g, spec) {
  const S = track(spec.pts, 900);
  const outer = S.map((p) => [p.x, p.y]);
  const at = (p, d) => [p.x - p.nx * d, p.y - p.ny * d];
  const inner = S.map((p) => at(p, p.w));
  const lw = spec.line ?? 1.8;
  E(g, "path", { d: poly([...outer, ...inner.slice().reverse()]), fill: PAL.ai });

  // flow lines inside the body
  for (const st of spec.stripes) {
    const seg = S.filter((p) => p.s >= st.s0 && p.s <= st.s1);
    const a = [];
    const b = [];
    for (const p of seg) {
      const u = (p.s - st.s0) / (st.s1 - st.s0);
      const taper = Math.pow(Math.sin(Math.PI * u), 0.6);
      const c = st.f * p.w;
      const h = st.h * taper * Math.min(1, p.w / (st.ref ?? 70));
      a.push(at(p, c - h));
      b.push(at(p, c + h));
    }
    E(g, "path", { d: poly([...a, ...b.reverse()]), fill: st.fill ?? PAL.hanada });
  }

  // small foam curls breaking on the body under the crest, each outlined on its own
  const R = rng(spec.seed ?? 7);
  if (spec.spray) {
    const C = spec.spray;
    const curls = E(g, "g", { fill: PAL.washi, stroke: PAL.ai, "stroke-width": lw * 0.9, "stroke-linejoin": "round" });
    for (let s = C.s0; s <= C.s1 + 1e-9; s += C.ds) {
      const p = S[Math.round(s * (S.length - 1))];
      const depth = Math.min(p.w * 0.7, C.depth * (0.8 + 0.4 * R()));
      const q = { ...p, x: p.x - p.nx * depth, y: p.y - p.ny * depth };
      const L = C.len * (0.7 + 0.6 * R());
      E(curls, "path", { d: tendril(q, L, 0.34, C.lean ?? 0.9, C.turn ?? -3.4, 0) });
    }
  }

  // foam: the lip band and the fingers are one washi shape with an ai outline on the outside,
  // drawn as an ai pass with a doubled stroke and then a washi pass on top
  const shapes = [];
  const lip = S.filter((p) => p.s >= spec.foam.s0);
  const la = lip.map((p) => [p.x, p.y]);
  const lb = lip.map((p) => {
    const u = (p.s - spec.foam.s0) / (1 - spec.foam.s0);
    const bw = spec.foam.w * Math.pow(Math.sin(Math.PI * Math.min(1, u * 1.1)), 0.6);
    return at(p, Math.min(bw, p.w * 0.6));
  });
  shapes.push(poly([...la, ...lb.reverse()]));
  const F = spec.fingers;
  for (let s = F.s0; s <= F.s1 + 1e-9; s += F.ds) {
    const p = S[Math.round(s * (S.length - 1))];
    const u = (s - F.s0) / (F.s1 - F.s0);
    const L = F.len * (0.5 + 0.5 * Math.sin(Math.PI * Math.min(1, u * 1.05))) * (0.7 + 0.6 * R());
    shapes.push(tendril(p, L, F.width ?? 0.42, F.lean ?? 0.6, F.turn ?? -4.2, spec.foam.w * 0.35));
  }
  const ink = E(g, "g", { fill: PAL.ai, stroke: PAL.ai, "stroke-width": lw * 2, "stroke-linejoin": "round" });
  const paper = E(g, "g", { fill: PAL.washi });
  for (const d of shapes) {
    E(ink, "path", { d });
    E(paper, "path", { d });
  }
  // a few ai lines across the lip, like the cutter's gouge marks
  for (const st of spec.foamLines ?? []) {
    const seg = S.filter((p) => p.s >= st.s0 && p.s <= st.s1);
    E(g, "path", {
      d: line(seg.map((p) => at(p, st.d))),
      fill: "none",
      stroke: PAL.ai,
      "stroke-width": lw * 0.8,
      "stroke-linecap": "round",
    });
  }
}

/* A foam finger rooted at p: it leaves along the normal, leans forward along the tangent and
   tightens into a curl (its heading turns by `turn` radians, quadratically, like a whiplash). */
function tendril(p, L, width, lean, turn, root = 6) {
  const th0 = Math.atan2(p.ny + p.ty * lean, p.nx + p.tx * lean);
  const N = 48;
  const W0 = L * width;
  let x = p.x - p.nx * root;
  let y = p.y - p.ny * root;
  const left = [];
  const right = [];
  for (let i = 0; i <= N; i++) {
    const u = i / N;
    const th = th0 + turn * u * u * u;
    const w = Math.max(1.4, W0 * Math.pow(1 - u, 0.9)) / 2;
    const px = -Math.sin(th);
    const py = Math.cos(th);
    left.push([x + px * w, y + py * w]);
    right.push([x - px * w, y - py * w]);
    x += (Math.cos(th) * L) / N;
    y += (Math.sin(th) * L) / N;
  }
  return poly([...left, ...right.reverse()]);
}

/* The red seal: 鍵 ("key") cut white into a vermilion square, with a slightly worn edge. */
function seal(g, { x, y, size, glyph = "鍵", rot = -2, id = "seal" }) {
  const defs = E(g, "defs");
  const f = E(defs, "filter", { id: `${id}-wear`, x: "-10%", y: "-10%", width: "120%", height: "120%" });
  E(f, "feTurbulence", { type: "fractalNoise", baseFrequency: 0.9 / (size / 60), numOctaves: 2, seed: 4, result: "n" });
  E(f, "feDisplacementMap", { in: "SourceGraphic", in2: "n", scale: size * 0.035 });
  const grp = E(g, "g", {
    transform: `rotate(${rot} ${x + size / 2} ${y + size / 2})`,
    filter: `url(#${id}-wear)`,
  });
  E(grp, "rect", { x, y, width: size, height: size, rx: size * 0.07, fill: PAL.shu });
  E(grp, "rect", {
    x: x + size * 0.07,
    y: y + size * 0.07,
    width: size * 0.86,
    height: size * 0.86,
    rx: size * 0.04,
    fill: "none",
    stroke: PAL.washi,
    "stroke-width": size * 0.03,
  });
  E(
    grp,
    "text",
    {
      x: x + size / 2,
      y: y + size * 0.53,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      "font-family": "Shippori Mincho B1",
      "font-weight": 800,
      "font-size": size * 0.7,
      fill: PAL.washi,
    },
    glyph,
  );
  return grp;
}

/* The Art Nouveau frame: a gold outer rule that loops at each corner, and a thin inner rule
   with concave corners that steps around the loops. */
function frame(g, { w, h, inset = 14, gap = 7, color = PAL.kin, scale = 1 }) {
  const grp = E(g, "g", { fill: "none", stroke: color, "stroke-linecap": "round" });
  const k = scale;
  const o = inset;
  const i = inset + gap * k;
  const m = 170 * k;
  for (const [x1, y1, x2, y2] of [
    [o + m, o, w - o - m, o],
    [o + m, h - o, w - o - m, h - o],
    [o, o + m, o, h - o - m],
    [w - o, o + m, w - o, h - o - m],
  ]) {
    E(grp, "line", { x1, y1, x2, y2, "stroke-width": 2.2 * k });
  }
  E(grp, "path", { d: innerRule({ w, h, inset, gap, scale }), "stroke-width": 1.1 * k });
  for (const [cx, cy, sx, sy] of [
    [0, 0, 1, 1],
    [w, 0, -1, 1],
    [0, h, 1, -1],
    [w, h, -1, -1],
  ]) {
    const c = E(grp, "g", { transform: `translate(${cx} ${cy}) scale(${sx} ${sy})` });
    corner(c, o, k, color);
  }
  return grp;
}

/* The inner rule of the frame, with concave corners; pages clip their art to it. */
function innerRule({ w, h, inset = 14, gap = 7, scale = 1, shrink = 0 }) {
  const i = inset + gap * scale + shrink;
  const cut = 84 * scale - shrink;
  return (
    `M${i + cut} ${i} L${w - i - cut} ${i} A${cut} ${cut} 0 0 0 ${w - i} ${i + cut} ` +
    `L${w - i} ${h - i - cut} A${cut} ${cut} 0 0 0 ${w - i - cut} ${h - i} ` +
    `L${i + cut} ${h - i} A${cut} ${cut} 0 0 0 ${i} ${h - i - cut} ` +
    `L${i} ${i + cut} A${cut} ${cut} 0 0 0 ${i + cut} ${i} Z`
  );
}

/* One top-left corner, drawn in a box from (0, 0): the outer rule loops once around the corner
   like a whiplash ribbon, symmetric about the diagonal, with a gold bead in the loop. */
function corner(g, o, k, color) {
  const s = (v) => fmt(v * k + o);
  const P = (pts) => pts.map(([x, y]) => `${s(x)} ${s(y)}`).join(" ");
  E(g, "path", {
    d:
      `M${P([[170, 0]])} C${P([[112, 0], [70, 6], [54, 32]])} ` +
      `C${P([[44, 48], [30, 50], [20, 40]])} C${P([[12, 32], [6, 16], [4, 4]])} ` +
      `C${P([[16, 6], [32, 12], [40, 20]])} C${P([[50, 30], [48, 44], [32, 54]])} ` +
      `C${P([[6, 70], [0, 112], [0, 170]])}`,
    "stroke-width": 2.2 * k,
  });
  E(g, "path", {
    d: `M${P([[34, 34]])} Q${P([[22, 26], [14, 14]])} Q${P([[26, 22], [34, 34]])} Z`,
    fill: color,
    stroke: "none",
  });
  // a tendril springing from the loop, ending in a coil, one per side
  for (const flip of [false, true]) {
    const pts = [
      [52, 30],
      [82, 22],
      [104, 34],
      [126, 30],
      [140, 27],
      [146, 36],
      [139, 40],
      [133, 43],
      [130, 36],
      [135, 34],
    ].map(([x, y]) => (flip ? [y, x] : [x, y]));
    E(g, "path", {
      d: `M${P([pts[0]])} C${P(pts.slice(1, 4))} C${P(pts.slice(4, 7))} C${P(pts.slice(7, 10))}`,
      "stroke-width": 1.4 * k,
    });
  }
}

/* The sun: a flat vermilion disc. */
function sun(g, { cx, cy, r }) {
  return E(g, "circle", { cx, cy, r, fill: PAL.shu });
}

/* The LeaseDesk wave, in hero coordinates (1280 x 440): it rises at the right edge and breaks
   to the left. Other pages place it with a transform. */
function houseWave(g) {
  greatWave(g, {
    pts: [
      [1310, 452, 330],
      [1246, 370, 292],
      [1184, 290, 246],
      [1112, 214, 196],
      [1036, 152, 150],
      [956, 112, 112],
      [878, 104, 88],
      [810, 124, 70],
      [758, 164, 54],
      [732, 214, 40],
      [736, 260, 26],
      [758, 290, 13],
      [784, 300, 0],
    ],
    stripes: [
      { f: 0.16, h: 3, s0: 0.12, s1: 0.5 },
      { f: 0.3, h: 6, s0: 0.0, s1: 0.64 },
      { f: 0.45, h: 8, s0: 0.0, s1: 0.76 },
      { f: 0.6, h: 7, s0: 0.0, s1: 0.72 },
      { f: 0.74, h: 6, s0: 0.04, s1: 0.62 },
      { f: 0.86, h: 4, s0: 0.1, s1: 0.52 },
      { f: 0.94, h: 2, s0: 0.14, s1: 0.48, fill: "#EDE3CC" },
    ],
    spray: { s0: 0.44, s1: 0.86, ds: 0.04, len: 32, depth: 50, lean: 0.7, turn: -4.6 },
    foam: { s0: 0.36, w: 20 },
    foamLines: [{ s0: 0.38, s1: 0.62, d: 7 }],
    fingers: { s0: 0.4, s1: 0.96, ds: 0.046, len: 74, width: 0.36, lean: 0.75, turn: -2.7 },
    line: 2,
    seed: 11,
  });
}
