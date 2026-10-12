// Live preview of an edit spec in the browser: the same layers and geometry as app/editor/render.py
// (background → banner under → shadow → clip → border → banner over → texts → subtitles), drawn on a canvas.
// The export is still done by FFmpeg; this only has to look the same while you drag things around.
const CW = 1080, CH = 1920;
const even = v => Math.max(2, Math.round(v / 2) * 2);
const rgb = hex => { const h = (hex || '#000000').replace('#', ''); return [0, 2, 4, 6].map(i => parseInt(h.slice(i, i + 2), 16)).map((v, i) => i === 3 ? (isNaN(v) ? 255 : v) : v); };
const css = hex => { const [r, g, b, a] = rgb(hex); return `rgba(${r},${g},${b},${a / 255})`; };

// Fonts as Pillow uses them (app/editor/subs.py and texts in render.py).
const FONT_FAMILY = { regular: 'bold {px}px Arial, sans-serif', heavy: '{px}px "Arial Black", Arial, sans-serif',
  impact: '{px}px Impact, "Arial Black", sans-serif', rounded: '{px}px "Arial Rounded MT Bold", Arial, sans-serif' };
const fontCss = (kind, px) => (FONT_FAMILY[kind] || FONT_FAMILY.regular).replace('{px}', px);

function geometry(S, sw, sh) {
  const v = S.video, k = (v.fit === 'cover' ? Math.max : Math.min)(v.w / sw, v.h / sh) * v.zoom;
  const SW = even(sw * k), SH = even(sh * k), cw = even(Math.min(SW, v.w)), ch = even(Math.min(SH, v.h));
  return { sw: SW, sh: SH, cw, ch, x: v.x + Math.floor((v.w - cw) / 2), y: v.y + Math.floor((v.h - ch) / 2) };
}
function clipDuration(S, dur) {
  const t = S.time, end = Math.min(t.trim_end || dur, dur), used = Math.max(0.1, end - t.trim_start);
  return [used, used / t.speed];
}
const totalDuration = (S, dur) => S.time.start_delay + clipDuration(S, dur)[1];
// result time → source time of the clip (null before the clip starts)
function sourceTime(S, dur, t) {
  if (t < S.time.start_delay) return null;
  const [used] = clipDuration(S, dur);
  return S.time.trim_start + Math.min(used - 0.03, (t - S.time.start_delay) * S.time.speed);
}

function shapePath(ctx, x, y, w, h, shape, r) {
  ctx.beginPath();
  if (shape === 'circle') { ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2); return; }
  if (shape !== 'rounded' || r <= 0) { ctx.rect(x, y, w, h); return; }
  r = Math.min(r, w / 2, h / 2);
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

// FFmpeg boxblur (mirrored edges), applied `power` times per direction, on RGB of an ImageData
function boxBlur(img, r, power) {
  const { width: w, height: h, data } = img;
  if (r < 1) return;
  const pass = (len, count, idx) => {
    const line = new Float32Array(len * 3), out = new Float32Array(len * 3);
    for (let n = 0; n < count; n++) {
      for (let i = 0; i < len; i++) { const p = idx(n, i); line[i * 3] = data[p]; line[i * 3 + 1] = data[p + 1]; line[i * 3 + 2] = data[p + 2]; }
      for (let rep = 0; rep < power; rep++) {
        const m = i => (i < 0 ? -i : i >= len ? 2 * (len - 1) - i : i);
        for (let c = 0; c < 3; c++) {
          let sum = 0;
          for (let i = -r; i <= r; i++) sum += line[m(i) * 3 + c];
          for (let i = 0; i < len; i++) {
            out[i * 3 + c] = sum / (2 * r + 1);
            sum += line[m(i + r + 1) * 3 + c] - line[m(i - r) * 3 + c];
          }
        }
        line.set(out);
      }
      for (let i = 0; i < len; i++) { const p = idx(n, i); data[p] = line[i * 3]; data[p + 1] = line[i * 3 + 1]; data[p + 2] = line[i * 3 + 2]; }
    }
  };
  r = Math.min(r, Math.floor(Math.min(w, h) / 2) - 1);
  pass(w, h, (y, x) => (y * w + x) * 4);
  pass(h, w, (x, y) => (y * w + x) * 4);
}

function gradientCanvas(c1, c2, angle) {
  const n = 64, cv = document.createElement('canvas'); cv.width = cv.height = n;
  const ctx = cv.getContext('2d'), im = ctx.createImageData(n, n), a = angle * Math.PI / 180;
  const dx = Math.sin(a), dy = -Math.cos(a), p1 = rgb(c1), p2 = rgb(c2), norm = (Math.abs(dx) * .5 + Math.abs(dy) * .5) || 1;
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    let t = ((x / (n - 1) - .5) * dx + (y / (n - 1) - .5) * dy) / norm * .5 + .5;
    t = Math.max(0, Math.min(1, t));
    const o = (y * n + x) * 4;
    for (let i = 0; i < 3; i++) im.data[o + i] = Math.floor(p1[i] + (p2[i] - p1[i]) * t);
    im.data[o + 3] = 255;
  }
  ctx.putImageData(im, 0, 0);
  return cv;
}

// FFmpeg colorkey: alpha = clip((distance − similarity) / blend), distance = |rgb − key| / (255·√3)
function chromaKey(source, w, h, chroma) {
  const cv = document.createElement('canvas'); cv.width = Math.max(1, Math.round(w)); cv.height = Math.max(1, Math.round(h));
  const ctx = cv.getContext('2d');
  ctx.drawImage(source, 0, 0, cv.width, cv.height);
  if (!chroma || !chroma.enabled) return cv;
  const im = ctx.getImageData(0, 0, cv.width, cv.height), d = im.data, [kr, kg, kb] = rgb(chroma.color);
  const sim = chroma.similarity, ib = chroma.blend > 0.0001 ? 1 / chroma.blend : null, den = 255 * 255 * 3;
  for (let i = 0; i < d.length; i += 4) {
    const r = d[i] - kr, g = d[i + 1] - kg, b = d[i + 2] - kb, diff = Math.sqrt((r * r + g * g + b * b) / den);
    const a = ib ? Math.max(0, Math.min(1, (diff - sim) * ib)) : (diff > sim ? 1 : 0);
    d[i + 3] = Math.round(d[i + 3] * a);
  }
  ctx.putImageData(im, 0, 0);
  return cv;
}

function wrapWords(ctx, text, maxW) {
  const lines = [];
  for (const para of text.split('\n')) {
    let cur = '';
    for (const w of para.split(/\s+/).filter(Boolean)) {
      const trial = (cur + ' ' + w).trim();
      if (ctx.measureText(trial).width <= maxW || !cur) cur = trial; else { lines.push(cur); cur = w; }
    }
    lines.push(cur);
  }
  return lines;
}

// app/editor/subs.py wrap(): greedy by characters, long words are cut
function wrapWords2(words, n) {
  const lines = []; let cur = [];
  for (let w of words) {
    while (w.length > n) { if (cur.length) { lines.push(cur); cur = []; } lines.push([w.slice(0, n)]); w = w.slice(n); }
    if (!w) continue;
    if (!cur.length || [...cur, w].join(' ').length <= n) cur.push(w); else { lines.push(cur); cur = [w]; }
  }
  if (cur.length) lines.push(cur);
  return lines;
}

// app/editor/subs.py parts(): phrase → screens of up to 2 lines; each word gets a share of time by length
function subtitleParts(cues, S, dur) {
  const t = S.time, clipLen = clipDuration(S, dur)[1], out = [], weight = ch => ch.flat().reduce((a, w) => a + w.length + 1, 0);
  for (const c of cues || []) {
    const s = (c.start - t.trim_start) / t.speed, e = (c.end - t.trim_start) / t.speed;
    if (e <= 0 || s >= clipLen) continue;
    const start = Math.max(0, s) + t.start_delay, end = Math.min(e, clipLen) + t.start_delay;
    const lines = wrapWords2(c.text.split(/\s+/).filter(Boolean), S.subtitles.max_chars); if (!lines.length) continue;
    const chunks = []; for (let i = 0; i < lines.length; i += 2) chunks.push(lines.slice(i, i + 2));
    const total = chunks.reduce((a, ch) => a + weight(ch), 0) || 1;
    let at = start;
    for (const ch of chunks) {
      const d = (end - start) * weight(ch) / total, words = ch.flat(), wt = [], wtotal = weight(ch) || 1;
      let wat = at; for (const w of words) { const wd = d * (w.length + 1) / wtotal; wt.push([wat, wat + wd]); wat += wd; }
      out.push({ start: at, end: at + d, lines: ch, words: wt }); at += d;
    }
  }
  return out;
}

class Composer {
  // media: { src: <video>, srcW, srcH, duration, assets: {id: {el, kind, width, height}} , cues }
  constructor(canvas, media) {
    this.cv = canvas; this.ctx = canvas.getContext('2d'); this.m = media;
    this.small = document.createElement('canvas');
    this.keyCache = new Map(); this.gradCache = new Map();
  }
  asset(id) { const a = id && this.m.assets[id]; return a && a.el && (a.kind === 'image' ? a.el.complete && a.el.naturalWidth : a.el.readyState >= 2) ? a : null; }

  draw(S, t) {
    const ctx = this.ctx, k = this.cv.width / CW, m = this.m, src = m.src;
    const srcReady = src && src.readyState >= 2;
    ctx.save(); ctx.setTransform(k, 0, 0, k, 0, 0); ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, CW, CH);
    this.background(S, srcReady);
    const ban = S.banner, ba = this.asset(ban.asset);
    if (ba && ban.layer === 'under') this.banner(S, ba);
    if (srcReady && t >= S.time.start_delay) this.clip(S, t);
    if (ba && ban.layer === 'over') this.banner(S, ba);
    this.texts(S);
    if (S.subtitles.enabled) this.subtitles(S, t);
    ctx.restore();
  }

  background(S, srcReady) {
    const ctx = this.ctx, bg = S.background;
    if (bg.type === 'blur') {
      if (!srcReady) return;
      const src = this.m.src, sw = this.m.srcW, sh = this.m.srcH;
      const big = this.cv.width >= 400, w = big ? 270 : 90, h = big ? 480 : 160, f = w / 270;  // FFmpeg blurs at a quarter of 1080×1920
      const sm = this.small; sm.width = w; sm.height = h;
      const sctx = sm.getContext('2d', { willReadFrequently: true });
      const kk = Math.max(w / sw, h / sh);
      sctx.drawImage(src, (sw - w / kk) / 2, (sh - h / kk) / 2, w / kk, h / kk, 0, 0, w, h);
      const im = sctx.getImageData(0, 0, w, h);
      boxBlur(im, Math.round(bg.blur * f), 2);
      const dark = bg.darken * 256 * 1.164;  // eq brightness works on luma (limited range)
      for (let i = 0; i < im.data.length; i += 4) { im.data[i] -= dark; im.data[i + 1] -= dark; im.data[i + 2] -= dark; }
      sctx.putImageData(im, 0, 0);
      ctx.drawImage(sm, 0, 0, CW, CH);
    } else if (bg.type === 'color') {
      ctx.fillStyle = css(bg.color); ctx.fillRect(0, 0, CW, CH);
    } else if (bg.type === 'gradient') {
      const key = bg.color + bg.color2 + bg.angle;
      if (!this.gradCache.has(key)) { this.gradCache.clear(); this.gradCache.set(key, gradientCanvas(bg.color, bg.color2, bg.angle)); }
      ctx.drawImage(this.gradCache.get(key), 0, 0, CW, CH);
    } else {
      const a = this.asset(bg.asset); if (!a) return;
      const aw = a.width || a.el.videoWidth || a.el.naturalWidth, ah = a.height || a.el.videoHeight || a.el.naturalHeight;
      const kk = Math.max(CW / aw, CH / ah);
      ctx.drawImage(a.el, (aw - CW / kk) / 2, (ah - CH / kk) / 2, CW / kk, CH / kk, 0, 0, CW, CH);
    }
  }

  banner(S, a) {
    const b = S.banner, aw = a.width || a.el.videoWidth || a.el.naturalWidth, ah = a.height || a.el.videoHeight || a.el.naturalHeight;
    const bw = even(CW * b.scale), bh = even(bw * ah / aw), k = this.cv.width / CW;
    let img;
    if (a.kind === 'image') {
      const key = [b.asset, b.chroma.enabled, b.chroma.color, b.chroma.similarity, b.chroma.blend, bw, this.cv.width].join('|');
      img = this.keyCache.get(key);
      if (!img) { if (this.keyCache.size > 12) this.keyCache.clear(); img = chromaKey(a.el, bw * k, bh * k, b.chroma); this.keyCache.set(key, img); }
    } else img = chromaKey(a.el, bw * k, bh * k, b.chroma);
    this.ctx.drawImage(img, b.x, b.y, bw, bh);
  }

  clip(S, t) {
    const ctx = this.ctx, v = S.video, tm = S.time, src = this.m.src, sw = this.m.srcW, sh = this.m.srcH;
    const g = geometry(S, sw, sh), ad = tm.appear_duration, since = t - tm.start_delay;
    let alpha = 1, dy = 0;
    if (tm.appear === 'fade') alpha = Math.max(0, Math.min(1, since / ad));
    if (tm.appear === 'slide_up' && since < ad) dy = (1 - since / ad) * Math.floor(CH * 0.35);
    const x = g.x, y = g.y + dy, kx = g.sw / sw, ky = g.sh / sh;
    ctx.save(); ctx.globalAlpha = alpha;
    if (v.shadow) {
      const off = Math.max(8, Math.floor(g.cw / 40));
      ctx.save(); ctx.shadowColor = 'rgba(0,0,0,0.59)'; ctx.shadowBlur = 2 * Math.max(10, Math.floor(g.cw / 25)) * this.cv.width / CW;
      ctx.shadowOffsetX = (10000 + off) * this.cv.width / CW; ctx.shadowOffsetY = off * 2 * this.cv.width / CW;
      shapePath(ctx, x - 10000, y, g.cw, g.ch, v.shape, v.radius); ctx.fillStyle = '#000'; ctx.fill(); ctx.restore();
    }
    ctx.save();
    if (v.shape !== 'rect') { shapePath(ctx, x, y, g.cw, g.ch, v.shape, v.radius); ctx.clip(); }
    ctx.drawImage(src, (g.sw - g.cw) / 2 / kx, (g.sh - g.ch) / 2 / ky, g.cw / kx, g.ch / ky, x, y, g.cw, g.ch);
    ctx.restore();
    if (v.border_width) {
      shapePath(ctx, x, y, g.cw, g.ch, v.shape, v.radius);
      ctx.lineWidth = v.border_width; ctx.strokeStyle = css(v.border_color); ctx.stroke();
    }
    ctx.restore();
  }

  texts(S) {
    const ctx = this.ctx;
    for (const t of S.texts) {
      if (!t.text.trim() && !t.box) continue;  // an empty plate is still drawn (as in render.py)
      ctx.font = fontCss(t.font, t.size);
      const lines = wrapWords(ctx, t.text, t.w), lineH = Math.floor(t.size * 1.2), blockH = lineH * lines.length;
      if (t.box) {
        const pad = Math.floor(t.size / 3), widest = t.text.trim() ? Math.max(...lines.map(l => ctx.measureText(l).width)) : t.w;
        const x0 = t.align === 'left' ? t.x : t.align === 'right' ? t.x + t.w - widest : t.x + (t.w - widest) / 2;
        ctx.fillStyle = css(t.box_color);
        shapePath(ctx, x0 - pad, t.y - pad, widest + 2 * pad, blockH + pad + Math.floor(pad / 2), 'rounded', pad); ctx.fill();
      }
      const stroke = t.box ? 0 : Math.max(2, Math.floor(t.size / 14));
      ctx.textAlign = t.align === 'left' ? 'left' : t.align === 'right' ? 'right' : 'center'; ctx.textBaseline = 'middle';
      const ax = t.align === 'left' ? t.x : t.align === 'right' ? t.x + t.w : t.x + t.w / 2;
      lines.forEach((l, i) => {
        const y = t.y + i * lineH + lineH / 2;
        if (stroke) { ctx.lineJoin = 'round'; ctx.lineWidth = stroke * 2; ctx.strokeStyle = '#000'; ctx.strokeText(l, ax, y); }
        ctx.fillStyle = css(t.color); ctx.fillText(l, ax, y);
      });
    }
  }

  subtitles(S, t) {
    const key = JSON.stringify([S.time, S.subtitles.max_chars, this.m.cues.length]);
    if (this._subsKey !== key) { this._subsKey = key; this._subs = subtitleParts(this.m.cues, S, this.m.duration); }
    const cur = this._subs.find(p => t >= p.start && t <= p.end); if (!cur) return;
    drawSubtitle(this.ctx, S.subtitles, cur.lines, S.subtitles.karaoke ? cur.words.findIndex(([a, b]) => t >= a && t <= b) : -1);
  }
}

// app/editor/subs.py render_png() + overlay position, drawn straight onto the 1080×1920 canvas
function drawSubtitle(ctx, sub, lines, highlight = -1) {
  const size = Math.floor(sub.size), stroke = sub.stroke > 0 ? Math.floor(size * sub.stroke) : 0;
  const lineH = Math.floor(size * 1.25), pad = Math.max(stroke * 2, Math.floor(size / 3));
  const height = lineH * lines.length + pad * 2, y0 = Math.max(0, Math.min(CH - height, sub.y - Math.floor(height / 2)));
  const up = s => sub.uppercase ? s.toUpperCase() : s;
  ctx.save(); ctx.font = fontCss(sub.font, size); ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  const widths = lines.map(l => ctx.measureText(up(l.join(' '))).width);
  if (sub.box) { const widest = Math.max(...widths), x0 = (CW - widest) / 2 - pad; ctx.fillStyle = css(sub.box_color); shapePath(ctx, x0, y0, CW - 2 * x0, height, 'rounded', pad); ctx.fill(); }
  let n = 0;
  lines.forEach((line, i) => {
    const x = (CW - widths[i]) / 2, y = y0 + pad + i * lineH + lineH / 2;
    line.forEach((w, j) => {
      const px = x + ctx.measureText(up(line.slice(0, j).join(' ') + (j ? ' ' : ''))).width;
      if (stroke) { ctx.lineJoin = 'round'; ctx.lineWidth = stroke * 2; ctx.strokeStyle = css(sub.stroke_color); ctx.strokeText(up(w), px, y); }
      ctx.fillStyle = css(n === highlight ? sub.highlight : sub.color); ctx.fillText(up(w), px, y); n++;
    });
  });
  ctx.restore();
  return { y0, height };
}
