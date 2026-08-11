/* Vaakku PoC — voice billing.
   Rules that drive most of this file:
   1. The button's colour must never lie about whether the mic is live.
   2. A low-confidence line is shown and asked about, never silently added (Principle 2).
   3. Only an unresolved line already on the bill blocks finalising. */

/* Push to talk, and nothing else: hold, speak as many items as you like, release. One
   gesture, one clip, one result — the same on every press.

   This replaces latching plus silence-based segmentation. Cutting on a pause meant the
   recording could end somewhere the shopkeeper did not choose, and a moment's hesitation
   split an item in two. Splitting the transcript afterwards is strictly more reliable:
   the grammar sees the whole sentence and can use word order, prices and quantities to
   find the boundaries, none of which a silence detector knows anything about. */
const MIN_CLIP_MS = 250;    // shorter than this is a mis-tap, not speech

const $ = (id) => document.getElementById(id);
/* Read from the document, not written out by hand. The hand-written list was a screen
   whose name you had to remember to add: a section could be built, styled, linked from the
   menu and reachable by a working handler, and still render as a blank page because show()
   deactivated every screen and activated none. Import shipped that way. A list derived
   from the markup cannot fall out of step with it. */
const screens = [...document.querySelectorAll("section.screen")].map((s) => s.id);
const show = (n) => screens.forEach((s) => $(s).classList.toggle("active", s === n));
const rupees = (n) => "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });

const state = {
  token: "", shop: { id: "", name: "", vpa: "", lang: "en" }, role: "user",
  items: [], mode: "billing", bill: null,
  askingPrice: null, proposal: null, queue: [],
  customer: "", history: [], picked: -1, bills: [],
  debug: localStorage.getItem("boloDebug") === "1",
  aliasing: null,
  expanded: false, products: [],
};

let health = { asr_configured: false };
let stream = null, recorder = null, chunks = [];
let pressedAt = 0, busy = false;

let toastTimer;
/* `always` marks the toasts that report a loss — an item heard but not billed, an
   utterance not understood. Those are the shopkeeper's only sign that goods are going
   out unpaid for, so they survive debug mode being off; everything else is chatter. */
function toast(msg, ms = 2400, always = false) {
  if (!state.debug && !always) return;
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}
const setStatus = (t) => { $("status").textContent = t; };

/* Hands-free means nobody is looking at the screen, so anything that would have been a
   glance has to be said out loud. SpeechSynthesis is on-device and free, which matters:
   the cost ceiling rules out anything that bills per utterance. The toast still shows —
   a loud shop swallows a phone speaker, and it is marked `always` so it survives debug
   mode being off. */
const SPEAK_LANG = { en: "en-IN", ta: "ta-IN", hi: "hi-IN", ml: "ml-IN",
                     te: "te-IN", kn: "kn-IN" };
function speak(msg) {
  toast(msg, 3600, true);
  if (!window.speechSynthesis) return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(msg);
    u.lang = SPEAK_LANG[state.shop && state.shop.lang] || "en-IN";
    u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch (err) { /* speaking is a courtesy, never the mechanism */ }
}

/* Run an action with the button visibly doing it.
   Guards against the second tap as well as announcing the first: the button is inert for
   the whole of the work, so a double tap cannot submit twice however fast it lands. The
   original disabled state is restored rather than assumed, because some of these buttons
   are disabled for their own reasons. */
async function withBusy(btn, fn) {
  if (!btn || btn.classList.contains("busy")) return undefined;
  const wasDisabled = btn.disabled;
  btn.classList.add("busy");
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.classList.remove("busy");
    btn.disabled = wasDisabled;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  // A session lasts a trading day, and until now nothing noticed when it ended. The
  // symptom was a screen reporting the server's words verbatim — "Sign in required" on
  // Past bills, and the worse "Owner only" on Settings, which blames the role for what is
  // really an expired token. One 401 while we believe we are signed in means exactly one
  // thing, so say it once and send them back to the passcode.
  if (res.status === 401 && state.token) sessionExpired();
  return res.json();
}

let expiring = false;
function sessionExpired() {
  if (expiring) return;                       // several calls can fail together
  expiring = true;
  state.token = "";
  try { localStorage.removeItem("vaakku"); } catch (e) { /* private mode */ }
  toast(t("sessionOver"), 5000, true);
  setTimeout(() => { expiring = false; resetAuth(); show("auth"); }, 400);
}

/* ---------- language ---------- */

function applyStrings() {
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  $("mobile").placeholder = "98400 12345";
  $("typeInput").placeholder = state.mode === "admin"
    ? "potato 1 kilo 100 rupees" : t("emptyBill");
  $("talkLabel").innerHTML = t("holdToSpeak");
  // Set here too: this button's label is rewritten as it toggles, so it never picks up a
  // language change from data-t alone.
  $("typeToggle").textContent = $("typeForm").hidden
    ? `⌨ ${t("typeInstead")}` : `✕ ${t("hideTyping")}`;
  $("signIn") && ($("signIn").textContent = t("signIn"));
  applyDebug();
}

(function buildLangPicker() {
  const sel = $("langPick");
  sel.innerHTML = Object.entries(LANGS)
    .map(([c, l]) => `<option value="${c}">${l.native} — ${l.label}</option>`).join("");
  sel.value = "en";
  sel.onchange = () => { setLang(sel.value); applyStrings(); };
})();
applyStrings();

/* ---------- health ---------- */

// Held as a promise, not fire-and-forget. Signing in used to read `health` before this
// resolved, so a returning user saw "Voice is off" on a perfectly working deployment —
// indistinguishable from an actually missing key.
const healthReady = fetch("/api/health").then((r) => r.json()).then((h) => {
  health = h;
  $("healthLine").textContent =
    `ASR: ${h.asr_backend}${h.asr_configured ? "" : " (no key — text mode only)"} · store: ${h.db}`;
  return h;
}).catch(() => {
  $("healthLine").textContent = "Backend unreachable.";
  return health;
});

/* ---------- auth ---------- */

const digits = (s) => (s || "").replace(/\D/g, "");

$("continueBtn").onclick = () => withBusy($("continueBtn"), async () => {
  const mobile = digits($("mobile").value);
  if (mobile.length < 10) { toast(t("need10")); return; }
  const r = await api("/api/auth/check", { method: "POST", body: { mobile } });
  $("continueBtn").hidden = true;
  $("mobile").disabled = true;
  if (r.exists) {
    $("whoLine").textContent = r.shop_name
      ? `${r.shop_name} — ${r.role === "owner" ? t("owner") : t("staff")}`
      : t("passcode");
    $("loginBox").hidden = false;
    $("loginCode").focus();
  } else {
    $("signupBox").hidden = false;
    $("shopName").focus();
  }
});

try {
  const pref = localStorage.getItem("boloRemember");
  if (pref === "0") { $("rememberMe").checked = false; $("rememberMe2").checked = false; }
} catch (e) { /* private mode */ }
$("rememberMe").onchange = $("rememberMe2").onchange = (e) => {
  const on = e.target.checked;
  $("rememberMe").checked = on; $("rememberMe2").checked = on;
  try { localStorage.setItem("boloRemember", on ? "1" : "0"); } catch (err) { /* ignore */ }
};

const resetAuth = () => {
  $("mobile").disabled = false;
  $("continueBtn").hidden = false;
  $("loginBox").hidden = true;
  $("signupBox").hidden = true;
  $("loginCode").value = ""; $("signupCode").value = "";
};
$("backBtn").onclick = resetAuth;
$("backBtn2").onclick = resetAuth;

$("loginBtn").onclick = () => withBusy($("loginBtn"), async () => {
  const r = await api("/api/auth/login", {
    method: "POST",
    body: { mobile: digits($("mobile").value), passcode: digits($("loginCode").value),
            remember: $("rememberMe").checked },
  });
  if (!r.ok) { toast(r.error || "Sign in failed", 3500); $("loginCode").value = ""; return; }
  await enter(r);
});

$("signupBtn").onclick = () => withBusy($("signupBtn"), async () => {
  const code = digits($("signupCode").value);
  const vpa = $("vpa").value.trim();
  if (code.length !== 6) { toast(t("need6")); return; }
  if (!vpa.includes("@")) { toast(t("needUpi")); return; }
  const r = await api("/api/auth/signup", {
    method: "POST",
    body: { mobile: digits($("mobile").value), passcode: code,
            name: $("shopName").value.trim() || "Shop", vpa, lang: $("langPick").value,
            remember: $("rememberMe2").checked },
  });
  if (!r.ok) { toast(r.error || "Could not create business", 4000); return; }
  await enter(r);
});

async function enter(session) {
  await healthReady;
  state.token = session.token;
  state.role = session.role;
  // After the passcode, never before: the microphone prompt in front of a sign-in screen
  // reads as an app asking for something it has not earned yet.
  setTimeout(() => window.handsFreeRestore && window.handsFreeRestore(), 400);
  state.shop = { id: session.shop_id, name: session.shop_name || "Shop",
                 vpa: session.vpa || "", lang: session.lang || "en" };
  setLang(state.shop.lang);
  applyStrings();
  // Storage key deliberately unchanged by the rename — changing it would sign out every
  // existing tester the moment they reload.
  try { localStorage.setItem("vaakku", JSON.stringify(session)); } catch (e) { /* private mode */ }
  $("shopLabel").textContent = state.shop.name;
  // Staff bill and nothing else, so neither the switch nor the account items are there
  // for them. Signing out stays — it is theirs, not the shop's.
  const owner = state.role === "owner";
  $("modeSwitch").hidden = !owner;
  $("miSettings").hidden = !owner;
  $("miStaff").hidden = !owner;
  $("miDiv").hidden = !owner;
  setMode("billing");
  show("main");
  applyAsrAvailability();
  if (health.asr_configured) await openMic();
}

// Resume a session so a reload mid-trade doesn't cost a sign-in.
try {
  const saved = JSON.parse(localStorage.getItem("vaakku") || "null");
  if (saved && saved.token) setTimeout(() => enter(saved), 80);
} catch (e) { /* ignore */ }

/* ---------- hamburger menu ---------- */

/* Settings, Add user and Sign out used to live in three different places: two buried in
   an accordion inside Prices, one as a power glyph in the corner. They are all
   "things you do to the account rather than to this bill", so they belong together —
   and out of the way of a counter that is mid-sale. */
function openMenu(open) {
  $("scrim").classList.toggle("open", open);
  $("menuBtn").setAttribute("aria-expanded", String(open));
}
$("menuBtn").onclick = () => openMenu(true);
$("scrim").onclick = (e) => { if (e.target === $("scrim")) openMenu(false); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") openMenu(false); });

/* Where the menu came from, so Back returns there rather than always to billing. */
let returnScreen = "main";
function goScreen(name) {
  openMenu(false);
  returnScreen = document.querySelector(".screen.active")?.id || "main";
  show(name);
}
document.querySelectorAll("[data-back]").forEach((b) => {
  b.onclick = () => show(returnScreen === "main" ? "main" : "main");
});

$("miSettings").onclick = () => { goScreen("settings"); loadSettings(); };
$("miStaff").onclick = () => { goScreen("staffScreen"); loadStaff(); };
$("miLogout").onclick = () => {
  openMenu(false);
  try { localStorage.removeItem("vaakku"); } catch (e) { /* ignore */ }
  location.reload();
};

/* ---------- mode ---------- */

function setMode(mode) {
  state.mode = mode;
  const admin = mode === "admin";
  $("adminPanel").hidden = !admin;
  $("billPanel").hidden = admin;
  $("finalize").hidden = true;
  $("totalRow").hidden = true;
  document.querySelectorAll("#modeSwitch button")
    .forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  $("typeInput").placeholder = admin ? "potato 1 kilo 100 rupees" : t("emptyBill");
  setStatus(admin ? t("sayItemPrice") : t("ready"));
  if (admin) loadCatalog(); else render();
}

document.querySelectorAll("#modeSwitch button").forEach((b) => {
  b.onclick = () => setMode(b.dataset.mode);
});

/* ---------- mic ---------- */

function applyAsrAvailability() {
  if (health.asr_configured) return;
  const b = $("asrBanner");
  b.hidden = false;
  b.innerHTML = `🔇 ${t("voiceOff")}`;
  $("talk").disabled = true;
  $("talk").classList.add("dead");
  $("talkLabel").innerHTML = t("voiceUnavailable");
  $("typeForm").hidden = false;
  $("typeToggle").hidden = true;
}

/* The phone's own microphone is 20cm from the shopkeeper's mouth only while the phone is
   in their hand — which is the thing we are trying to stop. A wired lapel or boundary mic
   on the counter is near-field permanently, costs a few hundred rupees, and is chosen here
   rather than assumed. The browser exposes it as just another input device, so this is a
   deviceId and nothing else changes. */
const MIC_PREF = "boloMicId";

async function openMic() {
  if (stream) return;
  const want = (() => { try { return localStorage.getItem(MIC_PREF) || ""; } catch (e) { return ""; } })();
  const base = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      // `exact` on purpose: silently falling back to the built-in mic would make a mic
      // comparison meaningless, and we would be measuring the wrong device.
      audio: want ? { ...base, deviceId: { exact: want } } : base,
    });
  } catch (err) {
    if (want) {                                  // the chosen mic is gone — say so, then fall back
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: base });
        toast(t("micGone"), 4000, true);
      } catch (e2) { /* handled below */ }
    }
    if (!stream) {
      setStatus(t("voiceUnavailable"));
      toast("Allow microphone access, then reload.", 4000);
      return;
    }
  }
  const track = stream.getAudioTracks()[0];
  micLabel = (track && track.label) || "default";
}

let micLabel = "";

/* Device labels are hidden until permission has been granted once, so the picker is
   populated after the mic has been opened, not before. */
async function listMics() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((d) => d.kind === "audioinput");
  } catch (err) { return []; }
}

async function renderMicPicker() {
  const sel = $("setMic");
  if (!sel) return;
  const mics = await listMics();
  const cur = (() => { try { return localStorage.getItem(MIC_PREF) || ""; } catch (e) { return ""; } })();
  sel.innerHTML = `<option value="">${t("micDefault")}</option>` + mics.map((m, i) =>
    `<option value="${m.deviceId}"${m.deviceId === cur ? " selected" : ""}>${
      m.label || `${t("micDefault")} ${i + 1}`}</option>`).join("");
  sel.onchange = async () => {
    try { localStorage.setItem(MIC_PREF, sel.value); } catch (e) { /* private mode */ }
    releaseMicForSwitch();
    await openMic();
    toast(`${t("micNow")} ${micLabel || t("micDefault")}`, 3000, true);
  };
}

/* Switching device means dropping the stream the old one owns. */
function releaseMicForSwitch() {
  if (!stream) return;
  try { stream.getTracks().forEach((tr) => tr.stop()); } catch (e) { /* gone */ }
  stream = null;
}

function setTalk(mode) {
  $("talk").className = "talk " + mode;
  const pt = $("payTalk");
  if (pt) pt.className = "talk paytalk " + mode;
  const l = $("talkLabel");
  if (mode === "rec") {
    l.innerHTML = `${t("recording")}<br><small>${t("releaseToStop")}</small>`;
  } else if (mode === "busy") {
    l.innerHTML = t("working");
  } else if (window.handsFreeActive && window.handsFreeActive()) {
    l.innerHTML = `${t("sayChitti")}<br><small>${t("holdToSpeak")}</small>`;
  } else {
    l.innerHTML = t("holdToSpeak");
  }
}

const pickMime = () => ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"]
  .find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";

function startRec() {
  if (!stream || busy) return;
  chunks = [];
  const mimeType = pickMime();
  recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = handleClip;
  recorder.start();
  pressedAt = Date.now();
  setTalk("rec");
  setStatus(t("speak"));
  if (navigator.vibrate) navigator.vibrate(12);
}

function stopRec() {
  if (recorder && recorder.state === "recording") recorder.stop();
}


async function handleClip() {
  const ms = Date.now() - pressedAt;
  const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
  if (ms < MIN_CLIP_MS || blob.size < 1200) { setTalk("idle"); setStatus(t("ready")); return; }
  busy = true;
  setTalk("busy");
  const t0 = performance.now();
  try {
    const fd = new FormData();
    fd.append("audio", blob, "clip.webm");
    fd.append("shop_id", state.shop.id);
    fd.append("mode", state.mode);
    fd.append("lang", state.shop.lang || "ta");
    fd.append("mic", micLabel || "");
    fd.append("clip_ms", String(ms));
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    const data = await res.json();
    // Which microphone produced this, how long the clip was, and what came back. Without
    // these three, "the counter mic is better" is an impression rather than a result.
    logAttempt(data, ms, Math.round(performance.now() - t0));
    apply(data, Math.round(performance.now() - t0));
  } catch (err) {
    toast(t("network"));
    setStatus(t("ready"));
  } finally {
    busy = false;
    setTalk("idle");
  }
}

$("payMore").onclick = () => {
  const box = $("payExtra");
  box.hidden = !box.hidden;
  $("payMore").textContent = box.hidden ? t("moreActions") : t("fewerActions");
};

const talk = $("talk");
/* The press acquires the microphone if nothing is holding it.
   It used to be held open from startup, so pressing could assume it was there. Hands-free
   now gives it back between utterances — which quietly killed the button: startRec() bails
   on a missing stream and says nothing, so after hands-free had run once, push-to-talk did
   nothing at all until the page was reloaded. Taking the mic away was right; letting the
   primary way of billing depend on somebody else having left it open was not.
   `holding` covers the race, because acquiring takes a moment and a thumb can be gone
   before it lands — without it that press would start a recording nobody ever stops. */
let holding = false;
talk.addEventListener("pointerdown", async (e) => {
  e.preventDefault();
  holding = true;
  if (!stream) { setTalk("rec"); await openMic(); }
  if (holding && stream) startRec();
  else if (!stream) { setTalk("idle"); toast(t("voiceUnavailable"), 3000, true); }
  else setTalk("idle");
});
// Release ends the utterance, wherever the finger lifts. pointerup only fires on the
// element it started on, so lostpointercapture covers a thumb that slides off mid-press —
// otherwise the recorder would run on with nobody watching it.
const release = () => { holding = false; stopRec(); };
talk.addEventListener("pointerup", (e) => { e.preventDefault(); release(); });
talk.addEventListener("pointercancel", release);
talk.addEventListener("lostpointercapture", release);
talk.addEventListener("contextmenu", (e) => e.preventDefault());

/* The same press-to-talk, on the payment screen. "Chitti, cash paid" and "Chitti, send
   receipt" were always commands for THIS screen; until now the only way to reach them was
   hands-free, which left anyone without the wake word tapping their way through. */
const payTalk = $("payTalk");
payTalk.addEventListener("pointerdown", (e) => { e.preventDefault(); startRec(); });
payTalk.addEventListener("pointerup", (e) => { e.preventDefault(); stopRec(); });
payTalk.addEventListener("pointercancel", stopRec);
payTalk.addEventListener("lostpointercapture", stopRec);
payTalk.addEventListener("contextmenu", (e) => e.preventDefault());

/* ---------- measurement ----------
   Kept on the device and never sent anywhere on its own. Its whole purpose is to answer
   questions we have been guessing at: how long a bill really takes, how often a line has
   to be corrected, and whether a counter microphone is actually better than the phone. */
function logAttempt(data, clipMs, roundTripMs) {
  try {
    const rec = {
      at: Date.now(), mic: micLabel || "default", clip_ms: clipMs, ms: roundTripMs,
      asr_ms: data.asr_ms || 0,
      items: (data.items || []).length,
      // A confirm is the parser saying "I am not sure" — the single best proxy for
      // recognition quality that does not need a human to grade it.
      confirms: (data.items || []).filter((i) => i.verdict === "confirm").length,
      rejects: (data.items || []).filter((i) => i.verdict === "reject").length,
      unmatched: (data.unmatched || []).length,
      empty: !data.transcript,
      chars: (data.transcript || "").length,
    };
    const log = JSON.parse(localStorage.getItem("boloLog") || "[]");
    log.push(rec);
    localStorage.setItem("boloLog", JSON.stringify(log.slice(-500)));
  } catch (err) { /* measurement must never break billing */ }
}

/* A correction is the strongest signal we have and the only one that needs the shopkeeper.
   Called wherever a line is removed or overwritten shortly after it appeared. */
function logCorrection(kind) {
  try {
    const log = JSON.parse(localStorage.getItem("boloLog") || "[]");
    const last = log[log.length - 1];
    if (last && Date.now() - last.at < 30000) {
      last.corrections = (last.corrections || 0) + 1;
      last.correction_kind = kind;
      localStorage.setItem("boloLog", JSON.stringify(log));
    }
  } catch (err) { /* never break billing */ }
}

/* Grouped by microphone, because that is the comparison being run. */
window.micReport = function micReport() {
  const log = JSON.parse(localStorage.getItem("boloLog") || "[]");
  const by = {};
  for (const r of log) {
    const k = r.mic || "default";
    by[k] = by[k] || { n: 0, ms: 0, trouble: 0, corrections: 0 };
    const b = by[k];
    b.n++; b.ms += r.ms; b.corrections += r.corrections || 0;
    // Counted once per utterance rather than once per symptom — an utterance that was both
    // unmatched and silent is one bad utterance, not two.
    if (r.confirms || r.rejects || r.unmatched || r.empty) b.trouble++;
  }
  return Object.entries(by).map(([mic, b]) => ({
    mic, utterances: b.n,
    avg_ms: Math.round(b.ms / b.n),
    // The headline: how often the system was unsure, wrong, or heard nothing.
    trouble_rate: +(b.trouble / b.n).toFixed(3),
    corrections_per_utterance: +(b.corrections / b.n).toFixed(3),
  }));
};

/* ---------- results ---------- */

function apply(data, roundTripMs) {
  if (data.error) {
    setStatus(`${t("notHeard")}: ${data.error}`);
    toast(`${t("notHeard")}: ${data.error}`, 3500);
    return;
  }
  if (!data.transcript) { setStatus(t("notHeard")); return; }

  const timing = data.asr_ms != null
    ? `${roundTripMs} ms (asr ${data.asr_ms}, parse ${data.parse_ms})` : `${roundTripMs} ms`;
  setStatus(`“${data.transcript}” · ${timing}`);

  // An outstanding price question is answered with a bare number. Anything else means the
  // shopkeeper has moved on, so abandon the question rather than leaving it stuck — that
  // is what used to hide the Finalise button for the rest of the bill.
  if (state.askingPrice) {
    if (data.number != null) { resolvePrice(data.number); return; }
    const skipped = state.askingPrice.name;
    state.askingPrice = null;
    hidePrompt();
    toast(`${skipped} — ${t("noPriceSkipped")}`, 3200);
  }

  // "Which item is this?" is answered by saying the name the shop already uses for it —
  // the shopkeeper never types, and the catalog match we already have is exactly the
  // right tool for turning that name into a product. Anything else abandons the question
  // rather than leaving it stuck.
  if (state.aliasing) {
    const hit = (data.items || []).find((i) => i.product_id && i.verdict !== "reject");
    if (hit) { linkAlias(hit.product_id); return; }
    const dropped = state.aliasing.name;
    state.aliasing = null;
    hidePrompt();
    toast(`${dropped} — ${t("notBilled")}`, 3200, true);
  }

  // A customer number opens the bill against that person. Handled before items so one
  // utterance can carry both: "phone number 98400 12345, two kilo sugar".
  if (data.customer_mobile) setCustomer(data.customer_mobile);

  if (data.admin && data.admin.length) { queueChanges(data.admin); return; }
  if (state.mode === "admin") {
    toast(t("sayItemPrice"));
    return;
  }
  if (data.mode_switch && !data.items.length && !data.command) return;

  /* ---- hands-free commands ----
     These arrive by voice with the phone untouched, so each one says out loud what it
     did. A command that acts silently is unusable when nobody is looking at the screen. */
  if (data.command === "new_bill") {
    newBill();
    if (!data.items.length) { speak(t("newBillReady")); render(); return; }
    // fall through: "bill me one filter coffee" starts the bill AND fills it
  }
  if (data.command === "cash_paid") { cashReceived(); return; }
  if (data.command === "send_receipt") { sendReceiptByVoice(data.customer_mobile); return; }
  if (data.command === "stock_in") { moveStock(data, "inward"); return; }
  if (data.command === "stock_count") { moveStock(data, "count"); return; }
  if (data.command === "add_item") { addItemByVoice(data); return; }

  if (data.command === "cancel_last" && state.items.length) {
    toast(`${t("removed")} ${state.items.pop().name}`);
    render(); return;
  }
  if (data.command === "clear_all") { state.items = []; render(); toast(t("cleared")); return; }
  if (data.command === "total" && state.items.length) { finalize(); return; }

  let added = 0, asked = 0;
  for (const it of data.items) {
    if (it.verdict === "reject") continue;
    if (it.needs_price) { askPrice(it); asked++; continue; }
    addOrUpdate({ ...it, pending: it.verdict === "confirm" });
    it.verdict === "confirm" ? asked++ : added++;
  }

  // Understood, but not in this shop's catalog. Ask the price once, create the SKU and put
  // it on the bill — the shopkeeper never has to stop and go set the catalog up first.
  // This used to run only when nothing else had been understood, so in a multi-item
  // breath the one unknown item disappeared without a word while its neighbours billed
  // fine. Now the shop can dictate three items, have two land and still be told about
  // the third. Anything beyond the first is named in a toast rather than queued: the
  // shopkeeper needs to know it was dropped, and one price prompt at a time is enough.
  const unknown = data.unmatched || [];
  if (unknown.length) {
    askWhichItem(unknown[0]);
    asked++;
  }
  const lost = unknown.slice(1).map((u) => u.name).concat(data.unparsed || []);
  if (lost.length) toast(`${t("notBilled")}: ${lost.join(", ")}`, 3600, true);

  if (!added && !asked && !data.customer_mobile) {
    toast(data.transcript
      ? `“${data.transcript}” — ${t("couldNotParse")}`
      : t("notHeard"), 3200, true);
  }
  render();
}

/* Saying an item again corrects it rather than billing it twice. A shopkeeper who repeats
   himself is fixing what he just said — "two kilo sugar… no, three kilo sugar" — and a
   second line would silently double the customer's bill. */
function addOrUpdate(line) {
  const key = (l) => l.combo && l.combo.length
    ? l.combo.map((c) => c.product_id || c.name).join("+")
    : (l.product_id || l.name);
  const i = state.items.findIndex((l) => key(l) === key(line));
  if (i === -1) { state.items.push(line); return false; }
  const was = state.items[i].amount;
  state.items[i] = line;
  if (was !== line.amount) {
    logCorrection("restated");
    toast(`${line.name} — ${t("updated")} ${rupees(line.amount)}`);
  }
  return true;
}

/* ---------- customer + repeat-order history ---------- */

async function setCustomer(mobile) {
  state.customer = mobile;
  state.picked = -1;
  $("custNum").textContent = mobile;
  $("custBar").hidden = false;
  try {
    const j = await api(
      `/api/history?shop_id=${encodeURIComponent(state.shop.id)}` +
      `&mobile=${encodeURIComponent(mobile)}&limit=5`);
    state.history = (j.bills || []).filter((b) => (b.items || []).length);
  } catch (err) { state.history = []; }
  renderHistory();
}

/* Removing the customer takes away the identity and the baskets, but NOT the bill.
   A misheard digit does not mean the items are wrong, and clearing a half-built bill
   because a number was mistyped would be its own small disaster. */
function clearCustomer() {
  state.customer = "";
  state.history = [];
  state.picked = -1;
  $("custBar").hidden = true;
  $("custNum").textContent = "";
  $("custMobile").value = "";
  renderHistory();
}
$("custClear").onclick = () => { clearCustomer(); toast(t("customerRemoved")); };

/* ---------- debug mode ---------- */

function applyDebug() {
  document.body.classList.toggle("debug", state.debug);
  $("miDebug").setAttribute("aria-checked", String(state.debug));
  $("miDebugState").textContent = state.debug ? t("on") : t("off");
}
$("miDebug").onclick = () => {
  state.debug = !state.debug;
  localStorage.setItem("boloDebug", state.debug ? "1" : "0");
  applyDebug();
};

/* "Last" for the most recent, dd-mmm before that. The date is only a memory cue, so the
   year is left off — it would cost width and tell the shopkeeper nothing. */
function chipLabel(bill, index) {
  if (index === 0) return t("lastVisit");
  const d = new Date(bill.created_at);
  if (isNaN(d)) return "-";
  const mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()];
  return `${String(d.getDate()).padStart(2, "0")}-${mon}`;
}

function renderHistory() {
  const row = $("histRow");
  if (!state.customer || !state.history.length) { row.hidden = true; row.innerHTML = ""; return; }
  row.hidden = false;
  row.innerHTML = state.history.slice(0, 5).map((b, i) =>
    `<button class="chip${i === state.picked ? " on" : ""}" data-hist="${i}">${chipLabel(b, i)}
       <small>${b.items.length} - ${rupees(b.total)}</small></button>`).join("");
  row.querySelectorAll("[data-hist]").forEach((el) => {
    el.onclick = () => pickBill(+el.dataset.hist);
  });
}

/* A chip REPLACES the bill rather than adding to it: picking a past basket means "the
   same again", and tapping a second chip is a correction, not a second order. So the
   cart is cleared each time — including anything spoken since, which the toast says
   out loud so it is never a silent loss. Tapping the selected chip again clears it.
   Prices come from the catalog as it stands today, never from the old bill: charging
   last month's rate would be wrong, and silently so. */
function pickBill(index) {
  const bill = state.history[index];
  if (!bill) return;
  const had = state.items.length;

  if (index === state.picked) {            // tap the selected one again to undo it
    state.picked = -1;
    state.items = [];
    renderHistory();
    render();
    toast(t("cleared"));
    return;
  }

  state.picked = index;
  state.items = [];
  for (const it of bill.items || []) {
    if (!it.name || it.amount == null) continue;
    const current = state.products.find((p) => p.id === it.product_id);
    const unitPrice = current ? current.unit_price : it.unit_price;
    const qty = it.qty || 1;
    state.items.push({
      ...it, unit_price: unitPrice,
      amount: +(it.price_led ? it.amount : qty * unitPrice).toFixed(2),
      pending: false, needs_price: false,
    });
  }
  renderHistory();
  render();
  toast(had ? `${state.items.length} ${t("loadedReplacing")}` : `${state.items.length} ${t("added")}`);
}

/* ---------- learning a price (D4) ---------- */

/* A name the catalog has never heard. Before offering to create a product, ask whether it
   is one the shop already sells under another name — because most of the time it is.
   "பொட்டேட்டோ" and "உருளைக்கிழங்கு" are the same potato; no string metric will ever join
   them, and answering "new item" mid-sale is how a shop ends up with two potatoes at two
   prices. Saying so once teaches the catalog for good. */
function askWhichItem(u) {
  const item = {
    product_id: null, name: u.name, qty: u.qty || 1, unit: u.unit || "piece",
    unit_price: 0, amount: u.money || 0, price_led: u.money != null, isNew: true,
  };
  const near = (u.candidates || []).filter((c) => c.score >= 0.45).slice(0, 3);
  if (!state.products.length) { askPrice(item); return; }   // nothing to be the same as

  state.aliasing = item;
  showPrompt({
    kind: t("notInList"),
    main: `“${u.name}” — ${t("whichItem")}`,
    note: t("saySoldName"),
    chips: near.map((c) => ({ label: c.name, id: c.id })),
    extra: [{ label: t("pickFromList"), act: "pick" },
            { label: t("itIsNew"), act: "new" }],
    onChip: (id) => linkAlias(id),
    onExtra: (act) => (act === "pick" ? openPicker() : startNewItem()),
    onCancel: () => { state.aliasing = null; hidePrompt(); render(); },
  });
  setStatus(`${u.name} — ${t("whichItem")}`);
}

function startNewItem() {
  const item = state.aliasing;
  state.aliasing = null;
  hidePrompt();
  askPrice(item);
}

/* The whole catalog, tappable. The near misses above are a shortcut; a synonym scores
   nothing at all against the name it means, so there has to be a way to just point. */
function openPicker() {
  const item = state.aliasing;
  showPrompt({
    kind: t("notInList"),
    main: `“${item.name}” — ${t("whichItem")}`,
    // The hint stays: even with the whole list on screen, saying the name is faster than
    // finding it, and a shopkeeper who never learns that keeps scrolling forever.
    note: t("saySoldName"),
    chips: state.products.map((p) => ({ label: p.name, id: p.id })),
    scroll: true,
    extra: [{ label: t("itIsNew"), act: "new" }],
    onChip: (id) => linkAlias(id),
    onExtra: () => startNewItem(),
    onCancel: () => { state.aliasing = null; hidePrompt(); render(); },
  });
}

/* Attach the spoken name to a product the shop already sells, then bill it — the sale
   never stops for the catalog. */
async function linkAlias(productId) {
  const item = state.aliasing;
  const product = state.products.find((p) => p.id === productId);
  state.aliasing = null;
  hidePrompt();
  if (!product) { render(); return; }
  try {
    const j = await api("/api/alias", {
      method: "POST",
      body: { product_id: productId, alias: item.name },
    });
    if (j.product) mergeProduct(j.product);
  } catch (err) { toast(t("notSaved"), 3000); }

  const price = Number(product.unit_price) || 0;
  const qty = item.price_led && price > 0
    ? +(item.amount / price).toFixed(3)
    : item.qty;
  addOrUpdate({
    product_id: product.id, name: product.name, qty,
    unit: product.unit, unit_price: price,
    amount: +(item.price_led ? item.amount : qty * price).toFixed(2),
    confidence: 1, verdict: "accept", price_led: item.price_led,
    needs_price: price <= 0, pending: false,
  });
  speak(`${item.name} = ${product.name}`);
  render();
}

function askPrice(item) {
  state.askingPrice = item;
  showPrompt({
    kind: item.isNew ? t("newItem") : t("priceUnknown"),
    main: `${item.name} — ${t("whatPrice")}`,
    note: `${t("sayPricePer")} ${item.unit}. ${item.isNew ? t("willBeAdded") : t("remembered")}`,
    onCancel: () => { state.askingPrice = null; hidePrompt(); render(); },
  });
  setStatus(`${item.name} — ${t("whatPrice")}`);
}

async function resolvePrice(price) {
  const item = state.askingPrice;
  state.askingPrice = null;
  hidePrompt();
  const j = await api("/api/catalog", {
    method: "POST",
    body: { shop_id: state.shop.id, id: item.product_id || "", name: item.name,
            unit: item.unit, unit_price: price },
  });
  if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4500); render(); return; }
  if (item.product_id == null && j.product) item.product_id = j.product.id;
  mergeProduct(j.product);
  const qty = item.price_led ? +(item.amount / price).toFixed(3) : item.qty;
  addOrUpdate({
    ...item, unit_price: price, qty,
    amount: +(item.price_led ? item.amount : qty * price).toFixed(2),
    needs_price: false, pending: false,
  });
  toast(`${item.name} → ${rupees(price)}/${item.unit}`);
  render();
}

/* ---------- admin: catalog ---------- */

async function commitProduct(a, quiet) {
  const j = await api("/api/catalog", {
    method: "POST",
    body: { shop_id: state.shop.id, id: a.id || "", name: a.name,
            unit: a.unit, unit_price: a.price },
  });
  if (!j.ok) {
    toast(`${t("notSaved")}: ${j.error || ""}`, 4500);
    return false;
  }
  mergeProduct(j.product);
  if (!quiet) { toast(`${a.name} → ${rupees(a.price)}/${a.unit}`); await loadCatalog(); }
  return true;
}

/* A single clip can carry several dictated items. Commit every unambiguous one straight
   away, then work through the rest one prompt at a time. */
async function queueChanges(list) {
  const ask = [];
  let saved = 0;
  for (const a of list) {
    if (a.certain) { if (await commitProduct(a, true)) saved++; }
    else ask.push(a);
  }
  if (saved) { toast(`${saved} ${t("added")}`); await loadCatalog(); }

  // Append, never replace. Dictating a run of items used to overwrite the queue, so an
  // item still waiting to be confirmed was silently dropped the moment the next one was
  // spoken — while the certain ones went on announcing themselves as added.
  state.queue = (state.queue || []).concat(ask);
  if (!state.proposal) nextInQueue();
}

function nextInQueue() {
  const a = (state.queue || []).shift();
  if (a) proposeChange(a); else hidePrompt();
}

function proposeChange(a) {
  // Unambiguous changes are written immediately and appear in the list. Confirmation is
  // reserved for the two cases that are genuinely uncertain: a name close to something
  // that already exists, and a quantity that leaves rate-vs-pack open.
  if (a.certain) { state.proposal = null; hidePrompt(); commitProduct(a); return; }

  state.proposal = a;
  const isNew = a.action === "create";
  showPrompt({
    kind: isNew ? t("newItem") : t("priceChange"),
    main: `${a.name} — ${rupees(a.price)}/${a.unit}`,
    note: a.qty && a.qty !== 1
      ? `${a.qty} ${a.unit} for ${rupees(a.price * a.qty)} → ${rupees(a.price)} per ${a.unit}. Right?`
      : isNew
        ? (a.near && a.near_score > 0.7 ? `Not “${a.near}”? Cancel if it is.` : "New item for this shop.")
        : `was ${rupees(a.was)}`,
    warn: isNew && a.near_score > 0.7,
    onOk: async () => {
      hidePrompt();
      await commitProduct(a);
      state.proposal = null;
      nextInQueue();
    },
    onCancel: () => { state.proposal = null; nextInQueue(); },
  });
}

/* Dictating with a 500ms pause fires several catalog refreshes at once, and they can come
   back out of order — an earlier, shorter list landing last would overwrite the newer one
   and re-render without the item just added, while the toast still said it saved. Only the
   most recently issued request is allowed to write state. `fresh=1` also bypasses the
   server-side cache, which is per serverless instance and so not guaranteed to have seen
   the write. */
let catalogSeq = 0;

function sortProducts(list) {
  return list.slice().sort((a, b) =>
    (b.unit_price > 0) - (a.unit_price > 0) ||
    String(a.name || "").localeCompare(String(b.name || "")));
}

async function loadCatalog() {
  const seq = ++catalogSeq;
  const j = await api(
    `/api/catalog?shop_id=${encodeURIComponent(state.shop.id)}&fresh=1`);
  if (seq !== catalogSeq) return;                 // a newer refresh already won
  state.products = sortProducts(j.products || []);
  renderCatalog();
}

/* The write response is authoritative for the row it just saved, so show it immediately
   rather than waiting on a refetch that might race or fail. */
function mergeProduct(row) {
  if (!row || !row.id) return;
  const i = state.products.findIndex((p) => p.id === row.id);
  if (i === -1) state.products.push(row); else state.products[i] = { ...state.products[i], ...row };
  state.products = sortProducts(state.products);
  if (state.mode === "admin") renderCatalog();
}

function renderCatalog() {
  const box = $("skuList");
  if (!state.products.length) { box.innerHTML = `<p class="empty small">No items yet.</p>`; return; }
  $("skuCount").textContent = `(${state.products.length})`;
  box.innerHTML = state.products.map((p, i) => `
    <div class="skurow${p.unit_price > 0 ? "" : " unpriced"}">
      <span class="sku-n">${p.name}${p.description ? `<em>${p.description}</em>` : ""}${
        (p.aliases || []).length ? `<span class="aliases">${(p.aliases || []).map((a, k) =>
          `<button class="alias" data-unalias="${i}:${k}" title="${t("removeAlias")}">${a}<i>×</i></button>`
        ).join("")}</span>` : ""}</span>
      <span class="sku-u">${p.unit}${p.category && p.category !== "resale"
        ? `<i class="catmark cat-${p.category}">${catLabel(p.category)}</i>` : ""}</span>
      <span class="sku-p">${p.unit_price > 0 ? rupees(p.unit_price) : "—"}</span>
      <button class="sku-e" data-edit="${i}" aria-label="Edit">✎</button>
      <button class="sku-d" data-del="${i}" aria-label="Delete">🗑</button>
    </div>`).join("");
  box.querySelectorAll("[data-edit]").forEach((b) => {
    b.onclick = () => editSku(state.products[+b.dataset.edit]);
  });
  box.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => deleteSku(state.products[+b.dataset.del]);
  });
  // Every alias the shop has been taught, with a way to take it back. An alias that bills
  // the wrong item is precisely the thing that has to be visible and undoable.
  box.querySelectorAll("[data-unalias]").forEach((b) => {
    b.onclick = async () => {
      const [pi, ai] = b.dataset.unalias.split(":").map(Number);
      const prod = state.products[pi];
      const alias = (prod.aliases || [])[ai];
      if (!prod || alias == null) return;
      try {
        const j = await api("/api/alias/remove", {
          method: "POST", body: { product_id: prod.id, alias },
        });
        if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4000); return; }
        mergeProduct(j.product);
        renderCatalog();
        toast(`${alias} — ${t("removed")}`);
      } catch (err) { toast(t("network")); }
    };
  });
}

function deleteSku(p) {
  showPrompt({
    kind: t("deleteTitle"),
    main: `${p.name} — ${p.unit_price > 0 ? rupees(p.unit_price) : "—"}/${p.unit}`,
    note: t("clearNote"),
    warn: true,
    onOk: async () => {
      hidePrompt();
      const j = await api("/api/catalog/delete", { method: "POST", body: { id: p.id } });
      if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4000); return; }
      toast(`${p.name} — ${t("removed")}`);
      loadCatalog();
    },
    onCancel: hidePrompt,
  });
}

/* Four kinds of thing sit on a café's shelf and they answer different questions, so the
   editor asks which one this is. `resale` is the default everywhere, because it is the
   inert answer: it decrements itself on sale, exactly as everything did before categories
   existed. Nothing a shop already has changes behaviour until someone says otherwise. */
const CATEGORIES = ["raw", "consumable", "menu", "resale"];
const catLabel = (c) => t(`cat_${c}`) || c;

function editSku(p) {
  const box = $("skuList");
  const row = document.createElement("div");
  row.className = "skuedit";
  const cat = p.category || "resale";
  row.innerHTML = `<input class="e-n" value="${esc(p.name)}" placeholder="Item">
    <input class="e-u" value="${esc(p.unit)}" placeholder="UOM">
    <input class="e-p" type="number" step="0.01" value="${p.unit_price || ""}" placeholder="Price">
    <select class="e-c">${CATEGORIES.map((c) =>
      `<option value="${c}"${c === cat ? " selected" : ""}>${catLabel(c)}</option>`).join("")}</select>
    <button class="mini go">${t("save")}</button><button class="mini x">✕</button>`;
  box.prepend(row);
  row.querySelector(".x").onclick = () => row.remove();
  row.querySelector(".go").onclick = async () => {
    const j = await api("/api/catalog", {
      method: "POST",
      body: { shop_id: state.shop.id, id: p.id, name: row.querySelector(".e-n").value.trim(),
              unit: row.querySelector(".e-u").value.trim() || "piece",
              category: row.querySelector(".e-c").value,
              unit_price: parseFloat(row.querySelector(".e-p").value) || 0 },
    });
    if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4000); return; }
    row.remove();
    toast(t("saved"));
    loadCatalog();
  };
}

/* ---------- admin: staff ---------- */

$("clearCatalogBtn").onclick = () => {
  if (!state.products.length) { toast(t("alreadyEmpty")); return; }
  showPrompt({
    kind: t("clearTitle"),
    main: `${state.products.length} items`,
    note: t("clearNote"),
    warn: true,
    onOk: async () => {
      hidePrompt();
      const j = await api("/api/catalog", { method: "DELETE" });
      if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4500); return; }
      toast(`${j.removed} ${t("removed")}`);
      loadCatalog();
    },
    onCancel: hidePrompt,
  });
};

/* ---------- settings ---------- */

(function buildSettingsLangPicker() {
  $("setLang").innerHTML = Object.entries(LANGS)
    .map(([c, l]) => `<option value="${c}">${l.native} — ${l.label}</option>`).join("");
})();

async function loadSettings() {
  const j = await api("/api/settings");
  if (!j.ok) { toast(j.error || t("signInRequired"), 3500); return; }
  $("setName").value = j.name || "";
  $("setVpa").value = j.vpa || "";
  $("setWa").value = j.wa_number || "";
  $("setGstin").value = j.gstin || "";
  renderMicPicker();
  $("gstState").textContent = j.gst_state ? `${j.gst_state} · ${t("gstOnReceipt")}` : "";
  $("setLang").value = j.lang;
  $("setMobile").textContent = `${t("signedInAs")} ${j.mobile}`;
  // A stored value that isn't a language code is what broke dictation for a shop whose
  // interface still looked right. Show it rather than quietly normalising in silence.
  if (j.stored_lang && j.stored_lang !== j.lang) {
    $("setMobile").textContent += `  ·  stored “${j.stored_lang}” → ${j.lang}`;
  }
}

/* The comparison, in the shopkeeper's own shop rather than in a lab. Trouble rate is the
   share of utterances the system was unsure about, got wrong, or heard nothing in — the
   best proxy for recognition quality that does not need a human to grade every line. */
$("micReportBtn").onclick = () => {
  const rows = window.micReport();
  const out = $("micReportOut");
  out.hidden = false;
  out.textContent = rows.length
    ? rows.map((r) => `${r.mic}\n  utterances      ${r.utterances}\n  avg round trip  ${r.avg_ms} ms\n` +
        `  trouble rate    ${(r.trouble_rate * 100).toFixed(1)}%\n` +
        `  corrections     ${r.corrections_per_utterance} per utterance`).join("\n\n")
    : t("noData");
};

$("setSave").onclick = (e) => withBusy($("setSave"), async () => {
  e.preventDefault();
  const j = await api("/api/settings", {
    method: "POST",
    body: { name: $("setName").value.trim(), lang: $("setLang").value,
            vpa: $("setVpa").value.trim(),
            wa_number: digits($("setWa").value),
            gstin: $("setGstin").value.trim().toUpperCase() },
  });
  // The GST number is checked to its last character before it is stored, so a rejection
  // here is a real one and worth showing on the field rather than in a passing toast.
  if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4500); return; }
  // Apply immediately: language drives the interface, the parser pack and the ASR locale,
  // so it must take effect on the very next utterance rather than at the next sign-in.
  state.shop.name = j.name;
  state.shop.lang = j.lang;
  state.shop.vpa = j.vpa;
  state.shop.wa_number = j.wa_number;
  state.shop.gstin = j.gstin;
  $("setGstin").value = j.gstin || "";
  renderMicPicker();
  $("gstState").textContent = j.gst_state ? `${j.gst_state} · ${t("gstOnReceipt")}` : "";
  setLang(j.lang);
  applyStrings();
  $("shopLabel").textContent = j.name;
  try {
    const saved = JSON.parse(localStorage.getItem("vaakku") || "{}");
    localStorage.setItem("vaakku", JSON.stringify(
      { ...saved, shop_name: j.name, lang: j.lang, vpa: j.vpa }));
  } catch (err) { /* private mode */ }
  toast(t("saved"));
});

$("staffForm").onsubmit = async (e) => {
  e.preventDefault();
  const j = await api("/api/staff", {
    method: "POST",
    body: { mobile: digits($("staffMobile").value), passcode: digits($("staffCode").value),
            name: $("staffName").value.trim() },
  });
  if (!j.ok) { toast(j.error || "Could not add", 4000); return; }
  toast(`${j.mobile} ${t("added")}`);
  $("staffForm").reset();
  loadStaff();
};

async function loadStaff() {
  const j = await api("/api/staff");
  const rows = (j.staff || []).filter((s) => s.role !== "owner");
  $("staffCount").textContent = rows.length ? `${rows.length}` : "";
  $("staffList").innerHTML = rows.length
    ? rows.map((s) => `<div class="staffrow"><b>${s.mobile}</b><span>${s.name || "—"}</span></div>`).join("")
    : `<p class="empty small">${t("noStaff")}</p>`;
}

/* ---------- prompt ---------- */

const hidePrompt = () => { $("prompt").hidden = true; $("prompt").innerHTML = ""; };

function showPrompt({ kind, main, note, warn, onOk, onCancel,
                     chips, extra, scroll, onChip, onExtra }) {
  const box = $("prompt");
  box.hidden = false;
  const chipRow = (chips || []).length
    ? `<div class="promptchips${scroll ? " tall" : ""}">${chips.map((c) =>
        `<button class="chip" data-chip="${c.id}">${c.label}</button>`).join("")}</div>` : "";
  const extraRow = (extra || []).length
    ? `<div class="promptextra">${extra.map((e) =>
        `<button class="ghost small" data-extra="${e.act}">${e.label}</button>`).join("")}</div>` : "";
  box.innerHTML = `<div class="promptbody"><b>${kind}</b>
      <div class="promptmain">${main}</div>
      ${note ? `<div class="promptnote${warn ? " warn" : ""}">${note}</div>` : ""}
      ${chipRow}${extraRow}</div>
    <div class="promptacts">${onOk ? `<button class="yes" data-ok>${t("yes")}</button>` : ""}
      <button class="del" data-no aria-label="Cancel">✕</button></div>`;
  const ok = box.querySelector("[data-ok]");
  if (ok) ok.onclick = onOk;
  box.querySelector("[data-no]").onclick = onCancel;
  box.querySelectorAll("[data-chip]").forEach((el) => {
    el.onclick = () => onChip && onChip(el.dataset.chip);
  });
  box.querySelectorAll("[data-extra]").forEach((el) => {
    el.onclick = () => onExtra && onExtra(el.dataset.extra);
  });
}

/* ---------- billing list (accordion) ---------- */

function render() {
  if (state.mode === "admin") return;
  const box = $("items");
  const n = state.items.length;

  if (!n) {
    box.hidden = false;
    box.innerHTML = `<p class="empty">${t("emptyBill")}<br><span class="en">${
      health.asr_configured ? t("emptyHint") : t("emptyHintType")}</span></p>`;
    $("totalRow").hidden = true;
    $("finalize").hidden = true;
    return;
  }

  const pending = state.items.filter((i) => i.pending);
  const total = state.items.reduce((s, i) => s + (i.pending ? 0 : i.amount), 0);

  // Always open. A bill is a flat list of what the customer is buying — there is nothing
  // to group, and collapsing it meant an item could be added without being visible, which
  // is the one thing this screen must never do.
  box.hidden = false;

  box.innerHTML = state.items.map((it, i) => {
    // A blend reads as "0.8+0.2 kg" so the shopkeeper can see both parts at a glance,
    // rather than a single 1 kg that hides what was actually weighed.
    const qty = it.combo && it.combo.length
      ? `${it.combo.map((c) => fmtNum(c.qty)).join("+")} ${it.unit}`
      : it.price_led ? `${rupees(it.amount)} worth`
                     : `${fmtNum(it.qty)} ${it.unit}`;
    return `<div class="item ${it.pending ? "confirm" : ""}">
      <span class="qty">${qty}</span>
      <span class="nm">${it.name}${
        it.combo && it.combo.length
          ? `<span class="parts">${it.combo.map((c) => `${c.name} ${rupees(c.amount)}`).join(" + ")}</span>`
          : ""}${it.pending ? `<span class="ask">${t("isThisRight")}</span>` : ""}</span>
      <span class="amt">${rupees(it.amount)}</span>
      ${it.pending ? `<button class="yes" data-ok="${i}">${t("yes")}</button>` : ""}
      <button class="del" data-del="${i}" aria-label="Remove">✕</button></div>`;
  }).join("");
  box.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => { logCorrection("removed"); state.items.splice(+b.dataset.del, 1); render(); };
  });
  box.querySelectorAll("[data-ok]").forEach((b) => {
    b.onclick = () => { state.items[+b.dataset.ok].pending = false; render(); };
  });

  $("runningTotal").innerHTML = pending.length
    ? `${rupees(total)}<span class="pendingnote">+${pending.length} ${t("toConfirm")}</span>`
    : rupees(total);
  $("totalRow").hidden = false;
  // Only an unresolved line already on the bill blocks finalising. An unanswered price
  // question does not — that item was never added, so there is nothing wrong to bill.
  $("finalize").hidden = pending.length > 0;
  if (pending.length) setStatus(t("confirmFirst"));
}

/* ---------- typed fallback ---------- */

$("typeToggle").onclick = () => {
  const f = $("typeForm");
  f.hidden = !f.hidden;
  $("typeToggle").textContent = f.hidden ? `⌨ ${t("typeInstead")}` : `✕ ${t("hideTyping")}`;
  if (!f.hidden) $("typeInput").focus();
};

$("typeForm").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("typeInput").value.trim();
  if (!text) return;
  $("typeInput").value = "";
  const t0 = performance.now();
  try {
    const d = await api("/api/parse", {
      method: "POST",
      body: { text, shop_id: state.shop.id, mode: state.mode, lang: state.shop.lang },
    });
    apply(d, Math.round(performance.now() - t0));
  } catch (err) { toast(t("network")); }
};

/* ---------- finalise, pay, receipt ---------- */

/* The screen changes on the tap, not on the response.
   Finalise allots a receipt number, writes a bill and builds a document — hundreds of
   milliseconds on a good connection and seconds on a shop's 3G. Waiting for that before
   moving meant the shopkeeper pressed again, or said "close bill" again, and got two.
   So the payment screen appears immediately in a waiting state and fills in when the
   answer arrives. The double-press has nowhere to land because the button is already gone. */
function showPaymentPending() {
  $("payAmount").textContent = rupees(state.items.reduce((s, i) => s + (i.pending ? 0 : i.amount), 0));
  $("qr").removeAttribute("src");
  $("qr").classList.add("loading");
  $("payRef").textContent = t("working");
  $("sendReceipt").disabled = true;
  $("nextSale").disabled = true;
  show("payment");
}

async function finalize() {
  if (!state.items.length || state.items.some((i) => i.pending)) return;
  $("finalize").disabled = true;
  showPaymentPending();
  try {
    const d = await api("/api/finalize", {
      method: "POST",
      body: { shop_id: state.shop.id, items: state.items,
              vpa: state.shop.vpa, payee: state.shop.name,
              customer_mobile: state.customer || "" },
    });
    state.bill = d;
    // Held from the finalise response, not fetched on demand. Opening WhatsApp has to
    // happen in the same tick as the tap, and anything awaited first ends the gesture —
    // which is exactly why the button appeared to do nothing.
    state.doc = { receipt: d.receipt, text: d.receipt_text, message: d.receipt_message };
    /* Already identified by voice at the start of the bill: no reason to ask again. */
    if (state.customer) $("custMobile").value = state.customer;
    $("payAmount").textContent = rupees(d.total);
    $("paidAmount").textContent = rupees(d.total);
    $("qr").src = d.qr;
    $("qr").classList.remove("loading");
    $("sendReceipt").disabled = false;
    $("nextSale").disabled = false;
    // The number and the moment, both fixed at finalise. Shown here as well as on the
    // receipt so the shopkeeper can read them back to a customer without printing.
    $("payRef").textContent = d.receipt_no
      ? `${d.receipt_no} · ${stamp(d.receipt && d.receipt.issued_at)}`
      : d.ref;
  } catch (err) {
    // Back to the bill rather than stranded on a payment screen with no QR.
    $("qr").classList.remove("loading");
    show("main");
    toast(t("notSaved"), 4000, true);
  } finally { $("finalize").disabled = false; }
}

$("finalize").onclick = () => withBusy($("finalize"), finalize);
$("backToBill").onclick = () => show("main");

/* Close the sale. `mobile` empty means the customer did not want a receipt. */
async function closeSale(mobile) {
  let result = null;
  if (state.bill) {
    try {
      result = await api("/api/receipt", {
        method: "POST",
        body: { bill_id: state.bill.bill_id, shop_id: state.shop.id, mobile },
      });
    } catch (err) { /* the sale still ends; the record can catch up */ }
  }
  showThanks(result);
}

/* The thank-you belongs to the customer, so it says nothing about totals, delivery or
   anything they would have to act on. It clears itself and the phone comes back ready.
   Whatever the shopkeeper needs to know is told to him afterwards, on his own screen. */
let thanksTimer;
function showThanks(result) {
  $("paidAmount").textContent = rupees((state.bill && state.bill.total) || 0);
  show("receipt");
  clearTimeout(thanksTimer);
  thanksTimer = setTimeout(() => {
    newBill();
    // Said after the phone is back with the shopkeeper, not in front of the customer.
    if (result && result.receipt_status === "requested" && !result.delivered) {
      toast(t("receiptSavedNotSent"), 4000);
    }
  }, 3200);
}
$("thanksTap").onclick = () => { clearTimeout(thanksTimer); newBill(); };

/* The button said Send receipt and sent nothing — it recorded the number and moved on.
   WhatsApp opens first, in the same tick as the tap, because that is the only moment the
   browser will allow it; the sale is then closed behind it. If WhatsApp cannot be opened
   the number is still recorded, so the sale is never lost to a failed hand-off. */
$("sendReceipt").onclick = () => {
  const mobile = digits($("custMobile").value);
  if (mobile.length < 10) { toast(t("noNumber"), 3200); $("custMobile").focus(); return; }
  openWhatsApp(mobile);
  withBusy($("sendReceipt"), () => closeSale(mobile));
};

$("nextSale").onclick = () => withBusy($("nextSale"), () => closeSale(""));

/* The purchase time, as the shop reads it. Formatted from the document rather than from
   the clock: the two are the same at finalise and are not the same on a reprint. */
function stamp(iso) {
  const d = iso ? new Date(iso) : new Date();
  if (isNaN(d)) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- stock ---------- */

/* "Chitti, received twenty kilo sugar" and "Chitti, count sugar eight kilo". The same
   grammar that reads a bill reads these — an item and a quantity is an item and a
   quantity, and only the verb differs. */
async function moveStock(data, reason) {
  if (state.role !== "owner") { speak(t("ownerOnly")); return; }
  const moves = (data.items || [])
    .filter((i) => i.product_id && i.verdict !== "reject")
    .map((i) => ({ product_id: i.product_id, qty: i.qty, name: i.name, unit: i.unit }));
  if (!moves.length) { speak(t("sayItemQty")); return; }
  try {
    const j = await api("/api/stock", { method: "POST", body: { moves, reason } });
    if (!j.ok) { speak(t("notSaved")); return; }
    const said = moves.map((m) => `${m.name} ${m.qty} ${m.unit}`).join(", ");
    speak(`${reason === "inward" ? t("stockedIn") : t("counted")} ${said}`);
    if (document.querySelector(".screen.active").id === "stock") openStock();
  } catch (err) { speak(t("network")); }
}

/* The gap between what the ledger says should be on the shelf and what was counted. That
   difference is the number worth showing — everything else on this screen is its working. */
async function openStock() {
  goScreen("stock");
  const box = $("stockList");
  box.innerHTML = `<p class="empty small">${t("working")}</p>`;
  try {
    const j = await api("/api/stock");
    if (!j.ok) { box.innerHTML = `<p class="empty small">${j.error || t("notSaved")}</p>`; return; }
    const lost = $("lossTop");
    if (j.total_lost > 0) {
      lost.hidden = false;
      lost.innerHTML = `<b>${rupees(j.total_lost)}</b><span>${t("unaccountedFor")}</span>`;
    } else { lost.hidden = true; }

    // Grouped, because the four kinds of thing answer different questions. Beans running
    // short is a supply problem; cups running short is a purchasing one; a menu item has
    // no shelf at all and is here only to say so.
    const groups = j.groups && j.groups.length
      ? j.groups
      : [{ category: "", items: j.items || [], value_lost: j.total_lost, on_hand: 0 }];
    box.innerHTML = groups.map((g) => {
      const head = g.category ? `<div class="grouphead">
        <span class="catmark cat-${g.category}">${catLabel(g.category)}</span>
        <span class="dim">${g.on_hand > 0 ? `${t("onShelfWorth")} ${rupees(g.on_hand)}` : ""}${
          g.value_lost > 0 ? ` · ${rupees(g.value_lost)} ${t("lostWord")}` : ""}</span>
      </div>` : "";
      return head + g.items.map(stockRow).join("");
    }).join("") || `<p class="empty small">${t("noStock")}</p>`;
  } catch (err) { box.innerHTML = `<p class="empty small">${t("network")}</p>`; }
}

function stockRow(r) {
  // Only a shortfall gets colour. A surplus is usually a miscount, not a windfall.
  const gap = r.unaccounted < 0
    ? `<span class="pill unpaid">${fmtNum(r.unaccounted)} ${r.unit} · ${rupees(r.value_lost)}</span>`
    : r.unaccounted > 0 ? `<span class="pill">+${fmtNum(r.unaccounted)} ${r.unit}</span>` : "";
  // A menu item is assembled at the moment of sale and never sat on a shelf, so a stock
  // figure for it would be a fiction. Say what it is instead of printing a zero.
  const sub = r.category === "menu"
    ? `<div class="hsub">${t("madeToOrder")}</div>`
    : `<div class="hsub">${t("onShelf")} ${fmtNum(r.stock)} ${r.unit} · ${t("soldWord")} ${fmtNum(r.sold)} · ${t("inWord")} ${fmtNum(r.inward)}</div>`;
  return `<div class="hrow"><div class="hmain"><b>${esc(r.name)}</b> ${gap}${sub}</div></div>`;
}

/* ---------- recipes ---------- */

/* The link between "an Americano was sold" and "20 g of beans left the shelf". Without it
   a café's entire input side is invisible to the stock screen — the only things it can
   see are the finished items, which were never on a shelf to begin with. */
let recipeState = { items: [], components: [] };

async function openRecipes() {
  goScreen("recipes");
  const box = $("recipeList");
  box.innerHTML = `<p class="empty small">${t("working")}</p>`;
  try {
    const j = await api("/api/recipes");
    if (!j.ok) { box.innerHTML = `<p class="empty small">${j.error || t("notSaved")}</p>`; return; }
    recipeState = { items: j.items || [], components: j.components || [] };
    renderRecipes();
  } catch (err) { box.innerHTML = `<p class="empty small">${t("network")}</p>`; }
}
$("miRecipes").onclick = openRecipes;

function renderRecipes() {
  const box = $("recipeList");
  if (!recipeState.items.length) {
    box.innerHTML = `<p class="empty small">${t("noSellable")}</p>`;
    return;
  }
  // No ingredients categorised yet means every draft would come back empty, and the AI
  // button would look broken. Say what is actually missing.
  const noParts = !recipeState.components.length;
  box.innerHTML = (noParts ? `<p class="empty small">${t("noComponents")}</p>` : "")
    + recipeState.items.map((s, i) => {
      const parts = s.components.map((c) =>
        `<div class="rpart">${esc(c.name)} <b>${fmtQtyUnit(c.qty, c.unit)}</b></div>`).join("");
      // Materials cost against selling price, when both are known. This is the first
      // honest answer the app can give to "what am I making on this".
      const margin = s.cost > 0 && s.unit_price > 0
        ? `<span class="pill${s.cost >= s.unit_price ? " unpaid" : " paid"}">${
            t("costsWord")} ${rupees(s.cost)} · ${Math.round((1 - s.cost / s.unit_price) * 100)}%</span>`
        : "";
      return `<div class="hrow rrow">
        <div class="hmain">
          <b>${esc(s.name)}</b> ${margin}
          <div class="rparts">${parts || `<i class="dim">${t("noRecipeYet")}</i>`}</div>
        </div>
        <div class="hacts">
          <button class="mini" data-redit="${i}">${t("editWord")}</button>
          ${noParts ? "" : `<button class="mini go" data-rai="${i}">${t("askAi")}</button>`}
        </div></div>`;
    }).join("");
  box.querySelectorAll("[data-redit]").forEach((b) => {
    b.onclick = () => editRecipe(recipeState.items[+b.dataset.redit]);
  });
  box.querySelectorAll("[data-rai]").forEach((b) => {
    b.onclick = () => withBusy(b, () => draftRecipe(recipeState.items[+b.dataset.rai]));
  });
}

/* 0.02 kg is a true number and an unreadable one. Shown in whatever unit a person would
   say out loud, while what is stored stays the component's own stock unit — the display
   bends, the arithmetic does not. */
function fmtQtyUnit(qty, unit) {
  const q = +qty || 0;
  const small = smallUnit(unit);
  return small !== unit && q < 1
    ? `${fmtNum(q * 1000)} ${small}` : `${fmtNum(q)} ${unit}`;
}

async function draftRecipe(item) {
  try {
    const j = await api("/api/recipe/draft", {
      method: "POST", body: { product_id: item.id },
    });
    if (!j.ok) {
      toast(j.error === "no_components" ? t("noComponents") : (j.error || t("notSaved")),
            5000, true);
      return;
    }
    // Opened for editing, never saved. A recipe applied silently would start consuming
    // stock on every later sale from numbers nobody read.
    editRecipe({ ...item, components: j.components }, j.note);
  } catch (err) { toast(t("network"), 3000, true); }
}

function editRecipe(item, note = "") {
  // Converted to display units once, here, so `shown` is the only quantity the sheet ever
  // reads or writes. Carrying both and picking between them meant a line the shopkeeper
  // did not touch was converted a second time on save: open a recipe, fix one number, and
  // every other gram-scale line silently became a thousandth of itself. Beans then never
  // depleted, and the shrinkage screen — the whole point of this — went quiet.
  let parts = item.components.map((c) => ({ ...c, shown: displayQty(c) }));
  const box = $("recipeList");
  const sheet = document.createElement("div");
  sheet.className = "rsheet";
  box.prepend(sheet);

  const draw = () => {
    const rows = parts.map((c, i) => `<div class="rline">
      <span>${esc(c.name)}</span>
      <input type="number" step="any" inputmode="decimal" value="${displayQty(c)}"
             data-rq="${i}"><span class="dim">${displayUnit(c)}</span>
      <button class="mini danger" data-rx="${i}">✕</button></div>`).join("");
    const options = recipeState.components
      .filter((c) => !parts.some((p) => p.component_id === c.id))
      .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    sheet.innerHTML = `<div class="rsheethead"><b>${esc(item.name)}</b>
        <button class="mini x" data-rclose>✕</button></div>
      ${note ? `<p class="fineprint dim">${esc(note)}</p>` : ""}
      ${rows || `<p class="fineprint dim">${t("noRecipeYet")}</p>`}
      ${options ? `<div class="rline"><select data-radd>${options}</select>
        <button class="mini" data-raddgo>+ ${t("addWord")}</button></div>` : ""}
      <button class="primary wide" data-rsave>${t("save")}</button>`;

    sheet.querySelector("[data-rclose]").onclick = () => sheet.remove();
    sheet.querySelectorAll("[data-rq]").forEach((el) => {
      el.oninput = () => { parts[+el.dataset.rq].shown = el.value; };
    });
    sheet.querySelectorAll("[data-rx]").forEach((el) => {
      el.onclick = () => { parts.splice(+el.dataset.rx, 1); draw(); };
    });
    const addGo = sheet.querySelector("[data-raddgo]");
    if (addGo) {
      addGo.onclick = () => {
        const id = sheet.querySelector("[data-radd]").value;
        const c = recipeState.components.find((x) => x.id === id);
        if (c) { parts.push({ component_id: c.id, name: c.name, unit: c.unit, qty: 0, shown: 0 }); draw(); }
      };
    }
    sheet.querySelector("[data-rsave]").onclick = () =>
      withBusy(sheet.querySelector("[data-rsave]"), async () => {
        const components = parts.map(toStockQty).filter((c) => c.qty > 0);
        try {
          const j = await api("/api/recipe", {
            method: "POST", body: { product_id: item.id, components },
          });
          if (!j.ok) { toast(`${t("notSaved")}: ${j.error || ""}`, 4000, true); return; }
          sheet.remove();
          speak(`${item.name} — ${j.components} ${t("componentsWord")}`);
          openRecipes();
        } catch (err) { toast(t("network"), 3000, true); }
      });
  };
  draw();
}

/* The editor shows grams; the store holds kilos. Which of the two a field is in depends on
   the component's stock unit and nothing else — never on the current value. Deriving it
   from the quantity meant the unit flipped underneath the shopkeeper as they typed: a box
   reading "18 g" became a box meaning kilos the moment they cleared it to type 20, and
   saved twenty kilos of beans per cup. */
/* Keyed on the canonical unit the catalog actually stores, which is "l" — not "litre".
   Matching the spelled-out word meant milk, the one thing a coffee shop measures by
   volume, fell through to being edited in litres while beans were edited in grams. */
const smallUnit = (u) => ({ kg: "g", l: "ml", litre: "ml" }[u] || u);
const displayUnit = (c) => smallUnit(c.unit);
const inSmall = (c) => smallUnit(c.unit) !== c.unit;
/* displayQty converts stock units -> what the box shows. It is called once per part when
   the sheet opens; after that `shown` is authoritative and this is not consulted again. */
const displayQty = (c) => {
  const q = +c.qty || 0;
  return inSmall(c) ? +(q * 1000).toFixed(3) : q;
};
/* toStockQty is its exact inverse and reads `shown` only — never `qty`. Falling back to
   `qty` is what caused the double conversion, because the two are in different units. */
const toStockQty = (c) => {
  const raw = parseFloat(c.shown);
  const v = isFinite(raw) ? raw : 0;
  return { component_id: c.component_id, qty: inSmall(c) ? v / 1000 : v };
};
/* Trailing zeros only mean nothing AFTER a decimal point. Stripping them unconditionally
   turned 20 kg into "2 kg" and 100 into "1" — the amount stayed right, so the bill was
   correct and the screen was lying, which is the worse of the two. */
const fmtNum = (n) => {
  const v = +n || 0;
  return v % 1 ? String(+v.toFixed(3)) : String(Math.round(v));
};
$("miStock").onclick = openStock;

/* ---------- import from paper ---------- */

/* No cold-start wall (D4) says a shop bills on day one with an empty catalog. It does not
   say the catalog has to stay empty, and typing forty products on a phone is nobody's
   evening. Almost every shop already owns the list — on a rate card, a menu board, a
   supplier's delivery note — so the fastest path to a full catalog is a photograph.

   What arrives back is a proposal, never a write. The screen is built around review: rows
   start ticked but each one can be dropped with a tap, a misread name or price is
   correctable in place, and the only thing that writes is the button at the bottom. */

const imp = { kind: "catalog", rows: [], cost: 0 };

function openImport() {
  goScreen("import");
  imp.rows = [];
  $("impList").innerHTML = "";
  $("impStatus").textContent = "";
  $("impFoot").hidden = true;
  setImportKind(imp.kind);
}
$("miImport").onclick = openImport;

function setImportKind(kind) {
  imp.kind = kind;
  $("impKindCatalog").classList.toggle("on", kind === "catalog");
  $("impKindInward").classList.toggle("on", kind === "inward");
  $("impHint").textContent = t(kind === "inward" ? "impHintInward" : "impHintCatalog");
}
$("impKindCatalog").onclick = () => { setImportKind("catalog"); openImport(); };
$("impKindInward").onclick = () => { setImportKind("inward"); openImport(); };

$("impPick").onclick = () => $("impFiles").click();
$("impFiles").onchange = () => {
  const files = [...$("impFiles").files];
  $("impFiles").value = "";                     // so the same photo can be retried
  if (files.length) readDocuments(files);
};

async function readDocuments(files) {
  if (files.length > 5) { toast(t("impTooMany"), 4000, true); return; }
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f, f.name || "page.jpg"));
  fd.append("kind", imp.kind);

  // Reading a page takes seconds, not milliseconds, and a screen that says nothing for ten
  // seconds reads as broken. This is the one place in the app where a wait is expected, so
  // it is named rather than hidden behind a spinner.
  $("impStatus").textContent = t("impReading");
  $("impList").innerHTML = "";
  $("impFoot").hidden = true;
  await withBusy($("impPick"), async () => {
    try {
      const res = await fetch("/api/import", {
        method: "POST",
        headers: state.token ? { Authorization: `Bearer ${state.token}` } : {},
        body: fd,
      });
      if (res.status === 401 && state.token) { sessionExpired(); return; }
      const j = await res.json();
      if (!j.ok) { $("impStatus").textContent = j.error || t("notSaved"); return; }
      imp.rows = j.items || [];
      imp.cost = j.cost_paise || 0;
      // Rows the model could not read are reported rather than quietly absent. A price
      // list that came back four items short and said nothing would be discovered weeks
      // later, at the counter, as a product that does not exist.
      const notes = [];
      if (j.skipped) notes.push(`${j.skipped} ${t("impSkipped")}`);
      if (j.truncated) notes.push(t("impTruncated"));
      $("impStatus").textContent = notes.join(" · ");
      renderImport();
    } catch (err) { $("impStatus").textContent = t("network"); }
  });
}

function renderImport() {
  const box = $("impList");
  if (!imp.rows.length) {
    box.innerHTML = `<p class="empty small">${t("impNothing")}</p>`;
    $("impFoot").hidden = true;
    return;
  }
  box.innerHTML = imp.rows.map((r, i) => (
    imp.kind === "inward" ? inwardRow(r, i) : catalogRow(r, i)
  )).join("");
  box.querySelectorAll("[data-imp]").forEach((el) => {
    el.oninput = () => {
      const r = imp.rows[+el.dataset.imp];
      r[el.dataset.field] = el.type === "checkbox" ? el.checked : el.value;
    };
  });
  box.querySelectorAll("[data-impmatch]").forEach((el) => {
    el.onchange = () => {
      const r = imp.rows[+el.dataset.impmatch];
      r.chosen_id = el.value;
      r.keep = !!el.value;
      renderImport();
    };
  });
  $("impFoot").hidden = false;
  $("impCost").textContent = imp.cost
    ? `${t("impCost")} ${rupees(imp.cost / 100)}` : "";
}

function catalogRow(r, i) {
  const known = r.match;
  const moved = known && Math.abs((r.was || 0) - r.price) > 0.005;
  // Only a price that actually moves is marked. A rate card is mostly figures the shop
  // already has; the two that changed are the whole reason for reading it.
  const tag = !known ? `<span class="pill">${t("impNew")}</span>`
    : moved ? `<span class="pill unpaid">${t("impChanged")}</span>` : "";
  const was = moved ? `<span class="impwas">${rupees(r.was)}</span>` : "";
  if (r.keep === undefined) r.keep = true;
  return `<div class="hrow improw${moved ? " changes" : ""}">
    <input type="checkbox" data-imp="${i}" data-field="keep" ${r.keep ? "checked" : ""}>
    <div class="hmain">
      <b>${esc(known ? known.name : r.name)}</b> ${tag}
      <div class="hsub">${was}₹<input class="mini" style="width:5.5em" inputmode="decimal"
        value="${r.price}" data-imp="${i}" data-field="price"> / ${esc(r.unit)}</div>
      ${r.verbatim ? `<div class="impverb">${esc(r.verbatim)}</div>` : ""}
    </div></div>`;
}

function inwardRow(r, i) {
  // Stock can only move for something the shop already sells — an invoice line with no SKU
  // behind it has nowhere to land, so it says so instead of silently creating one. Prices
  // come from the price list, not from a delivery note.
  const id = r.chosen_id || (r.match && r.match.id) || "";
  if (r.keep === undefined) r.keep = !!id;
  const options = (r.candidates || []).map((c) =>
    `<option value="${c.id}" ${c.id === id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const picker = r.match
    ? `<b>${esc(r.match.name)}</b>`
    : `<select class="mini" data-impmatch="${i}">
         <option value="">${t("impNoMatch")}</option>${options}</select>`;
  return `<div class="hrow improw">
    <input type="checkbox" data-imp="${i}" data-field="keep"
      ${r.keep ? "checked" : ""} ${id ? "" : "disabled"}>
    <div class="hmain">
      ${picker}
      <div class="hsub"><input class="mini" style="width:5em" inputmode="decimal"
        value="${r.qty}" data-imp="${i}" data-field="qty"> ${esc(r.unit)}</div>
      ${r.verbatim ? `<div class="impverb">${esc(r.verbatim)}</div>` : ""}
    </div></div>`;
}

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

$("impSave").onclick = () => withBusy($("impSave"), async () => {
  const kept = imp.rows.filter((r) => r.keep);
  if (!kept.length) { toast(t("impNothingPicked"), 3000, true); return; }
  const items = imp.kind === "inward"
    ? kept.map((r) => ({ product_id: r.chosen_id || (r.match && r.match.id),
                         qty: +r.qty || 0 })).filter((x) => x.product_id && x.qty > 0)
    : kept.map((r) => ({ id: r.match ? r.match.id : "",
                         name: r.match ? r.match.name : r.name,
                         unit: r.unit, price: +r.price || 0 }));
  try {
    const j = await api("/api/import/apply", { method: "POST",
                                               body: { kind: imp.kind, items } });
    if (!j.ok && !j.written) { toast(j.error || t("notSaved"), 4000, true); return; }
    const n = imp.kind === "inward" ? j.applied : j.written;
    speak(`${n} ${t("impSaved")}`);
    imp.rows = [];
    renderImport();
    $("impStatus").textContent = "";
    if (imp.kind === "catalog") loadCatalog();
  } catch (err) { toast(t("network"), 3000, true); }
});

/* ---------- past bills ---------- */

/* The screen exists to answer two questions at a counter: did that one get paid, and can
   you send me that receipt again. So payment state is the loudest thing on the row, and
   both actions are one tap from it. */
async function openHistory() {
  goScreen("history");
  $("histMobile").value = "";
  $("rangeBox").hidden = true;
  $("histNote").hidden = true;
  loadSales();
  loadBills();
}
$("miHistory").onclick = openHistory;

async function loadBills(mobile = "") {
  const box = $("histList");
  box.innerHTML = `<p class="empty small">${t("working")}</p>`;
  try {
    const q = mobile ? `&mobile=${encodeURIComponent(mobile)}` : "";
    const j = await api(`/api/bills?limit=${mobile ? 100 : 40}${q}`);
    if (!j.ok) { box.innerHTML = `<p class="empty small">${j.error || t("notSaved")}</p>`; return; }
    state.bills = j.bills || [];
    // A search that found nobody has to say so. Falling back to the full list would look
    // like the customer's entire history, which is the one wrong answer here.
    if (mobile && !state.bills.length) {
      showHistNote(`${t("noCustomerBills")} ${mobile}`, false);
    } else if (j.customer) {
      const c = j.customer;
      showHistNote(`<b>${rupees(c.total)}</b><span>${c.count} ${t("bills")} · ${c.mobile}${
        c.unpaid > 0 ? ` · ${rupees(c.unpaid)} ${t("unpaid").toLowerCase()}` : ""}${
        c.capped ? ` · ${t("lastN")}` : ""}</span>`, c.unpaid > 0);
    } else {
      $("histNote").hidden = true;
    }
    renderHistoryList();
  } catch (err) { box.innerHTML = `<p class="empty small">${t("network")}</p>`; }
}

function showHistNote(html, warn) {
  const el = $("histNote");
  el.hidden = false;
  el.classList.toggle("calm", !warn);
  el.innerHTML = html;
}

/* ---------- takings ---------- */

/* The owner "doesn't know objectively how much margin he is making" — this is the first
   half of the answer, and the cheapest half: what came in, over the three periods anyone
   running a shop already thinks in. Billed and collected are shown apart, because a total
   that silently includes unpaid bills is a number that will be believed and shouldn't be. */
let salesRows = {};

async function loadSales(frm = "", to = "") {
  const custom = frm && to;
  try {
    const j = await api(custom
      ? `/api/sales?frm=${frm}&to=${to}`
      : "/api/sales");
    if (!j.ok) return;
    if (custom) {
      // A specific question deserves an undivided answer: the three standing periods step
      // aside rather than sitting alongside a range that means something else.
      salesRows = { range: j.range };
      $("kpiRow").innerHTML = `<button class="kpi wide on" data-kpi="range">
        <b>${rupees(j.range.total)}</b><span>${fmtDay(j.range.from)} — ${fmtDay(j.range.to)}</span></button>`;
      showKpi("range");
    } else {
      salesRows = { today: j.today, week: j.week, month: j.month };
      ["today", "week", "month"].forEach((k) => {
        const el = document.querySelector(`[data-kpi="${k}"] b`);
        if (el) el.textContent = rupees(j[k].total);
      });
      showKpi("today");
    }
    $("kpiRow").querySelectorAll("[data-kpi]").forEach((b) => {
      b.onclick = () => showKpi(b.dataset.kpi);
    });
    if (j.partial) toast(t("salesPartial"), 5000, true);
  } catch (err) { /* the bill list is the screen's real job; KPIs are a bonus */ }
}

function showKpi(which) {
  const r = salesRows[which];
  if (!r) return;
  $("kpiRow").querySelectorAll("[data-kpi]").forEach((b) => {
    b.classList.toggle("on", b.dataset.kpi === which);
  });
  const bits = [`${r.count} ${t("bills")}`];
  if (r.unpaid > 0) bits.push(`${rupees(r.unpaid)} ${t("stillUnpaid")}`);
  if (r.cash > 0) bits.push(`${t("cashWord")} ${rupees(r.cash)}`);
  if (r.upi > 0) bits.push(`UPI ${rupees(r.upi)}`);
  $("kpiDetail").textContent = bits.join(" · ");
}

const fmtDay = (iso) => {
  const [y, m, d] = (iso || "").split("-");
  return d ? `${d}/${m}` : iso;
};

$("histFind").onclick = () => {
  const m = digits($("histMobile").value);
  if (!m) { openHistory(); return; }
  if (m.length < 10) { toast(t("noNumber"), 3200); return; }
  loadBills(m);
};
$("histMobile").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("histFind").click(); }
});
$("histRange").onclick = () => { $("rangeBox").hidden = !$("rangeBox").hidden; };
$("rangeGo").onclick = () => {
  const a = $("rangeFrom").value, b = $("rangeTo").value;
  if (!a || !b) { toast(t("pickBothDates"), 3200); return; }
  loadSales(a, b);
};
$("rangeClear").onclick = () => {
  $("rangeFrom").value = "";
  $("rangeTo").value = "";
  $("rangeBox").hidden = true;
  // Rebuilt rather than un-hidden: the range view replaced the three buttons, so the
  // markup they lived in is gone.
  $("kpiRow").innerHTML = ["today", "week", "month"].map((k) =>
    `<button class="kpi" data-kpi="${k}"><b>—</b><span>${t(
      k === "today" ? "kpiToday" : k === "week" ? "kpiWeek" : "kpiMonth")}</span></button>`).join("");
  loadSales();
};

function renderHistoryList() {
  const box = $("histList");
  if (!state.bills.length) { box.innerHTML = `<p class="empty small">${t("noBills")}</p>`; return; }
  box.innerHTML = state.bills.map((b, i) => {
    // Unpaid is the state worth noticing, so it is the one that gets colour.
    const paid = b.paid
      ? `<span class="pill paid">${t("paid")}${b.method ? ` · ${b.method.toUpperCase()}` : ""}</span>`
      : `<span class="pill unpaid">${t("unpaid")}</span>`;
    return `<div class="hrow">
      <div class="hmain">
        <b>${rupees(b.total)}</b> ${paid}
        <div class="hsub">${b.receipt_no || "—"} · ${stamp(b.created_at)}</div>
        <div class="hsub">${b.customer_mobile
          ? `📱 ${b.customer_mobile}` : `<i>${t("noCustomer")}</i>`} · ${b.items} ${t("items")}</div>
      </div>
      <div class="hacts">
        <button class="mini" data-hwa="${i}">${t("shareWhatsapp")}</button>
        <button class="mini" data-hpr="${i}">${t("printBill")}</button>
      </div></div>`;
  }).join("");
  box.querySelectorAll("[data-hwa]").forEach((el) => {
    el.onclick = () => reopenBill(state.bills[+el.dataset.hwa], "wa");
  });
  box.querySelectorAll("[data-hpr]").forEach((el) => {
    el.onclick = () => reopenBill(state.bills[+el.dataset.hpr], "print");
  });
}

/* Fetches the stored document rather than rebuilding one — a receipt sent again must be
   the same receipt, with the same number and the same date. */
async function reopenBill(b, how) {
  if (!b) return;
  try {
    const j = await api(`/api/receipt/${encodeURIComponent(b.id)}`);
    if (!j.ok) { toast(j.error || t("notSaved"), 3500, true); return; }
    state.bill = { bill_id: b.id, total: b.total };
    state.doc = j;
    if (how === "wa") {
      $("custMobile").value = b.customer_mobile || "";
      openWhatsApp(b.customer_mobile || "");
    } else {
      $("docText").textContent = j.text;
      $("docNote").textContent = j.receipt.number
        ? `${j.receipt.title} ${j.receipt.number} · ${stamp(j.receipt.issued_at)}`
        : t("unnumbered");
      goScreen("billdoc");
    }
  } catch (err) { toast(t("network"), 3000, true); }
}

/* ---------- the document ---------- */

/* Fetched, never rebuilt on the client. The receipt that matters is the one the server
   issued and stored under a number; a copy assembled here from whatever the screen happens
   to be holding would be a different document wearing the same serial. */
async function openDoc() {
  if (!state.bill) { toast(t("noBillYet"), 3000, true); return; }
  try {
    const j = await api(`/api/receipt/${encodeURIComponent(state.bill.bill_id)}`);
    if (!j.ok) { toast(j.error || t("notSaved"), 3500, true); return; }
    state.doc = j;
    $("docText").textContent = j.text;
    $("docNote").textContent = j.receipt.number
      ? `${j.receipt.title} ${j.receipt.number} · ${stamp(j.receipt.issued_at)}`
      : t("unnumbered");
    goScreen("billdoc");
  } catch (err) { toast(t("network"), 3000, true); }
}
$("printReceipt").onclick = openDoc;
$("docPrint").onclick = () => window.print();

/* WhatsApp without an API, an account or a rupee: the shopkeeper's own app opens with the
   message already written and the customer already addressed, and they press send. It is
   one tap rather than none, and it works for every shop on day one — which the Business
   API does not, needing verification, an approved template and a per-message fee. */
function waLink(mobile) {
  const msg = (state.doc && state.doc.message) || "";
  if (!msg) return "";
  const to = digits(mobile || "");
  const text = encodeURIComponent(msg);
  return to.length === 10 ? `https://wa.me/91${to}?text=${text}` : `https://wa.me/?text=${text}`;
}

/* Navigating by clicking a real anchor rather than calling window.open(): a popup opened
   from script is blocked on mobile unless the browser can see it came straight from a tap,
   and "straight from" does not survive an await. Nothing is awaited on this path now, but
   the anchor is the belt to that braces. */
function openWhatsApp(mobile, spoken) {
  const href = waLink(mobile);
  if (!href) { toast(t("noBillYet"), 3000, true); return false; }
  if (spoken) {
    // A voice command is not a tap, and a browser will not open a second tab for one —
    // an anchor click without a gesture behind it is blocked exactly like a popup. So the
    // spoken path navigates this tab instead, which is never blocked. The session is in
    // localStorage, so coming back from WhatsApp lands on a signed-in app rather than a
    // sign-in screen.
    window.location.href = href;
    return true;
  }
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  return true;
}

/* "Chitti, send receipt" — and if the customer was never named, "Chitti, phone number
   98400 12345, send receipt" in one breath. The number is taken from whichever of those
   the shopkeeper actually gave: the one just spoken, the one that opened the bill, or the
   one the customer typed on the payment screen.

   The sale is recorded BEFORE the hand-off, not after. The spoken path leaves this page,
   and a request in flight when that happens does not necessarily arrive. */
async function sendReceiptByVoice(spokenMobile) {
  if (!state.bill) { speak(t("noBillYet")); return; }
  const mobile = digits(spokenMobile || state.customer || $("custMobile").value || "");
  if (mobile.length !== 10) { speak(t("needNumberToSend")); return; }
  $("custMobile").value = mobile;
  speak(`${t("sendingTo")} ${mobile}`);
  try {
    await api("/api/receipt", {
      method: "POST",
      body: { bill_id: state.bill.bill_id, shop_id: state.shop.id, mobile },
    });
  } catch (err) { /* the hand-off still goes ahead; the record can catch up */ }
  openWhatsApp(mobile, true);
}
$("waShare").onclick = () => openWhatsApp($("custMobile").value);
$("docWa").onclick = () => openWhatsApp($("custMobile").value);

$("docBt").onclick = () => withBusy($("docBt"), async () => {
  if (!state.doc) return;
  await window.btPrint(state.doc.text);
});

/* "Chitti, cash received" — the shopkeeper stating a fact they witnessed. It is the only
   payment we will ever record from the handset: a UPI settlement has to be confirmed
   server-side by the provider, never inferred from this phone. */
async function cashReceived() {
  if (!state.bill) { speak(t("noBillYet")); return; }
  try {
    await api("/api/settle", {
      method: "POST",
      body: { bill_id: state.bill.bill_id, shop_id: state.shop.id, method: "cash" },
    });
    speak(`${t("cashClosed")} ${rupees(state.bill.total)}`);
  } catch (err) {
    speak(t("notSaved"));
    return;
  }
  closeSale(digits($("custMobile").value).length >= 10 ? digits($("custMobile").value) : "");
}

/* "Chitti, add item lemonade 50 rupees". Prices are the owner's to set — a worker who
   bills all day must not be able to reprice the shop by speaking. */
async function addItemByVoice(data) {
  if (state.role !== "owner") { speak(t("ownerOnly")); return; }
  const u = (data.unmatched || [])[0];
  const heard = u || (data.items || [])[0];
  if (!heard) { speak(t("sayItemPrice")); return; }
  const price = u ? u.money : heard.amount;
  const name = u ? u.name : heard.name;
  if (!price) { speak(t("sayItemPrice")); return; }
  try {
    await api("/api/catalog", {
      method: "POST",
      body: { shop_id: state.shop.id, name, unit: (u && u.unit) || "piece",
              unit_price: price },
    });
    await loadCatalog();
    speak(`${name} ${rupees(price)} — ${t("saved")}`);
  } catch (err) { speak(t("notSaved")); }
}

function newBill() {
  state.items = [];
  state.bill = null;
  $("custMobile").value = "";
  clearCustomer();
  state.askingPrice = null;
  state.proposal = null;
  hidePrompt();
  setMode("billing");
  show("main");
}

