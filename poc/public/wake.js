/* Hands-free: "Chitti, two kilo sugar."
 *
 * The shopkeeper is weighing with one hand and passing goods with the other. Push-to-talk
 * already beats paper, but only if a hand is free — so the phone gets a name, and answers
 * to it.
 *
 * How the mic is shared, and why it is split in two:
 *
 *   The wake word is spotted by the browser's own recogniser (SpeechRecognition), which
 *   costs nothing and runs continuously. The command that follows is recorded and sent to
 *   Sarvam, exactly as a button press would be. That division is the whole design: sending
 *   a continuous stream to a paid ASR would blow the 10-rupee-a-month ceiling many times
 *   over, and the browser recogniser is nowhere near good enough on code-mixed Tamil item
 *   names to bill from. One is a doorbell, the other reads the order.
 *
 *   The shipped Android app should replace the doorbell with an on-device keyword model
 *   (openWakeWord and friends), which is both better and genuinely free. Nothing above the
 *   wake_() call has to change when it does.
 *
 * The clip ends on three seconds of quiet, measured from the waveform rather than by
 * asking the recogniser — a shop at 70-80 dB(A) never goes truly silent, so the threshold
 * is relative to the room, not absolute.
 */

(function handsFree() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const SILENCE_MS = 3000;      // the pause that means "I have finished the sentence"
  const MAX_CLIP_MS = 20000;    // nothing a shopkeeper says in one breath is longer
  const CALIBRATE_MS = 600;     // how long we listen to the room before judging quiet

  let on = false;               // the shopkeeper's switch
  let rec = null;               // the wake-word recogniser
  let capturing = false;        // a command clip is being recorded right now
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
    return words.some((w) => s.includes(` ${w} `) || s.includes(`${w} `));
  };

  /* ---- deciding when the sentence has ended ---- */

  function listenToRoom() {
    if (!stream) return;
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (!analyser) {
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      audioCtx.createMediaStreamSource(stream).connect(analyser);
      data = new Uint8Array(analyser.fftSize);
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
  }

  function level() {
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
      const speaking = rms > floor * 2.2;
      if (speaking) { quietSince = 0; return; }
      if (!quietSince) quietSince = now;
      if (now - quietSince >= SILENCE_MS || now - startedAt >= MAX_CLIP_MS) endCapture();
    }, 100);
  }

  /* ---- the capture itself ---- */

  function beginCapture() {
    if (capturing || busy) return;
    capturing = true;
    // The recogniser and the recorder both want the microphone; letting the doorbell keep
    // ringing while the order is being read gets the wake word into the order.
    stopRecogniser();
    listenToRoom();
    startRec();
    setTalk("rec");
    if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
    watchForSilence();
  }

  function endCapture() {
    if (!capturing) return;
    capturing = false;
    clearInterval(vadTimer);
    stopRec();
    // handleClip is async; the doorbell goes back on once it has let go of the mic.
    setTimeout(() => { if (on) startRecogniser(); }, 1200);
  }

  /* ---- the doorbell ---- */

  function startRecogniser() {
    if (!on || capturing || !SR) return;
    try {
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = srLang();
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (heardName(e.results[i][0].transcript)) { beginCapture(); return; }
        }
      };
      // Chrome ends a continuous session on its own every minute or so, and on any silence
      // it decides is long enough. Restarting is not error handling, it is the normal path.
      rec.onend = () => { if (on && !capturing) setTimeout(startRecogniser, 300); };
      rec.onerror = (e) => {
        if (e.error === "not-allowed" || e.error === "service-not-allowed") {
          setEnabled(false);
          speak(t("handsFreeNo"));
        }
      };
      rec.start();
    } catch (err) { /* a failed start is picked up by onend and retried */ }
  }

  function stopRecogniser() {
    if (!rec) return;
    const r = rec;
    rec = null;
    try { r.onend = null; r.stop(); } catch (err) { /* already gone */ }
  }

  /* ---- the switch ---- */

  async function setEnabled(next) {
    on = next;
    localStorage.setItem("boloHandsFree", on ? "1" : "0");
    $("miWake").setAttribute("aria-checked", String(on));
    $("miWakeState").textContent = on ? t("on") : t("off");
    document.body.classList.toggle("handsfree", on);
    if (on) {
      await openMic();          // the recorder needs the stream ready before the wake word
      await loadWords();
      startRecogniser();
      $("talkLabel").innerHTML = `${t("sayChitti")}<br><small>${t("holdToSpeak")}</small>`;
    } else {
      stopRecogniser();
      endCapture();
      $("talkLabel").innerHTML = t("holdToSpeak");
    }
  }

  window.handsFreeActive = () => on;
  window.handsFreeRefresh = () => { if (on) setEnabled(true); };

  $("miWake").onclick = () => {
    if (!SR) { speak(t("handsFreeNo")); return; }
    setEnabled(!on);
  };

  // Restored on load, but only once a shop is signed in — the mic prompt belongs after the
  // passcode, not in front of it.
  window.handsFreeRestore = () => {
    if (!SR) { $("miWake").classList.add("dim"); return; }
    if (localStorage.getItem("boloHandsFree") === "1") setEnabled(true);
    else setEnabled(false);
  };
})();
