// Reels Cutter Web: finds good moments in a long video and exports vertical clips, all inside the browser.
import {
  Input, Output, Conversion, BlobSource, BufferTarget, Mp4OutputFormat, ALL_FORMATS,
  AudioBufferSink, CanvasSink, QUALITY_HIGH, canEncodeVideo, canEncodeAudio, VideoSample,
} from './vendor/mediabunny.min.mjs';
import * as F from './features.js';

const $ = (id) => document.getElementById(id);
const BIN = 0.5;                       // seconds per loudness bin

const state = {
  file: null, input: null, vt: null, at: null, duration: 0,
  db: [], cuts: [], clips: [], busy: false, stop: false, conversion: null,
  vcodec: 'avc', acodec: 'aac',
  transcript: null, brand: F.loadBrand(), logoImg: null, ai: false,
};

// ---------- helpers ----------
const fmt = (s) => {
  s = Math.max(0, s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = Math.floor(s % 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
};
const even = (n) => Math.max(2, Math.round(n / 2) * 2);
const pct = (arr, p) => {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.floor(p * (s.length - 1))))];
};
const clamp01 = (x) => Math.max(0, Math.min(1, x));
const setStatus = (t) => { $('status').textContent = t; };
const setProg = (f) => { $('prog').style.width = `${Math.round(clamp01(f) * 100)}%`; };
const settings = () => ({
  len: +$('len').value,
  count: Math.max(1, Math.min(30, +$('count').value || 5)),
  framing: $('framing').value,
  res: +$('res').value,
  mode: $('mode').value,
  skip: Math.max(0, +$('skip').value || 0),
  caps: $('caps').value,
  lang: $('lang').value,
  capLang: $('capLang').value,
  hook: $('optHook').checked,
  emoji: $('optEmoji').checked,
  zoom: $('optZoom').checked,
  jump: $('optJump').checked,
  logo: $('optLogo').checked,
});

// ---------- templates ----------
const TEMPLATES = {
  podcast: { len: 45, count: 5, framing: 'crop', mode: 'viral', caps: 'bold', jump: true, zoom: true, emoji: false, hook: true },
  talking: { len: 30, count: 6, framing: 'crop', mode: 'viral', caps: 'pop', jump: true, zoom: true, emoji: true, hook: true },
  gaming: { len: 30, count: 5, framing: 'crop', mode: 'smart', caps: 'clean', jump: false, zoom: false, emoji: true, hook: true },
  vlog: { len: 15, count: 8, framing: 'crop', mode: 'smart', caps: 'clean', jump: false, zoom: true, emoji: true, hook: true },
};
function applyTemplate() {
  const t = TEMPLATES[$('tpl').value];
  if (!t) return;
  $('len').value = t.len; $('count').value = t.count; $('framing').value = t.framing;
  $('mode').value = t.mode; $('caps').value = t.caps;
  $('optJump').checked = t.jump; $('optZoom').checked = t.zoom; $('optEmoji').checked = t.emoji; $('optHook').checked = t.hook;
  renderClips();
}

// ---------- speech recognition (Whisper in a worker) ----------
let worker = null, jobId = 0;
const pending = new Map();
function getWorker() {
  if (worker) return worker;
  worker = new Worker(new URL('./asr-worker.js', import.meta.url), { type: 'module' });
  worker.onmessage = (ev) => {
    const m = ev.data;
    if (m.type === 'loading') setStatus(`Downloading the speech model (first time only)… ${Math.round(m.progress * 100)}%`);
    else if (m.type === 'loaded') { /* model ready */ }
    else if (pending.has(m.id)) {
      const { res, rej } = pending.get(m.id); pending.delete(m.id);
      m.type === 'result' ? res(m) : rej(new Error(m.message));
    }
  };
  worker.onerror = (e) => { for (const { rej } of pending.values()) rej(new Error(e.message || 'speech worker failed')); pending.clear(); worker = null; };
  return worker;
}

// Mono 16 kHz audio for [start, end) seconds of the source.
async function clipAudio16k(start, end) {
  const sink = new AudioBufferSink(state.at);
  const chunks = []; let sr = 48000, n = 0;
  for await (const { buffer, timestamp } of sink.buffers(start, end)) {
    sr = buffer.sampleRate;
    const a = buffer.getChannelData(0), b = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
    const from = Math.max(0, Math.round((start - timestamp) * sr));
    const to = Math.min(buffer.length, Math.round((end - timestamp) * sr));
    if (to <= from) continue;
    const m = new Float32Array(to - from);
    for (let i = from; i < to; i++) m[i - from] = b ? (a[i] + b[i]) * 0.5 : a[i];
    chunks.push(m); n += m.length;
  }
  const mono = new Float32Array(n); let o = 0;
  for (const c of chunks) { mono.set(c, o); o += c.length; }
  if (!n) return new Float32Array(0);
  if (sr === 16000) return mono;
  const len = Math.max(1, Math.round(n * 16000 / sr));
  const ctx = new OfflineAudioContext(1, len, 16000);
  const buf = ctx.createBuffer(1, n, sr); buf.copyToChannel(mono, 0);
  const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination); src.start();
  return (await ctx.startRendering()).getChannelData(0);
}

async function transcribeClip(c, lang) {
  const audio = await clipAudio16k(c.start, c.end);
  if (audio.length < 1600) return [];
  const id = ++jobId;
  const p = new Promise((res, rej) => pending.set(id, { res, rej }));
  getWorker().postMessage({ id, audio, language: lang }, [audio.buffer]);
  const r = await p;
  return r.words;
}

// Transcribe [from, to) of the source in 60-second pieces; word times are absolute (source timeline).
async function transcribeRange(from, to, lang, onProg) {
  const words = [];
  const STEP = 60;
  for (let t = from; t < to; t += STEP) {
    if (state.stop) throw new Error('stopped');
    const e = Math.min(to, t + STEP + 1);       // 1 s overlap so words on the boundary aren't lost
    const audio = await clipAudio16k(t, e);
    if (audio.length >= 1600) {
      const id = ++jobId;
      const p = new Promise((res, rej) => pending.set(id, { res, rej }));
      getWorker().postMessage({ id, audio, language: lang }, [audio.buffer]);
      const r = await p;
      for (const w of r.words) {
        const ws = w.start + t, we = w.end + t;
        if (words.length && ws < words[words.length - 1].end - 0.05) continue;   // overlap duplicate
        words.push({ text: w.text, start: ws, end: we });
      }
    }
    onProg?.(Math.min(1, (e - from) / (to - from)));
  }
  return words;
}
const clipWords = (c) => (state.transcript || [])
  .filter((w) => w.start >= c.start - 0.05 && w.end <= c.end + 0.3)
  .map((w) => ({ text: w.text, start: w.start - c.start, end: w.end - c.start }));

// ---------- caption drawing ----------
let FONT = F.FONTS.system;
let ACCENT = '#FFE14D';
function groupWords(words, maxWords) {
  const groups = []; let cur = [];
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    cur.push(w);
    const gap = words[i + 1] ? words[i + 1].start - w.end : 99;
    if (cur.length >= maxWords || gap > 0.6 || /[.!?,]$/.test(w.text)) { groups.push(cur); cur = []; }
  }
  if (cur.length) groups.push(cur);
  return groups.map((g, i) => ({ words: g, start: g[0].start, end: Math.max(g[g.length - 1].end, g[0].start + 0.4) }));
}
function wrapLines(ctx, items, maxW) {
  const lines = [[]]; let w = 0;
  const space = ctx.measureText(' ').width;
  for (const it of items) {
    const ww = ctx.measureText(it.text).width;
    if (lines[lines.length - 1].length && w + space + ww > maxW) { lines.push([]); w = 0; }
    const line = lines[lines.length - 1];
    w += (line.length ? space : 0) + ww; line.push({ ...it, w: ww });
  }
  return lines.map((l) => ({ items: l, width: l.reduce((a, x) => a + x.w, 0) + space * (l.length - 1) }));
}
function drawCaption(ctx, W, H, groups, t, style) {
  let g = groups.find((x, i) => t >= x.start - 0.05 && t < (groups[i + 1] ? Math.min(groups[i + 1].start, x.end + 0.6) : x.end + 0.6));
  if (!g) return;
  if (g.translated) {           // translated line: no per-word timing, so show the whole line
    style = style === 'pop' ? 'bold' : style;
    g = { ...g, words: g.translated.split(/\s+/).map((text) => ({ text, start: -1, end: -1 })) };
  }
  ctx.save();
  ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  if (style === 'pop') {
    const w = g.words.slice().reverse().find((x) => t >= x.start - 0.05) || g.words[0];
    const age = Math.max(0, t - w.start);
    const scale = 0.82 + 0.18 * Math.min(1, age / 0.12);
    let size = Math.round(W * 0.115);
    const text = w.text.replace(/^[,.!?]+|[,.]+$/g, '').toUpperCase();
    ctx.font = `900 ${size}px ${FONT}`;
    const tw = ctx.measureText(text).width;
    if (tw > W * 0.88) { size = Math.floor(size * W * 0.88 / tw); ctx.font = `900 ${size}px ${FONT}`; }
    ctx.translate(W / 2, H * 0.62); ctx.scale(scale, scale);
    ctx.textAlign = 'center';
    ctx.lineWidth = size * 0.16; ctx.strokeStyle = '#000'; ctx.strokeText(text, 0, 0);
    ctx.fillStyle = '#fff'; ctx.fillText(text, 0, 0);
  } else if (style === 'bold') {
    const size = Math.round(W * 0.085);
    ctx.font = `900 ${size}px ${FONT}`;
    const items = g.words.map((w) => ({ text: w.text.toUpperCase(), on: t >= w.start - 0.05 && t < w.end + 0.05 }));
    const lines = wrapLines(ctx, items, W * 0.86);
    const lh = size * 1.18, y0 = H * 0.68 - (lines.length - 1) * lh / 2;
    const space = ctx.measureText(' ').width;
    ctx.textAlign = 'left';
    lines.forEach((ln, li) => {
      let x = (W - ln.width) / 2;
      for (const it of ln.items) {
        ctx.lineWidth = size * 0.17; ctx.strokeStyle = '#000'; ctx.strokeText(it.text, x, y0 + li * lh);
        ctx.fillStyle = it.on ? ACCENT : '#fff'; ctx.fillText(it.text, x, y0 + li * lh);
        x += it.w + space;
      }
    });
  } else {
    const size = Math.round(W * 0.058);
    ctx.font = `650 ${size}px ${FONT}`;
    const lines = wrapLines(ctx, g.words.map((w) => ({ text: w.text })), W * 0.82);
    const lh = size * 1.3, pad = size * 0.45;
    const boxW = Math.max(...lines.map((l) => l.width)) + pad * 2, boxH = lines.length * lh + pad * 1.2;
    const bx = (W - boxW) / 2, by = H * 0.8 - boxH / 2;
    ctx.fillStyle = 'rgba(0,0,0,0.62)';
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(bx, by, boxW, boxH, size * 0.4) : ctx.rect(bx, by, boxW, boxH); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
    lines.forEach((ln, li) => ctx.fillText(ln.items.map((x) => x.text).join(' '), W / 2, by + pad * 0.6 + lh * (li + 0.5)));
  }
  ctx.restore();
}

function setBusy(b) {
  state.busy = b;
  $('find').disabled = b;
  $('exportAll').disabled = b || !state.clips.some((c) => c.on);
  $('cancel').classList.toggle('hidden', !b);
  $('file').disabled = b;
}

// ---------- startup checks ----------
async function checkSupport() {
  if (typeof VideoEncoder === 'undefined' || typeof VideoDecoder === 'undefined') {
    $('unsupported').classList.remove('hidden');
    return false;
  }
  try {
    if (!(await canEncodeVideo('avc', { width: 720, height: 1280 }))) state.vcodec = 'vp9';
    if (!(await canEncodeAudio('aac'))) state.acodec = 'opus';
  } catch { /* keep defaults; the conversion reports problems itself */ }
  return true;
}

// ---------- loading ----------
async function loadFile(file) {
  if (!file || state.busy) return;
  state.file = file;
  state.clips = []; state.db = []; state.cuts = []; state.transcript = null;
  $('repOut').innerHTML = '';
  renderClips();
  $('results').classList.add('hidden');
  $('work').classList.remove('hidden');
  setProg(0);
  setStatus('Reading the video…');
  const player = $('player');
  if (player.src) URL.revokeObjectURL(player.src);
  player.src = URL.createObjectURL(file);
  try {
    state.input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
    state.vt = await state.input.getPrimaryVideoTrack();
    state.at = await state.input.getPrimaryAudioTrack();
    if (!state.vt) throw new Error('This file has no video track.');
    if (!(await state.vt.canDecode())) throw new Error('Your browser can’t decode this video’s format. Try an MP4 (H.264) file, or use the Mac app.');
    state.duration = await state.input.computeDuration();
    const w = state.vt.displayWidth, h = state.vt.displayHeight;
    $('meta').innerHTML = '';
    for (const t of [file.name, fmt(state.duration), `${w} × ${h}`, `${(file.size / 1e6).toFixed(0)} MB`,
      state.at ? 'with sound' : 'no sound']) {
      const s = document.createElement('span'); s.textContent = t; $('meta').appendChild(s);
    }
    drawWave();
    setStatus('Ready. Choose your settings and press “Find clips”.');
  } catch (e) {
    setStatus(e.message || String(e));
    state.input = null;
  }
}

// ---------- analysis ----------
async function scanAudio(onProg) {
  const nb = Math.ceil(state.duration / BIN) + 1;
  const sum = new Float64Array(nb), cnt = new Float64Array(nb);
  const sink = new AudioBufferSink(state.at);
  for await (const { buffer, timestamp } of sink.buffers()) {
    if (state.stop) throw new Error('stopped');
    const sr = buffer.sampleRate, n = buffer.length, chans = buffer.numberOfChannels;
    const c0 = buffer.getChannelData(0), c1 = chans > 1 ? buffer.getChannelData(1) : null;
    let i = 0;
    while (i < n) {
      const t = timestamp + i / sr;
      const b = Math.floor(t / BIN);
      const end = Math.min(n, Math.ceil(((b + 1) * BIN - timestamp) * sr));
      let s = 0;
      for (let k = i; k < end; k++) {
        const v = c1 ? (c0[k] + c1[k]) * 0.5 : c0[k];
        s += v * v;
      }
      if (b >= 0 && b < nb) { sum[b] += s; cnt[b] += end - i; }
      i = Math.max(end, i + 1);
    }
    onProg(clamp01(timestamp / state.duration));
  }
  const db = [];
  for (let b = 0; b < nb; b++) db.push(cnt[b] ? 10 * Math.log10(sum[b] / cnt[b] + 1e-10) : -100);
  return db;
}

async function scanScenes(onProg) {
  const step = state.duration > 3600 ? 3 : state.duration > 1200 ? 2 : 1;
  const times = [];
  for (let t = 0; t < state.duration; t += step) times.push(t);
  const sink = new CanvasSink(state.vt, { width: 48, height: 27, fit: 'fill', poolSize: 2 });
  const diffs = [];
  let prev = null;
  for await (const wc of sink.canvasesAtTimestamps(times)) {
    if (state.stop) throw new Error('stopped');
    if (!wc) continue;
    const ctx = wc.canvas.getContext('2d', { willReadFrequently: true });
    const px = ctx.getImageData(0, 0, 48, 27).data;
    const gray = new Float32Array(48 * 27);
    for (let i = 0; i < gray.length; i++) gray[i] = (px[i * 4] * 0.3 + px[i * 4 + 1] * 0.59 + px[i * 4 + 2] * 0.11);
    if (prev) {
      let d = 0;
      for (let i = 0; i < gray.length; i++) d += Math.abs(gray[i] - prev[i]);
      diffs.push({ t: wc.timestamp, d: d / gray.length });
    }
    prev = gray;
    onProg(clamp01(wc.timestamp / state.duration));
  }
  const med = pct(diffs.map((x) => x.d), 0.5);
  return diffs.filter((x) => x.d > Math.max(25, med * 3)).map((x) => x.t);
}

function scoreWindows(s) {
  const L = s.len, db = state.db, dur = state.duration;
  const lo = s.skip, hi = dur - s.skip - L;
  if (hi < lo) return [];
  const has = db.length > 0;
  const valid = has ? db.filter((x) => x > -90) : [];
  const p10 = pct(valid, 0.1), p95 = pct(valid, 0.95);
  const span = Math.max(6, p95 - p10);
  const quiet = p10 + Math.min(6, span * 0.25);
  const cuts = state.cuts;
  const bin = (t) => Math.max(0, Math.min(db.length - 1, Math.floor(t / BIN)));
  const out = [];
  for (let st = lo; st <= hi; st += 1) {
    let loud = 0, hook = 0, silent = 0, n = 0, sq = 0;
    if (has) {
      const b0 = bin(st), b1 = bin(st + L);
      for (let b = b0; b < b1; b++) {
        const v = clamp01((db[b] - p10) / span);
        loud += v; sq += v * v; n++;
        if (db[b] < quiet) silent++;
        if (b < b0 + 3 / BIN) hook += v;
      }
    }
    n = Math.max(1, n);
    const mean = loud / n;
    const varn = Math.sqrt(Math.max(0, sq / n - mean * mean));
    const hookN = hook / (3 / BIN);
    const silFrac = silent / n;
    const nCuts = cuts.filter((c) => c > st && c < st + L).length;
    const sceneScore = cuts.length ? clamp01(nCuts / Math.max(1, L / 8)) : 0;
    const w = has ? (cuts.length ? [0.4, 0.15, 0.15, 0.15, 0.15] : [0.45, 0.2, 0.2, 0, 0.15])
      : [0, 0, 0, 1, 0];
    const score = w[0] * mean + w[1] * clamp01(varn * 3) + w[2] * hookN + w[3] * sceneScore + w[4] * (1 - silFrac);
    out.push({ start: st, score, mean, hookN, silFrac, nCuts });
  }
  return out;
}

// Move a start time onto a natural boundary: a scene cut or a pause, within 2 seconds.
function snapStart(t, s) {
  const lo = Math.max(s.skip, t - 2), hi = Math.min(state.duration - s.skip - s.len, t + 2);
  const cut = state.cuts.find((c) => c >= lo && c <= hi);
  if (cut !== undefined) return { t: cut, why: 'starts on a scene change' };
  if (state.db.length) {
    let best = t, bv = Infinity;
    for (let x = lo; x <= hi; x += BIN) {
      const v = state.db[Math.floor(x / BIN)] ?? 0;
      if (v < bv) { bv = v; best = x; }
    }
    if (best !== t) return { t: best, why: 'starts on a pause' };
  }
  return { t, why: '' };
}

function pickClips(s) {
  const L = s.len;
  const dur = state.duration;
  const usable = dur - 2 * s.skip;
  if (usable < L) {
    const st = Math.max(0, (dur - L) / 2);
    return [{ start: st, end: Math.min(dur, st + L), score: 50, why: 'video is shorter than the clip length' }];
  }
  if (s.mode === 'even' || (!state.db.length && !state.cuts.length)) {
    const n = Math.min(s.count, Math.floor(usable / L));
    const gap = (usable - n * L) / (n + 1);
    return Array.from({ length: n }, (_, i) => {
      const st = s.skip + gap * (i + 1) + L * i;
      return { start: st, end: st + L, score: null, why: 'evenly spaced' };
    });
  }
  const wins = scoreWindows(s).sort((a, b) => b.score - a.score);
  const top = wins.length ? wins[0].score : 1;
  const chosen = [];
  for (const w of wins) {
    if (chosen.length >= s.count) break;
    const sn = snapStart(w.start, s);
    const st = sn.t;
    if (chosen.some((c) => st < c.end + 2 && st + L > c.start - 2)) continue;
    const why = [];
    if (w.mean > 0.6) why.push('loud, lively audio'); else if (w.mean > 0.4) why.push('steady sound');
    if (w.hookN > 0.65) why.push('strong opening');
    if (w.silFrac < 0.08 && state.db.length) why.push('no dead air');
    if (w.nCuts >= 2) why.push(`${w.nCuts} scene changes`);
    if (sn.why) why.push(sn.why);
    chosen.push({ start: st, end: st + L, score: Math.round(100 * w.score / (top || 1)), why: why.join(' · ') || 'best remaining moment' });
  }
  return chosen.sort((a, b) => a.start - b.start);
}

async function F_transcribeAll(s, onProg) {
  return transcribeRange(Math.max(0, s.skip - 1), Math.min(state.duration, state.duration - s.skip + 1), s.lang, onProg);
}

async function findClips() {
  if (!state.input || state.busy) return;
  const s = settings();
  state.stop = false;
  setBusy(true);
  try {
    if (s.mode !== 'even') {
      if (state.at && !state.db.length) {
        setStatus('Listening to the audio…');
        state.db = await scanAudio((f) => setProg(f * (s.mode === 'smart' ? 0.5 : 1)));
      }
      if (s.mode === 'smart' && !state.cuts.length) {
        setStatus('Looking for scene changes…');
        state.cuts = await scanScenes((f) => setProg(0.5 + f * 0.5));
      }
    }
    let picked;
    if (s.mode === 'viral') {
      if (!state.at) throw new Error('This video has no sound, so there is nothing to read. Pick “Best moments” instead.');
      if (!state.transcript) {
        setStatus('Listening to everything that’s said… (this takes a while for long videos)');
        state.transcript = await F_transcribeAll(s, (f) => { setProg(f); setStatus(`Listening to everything that’s said… ${Math.round(f * 100)}%`); });
      }
      const cands = F.transcriptCandidates(state.transcript, { len: s.len, skip: s.skip, duration: state.duration, db: state.db, binSec: BIN });
      if (!cands.length) throw new Error('Not enough speech found for that clip length. Try a shorter length or “Best moments”.');
      let pool = F.pickNonOverlapping(cands, Math.min(30, s.count * 3));
      const ai = await F.aiRank(pool, s.count, setStatus);
      if (ai) {
        const rated = new Map(ai.ratings.map((r) => [r.id, r]));
        pool = pool.map((c, i) => ({ ...c, ai: rated.get(i), score: 0.35 * c.score + 0.65 * ((rated.get(i)?.score ?? 0) / 10) }));
        for (const b of ai.best || []) if (pool[b.id]) Object.assign(pool[b.id], { hook: b.hook, caption: b.caption, hashtags: b.hashtags });
      }
      const best = F.pickNonOverlapping(pool, s.count);
      const top = Math.max(...best.map((c) => c.score), 0.01);
      picked = best.sort((a, b) => a.start - b.start).map((c) => ({
        start: Math.max(0, c.start - 0.15), end: Math.min(state.duration, c.end + 0.35),
        score: Math.round(100 * c.score / top),
        why: (c.ai ? `AI ${c.ai.score}/10: ${c.ai.reason || ''} · ` : '') + c.why,
        hook: c.hook || null, caption: c.caption || null, hashtags: c.hashtags || null,
      }));
    } else {
      picked = pickClips(s);
    }
    state.clips = picked.map((c, i) => ({ ...c, n: i + 1, on: true, pos: 0.5, url: null, err: null }));
    setProg(1);
    setStatus(state.clips.length ? `Found ${state.clips.length} clip${state.clips.length > 1 ? 's' : ''}. Untick any you don’t want, then press “Make selected clips”.`
      : 'No clips fit. Try a shorter clip length or a smaller skip.');
    renderClips();
    drawWave();
  } catch (e) {
    setStatus(e.message === 'stopped' ? 'Stopped.' : `Couldn’t scan the video: ${e.message || e}`);
  } finally {
    setBusy(false);
  }
}

// ---------- exporting ----------
function cropFor(pos) {
  const W = state.vt.displayWidth, H = state.vt.displayHeight;
  let cw = even(H * 9 / 16), ch = even(H);
  if (cw > W) { cw = even(W); ch = Math.min(even(W * 16 / 9), even(H)); }
  const left = even((W - cw) * pos), top = even((H - ch) / 2);
  return { left: Math.min(left, W - cw), top: Math.max(0, top), width: cw, height: ch };
}

async function ensureWords(c, s) {
  if (c.words) return c.words;
  if (state.transcript) { c.words = clipWords(c); return c.words; }
  c.prog = 0; c.phase = 'Listening'; updateClipRow(c);
  c.words = await transcribeClip(c, s.lang);
  c.phase = null;
  return c.words;
}

async function exportClip(c, s) {
  const outW = s.res === 1080 ? 1080 : 720, outH = s.res === 1080 ? 1920 : 1280;
  const L = c.end - c.start;
  const output = new Output({ format: new Mp4OutputFormat({ fastStart: 'in-memory' }), target: new BufferTarget() });
  const video = { width: outW, height: outH, codec: state.vcodec, quality: QUALITY_HIGH, forceTranscode: true, frameRate: 30 };
  if (s.framing === 'crop') { video.crop = cropFor(c.pos); video.fit = 'cover'; } else { video.fit = 'contain'; }

  const needWords = state.at && (s.caps !== 'none' || s.hook || s.zoom || s.jump || s.emoji);
  const words = needWords ? await ensureWords(c, s) : [];
  FONT = F.FONTS[state.brand.font] || F.FONTS.system;
  ACCENT = state.brand.accent || '#FFE14D';

  // Hook title: AI-written when available, otherwise from the first sentence.
  if (s.hook && !c.hook && words.length) {
    const w = await F.aiWrite([words.map((x) => x.text).join(' ')]);
    if (w?.[0]) Object.assign(c, { hook: w[0].hook, caption: c.caption || w[0].caption, hashtags: c.hashtags || w[0].hashtags });
    else c.hook = F.heuristicHook(words.map((x) => x.text).join(' '));
    renderClips();
  }

  // Captions (optionally translated, per line).
  let groups = [];
  if (s.caps !== 'none' && words.length) {
    groups = groupWords(words, s.caps === 'bold' ? 3 : s.caps === 'clean' ? 6 : 1);
    if (s.capLang !== 'same') {
      const lineGroups = groupWords(words, 6);
      const tr = await F.aiTranslate(lineGroups.map((g) => g.words.map((w) => w.text).join(' ')), s.capLang);
      if (tr) { groups = lineGroups.map((g, i) => ({ ...g, translated: tr[i] })); c.note = `Captions translated to ${s.capLang}.`; }
      else c.note = 'Translation needs AI, which works when this page is opened in Claude. Captions kept in the spoken language.';
    }
  }
  const marks = s.zoom ? F.zoomPlan(words, (t) => state.db[Math.floor((c.start + t) / BIN)] ?? -60) : [];
  let segs = s.jump && words.length ? F.keepSegments(words, L) : null;
  if (segs && F.keptLength(segs) > L - 0.4) segs = null;               // nothing worth cutting
  const emojiWords = s.emoji ? words.map((w) => ({ ...w, e: F.emojiFor(w.text) })).filter((w) => w.e) : [];
  const logo = s.logo && (state.logoImg || state.brand.handle);

  const draws = groups.length || s.hook || marks.length || segs || emojiWords.length || logo;
  if (draws) {
    const canvas = new OffscreenCanvas(outW, outH);
    const ctx = canvas.getContext('2d');
    let t0 = null;
    video.process = (sample) => {
      if (t0 === null) t0 = sample.timestamp >= c.start - 0.5 ? c.start : 0;   // input or output timeline
      const t = sample.timestamp - t0;                                          // seconds into the clip
      const tOut = segs ? F.mapTime(t, segs) : t;
      if (tOut === null) return null;                                           // inside a removed pause
      const z = marks.length ? F.zoomAt(t, marks) : 1;
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, outW, outH);
      sample.draw(ctx, -(z - 1) * outW / 2, -(z - 1) * outH * 0.42, outW * z, outH * z);
      if (groups.length) drawCaption(ctx, outW, outH, groups, t, s.caps);
      if (emojiWords.length) {
        const ew = emojiWords.filter((w) => t >= w.start && t < w.start + 1.2).pop();
        if (ew) F.drawEmoji(ctx, outW, outH, ew.e, t - ew.start, outH * (s.caps === 'clean' ? 0.66 : s.caps === 'bold' ? 0.56 : 0.5));
      }
      if (s.hook && c.hook) F.drawHook(ctx, outW, outH, c.hook, tOut, state.brand);
      if (logo) F.drawLogo(ctx, outW, outH, state.logoImg, state.brand.logoPos || 'tr', state.brand.handle);
      return segs ? new VideoSample(canvas, { timestamp: t0 + tOut, duration: sample.duration }) : canvas;
    };
    video.processedWidth = outW; video.processedHeight = outH;
  }
  const audio = { codec: state.acodec, bitrate: 160000, numberOfChannels: 2, sampleRate: 48000 };
  if (segs) {
    let ta0 = null;
    audio.process = (sample) => {
      if (ta0 === null) ta0 = sample.timestamp >= c.start - 0.5 ? c.start : 0;
      const t = sample.timestamp - ta0, d = sample.duration, sr = sample.sampleRate;
      const out = [];
      for (const [a, b] of segs) {
        const from = Math.max(a, t), to = Math.min(b, t + d);
        if (to - from < 1 / sr) continue;
        const f0 = Math.round((from - t) * sr), f1 = Math.min(sample.numberOfFrames, Math.round((to - t) * sr));
        if (f1 <= f0) continue;
        const piece = sample.trim(f0, f1);
        piece.setTimestamp(ta0 + F.mapTime(from, segs));
        out.push(piece);
      }
      return out.length ? out : null;
    };
  }
  const conv = await Conversion.init({
    input: state.input, output, tracks: 'primary', trim: { start: c.start, end: c.end },
    video, audio, showWarnings: false,
  });
  if (!conv.isValid) {
    throw new Error('this video can’t be converted in the browser (' + conv.discardedTracks.map((d) => d.reason).join(', ') + ')');
  }
  state.conversion = conv;
  conv.onProgress = (p) => { c.prog = p; updateClipRow(c); };
  await conv.execute();
  state.conversion = null;
  const blob = new Blob([output.target.buffer], { type: 'video/mp4' });
  if (c.url) URL.revokeObjectURL(c.url);
  c.url = URL.createObjectURL(blob);
  c.blob = blob;
  c.size = blob.size;
  c.outLen = segs ? F.keptLength(segs) : L;
}

async function exportSelected() {
  if (state.busy) return;
  const s = settings();
  const todo = state.clips.filter((c) => c.on);
  if (!todo.length) return;
  state.stop = false;
  setBusy(true);
  let done = 0, failed = 0;
  for (const c of todo) {
    if (state.stop) break;
    c.err = null; c.prog = 0; updateClipRow(c);
    setStatus(`Making clip ${c.n} of ${state.clips.length}…`);
    try {
      await exportClip(c, s);
      done++;
    } catch (e) {
      state.conversion = null;
      if (state.stop) break;
      c.err = e.message || String(e);
      failed++;
    }
    c.prog = null;
    updateClipRow(c);
    setProg((done + failed) / todo.length);
  }
  setBusy(false);
  setStatus(state.stop ? `Stopped. ${done} clip(s) ready.`
    : `${done} clip${done === 1 ? '' : 's'} ready${failed ? `, ${failed} failed` : ''}. Press “Download” next to each one.`
      + (state.vcodec !== 'avc' ? ' Note: this browser can’t make H.264, so clips use VP9. Instagram may re-encode them.' : ''));
}

// ---------- UI ----------
const base = () => (state.file ? state.file.name.replace(/\.[^.]+$/, '').replace(/[^\w\- ]+/g, '').slice(0, 40) : 'clip');

function renderClips() {
  const box = $('clips');
  box.innerHTML = '';
  $('results').classList.toggle('hidden', !state.clips.length);
  for (const c of state.clips) {
    const row = document.createElement('div');
    row.className = 'clip';
    row.id = `clip${c.n}`;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = c.on; cb.id = `on${c.n}`;
    cb.setAttribute('aria-label', `Include clip ${c.n}`);
    cb.onchange = () => { c.on = cb.checked; setBusy(state.busy); drawWave(); };
    const mid = document.createElement('div');
    mid.style.minWidth = '0';
    const t = document.createElement('button');
    t.className = 'btn sm'; t.style.marginRight = '8px';
    t.textContent = `${fmt(c.start)} – ${fmt(c.end)}`;
    t.title = 'Preview this part';
    t.onclick = () => { const p = $('player'); p.currentTime = c.start; p.play().catch(() => {}); };
    const sc = document.createElement('span');
    if (c.score !== null) { sc.className = 'score' + (c.score < 70 ? ' mid' : ''); sc.textContent = c.score; sc.title = 'Score compared with the best moment in this video'; }
    const why = document.createElement('div'); why.className = 'why'; why.textContent = c.why;
    const out = document.createElement('div'); out.className = 'out small';
    const hk = document.createElement('div'); hk.className = 'hook';
    const hl = document.createElement('span'); hl.className = 'small muted'; hl.textContent = 'Hook';
    const hi = document.createElement('input'); hi.type = 'text'; hi.id = `hook${c.n}`; hi.maxLength = 60;
    hi.placeholder = 'Written for you when the clip is made'; hi.value = c.hook || '';
    hi.setAttribute('aria-label', `Hook title for clip ${c.n}`);
    hi.oninput = () => { c.hook = hi.value.trim() || null; };
    hk.append(hl, hi);
    mid.append(t, sc, why, hk, out);
    const ctl = document.createElement('div'); ctl.className = 'ctl';
    if ($('framing').value === 'crop') {
      const lab = document.createElement('label'); lab.className = 'small muted'; lab.textContent = 'Frame ';
      const r = document.createElement('input'); r.type = 'range'; r.min = 0; r.max = 1; r.step = 0.05; r.value = c.pos; r.id = `pos${c.n}`;
      r.setAttribute('aria-label', `Horizontal framing for clip ${c.n}`);
      r.oninput = () => { c.pos = +r.value; };
      lab.appendChild(r); ctl.appendChild(lab);
    }
    row.append(cb, mid, ctl);
    box.appendChild(row);
    updateClipRow(c);
  }
}

function updateClipRow(c) {
  const row = $(`clip${c.n}`);
  if (!row) return;
  const out = row.querySelector('.out');
  out.innerHTML = '';
  if (c.prog !== null && c.prog !== undefined) {
    out.textContent = c.phase ? `${c.phase} for captions…` : `Making… ${Math.round(c.prog * 100)}%`;
  } else if (c.err) {
    const e = document.createElement('span'); e.className = 'err'; e.textContent = `Failed: ${c.err}`; out.appendChild(e);
  } else if (c.url) {
    const a = document.createElement('a');
    a.href = c.url;
    a.download = `${base()}_clip${String(c.n).padStart(2, '0')}_${fmt(c.start).replace(/:/g, 'm')}s.mp4`;
    a.textContent = `Download (${(c.size / 1e6).toFixed(1)} MB)`;
    a.onclick = async (ev) => {
      if (!state.dl) return;            // normal browser download
      ev.preventDefault();
      try { await state.dl.save({ filename: a.download, data: c.blob }); }
      catch (e) { if (e?.code !== 'declined') setStatus(`Couldn’t save the clip (${e?.code || e}). Try the Mac app or open this page in Chrome.`); }
    };
    const v = document.createElement('a');
    v.href = '#'; v.textContent = 'Watch'; v.style.marginLeft = '12px';
    v.onclick = (ev) => { ev.preventDefault(); const p = $('player'); p.dataset.orig = p.dataset.orig || p.src; p.src = c.url; p.play().catch(() => {}); };
    out.append(a, v);
  }
  if (c.caption || c.hashtags) {
    const post = document.createElement('div'); post.className = 'post';
    const txt = `${c.caption || ''} ${(c.hashtags || []).join(' ')}`.trim();
    post.textContent = `Post text: ${txt} `;
    const cp = document.createElement('a'); cp.href = '#'; cp.textContent = 'Copy';
    cp.onclick = async (ev) => {
      ev.preventDefault();
      try { await navigator.clipboard.writeText(txt); cp.textContent = 'Copied'; }
      catch { const r = document.createRange(); r.selectNodeContents(post); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }
    };
    post.appendChild(cp); out.appendChild(post);
  }
  if (c.note) { const n = document.createElement('div'); n.className = 'post'; n.textContent = c.note; out.appendChild(n); }
  if (c.url && c.outLen && c.outLen < c.end - c.start - 0.4) {
    const n = document.createElement('div'); n.className = 'post';
    n.textContent = `Jump cuts removed ${(c.end - c.start - c.outLen).toFixed(1)} s of pauses (${fmt(c.outLen)} long).`;
    out.appendChild(n);
  }
  if (c.words && c.words.length) {
    const said = document.createElement('div'); said.className = 'said';
    said.textContent = '“' + c.words.map((w) => w.text).join(' ').slice(0, 220) + (c.words.length > 40 ? '…”' : '”');
    out.appendChild(said);
  }
}

function drawWave() {
  const cv = $('wave'), ctx = cv.getContext('2d');
  const W = cv.width = Math.max(300, Math.round(cv.clientWidth * (window.devicePixelRatio || 1)));
  const H = cv.height = Math.round(70 * (window.devicePixelRatio || 1));
  ctx.clearRect(0, 0, W, H);
  const dur = state.duration || 1;
  for (const c of state.clips) {
    if (!c.on) continue;
    ctx.fillStyle = 'rgba(255,79,109,0.22)';
    ctx.fillRect(c.start / dur * W, 0, Math.max(2, (c.end - c.start) / dur * W), H);
  }
  const db = state.db;
  if (db.length) {
    const valid = db.filter((x) => x > -90);
    const lo = pct(valid, 0.05), hi = pct(valid, 0.99);
    ctx.fillStyle = '#8a93a6';
    const per = db.length / W;
    for (let x = 0; x < W; x++) {
      let m = -100;
      for (let b = Math.floor(x * per); b < Math.floor((x + 1) * per) || b === Math.floor(x * per); b++) m = Math.max(m, db[b] ?? -100);
      const h = clamp01((m - lo) / Math.max(1, hi - lo)) * (H - 6);
      ctx.fillRect(x, (H - h) / 2, 1, Math.max(1, h));
    }
  } else {
    ctx.fillStyle = '#8a93a6';
    ctx.font = `${12 * (window.devicePixelRatio || 1)}px -apple-system,sans-serif`;
    ctx.fillText(state.input ? 'Loudness appears here after “Find clips”.' : '', 10, H / 2 + 4);
  }
  ctx.fillStyle = '#ff8a5c';
  for (const t of state.cuts) ctx.fillRect(t / dur * W, H - 5, 2, 5);
}

// ---------- wiring ----------
const drop = $('drop');
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => loadFile(e.dataTransfer.files[0]));
$('file').addEventListener('change', (e) => loadFile(e.target.files[0]));
$('find').addEventListener('click', findClips);
$('exportAll').addEventListener('click', exportSelected);
$('cancel').addEventListener('click', () => { state.stop = true; state.conversion?.cancel(); });
$('framing').addEventListener('change', renderClips);
$('tpl').addEventListener('change', applyTemplate);
for (const id of ['len', 'count', 'framing', 'mode', 'caps']) $(id).addEventListener('input', () => { $('tpl').value = 'custom'; });
$('lang').addEventListener('change', () => { state.transcript = null; state.clips.forEach((c) => { c.words = null; }); });
for (const id of ['optJump', 'optZoom', 'optEmoji', 'optHook']) $(id).addEventListener('change', () => { $('tpl').value = 'custom'; });

// ---------- brand kit ----------
function showBrand() {
  const b = state.brand;
  $('accent').value = b.accent; $('font').value = b.font; $('logoPos').value = b.logoPos; $('handle').value = b.handle || '';
  $('logoPreview').innerHTML = '';
  if (b.logo) { const im = document.createElement('img'); im.src = b.logo; im.alt = 'Your logo'; $('logoPreview').appendChild(im); }
  else $('logoPreview').textContent = 'No logo yet';
  state.logoImg = null;
  if (b.logo) { const im = new Image(); im.onload = () => { state.logoImg = im; }; im.src = b.logo; }
}
function brandChanged() {
  Object.assign(state.brand, { accent: $('accent').value, font: $('font').value, logoPos: $('logoPos').value, handle: $('handle').value.trim() });
  F.saveBrand(state.brand);
}
for (const id of ['accent', 'font', 'logoPos', 'handle']) $(id).addEventListener('input', brandChanged);
$('logoFile').addEventListener('change', (e) => {
  const f = e.target.files[0]; if (!f) return;
  if (f.size > 1.5e6) { setStatus('That logo is over 1.5 MB. Please use a smaller PNG.'); return; }
  const r = new FileReader();
  r.onload = () => { state.brand.logo = r.result; F.saveBrand(state.brand); showBrand(); };
  r.readAsDataURL(f);
});
$('logoClear').addEventListener('click', () => { state.brand.logo = null; F.saveBrand(state.brand); showBrand(); });
showBrand();

// ---------- repurpose: carousel, thread, description ----------
function slideCanvas(sl, i, total) {
  const W = 1080, H = 1350, cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d'); const b = state.brand; const font = F.FONTS[b.font] || F.FONTS.system;
  const dark = i === 0 || i === total - 1;
  ctx.fillStyle = dark ? '#111318' : '#f6f4ef'; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = b.accent; ctx.fillRect(80, 120, 120, 14);
  const wrap = (text, size, maxW) => { ctx.font = size; const out = []; let cur = ''; for (const w of String(text).split(/\s+/)) { const n = cur ? cur + ' ' + w : w; if (ctx.measureText(n).width > maxW && cur) { out.push(cur); cur = w; } else cur = n; } if (cur) out.push(cur); return out; };
  ctx.textBaseline = 'top'; ctx.fillStyle = dark ? '#fff' : '#111318';
  const tsize = i === 0 ? 96 : 72;
  const tl = wrap(sl.title || '', `900 ${tsize}px ${font}`, W - 160);
  tl.forEach((l, k) => ctx.fillText(l, 80, 180 + k * tsize * 1.12));
  ctx.font = `500 44px ${F.FONTS.system}`; ctx.fillStyle = dark ? '#c9ccd4' : '#3a3d45';
  const bl = wrap(sl.body || '', `500 44px ${F.FONTS.system}`, W - 160);
  bl.forEach((l, k) => ctx.fillText(l, 80, 220 + tl.length * tsize * 1.12 + k * 62));
  ctx.font = `700 34px ${F.FONTS.system}`; ctx.fillStyle = dark ? '#8a93a6' : '#6b6f78';
  ctx.fillText(`${i + 1} / ${total}`, 80, H - 110);
  if (b.handle) { ctx.textAlign = 'right'; ctx.fillText(b.handle, W - 80, H - 110); ctx.textAlign = 'left'; }
  if (state.logoImg) { const h = 90, w = h * state.logoImg.width / state.logoImg.height; ctx.drawImage(state.logoImg, W - 80 - w, 100, w, h); }
  return cv;
}
async function saveFile(name, data) {
  if (state.dl) { try { await state.dl.save({ filename: name, data }); } catch (e) { if (e?.code !== 'declined') setStatus(`Couldn’t save ${name} (${e?.code || e}).`); } return; }
  const a = document.createElement('a'); a.href = URL.createObjectURL(data instanceof Blob ? data : new Blob([data])); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
$('repGo').addEventListener('click', async () => {
  if (!state.input || state.busy) { setStatus('Choose a video first.'); return; }
  if (!state.at) { setStatus('This video has no sound, so there is nothing to turn into posts.'); return; }
  const s = settings(); state.stop = false; setBusy(true);
  try {
    if (!state.transcript) state.transcript = await F_transcribeAll(s, (f) => { setProg(f); setStatus(`Listening to everything that’s said… ${Math.round(f * 100)}%`); });
    setStatus('Writing posts…');
    const text = state.transcript.map((w) => w.text).join(' ');
    const r = (await F.aiRepurpose(text, base())) || F.heuristicRepurpose(state.transcript, null);
    const out = $('repOut'); out.innerHTML = '';
    const slides = (r.carousel || []).slice(0, 10);
    const h1 = document.createElement('h3'); h1.textContent = `Carousel (${slides.length} slides, 1080 × 1350)`;
    const row = document.createElement('div'); row.className = 'slides';
    const canv = slides.map((sl, i) => slideCanvas(sl, i, slides.length));
    canv.forEach((cv) => row.appendChild(cv));
    const sv = document.createElement('button'); sv.className = 'btn sm'; sv.textContent = 'Save slides as PNG';
    sv.onclick = async () => { for (let i = 0; i < canv.length; i++) { const blob = await new Promise((res) => canv[i].toBlob(res, 'image/png')); await saveFile(`${base()}_slide${i + 1}.png`, blob); } };
    const box = (title, value) => { const d = document.createElement('div'); const h = document.createElement('h3'); h.textContent = title; const ta = document.createElement('textarea'); ta.value = value; const b = document.createElement('button'); b.className = 'btn sm'; b.textContent = 'Copy'; b.onclick = async () => { try { await navigator.clipboard.writeText(ta.value); b.textContent = 'Copied'; } catch { ta.select(); } }; d.append(h, ta, b); return d; };
    out.append(h1, row, sv,
      box('X thread', (r.thread || []).map((t, i) => `${i + 1}/ ${t}`).join('\n\n')),
      box('YouTube description', r.description || ''));
    setStatus(await F.aiAvailable() ? 'Posts written by AI from the transcript. Check them before posting.' : 'Posts made from the strongest sentences. Open this page in Claude for AI-written posts.');
    setProg(1);
  } catch (e) {
    setStatus(e.message === 'stopped' ? 'Stopped.' : `Couldn’t make posts: ${e.message || e}`);
  } finally { setBusy(false); }
});
F.aiAvailable().then((ok) => {
  state.ai = ok;
  $('aiState').textContent = ok ? 'AI is on: it ranks moments and writes hooks, captions and translations.'
    : 'AI is off here: moments are ranked by rules. Open the Claude version of this page to turn on AI ranking, hooks and translation.';
});
window.addEventListener('resize', drawWave);

// When this page runs inside a claude.ai artifact, downloads go through the viewer's save prompt.
if (window.claude?.use) window.claude.use('downloads').then((d) => { state.dl = d; }).catch(() => {});
applyTemplate();
checkSupport().then((ok) => { if (!ok) $('drop').classList.add('hidden'); });
window.__reels = state;  // handy for debugging in the console
