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
  const SILENCE_MS = 3000;      // the pause that means "I have finished the sentence"
  const MAX_CLIP_MS = 20000;    // nothing a shopkeeper says in one breath is longer
  const CALIBRATE_MS = 600;     // how long we listen to the room before judging quiet
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
  let audioCtx, analyser, data, vadTimer, floor = 0.02;
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

  const heardName = (text) => {
    const s = ` ${text.toLowerCase().replace(/[.,!?]/g, " ")} `;
    return words.some((w) => w.length > 2 && s.includes(` ${w}`));
  };

  /* ---- deciding when the sentence has ended ---- */

  function listenToRoom() {
    if (!stream) return;
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    audioCtx.createMediaStreamSource(stream).connect(analyser);
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

  /* A ceiling fan, a television and the road are all "silence" to the shopkeeper, so the
     bar is set from the room itself at the moment recording starts, then speech has to
     clear it by a healthy margin. An absolute threshold either never fires in a loud shop
     or fires constantly in a quiet one. */
  function watchForSilence() {
    const startedAt = Date.now();
    let quietSince = 0, samples = [];
    clearInterval(vadTimer);
    vadTimer = setInterval(() => {
      if (!capturing) { clearInterval(vadTimer); return; }
      const now = Date.now();
      const rms = level();
      if (now - startedAt < CALIBRATE_MS) { samples.push(rms); return; }
      if (samples.length) {
        floor = Math.max(0.008, samples.reduce((a, b) => a + b, 0) / samples.length);
        samples = [];
      }
      if (rms > floor * 2.2) { quietSince = 0; return; }
      if (!quietSince) quietSince = now;
      if (now - quietSince >= SILENCE_MS || now - startedAt >= MAX_CLIP_MS) endCapture();
    }, 100);
  }

  /* ---- the capture itself ---- */

  async function beginCapture() {
    if (capturing || busy) return;
    capturing = true;
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
    clearInterval(vadTimer);
    stopRec();
    // handleClip is async and the recorder still owns the stream; the doorbell goes back
    // on once it has let go.
    setTimeout(() => { if (on) { releaseMic(); startRecogniser(); } }, 1400);
  }

  /* Nothing holds the microphone while we are only waiting for a name. */
  function releaseMic() {
    if (!stream) return;
    try { stream.getTracks().forEach((tr) => tr.stop()); } catch (err) { /* already gone */ }
    stream = null;
    analyser = null;
  }

  /* ---- the doorbell ---- */

  function startRecogniser() {
    if (!on || capturing || !SR) return;
    try {
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = srLang();
      rec.onaudiostart = () => { hearing = true; fails = 0; armedLabel(); };
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (heardName(e.results[i][0].transcript)) { beginCapture(); return; }
        }
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
        setTimeout(startRecogniser, 400);
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
    }
  }

  function stopRecogniser() {
    if (!rec) return;
    const r = rec;
    rec = null;
    try { r.onend = null; r.onerror = null; r.stop(); } catch (err) { /* already gone */ }
  }

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
      startRecogniser();
      if (gesture) loadWords();
    } else {
      stopRecogniser();
      endCapture();
      releaseMic();
    }
  }

  window.handsFreeActive = () => on;

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
