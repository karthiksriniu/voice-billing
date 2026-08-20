/* Silero VAD — telling speech from a room that is never quiet.
 *
 * The endpointer this supports judged "has he stopped talking?" from loudness alone,
 * relative to the loudest thing in the clip. That works in a quiet shop and fails in a
 * loud one for a reason no threshold can fix: a ceiling fan, traffic and a mixer all sit
 * within a few decibels of a voice, so the level never falls far enough to count as
 * silence and the clip runs to its twelve-second cap. The shopkeeper says four words and
 * waits eight seconds. DECISIONS D2 called for Silero from the start; the energy gate was
 * the stand-in.
 *
 * Silero answers a different question — "is this speech at all" — which is the one the
 * fan gets wrong. It does NOT answer "is this HIM", because a television is speech by any
 * honest measure. So this does not replace the near-field decibel test, it joins it: the
 * model rules out the room, the level rules out other people in it. Neither is sufficient
 * and the pair is what the shop actually needs.
 *
 * On the weight of it. The runtime and the model are about 5 MB over the wire, which on a
 * 2 GB handset on patchy 4G is not something to put in the way of a first bill. So none of
 * it loads until hands-free is switched on, all of it is cached permanently after the
 * first time, and every failure — no network, a refused cache, a phone too slow to keep
 * up — falls back to the decibel gate rather than breaking the feature. Billing never
 * touches this file; push-to-talk does not know it exists.
 */

(function sileroVad() {
  const ORT_DIR = "/vendor/ort/";
  const WASM = ORT_DIR + "ort-wasm-simd-threaded.wasm";
  const MJS = ORT_DIR + "ort-wasm-simd-threaded.mjs";
  const MODEL = "/vendor/silero_vad_v5.onnx";
  const CACHE = "bolo-vad-1";

  // Silero v5 speaks 16 kHz in 512-sample frames — 32 ms, which is finer than any decision
  // made from it, so nothing here waits on the model's own granularity.
  const RATE = 16000;
  const FRAME = 512;

  // Enter high, leave low. One threshold makes the flag chatter on every unvoiced
  // consonant, and a gap of a few frames mid-sentence is what cuts an item in half.
  const ENTER = 0.5;
  const EXIT = 0.35;

  // If the handset cannot infer a 32 ms frame inside 32 ms it is not going to keep up, and
  // a VAD running minutes behind the audio is worse than no VAD at all. Measured over the
  // first frames of real use, then decided once.
  const SLOW_MS = 28;
  const SLOW_AFTER = 24;

  let ort = null, session = null, node = null, ctx = null, src = null;
  let stateT = null, srT = null;
  let busy = false, dropped = 0, slow = 0, times = [];
  let prob = 0, speaking = false;

  const api = {
    status: "idle",       // idle | loading | ready | running | unavailable | slow
    error: "",
    get prob() { return prob; },
    get speaking() { return speaking; },
    get running() { return api.status === "running"; },
    get dropped() { return dropped; },
    onFrame: null,        // (prob, speaking) => void — set by the check, and by tests
  };
  window.BoloVAD = api;

  /* ---- getting the bytes, once and for ever ---- */

  /* Cache Storage rather than the HTTP cache, because the HTTP cache is a hint and this
     has to survive a shop that is offline for a week. Both files are content-addressed by
     name and never change under a given name, so there is no revalidation to get wrong. */
  async function bytes(url, onProgress) {
    try {
      const c = await caches.open(CACHE);
      const hit = await c.match(url);
      if (hit) return await hit.arrayBuffer();
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${res.status} ${url}`);
      // Cloned before reading: a Response body can only be consumed once, and putting the
      // clone in the cache is what makes the second launch free.
      try { await c.put(url, res.clone()); } catch (e) { /* quota, private mode */ }
      if (onProgress) onProgress(url);
      return await res.arrayBuffer();
    } catch (err) {
      // No Cache Storage at all (private mode on some builds) is not fatal — it just means
      // paying the download again next time.
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${res.status} ${url}`);
      return await res.arrayBuffer();
    }
  }

  api.load = async function load(onProgress) {
    if (api.status === "ready" || api.status === "running") return true;
    if (api.status === "loading") return false;
    api.status = "loading";
    try {
      ort = await import(ORT_DIR + "ort.wasm.min.mjs");
      const wasm = await bytes(WASM, onProgress);
      // Handing the runtime the bytes directly is what keeps this working offline. Left to
      // fetch them itself it would go through the HTTP cache, which no service worker here
      // guarantees. The glue module beside it is 25 kB and can be re-fetched.
      ort.env.wasm.wasmBinary = wasm;
      ort.env.wasm.wasmPaths = { mjs: MJS };
      // One thread. Threads need cross-origin isolation, which this app does not set and
      // should not have to; and a 32 ms frame of a 2 MB model is not work worth splitting.
      ort.env.wasm.numThreads = 1;
      ort.env.logLevel = "error";

      const model = await bytes(MODEL, onProgress);
      session = await ort.InferenceSession.create(model, {
        executionProviders: ["wasm"],
        graphOptimizationLevel: "all",
      });
      srT = new ort.Tensor("int64", BigInt64Array.from([BigInt(RATE)]), []);
      api.reset();
      api.status = "ready";
      return true;
    } catch (err) {
      api.error = String((err && err.message) || err);
      api.status = "unavailable";
      session = null;
      return false;
    }
  };

  /* ---- the model's memory ---- */

  /* Silero carries an LSTM state across frames, so it hears a sentence rather than 512
     unrelated samples. That state belongs to one utterance: carried into the next capture
     it would start out believing the previous customer was still talking. */
  api.reset = function reset() {
    if (!ort) return;
    stateT = new ort.Tensor("float32", new Float32Array(2 * 1 * 128), [2, 1, 128]);
    prob = 0;
    speaking = false;
    dropped = 0;
    times = [];
  };

  async function infer(frame) {
    if (!session || busy) { dropped++; return; }
    busy = true;
    const t0 = performance.now();
    try {
      const out = await session.run({
        input: new ort.Tensor("float32", frame, [1, FRAME]),
        state: stateT,
        sr: srT,
      });
      stateT = out.stateN;
      prob = out.output.data[0];
      speaking = speaking ? prob >= EXIT : prob >= ENTER;
      // Every frame, from the audio thread. The alternative is polling from a timer, and a
      // timer cannot see this: a backgrounded tab is clamped to roughly one tick a second,
      // which is thirty frames missed for every one seen. The whole feature turns on
      // whether this number is right, so it should not be the one thing here that cannot
      // be watched.
      if (api.onFrame) { try { api.onFrame(prob, speaking); } catch (e) { /* a probe */ } }

      // A phone that cannot keep up is told so once, and then left alone. Silently running
      // a VAD whose answers arrive after the decision they were for is the kind of failure
      // that looks like bad luck for months.
      const ms = performance.now() - t0;
      times.push(ms);
      if (times.length > 60) times.shift();
      if (ms > SLOW_MS && ++slow >= SLOW_AFTER) {
        api.status = "slow";
        api.error = `inference ${Math.round(ms)}ms per 32ms frame`;
        api.detach();
      }
    } catch (err) {
      api.error = String((err && err.message) || err);
      api.status = "unavailable";
      api.detach();
    } finally {
      busy = false;
    }
  }

  api.medianMs = function medianMs() {
    if (!times.length) return 0;
    const s = [...times].sort((a, b) => a - b);
    return Math.round(s[s.length >> 1] * 10) / 10;
  };

  /* ---- tapping the microphone ---- */

  /* An AudioWorklet rather than the AnalyserNode the decibel gate uses, because the model
     needs every sample in order. An analyser hands back whatever the last window happened
     to hold, and frames sampled with gaps between them would feed an LSTM a sentence with
     holes in it. Resampling lives here too: asking for a 16 kHz context usually works, and
     when the handset refuses, the ratio falls out of its real rate instead. */
  const WORKLET = `
    class VadTap extends AudioWorkletProcessor {
      constructor(o) {
        super();
        const rate = (o.processorOptions && o.processorOptions.rate) || sampleRate;
        this.ratio = rate / ${RATE};
        this.buf = new Float32Array(${FRAME});
        this.n = 0;
        this.pos = 0;
        this.carry = new Float32Array(0);
      }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (!ch || !ch.length) return true;
        const src = new Float32Array(this.carry.length + ch.length);
        src.set(this.carry, 0);
        src.set(ch, this.carry.length);
        let p = this.pos;
        while (p + 1 < src.length) {
          const i = p | 0, f = p - i;
          this.buf[this.n++] = src[i] * (1 - f) + src[i + 1] * f;
          if (this.n === ${FRAME}) { this.port.postMessage(this.buf.slice()); this.n = 0; }
          p += this.ratio;
        }
        const used = p | 0;
        this.carry = src.slice(used);
        this.pos = p - used;
        return true;
      }
    }
    registerProcessor("vad-tap", VadTap);
  `;

  api.attach = async function attach(stream) {
    if (api.status !== "ready" || !stream) return false;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      // Its own context, at the model's rate. The decibel gate keeps the one it has: two
      // cheap contexts are better than one shared one that has to suit both.
      try { ctx = new AC({ sampleRate: RATE }); } catch (e) { ctx = new AC(); }
      if (ctx.state === "suspended") await ctx.resume();
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
      try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      node = new AudioWorkletNode(ctx, "vad-tap", {
        numberOfOutputs: 0,
        processorOptions: { rate: ctx.sampleRate },
      });
      node.port.onmessage = (e) => infer(e.data);
      src = ctx.createMediaStreamSource(stream);
      src.connect(node);
      api.reset();
      api.status = "running";
      return true;
    } catch (err) {
      api.error = String((err && err.message) || err);
      api.status = "unavailable";
      api.detach();
      return false;
    }
  };

  api.detach = function detach() {
    try { if (src) src.disconnect(); } catch (e) { /* gone */ }
    try { if (node) { node.port.onmessage = null; node.disconnect(); } } catch (e) { /* gone */ }
    try { if (ctx) ctx.close(); } catch (e) { /* gone */ }
    src = node = ctx = null;
    speaking = false;
    prob = 0;
    if (api.status === "running") api.status = session ? "ready" : "unavailable";
  };
})();
