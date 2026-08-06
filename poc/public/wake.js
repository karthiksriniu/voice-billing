/* Hands-free: "Chitti, two kilo sugar."
 *
 * The shopkeeper is weighing with one hand and passing goods with the other. Push-to-talk
 * already beats paper, but only if a hand is free — so the phone gets a name, and answers
 * to it.
 *
 * How the mic is shared, and why it is split in two:
 *
 *   The wake word is spotted by the browser's own recogniser (SpeechRecognition), which
 *   costs nothing and can run all day. The command that follows is recorded and sent to
 *   Sarvam, exactly as a button press would be. That division is the whole design: sending
 *   a continuous stream to a paid ASR would blow the 10-rupee-a-month ceiling many times
 *   over, and the browser recogniser is nowhere near good enough on code-mixed Tamil item
 *   names to bill from. One is a doorbell, the other reads the order.
 *
 *   The doorbell is Chrome's, which means it goes over Google's servers. Free, but cloud —
 *   a PoC compromise, not the shipped design. The Android app replaces it with an
 *   on-device keyword model (openWakeWord and friends), which is better and genuinely
 *   private. Nothing below the wake_() boundary changes when it does.
 *
 * Two rules learned the hard way, both of which had this silently doing nothing:
 *
 *   The microphone is not shared. Holding a getUserMedia stream open while the recogniser
 *   also wants to listen leaves one of them with silence on many Android handsets — and
 *   pins the recording indicator on all day besides, which is not a thing to do to
 *   somebody's phone. So nothing holds the mic while we are merely waiting for a name;
 *   the stream is opened when the name is heard and dropped when the clip ends.
 *
 *   And a recogniser has to be started from a real tap. Safari only honours start() inside
 *   a user gesture, and an `await` before it ends the gesture just as surely as a timeout
 *   does — so the switch starts it synchronously and does its housekeeping afterwards.
 */

(function handsFree() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  // The trace from a real handset stops counting his voice within 60ms of him stopping,
  // so the wait afterwards is pure latency. Three seconds at a counter is an age; the
  // clip closed at 8.7s in a rehearsal that only watched for 8 and was called a failure
  // for it. This is the pause a shopkeeper leaves between items, not between customers.
  const SILENCE_MS = 1700;
  const MAX_CLIP_MS = 12000;    // the worst case when the room wins, kept short
  const GIVE_UP_AFTER = 4;      // consecutive failed starts before we stop and say so

  // iOS ignores `continuous` and refuses to restart without a fresh tap, so the doorbell
  // rings once and dies. Rather than look broken, it says so. iOS is a stated non-goal
  // (CLAUDE.md); this exists so the shopkeeper is told, not so it half-works.
  const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  let on = false;               // the shopkeeper's switch
  let rec = null;               // the wake-word recogniser
  let capturing = false;        // a command clip is being recorded right now
  let hearing = false;          // the recogniser has actually got audio, not just started
  let fails = 0;
  let audioCtx, analyser, data, vadTimer, watchdog, micSource;
  let lastAudioAt = 0, heartbeat = null;
  let words = ["chitti", "chithi", "chitty", "chiti"];

  const srLang = () => ({ en: "en-IN", ta: "ta-IN", hi: "hi-IN", ml: "ml-IN",
                          te: "te-IN", kn: "kn-IN" })[state.shop && state.shop.lang] || "en-IN";

  /* The wake words live in the language pack with everything else, so a new language is
     still only data. Latin spellings stay in the list regardless: the browser recogniser
     hands back romanised text as often as native script. */
  async function loadWords() {
    try {
      const j = await api(`/api/lang?code=${encodeURIComponent(state.shop.lang || "en")}`);
      if (j.wake && j.wake.length) words = j.wake;
    } catch (err) { /* the built-in list is a fine fallback */ }
  }

  /* Chrome does not hand back the word that was said, it hands back its best guess at it,
     and for a name it has never met that guess wanders: chitty, chithi, cheeti, city,
     chetty, and something else again in Tamil script. Exact substring matching caught a
     fraction of them, which is precisely what "works, but not consistently" feels like.

     So the match is by distance, and the bar is deliberately lower than the one commands
     get. The two mistakes are not equal: a false wake records a clip of shop noise that
     Sarvam returns nothing for and no line is billed, while a missed wake makes the
     shopkeeper say it again and stop trusting the feature. Cheap versus corrosive. */
  const WAKE_THRESHOLD = 0.7;
  const CARRIERS = ["hey", "hay", "hai", "hi", "ok", "okay", "a", "the"];

  function ratio(a, b) {
    if (a === b) return 1;
    if (!a.length || !b.length) return 0;
    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const row = [i];
      for (let j = 1; j <= b.length; j++) {
        row[j] = Math.min(prev[j] + 1, row[j - 1] + 1,
                          prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = row;
    }
    return 1 - prev[b.length] / Math.max(a.length, b.length);
  }

  /* Chrome treats a short utterance as a whole sentence. Say "Hey Chitti" and it can
     finalise on "hey", end the session, and hand "chitti" to the NEXT one — or lose it in
     the gap between them. So adding the carrier made things worse, not better, which is
     exactly what was reported.

     The fix is to stop treating a session as a unit. Recent transcripts are kept for a
     couple of seconds and matched as one running string, so "hey" and "chitti" arriving
     separately still add up to the name. */
  const MEMORY_MS = 2500;
  let recent = [];
  function remember(text) {
    const now = Date.now();
    recent.push({ at: now, text });
    recent = recent.filter((r) => now - r.at < MEMORY_MS);
    return recent.map((r) => r.text).join(" ");
  }
  function forget() { recent = []; }

  const alternatives = (result) => {
    const out = [];
    for (let k = 0; k < result.length; k++) out.push(result[k].transcript);
    return out;
  };

  let lastMiss = "";
  function heardName(text) {
    const clean = text.toLowerCase().replace(/[.,!?;:"'’]/g, " ").replace(/\s+/g, " ").trim();
    if (!clean) return false;
    const toks = clean.split(" ");
    // "hey chitti" and a bare "chitti" are the same summons; the carrier only ever adds
    // audio for the recogniser to work with, so it is matched with and without.
    const windows = [];
    for (let i = 0; i < toks.length; i++) {
      windows.push(toks[i]);
      if (i + 1 < toks.length) windows.push(`${toks[i]} ${toks[i + 1]}`);
      if (CARRIERS.includes(toks[i]) && i + 1 < toks.length) windows.push(toks[i + 1]);
    }
    let best = 0;
    for (const w of words) {
      const bare = w.split(" ").filter((x) => !CARRIERS.includes(x)).join(" ") || w;
      // A two-letter spelling has to be exactly that. "hd" is in the list because it is
      // literally what Chrome returned for "Hey Chitti" four times over, but at two
      // characters a distance test would match half the alphabet, so it only ever counts
      // as a whole token. Anything longer gets the fuzzy treatment.
      if (w.length < 3) {
        if (toks.includes(w)) return true;
        continue;
      }
      for (const win of windows) {
        for (const target of new Set([w, bare])) {
          if (win.includes(target)) return true;
          best = Math.max(best, ratio(win, target));
          if (best >= WAKE_THRESHOLD) return true;
        }
      }
    }
    // Kept so the check and debug mode can show what it nearly was. A name the recogniser
    // keeps producing belongs in the pack, not behind a lower threshold.
    lastMiss = `${clean} (${best.toFixed(2)})`;
    return false;
  }
  window.handsFreeLastMiss = () => lastMiss;
  // Exposed so the matcher can be exercised against real transcripts. It decides whether
  // the feature works at all; it should not be the one thing here that cannot be tested.
  window.handsFreeMatch = (text, list) => {
    const keep = words;
    if (list) words = list;
    try { return { hit: heardName(text), miss: lastMiss }; } finally { words = keep; }
  };

  /* ---- deciding when the sentence has ended ---- */

  function listenToRoom() {
    if (!stream) return;
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    // Held in a variable on purpose. An unreferenced MediaStreamAudioSourceNode can be
    // collected, and when it is the analyser goes on returning zeroes for ever — which
    // reads as a silent room, so the clip never ends.
    micSource = audioCtx.createMediaStreamSource(stream);
    micSource.connect(analyser);
    data = new Uint8Array(analyser.fftSize);
    if (audioCtx.state === "suspended") audioCtx.resume();
  }

  function level() {
    if (!analyser) return 1;                  // no meter: never call it silence
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / data.length);
  }

  /* Knowing when the sentence has ended, in a room that is never quiet.
   *
   * Calibrating once at the start was the mistake. The shopkeeper starts talking
   * immediately, so the "quiet" sample was often their own voice — and in a shop with a
   * television and a conversation two feet away, a fixed multiple of that either ends the
   * clip mid-word or never ends it at all. The reported symptom, a clip that runs on
   * through background speech, is the second of those.
   *
   * Two changes. The floor is tracked continuously as the quietest thing heard recently,
   * decaying so it follows the room rather than one moment of it. And the decision is in
   * decibels, where the distance between a voice held near the phone and a voice across
   * the shop is a stable ~12 dB whatever the absolute levels are — which is as close to
   * telling the speakers apart as anything that fits on this hardware. It is near-field
   * gating, not speaker recognition, and it should not be described as more than that.
   */
  /* Judging "has he stopped talking?" against the room does not survive a television.
     A podcast playing two feet away IS speech, energy-wise, and no threshold measured
     against the room's quiet will ever say otherwise — which is why the clip ran on.
   *
   * So the reference is the speaker, not the room. The wake word guarantees the shopkeeper
   * was talking when the clip opened, so the loudest thing in the clip is his own voice at
   * arm's length. Everything is then judged relative to that: background chatter sits well
   * below the person holding the phone, and falling more than DROP_DB under the clip's own
   * peak is what "he has stopped" means. The peak decays slowly so it follows him rather
   * than being pinned by one loud syllable.
   *
   * This is near-field gating and nothing more. If he says the wake word and then says
   * nothing at all, the peak becomes the television and the clip runs to the cap — which
   * is why the cap is now twelve seconds and not twenty. */
  const DROP_DB = 10;                // how far under his own voice counts as stopped
  const PEAK_DECAY_DB_S = 4;         // the peak follows the speaker, it does not stick
  const OVER_FLOOR_DB = 6;           // a floor sanity check for a genuinely silent room
  const MIN_SPEECH_MS = 400;         // never end before anything has actually been said
  const dB = (rms) => 20 * Math.log10(Math.max(rms, 1e-5));

  let vadTrace = [];
  function watchForSilence() {
    const startedAt = Date.now();
    let quietSince = 0, spokeFor = 0, lastAt = Date.now();
    let floorDb = null, peakDb = null;
    vadTrace = [];
    clearInterval(vadTimer);
    vadTimer = setInterval(() => {
      if (!capturing) { clearInterval(vadTimer); return; }
      const now = Date.now();
      const dt = now - lastAt;
      lastAt = now;
      const cur = dB(level());

      // Floor: drops to any new quiet at once, creeps back at ~3 dB a second.
      floorDb = floorDb == null ? cur
        : cur < floorDb ? cur : Math.min(cur, floorDb + 0.003 * dt);
      // Peak: rises instantly to his voice, decays slowly so it stays his voice.
      peakDb = peakDb == null ? cur
        : cur > peakDb ? cur : Math.max(cur, peakDb - (PEAK_DECAY_DB_S / 1000) * dt);

      const speaking = cur > peakDb - DROP_DB && cur > floorDb + OVER_FLOOR_DB;
      if (vadTrace.length < 220) {
        vadTrace.push(`${now - startedAt}:${cur.toFixed(0)}/${peakDb.toFixed(0)}/${
          floorDb.toFixed(0)}${speaking ? "S" : "."}`);
      }
      if (state.debug && now - startedAt > 300) {
        setStatus(`${cur.toFixed(0)}dB peak ${peakDb.toFixed(0)} floor ${
          floorDb.toFixed(0)} ${speaking ? "SPEECH" : "-"}`);
      }
      if (speaking) { spokeFor += dt; quietSince = 0; return; }
      if (spokeFor < MIN_SPEECH_MS) return;
      if (!quietSince) quietSince = now;
      if (now - quietSince >= SILENCE_MS || now - startedAt >= MAX_CLIP_MS) endCapture();
    }, 60);
  }
  window.handsFreeTrace = () => vadTrace.join(" ");

  /* ---- the capture itself ---- */

  /* A short tone, because the whole premise is that nobody is watching the screen. The
     button turning red is no use to someone whose hands are in the rice. */
  function tone(hz, ms) {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume();
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.frequency.value = hz;
      g.gain.value = 0.09;
      o.connect(g).connect(audioCtx.destination);
      o.start();
      o.stop(audioCtx.currentTime + ms / 1000);
    } catch (err) { /* a courtesy, never the mechanism */ }
  }

  async function beginCapture() {
    if (capturing || busy) return;
    capturing = true;
    tone(880, 90);
    // Nothing may leave this flag set. A capture that never ended used to make every
    // later wake word a no-op, so the feature went quietly dead until a reload.
    clearTimeout(watchdog);
    watchdog = setTimeout(() => { if (capturing) endCapture(); }, MAX_CLIP_MS + 4000);
    // Hand the microphone over completely. The doorbell stops ringing before the order is
    // read, so the wake word cannot end up inside the order and the two never compete.
    stopRecogniser();
    setTalk("rec");
    if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
    await openMic();
    if (!stream) { capturing = false; speak(t("voiceUnavailable")); return; }
    listenToRoom();
    startRec();
    watchForSilence();
  }

  function endCapture() {
    if (!capturing) return;
    capturing = false;
    clearTimeout(watchdog);
    clearInterval(vadTimer);
    tone(520, 70);
    stopRec();
    // handleClip is async and the recorder still owns the stream; the doorbell goes back
    // on once it has let go.
    fails = 0;
    setTimeout(() => {
      if (!on) return;
      releaseMic();
      hearing = false;
      lastAudioAt = Date.now();
      startRecogniser();
    }, 1400);
  }

  /* Nothing holds the microphone while we are only waiting for a name. */
  function releaseMic() {
    if (!stream) return;
    try { stream.getTracks().forEach((tr) => tr.stop()); } catch (err) { /* already gone */ }
    try { if (micSource) micSource.disconnect(); } catch (err) { /* already gone */ }
    stream = null;
    analyser = null;
    micSource = null;
  }

  /* ---- the doorbell ---- */

  function startRecogniser() {
    if (!on || capturing || !SR) return;
    try {
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      // Chrome's first guess at an unfamiliar name is often rubbish — it returned "HD"
      // for "Hey Chitti" — but the runners-up are frequently closer. They cost nothing to
      // ask for and are checked alongside it.
      rec.maxAlternatives = 5;
      rec.lang = srLang();
      rec.onaudiostart = () => { hearing = true; fails = 0; lastAudioAt = Date.now(); armedLabel(); };
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          for (const alt of alternatives(e.results[i])) {
            if (heardName(alt) || heardName(remember(alt))) {
              forget();
              beginCapture();
              return;
            }
          }
        }
        if (state.debug) setStatus(`~ ${lastMiss}`);
      };
      // Chrome ends a continuous session on its own every minute or so, and on any silence
      // it decides is long enough. Restarting is the normal path, not error handling — but
      // a restart that never reaches audio is a failure wearing a loop's clothing, so it
      // is counted and eventually given up on out loud.
      rec.onend = () => {
        rec = null;
        if (!on || capturing) return;
        if (!hearing && ++fails >= GIVE_UP_AFTER) { fail(t("handsFreeNo")); return; }
        hearing = false;
        // Android Chrome ends a session on every pause, so this gap is repeated all day
        // and anything said inside it is simply not heard. It is the difference between
        // "works" and "works sometimes", so it is as short as the API will tolerate.
        setTimeout(startRecogniser, 120);
      };
      rec.onerror = (e) => {
        if (e.error === "not-allowed" || e.error === "service-not-allowed") {
          fail(t("micDenied"));
        } else if (e.error === "audio-capture") {
          fail(t("voiceUnavailable"));
        } else if (e.error === "network") {
          fail(t("handsFreeNet"));
        }
        // no-speech and aborted are ordinary; onend restarts.
      };
      rec.start();
    } catch (err) {
      if (++fails >= GIVE_UP_AFTER) fail(t("handsFreeNo"));
      else setTimeout(startRecogniser, 300);
    }
  }

  function stopRecogniser() {
    if (!rec) return;
    const r = rec;
    rec = null;
    try { r.onend = null; r.onerror = null; r.stop(); } catch (err) { /* already gone */ }
  }

  /* The doorbell stops ringing for reasons this code cannot enumerate.
   *
   * Chrome suspends speech recognition when the tab is hidden — the screen going off or
   * the shopkeeper glancing at WhatsApp is enough — and what comes back is not always a
   * working recogniser. It can also end a session and simply not deliver the event that
   * would have restarted it. The reported symptom is exactly this shape: fine the first
   * time, dead afterwards, alive again after a reload or a toggle, which is to say alive
   * again after something rebuilt it.
   *
   * Rather than chase each of those, this checks that the thing is actually alive and
   * rebuilds it when it is not. Audio arriving is the proof of life; a recogniser that has
   * not seen any for twenty seconds while the shop is in front of it is not listening,
   * whatever its own state says.
   */
  const STALL_MS = 20000;
  function watchTheWatcher() {
    clearInterval(heartbeat);
    heartbeat = setInterval(() => {
      if (!on) { clearInterval(heartbeat); return; }
      if (capturing || document.visibilityState !== "visible") return;
      const quietFor = Date.now() - lastAudioAt;
      if (rec && quietFor < STALL_MS) return;
      stopRecogniser();
      hearing = false;
      lastAudioAt = Date.now();          // one grace period per rebuild, not a spin
      startRecogniser();
    }, 5000);
  }

  /* Hidden means suspended, so it is stopped deliberately and rebuilt on the way back.
     Leaving it to die on its own is what left a recogniser that existed and heard
     nothing. */
  document.addEventListener("visibilitychange", () => {
    if (!on) return;
    if (document.visibilityState === "hidden") {
      stopRecogniser();
    } else if (!capturing) {
      hearing = false;
      lastAudioAt = Date.now();
      setTimeout(startRecogniser, 250);
    }
  });

  /* Failing quietly is the one thing a hands-free feature must never do: the shopkeeper is
     not looking at the phone, so silence is indistinguishable from working. */
  function fail(message) {
    setEnabled(false);
    speak(message);
  }

  /* ---- the switch ---- */

  function armedLabel() {
    $("talkLabel").innerHTML = on
      ? `${t("sayChitti")}<br><small>${t("holdToSpeak")}</small>`
      : t("holdToSpeak");
  }

  function setEnabled(next, gesture) {
    on = next;
    fails = 0;
    hearing = false;
    localStorage.setItem("boloHandsFree", on ? "1" : "0");
    $("miWake").setAttribute("aria-checked", String(on));
    $("miWakeState").textContent = on ? t("on") : t("off");
    document.body.classList.toggle("handsfree", on);
    armedLabel();
    if (on) {
      // Started before any await: Safari only honours start() inside the tap that caused
      // it, and awaiting anything at all ends that tap.
      lastAudioAt = Date.now();
      startRecogniser();
      watchTheWatcher();
      // Always, not only when a thumb turned it on. Restoring the switch after a reload
      // skipped this, so the live feature quietly ran on the built-in fallback list —
      // without "hd", which is the only spelling this handset actually produces. It would
      // have worked when toggled and failed after every reload, which is the worst of
      // both: intermittent, and untraceable to the thing that changed.
      loadWords();
    } else {
      clearInterval(heartbeat);
      stopRecogniser();
      endCapture();
      releaseMic();
    }
  }

  window.handsFreeActive = () => on;

  /* ---- the check ----
     Twelve seconds of the real thing, with every event the browser emits written down.
     Nothing here is a simulation: it is the same recogniser, the same language, the same
     wake words. Android Chrome ends a session on its own every few seconds and reports
     `no-speech` when the shop is quiet — seeing that in the log is the feature working,
     not failing, and knowing the difference is the whole point of writing it down. */
  async function runCheck() {
    const box = $("hfLog");
    const line = [];
    const t0 = Date.now();
    const say = (m) => {
      line.push(`${String(Date.now() - t0).padStart(5)}ms  ${m}`);
      box.textContent = line.join("\n");
      box.scrollTop = box.scrollHeight;
    };
    box.hidden = false;
    box.textContent = "";
    $("hfCopy").hidden = true;

    // The live feature loads its wake words from the pack; the check was running on the
    // built-in fallback, so it was not testing the same thing the shopkeeper uses.
    await loadWords();
    say(`build ${window.BOLO_BUILD || "unknown"}`);
    say(`ua ${navigator.userAgent}`);
    say(`secure=${window.isSecureContext} standalone=${
      !!(window.matchMedia("(display-mode: standalone)").matches || navigator.standalone)}`);
    say(`SpeechRecognition=${!!SR} iOS=${IOS} lang=${srLang()} words=${words.join("/")}`);
    if (navigator.permissions) {
      try {
        const st = await navigator.permissions.query({ name: "microphone" });
        say(`mic permission=${st.state}`);
      } catch (err) { say(`mic permission unknown (${err.name})`); }
    }
    if (!SR) { say("STOP: this browser has no speech recognition"); return finish(); }

    // The recorder side, tested on its own so a fault can be told from the doorbell's,
    // and with it a read of the room — the numbers the end-of-sentence decision uses.
    await openMic();
    say(stream ? `getUserMedia ok, ${stream.getAudioTracks().length} track(s)`
               : "getUserMedia FAILED — no microphone");
    if (stream) {
      listenToRoom();
      const reads = [];
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => setTimeout(r, 100));
        reads.push(dB(level()));
      }
      const lo = Math.min(...reads), hi = Math.max(...reads);
      say(`room ${lo.toFixed(0)}dB quiet .. ${hi.toFixed(0)}dB loud ` +
          `(counts as him: within ${DROP_DB}dB of the clip peak, and ${
            OVER_FLOOR_DB}dB over the floor)`);
    }
    releaseMic();

    let sessions = 0, gotAudio = 0, heard = 0, stop = false;
    const spin = () => {
      if (stop) return;
      sessions++;
      const r = new SR();
      r.continuous = true; r.interimResults = true; r.lang = srLang();
      r.onstart = () => say(`session ${sessions}: start`);
      r.onaudiostart = () => { gotAudio++; say(`session ${sessions}: audio reaching it`); };
      r.onresult = (e) => {
        const alts = alternatives(e.results[e.results.length - 1]);
        const txt = alts.join(" | ");
        // Matched exactly as the live path does — piece alone, then the running memory,
        // because Chrome splits "hey chitti" across sessions more often than not.
        const hit = alts.some((a) => heardName(a) || heardName(remember(a)));
        if (hit) { heard++; forget(); }
        say(`session ${sessions}: heard "${txt}"${
          hit ? "  <-- WAKE WORD MATCHED" : `  (best ${lastMiss.split(" ").pop()})`}`);
      };
      r.onerror = (e) => say(`session ${sessions}: error ${e.error}`);
      r.onend = () => { say(`session ${sessions}: end`); if (!stop) setTimeout(spin, 400); };
      try { r.start(); } catch (err) { say(`session ${sessions}: start threw ${err.name}`); }
      window.__hfRec = r;
    };
    say('SAY "HEY CHITTI" NOW — listening for 12 seconds');
    spin();

    await new Promise((res) => setTimeout(res, 12000));
    stop = true;
    try { window.__hfRec.onend = null; window.__hfRec.stop(); } catch (err) { /* done */ }
    say(`--- ${sessions} session(s), audio in ${gotAudio}, wake word matched ${heard}x`);
    say(gotAudio === 0
      ? "VERDICT: the recogniser never received audio."
      : heard === 0
        ? "VERDICT: audio is reaching it but the wake word was not matched."
        : "VERDICT: working — the wake word was matched.");

    // Then the other half. Knowing the wake word fires is no use if the clip never ends,
    // so the end-of-sentence decision is rehearsed on the real room: say a sentence, stop,
    // and the trace shows whether and when it would have closed the clip.
    say("");
    say(`NOW SAY A SENTENCE AND STOP — rehearsing the end of a clip (12s, closes after ${
      SILENCE_MS}ms of quiet)`);
    await openMic();
    if (!stream) { say("no microphone for the rehearsal"); return finish(); }
    listenToRoom();
    capturing = true;
    watchForSilence();
    const began = Date.now();
    await new Promise((res) => {
      const iv = setInterval(() => {
        if (!capturing || Date.now() - began > 12000) { clearInterval(iv); res(); }
      }, 100);
    });
    const ended = !capturing;
    const took = Date.now() - began;
    capturing = false;
    clearInterval(vadTimer);
    releaseMic();
    say(`trace dB/peak/floor (S = counted as him speaking):`);
    say(window.handsFreeTrace());
    say(ended ? `clip would have closed after ${took}ms`
              : "clip did NOT close in 12s — the room is holding it open");
    finish();

    function finish() {
      $("hfCopy").hidden = false;
      $("hfCopy").onclick = async () => {
        try { await navigator.clipboard.writeText(box.textContent); toast(t("copied"), 2000, true); }
        catch (err) { toast(t("network"), 2000, true); }
      };
    }
  }

  $("hfRun").onclick = async () => {
    const was = on;
    if (was) setEnabled(false);          // the check needs the microphone to itself
    $("hfRun").disabled = true;
    const box = $("hfLog");
    try {
      await runCheck();
    } catch (err) {
      // A check that dies takes its own report with it, which is how the one run that
      // would have explained everything explained nothing. Whatever killed it goes into
      // the report, and the report goes out regardless.
      box.hidden = false;
      box.textContent += `\n!! CHECK CRASHED: ${err && err.stack ? err.stack : err}`;
    } finally {
      // Sent rather than copied. Reading an audio fault out of somebody's phone by hand is
      // a poor way to debug one, and there is nothing personal in here.
      try {
        const j = await api("/api/diag", { method: "POST", body: { report: box.textContent } });
        box.textContent += `\n\n===== REPORT CODE: ${j.code} =====`;
        speak(`Report code ${(j.code || "").split("").join(" ")}`);
      } catch (e) { box.textContent += "\n(could not send — copy it instead)"; }
      $("hfCopy").hidden = false;
      $("hfRun").disabled = false;
      if (was) setEnabled(true, true);
    }
  };

  $("miWake").onclick = () => {
    if (!SR || IOS) { speak(t("handsFreeNo")); return; }
    setEnabled(!on, true);
  };

  // Restored on load, but only once a shop is signed in — and never on iOS, where a
  // recogniser cannot be started without a tap and would fail on every restart.
  window.handsFreeRestore = () => {
    if (!SR || IOS) { $("miWake").classList.add("dim"); return; }
    setEnabled(localStorage.getItem("boloHandsFree") === "1", false);
  };
})();
