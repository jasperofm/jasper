// Speech recognition worker: runs Whisper (ONNX) inside the browser. Audio never leaves the computer.
// The library ships next to the page when hosted as a bundle; otherwise it comes from the jsDelivr CDN.
// Loaded inside setup() so the message handler below is registered before any await.
let pipeline, env;

const MODEL = 'onnx-community/whisper-base_timestamped';
let manifest = null;        // present when the model files are hosted next to the page (split into parts)
let asr = null;

const base = new URL('./', self.location.href);
const realFetch = self.fetch.bind(self);

async function localFile(key, opts = {}) {
  const f = manifest.files[key];
  if (!f) return new Response('not found', { status: 404 });
  const range = opts.headers && (opts.headers.get ? opts.headers.get('Range') : opts.headers.Range);
  if (range === 'bytes=0-0') {
    return new Response(new Uint8Array(1), {
      status: 206, headers: { 'content-range': `bytes 0-0/${f.size}`, 'content-length': '1' },
    });
  }
  const parts = await Promise.all(f.parts.map(async (p) => {
    const r = await realFetch(new URL(p, base));
    if (!r.ok) throw new Error(`model file missing: ${p}`);
    return r.blob();
  }));
  return new Response(new Blob(parts), { status: 200, headers: { 'content-length': String(f.size) } });
}

async function setup() {
  ({ pipeline, env } = await import('./vendor/transformers.min.js')
    .catch(() => import('https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.1/dist/transformers.min.js')));
  try {
    const r = await realFetch(new URL('models/manifest.json', base));
    if (r.ok) manifest = await r.json();
  } catch { /* not hosted locally: load from the model hub */ }
  if (manifest) {
    env.fetch = (url, opts) => {
      const m = String(url).match(/huggingface\.co\/(.+?)\/resolve\/[^/]+\/(.+)$/);
      return m ? localFile(`${m[1]}/${m[2]}`, opts) : realFetch(url, opts);
    };
    env.backends.onnx.wasm.wasmPaths = {
      mjs: new URL('vendor/ort-wasm-simd-threaded.mjs', base).href,
      wasm: new URL('vendor/ort-wasm-simd-threaded.wasm', base).href,
    };
  }
  env.backends.onnx.wasm.numThreads = 1;   // threads need cross-origin isolation, which static hosts don't give
  try { env.useBrowserCache = 'caches' in self; } catch { env.useBrowserCache = false; }
  const seen = {};
  asr = await pipeline('automatic-speech-recognition', MODEL, {
    device: 'wasm',
    dtype: { encoder_model: 'q8', decoder_model_merged: 'q8' },
    progress_callback: (p) => {
      if (p.status === 'progress' && p.file) {
        seen[p.file] = [p.loaded || 0, p.total || 0];
        const tot = Object.values(seen).reduce((a, [, t]) => a + t, 0);
        const got = Object.values(seen).reduce((a, [l]) => a + l, 0);
        self.postMessage({ type: 'loading', progress: tot ? got / tot : 0 });
      }
    },
  });
}

let ready = null;
self.onmessage = async (ev) => {
  const { id, audio, language } = ev.data;
  try {
    ready ??= setup();
    await ready;
    self.postMessage({ type: 'loaded' });
    const out = await asr(audio, {
      return_timestamps: 'word', chunk_length_s: 30, stride_length_s: 5,
      ...(language && language !== 'auto' ? { language, task: 'transcribe' } : {}),
    });
    const words = (out.chunks || [])
      .map((c) => ({ text: c.text.trim(), start: c.timestamp[0], end: c.timestamp[1] ?? c.timestamp[0] + 0.3 }))
      .filter((w) => w.text);
    self.postMessage({ type: 'result', id, words, text: out.text });
  } catch (e) {
    ready = null;
    self.postMessage({ type: 'error', id, message: e?.message || String(e) });
  }
};
