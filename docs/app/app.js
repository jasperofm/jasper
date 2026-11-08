// Reels Cutter Web: finds good moments in a long video and exports vertical clips, all inside the browser.
import {
  Input, Output, Conversion, BlobSource, BufferTarget, Mp4OutputFormat, ALL_FORMATS,
  AudioBufferSink, CanvasSink, QUALITY_HIGH, canEncodeVideo, canEncodeAudio,
} from './vendor/mediabunny.min.mjs';

const $ = (id) => document.getElementById(id);
const BIN = 0.5;                       // seconds per loudness bin

const state = {
  file: null, input: null, vt: null, at: null, duration: 0,
  db: [], cuts: [], clips: [], busy: false, stop: false, conversion: null,
  vcodec: 'avc', acodec: 'aac',
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
});

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
  state.clips = []; state.db = []; state.cuts = [];
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
    state.clips = pickClips(s).map((c, i) => ({ ...c, n: i + 1, on: true, pos: 0.5, url: null, err: null }));
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

async function exportClip(c, s) {
  const outW = s.res === 1080 ? 1080 : 720, outH = s.res === 1080 ? 1920 : 1280;
  const output = new Output({ format: new Mp4OutputFormat({ fastStart: 'in-memory' }), target: new BufferTarget() });
  const video = { width: outW, height: outH, codec: state.vcodec, quality: QUALITY_HIGH, forceTranscode: true, frameRate: 30 };
  if (s.framing === 'crop') { video.crop = cropFor(c.pos); video.fit = 'cover'; } else { video.fit = 'contain'; }
  const conv = await Conversion.init({
    input: state.input, output, tracks: 'primary', trim: { start: c.start, end: c.end },
    video, audio: { codec: state.acodec, bitrate: 160000, numberOfChannels: 2, sampleRate: 48000 }, showWarnings: false,
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
    mid.append(t, sc, why, out);
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
    out.textContent = `Making… ${Math.round(c.prog * 100)}%`;
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
window.addEventListener('resize', drawWave);

// When this page runs inside a claude.ai artifact, downloads go through the viewer's save prompt.
if (window.claude?.use) window.claude.use('downloads').then((d) => { state.dl = d; }).catch(() => {});
checkSupport().then((ok) => { if (!ok) $('drop').classList.add('hidden'); });
window.__reels = state;  // handy for debugging in the console
