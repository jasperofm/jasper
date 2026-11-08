// Clip-quality features for Reels Cutter Web: transcript-based moment finding, AI ranking and writing,
// hook titles, emojis, punch-in zooms, jump cuts, brand kit and repurposing (carousel, thread, description).

// ---------- AI (Claude through the page's `sample` capability, when this page runs as a claude.ai artifact) ----------
let samplerP = null;
export function getSampler() {
  if (!samplerP) {
    samplerP = window.claude?.use ? window.claude.use('sample').catch(() => null) : Promise.resolve(null);
  }
  return samplerP;
}
export async function aiAvailable() { return !!(await getSampler()); }
export async function aiJSON(prompt, tier = 'default') {
  const s = await getSampler();
  if (!s) return null;
  try { return await s.json(prompt, { modelTier: tier }); } catch (e) {
    if (e?.code === 'not_granted') samplerP = Promise.resolve(null);
    return null;
  }
}

// ---------- transcript helpers ----------
export function sentences(words) {
  const out = []; let cur = [];
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    cur.push(w);
    const gap = words[i + 1] ? words[i + 1].start - w.end : 99;
    if (/[.!?]["')]*$/.test(w.text) || gap > 1.2 || cur.length >= 40) {
      out.push({ words: cur, start: cur[0].start, end: cur[cur.length - 1].end, text: cur.map((x) => x.text).join(' ') });
      cur = [];
    }
  }
  if (cur.length) out.push({ words: cur, start: cur[0].start, end: cur[cur.length - 1].end, text: cur.map((x) => x.text).join(' ') });
  return out;
}

const HOOK_WORDS = /\b(you|your|why|how|what|secret|never|always|nobody|everyone|best|worst|mistake|truth|actually|crazy|insane|stop|money|free|first|biggest|only|wrong|real|need|must|tip|trick|hack|story|happened|imagine|listen)\b/i;
const EMOTION = /\b(love|hate|amazing|incredible|wow|unbelievable|shocking|funny|laugh|scared|angry|crazy|insane|wild|perfect|terrible|beautiful|huge|massive)\b/i;

// Candidate clips that start and end on sentence boundaries, scored from what is said.
export function transcriptCandidates(words, opt) {
  const { len, skip, duration, db, binSec } = opt;
  const sents = sentences(words);
  const minL = len * 0.7, maxL = len * 1.35;
  const loudAt = (a, b) => {
    if (!db?.length) return 0.5;
    const valid = db.filter((x) => x > -90);
    const s = [...valid].sort((x, y) => x - y);
    const p10 = s[Math.floor(s.length * 0.1)] ?? -60, p95 = s[Math.floor(s.length * 0.95)] ?? -10;
    let sum = 0, n = 0;
    for (let i = Math.floor(a / binSec); i < Math.floor(b / binSec); i++) { sum += Math.max(0, Math.min(1, (db[i] - p10) / Math.max(6, p95 - p10))); n++; }
    return n ? sum / n : 0.5;
  };
  const out = [];
  for (let i = 0; i < sents.length; i++) {
    const s0 = sents[i];
    if (s0.start < skip || s0.start > duration - skip - minL) continue;
    let j = i, end = s0.end;
    while (j + 1 < sents.length && sents[j + 1].end - s0.start <= maxL) { j++; end = sents[j].end; if (end - s0.start >= len) break; }
    const L = end - s0.start;
    if (L < minL) continue;
    const ws = words.filter((w) => w.start >= s0.start - 0.01 && w.end <= end + 0.01);
    const text = ws.map((w) => w.text).join(' ');
    const first = s0.text;
    const density = ws.length / L;                                   // words per second
    const hook = (HOOK_WORDS.test(first) ? 0.45 : 0) + (/\?/.test(first) ? 0.3 : 0) + (/\d/.test(first) ? 0.25 : 0);
    const emo = Math.min(1, (text.match(new RegExp(EMOTION.source, 'gi')) || []).length / 3);
    const punch = Math.min(1, (text.match(/!/g) || []).length / 2);
    const lowStart = /^(and|but|so|because|which|that|um|uh)\b/i.test(first) ? 0.25 : 0;
    let silence = 0;
    for (let k = 1; k < ws.length; k++) silence += Math.max(0, ws[k].start - ws[k - 1].end - 0.3);
    const dead = Math.min(1, silence / L * 3);
    const score = 0.3 * Math.min(1, hook) + 0.15 * emo + 0.1 * punch + 0.2 * Math.min(1, density / 2.8)
      + 0.15 * loudAt(s0.start, end) + 0.1 * (1 - dead) - lowStart;
    const why = [];
    if (hook >= 0.45) why.push('hooky opening line'); if (/\?/.test(first)) why.push('opens with a question');
    if (emo > 0.3) why.push('emotional words'); if (density > 2.4) why.push('fast, dense talking');
    if (dead < 0.1) why.push('no dead air');
    out.push({ start: s0.start, end, score, text, first, why: why.join(' · ') || 'complete thought' });
  }
  return out;
}

export function pickNonOverlapping(cands, count, gap = 2) {
  const chosen = [];
  for (const c of [...cands].sort((a, b) => b.score - a.score)) {
    if (chosen.length >= count) break;
    if (chosen.some((x) => c.start < x.end + gap && c.end > x.start - gap)) continue;
    chosen.push(c);
  }
  return chosen;
}

// Ask Claude to rank candidates for virality and write hook titles, captions and hashtags.
export async function aiRank(cands, count, onStatus) {
  if (!(await aiAvailable()) || !cands.length) return null;
  onStatus?.('Asking AI which moments will hit hardest…');
  const list = cands.slice(0, 30).map((c, i) => ({ id: i, text: c.text.slice(0, 900) }));
  const prompt = `You are a short-form video editor. Below are candidate clips (transcripts) cut from one long video.
Rate each for how likely it is to perform well as an Instagram Reel / TikTok / YouTube Short: a strong first line, a complete
thought, surprise, humor, emotion, or a useful insight. Then, for the best ${count}, write:
- "hook": an on-screen title for the first 2 seconds, max 7 words, no emojis, no clickbait lies, based only on what is said
- "caption": a post caption of 1-2 short sentences
- "hashtags": 3 to 5 relevant hashtags
Reply with JSON only: {"ratings":[{"id":0,"score":0-10,"reason":"max 8 words"}...],"best":[{"id":0,"hook":"","caption":"","hashtags":["#..."]}]}
Candidates:
${JSON.stringify(list)}`;
  const r = await aiJSON(prompt, 'default');
  if (!r || !Array.isArray(r.ratings)) return null;
  return r;
}

export async function aiWrite(clipTexts) {
  if (!(await aiAvailable())) return null;
  const prompt = `For each short video transcript below, write an on-screen hook title (max 7 words, based only on what is said, no emojis),
a 1-2 sentence post caption, and 3-5 relevant hashtags. Reply with JSON only: [{"hook":"","caption":"","hashtags":["#.."]}, ...] in the same order.
${JSON.stringify(clipTexts.map((t) => t.slice(0, 900)))}`;
  const r = await aiJSON(prompt, 'quick');
  return Array.isArray(r) ? r : null;
}

export async function aiTranslate(lines, language) {
  if (!(await aiAvailable())) return null;
  const prompt = `Translate each caption line into ${language}. Keep them short and natural for on-screen captions.
Reply with JSON only: an array of strings, same length and order.
${JSON.stringify(lines)}`;
  const r = await aiJSON(prompt, 'quick');
  return Array.isArray(r) && r.length === lines.length ? r.map(String) : null;
}

export async function aiRepurpose(transcript, title) {
  if (!(await aiAvailable())) return null;
  const prompt = `Here is the transcript of a video${title ? ` called "${title}"` : ''}. Turn it into:
1. "carousel": 6-8 Instagram carousel slides. Slide 1 is a bold title (max 8 words). Middle slides each give one key point (title max 6 words, body max 25 words). Last slide is a call to action.
2. "thread": an X thread of 5-8 posts, each under 270 characters. First post is the hook.
3. "description": a YouTube description (80-150 words) plus 5 hashtags at the end.
Only use what the transcript says. Reply with JSON only:
{"carousel":[{"title":"","body":""}],"thread":[""],"description":""}
Transcript:
${transcript.slice(0, 24000)}`;
  return aiJSON(prompt, 'default');
}

// Without AI: a hook from the first sentence, and simple repurposing from the strongest sentences.
export function heuristicHook(text) {
  const first = (text.split(/(?<=[.!?])\s/)[0] || text).replace(/^(so|and|but|um|uh|okay|ok)[, ]+/i, '');
  const w = first.split(/\s+/).slice(0, 7).join(' ').replace(/[,.;:]+$/, '');
  return w ? w.charAt(0).toUpperCase() + w.slice(1) + (first.split(/\s+/).length > 7 ? '…' : '') : '';
}
export function heuristicRepurpose(words, title) {
  const ss = sentences(words).filter((s) => s.words.length >= 6);
  const scored = ss.map((s) => ({ s, v: (HOOK_WORDS.test(s.text) ? 1 : 0) + (EMOTION.test(s.text) ? 0.6 : 0) + (/\d/.test(s.text) ? 0.5 : 0) + Math.min(1, s.words.length / 20) }))
    .sort((a, b) => b.v - a.v).slice(0, 6).sort((a, b) => a.s.start - b.s.start).map((x) => x.s.text);
  const head = title || heuristicHook(scored[0] || '');
  return {
    carousel: [{ title: head, body: '' }, ...scored.map((t, i) => ({ title: `Point ${i + 1}`, body: t.slice(0, 160) })), { title: 'Follow for more', body: 'Save this post and share it with a friend.' }],
    thread: [head, ...scored.map((t) => t.slice(0, 270))],
    description: scored.join(' ').slice(0, 900),
  };
}

// ---------- emojis on key words ----------
const EMOJI = {
  money: '💰', cash: '💵', rich: '🤑', dollar: '💵', dollars: '💵', pay: '💸', paid: '💸', price: '🏷️', free: '🆓',
  fire: '🔥', hot: '🔥', crazy: '🤯', insane: '🤯', mind: '🧠', brain: '🧠', think: '🤔', idea: '💡', ideas: '💡', secret: '🤫',
  love: '❤️', heart: '❤️', happy: '😄', funny: '😂', laugh: '😂', sad: '😢', cry: '😭', angry: '😡', scared: '😱', shocked: '😱', wow: '😮',
  win: '🏆', winner: '🏆', best: '🏆', first: '🥇', goal: '🎯', goals: '🎯', target: '🎯', success: '🚀', grow: '📈', growth: '📈', up: '📈', down: '📉',
  time: '⏰', fast: '⚡', quick: '⚡', speed: '⚡', power: '⚡', strong: '💪', gym: '💪', workout: '💪', work: '💼', business: '💼', job: '💼',
  phone: '📱', video: '🎥', camera: '📷', music: '🎵', song: '🎶', game: '🎮', games: '🎮', book: '📚', learn: '📚', school: '🎓',
  food: '🍔', eat: '🍽️', coffee: '☕', water: '💧', car: '🚗', home: '🏠', house: '🏠', world: '🌍', travel: '✈️', sun: '☀️',
  stop: '🛑', no: '🙅', yes: '✅', right: '✅', wrong: '❌', mistake: '❌', warning: '⚠️', danger: '⚠️', question: '❓', why: '❓',
  light: '💡', lighting: '💡', sound: '🔊', microphone: '🎤', mic: '🎤', window: '🪟', tip: '💡', trick: '🪄', magic: '🪄', amazing: '✨', perfect: '👌',
  people: '👥', friends: '👯', family: '👨‍👩‍👧', baby: '👶', dog: '🐶', cat: '🐱', party: '🎉', celebrate: '🎉', birthday: '🎂', gift: '🎁',
  sleep: '😴', tired: '😴', health: '🩺', doctor: '🩺', eyes: '👀', look: '👀', watch: '👀', listen: '👂', talk: '🗣️', speak: '🗣️',
};
export function emojiFor(word) {
  const k = word.toLowerCase().replace(/[^a-z']/g, '');
  return EMOJI[k] || EMOJI[k.replace(/s$/, '')] || null;
}

// ---------- brand kit (saved in this browser) ----------
const BRAND_KEY = 'reelscutter.brand.v1';
export const FONTS = {
  system: '-apple-system, "SF Pro Display", "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  impact: 'Impact, "Arial Narrow Bold", "Helvetica Neue Condensed Black", sans-serif',
  rounded: '"Arial Rounded MT Bold", "SF Pro Rounded", "Nunito", "Varela Round", Helvetica, sans-serif',
  serif: 'Georgia, "Times New Roman", serif',
  mono: '"SF Mono", Menlo, Consolas, monospace',
};
export function loadBrand() {
  try { return { accent: '#FFE14D', font: 'system', logoPos: 'tr', logo: null, handle: '', ...(JSON.parse(localStorage.getItem(BRAND_KEY)) || {}) }; }
  catch { return { accent: '#FFE14D', font: 'system', logoPos: 'tr', logo: null, handle: '' }; }
}
export function saveBrand(b) { try { localStorage.setItem(BRAND_KEY, JSON.stringify(b)); } catch { /* storage unavailable: kept for this visit only */ } }

// ---------- drawing ----------
export function drawHook(ctx, W, H, text, t, brand) {
  if (!text || t > 2.6) return;
  const a = t < 0.15 ? t / 0.15 : t > 2.3 ? Math.max(0, (2.6 - t) / 0.3) : 1;
  const font = FONTS[brand.font] || FONTS.system;
  let size = Math.round(W * 0.07);
  ctx.save();
  ctx.globalAlpha = a;
  ctx.font = `900 ${size}px ${font}`;
  const words = text.split(/\s+/); const lines = []; let cur = '';
  for (const w of words) { const n = cur ? cur + ' ' + w : w; if (ctx.measureText(n).width > W * 0.8 && cur) { lines.push(cur); cur = w; } else cur = n; }
  if (cur) lines.push(cur);
  const lh = size * 1.2, pad = size * 0.45;
  const bw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + pad * 2, bh = lines.length * lh + pad;
  const x = (W - bw) / 2, y = H * 0.14;
  ctx.fillStyle = brand.accent || '#FFE14D';
  ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, bw, bh, size * 0.3) : ctx.rect(x, y, bw, bh); ctx.fill();
  ctx.fillStyle = contrastText(brand.accent || '#FFE14D');
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  lines.forEach((l, i) => ctx.fillText(l, W / 2, y + pad / 2 + lh * (i + 0.5)));
  ctx.restore();
}
export function contrastText(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || ''); if (!m) return '#000';
  const n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? '#000' : '#fff';
}
export function drawEmoji(ctx, W, H, emoji, age, y) {
  if (!emoji || age > 1.2) return;
  const s = 0.6 + 0.4 * Math.min(1, age / 0.15);
  const size = Math.round(W * 0.16 * s);
  ctx.save();
  ctx.globalAlpha = age > 0.9 ? Math.max(0, (1.2 - age) / 0.3) : 1;
  ctx.font = `${size}px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(emoji, W / 2, y);
  ctx.restore();
}
export function drawLogo(ctx, W, H, img, pos, handle) {
  const m = W * 0.04;
  ctx.save();
  if (img) {
    const h = W * 0.11, w = h * (img.width / img.height);
    const x = pos.includes('l') ? m : W - m - w, y = pos.includes('t') ? m * 2.5 : H - m * 2.5 - h;
    ctx.globalAlpha = 0.92; ctx.drawImage(img, x, y, w, h);
  } else if (handle) {
    const size = Math.round(W * 0.035);
    ctx.font = `700 ${size}px ${FONTS.system}`; ctx.textBaseline = 'top';
    ctx.textAlign = pos.includes('l') ? 'left' : 'right';
    const x = pos.includes('l') ? m : W - m, y = pos.includes('t') ? m * 2.5 : H - m * 2.5 - size;
    ctx.lineWidth = size * 0.18; ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.strokeText(handle, x, y);
    ctx.fillStyle = '#fff'; ctx.fillText(handle, x, y);
  }
  ctx.restore();
}

// ---------- punch-in zooms ----------
// Zoom targets: short moments of emphasis (word starts that are louder than the clip's usual level).
export function zoomPlan(words, dbAt) {
  const marks = [];
  if (!words?.length) return marks;
  const levels = words.map((w) => dbAt(w.start + 0.1));
  const s = [...levels].sort((a, b) => a - b);
  const p80 = s[Math.floor(s.length * 0.8)] ?? 0;
  let last = -9;
  words.forEach((w, i) => {
    const emph = levels[i] >= p80 || /!$/.test(w.text) || EMOTION.test(w.text);
    if (emph && w.start - last > 3) { marks.push(w.start); last = w.start; }
  });
  return marks;
}
export function zoomAt(t, marks) {
  let z = 1;
  for (const m of marks) {
    const d = t - m;
    if (d >= 0 && d < 1.6) z = Math.max(z, 1 + 0.09 * (d < 0.12 ? d / 0.12 : d > 1.2 ? (1.6 - d) / 0.4 : 1));
  }
  return z;
}

// ---------- jump cuts ----------
// Keep-segments (clip-relative seconds) that drop pauses longer than `minGap` between words.
export function keepSegments(words, clipLen, minGap = 0.6, pad = 0.15) {
  if (!words?.length) return [[0, clipLen]];
  const segs = [];
  let a = Math.max(0, words[0].start - pad), b = words[0].end + pad;
  for (let i = 1; i < words.length; i++) {
    const w = words[i];
    if (w.start - b > minGap - 2 * pad) { segs.push([a, b]); a = Math.max(b, w.start - pad); }
    b = Math.max(b, w.end + pad);
  }
  segs.push([a, Math.min(clipLen, b)]);
  if (segs[0][0] < 0.4) segs[0][0] = 0;
  return segs.filter(([x, y]) => y - x > 0.05);
}
export function mapTime(t, segs) {
  let acc = 0;
  for (const [a, b] of segs) {
    if (t < a) return null;
    if (t < b) return acc + (t - a);
    acc += b - a;
  }
  return null;
}
export const keptLength = (segs) => segs.reduce((s, [a, b]) => s + b - a, 0);
