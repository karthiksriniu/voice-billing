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
const screens = ["auth", "main", "payment", "receipt", "settings", "staffScreen",
                 "billdoc"];
const show = (n) => screens.forEach((s) => $(s).classList.toggle("active", s === n));
const rupees = (n) => "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });

const state = {
  token: "", shop: { id: "", name: "", vpa: "", lang: "en" }, role: "user",
  items: [], mode: "billing", bill: null,
  askingPrice: null, proposal: null, queue: [],
  customer: "", history: [], picked: -1,
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
  return res.json();
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
    body: { mobile: digits($("mobile").value), passcode: digits($("loginCode").value) },
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
            name: $("shopName").value.trim() || "Shop", vpa, lang: $("langPick").value },
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

async function openMic() {
  if (stream) return;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    setStatus(t("voiceUnavailable"));
    toast("Allow microphone access, then reload.", 4000);
  }
}

function setTalk(mode) {
  $("talk").className = "talk " + mode;
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
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    apply(await res.json(), Math.round(performance.now() - t0));
  } catch (err) {
    toast(t("network"));
    setStatus(t("ready"));
  } finally {
    busy = false;
    setTalk("idle");
  }
}

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
  if (was !== line.amount) toast(`${line.name} — ${t("updated")} ${rupees(line.amount)}`);
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
      <span class="sku-u">${p.unit}</span>
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

function editSku(p) {
  const box = $("skuList");
  const row = document.createElement("div");
  row.className = "skuedit";
  row.innerHTML = `<input class="e-n" value="${p.name}" placeholder="Item">
    <input class="e-u" value="${p.unit}" placeholder="UOM">
    <input class="e-p" type="number" step="0.01" value="${p.unit_price || ""}" placeholder="Price">
    <button class="mini go">${t("save")}</button><button class="mini x">✕</button>`;
  box.prepend(row);
  row.querySelector(".x").onclick = () => row.remove();
  row.querySelector(".go").onclick = async () => {
    const j = await api("/api/catalog", {
      method: "POST",
      body: { shop_id: state.shop.id, id: p.id, name: row.querySelector(".e-n").value.trim(),
              unit: row.querySelector(".e-u").value.trim() || "piece",
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
  $("gstState").textContent = j.gst_state ? `${j.gst_state} · ${t("gstOnReceipt")}` : "";
  $("setLang").value = j.lang;
  $("setMobile").textContent = `${t("signedInAs")} ${j.mobile}`;
  // A stored value that isn't a language code is what broke dictation for a shop whose
  // interface still looked right. Show it rather than quietly normalising in silence.
  if (j.stored_lang && j.stored_lang !== j.lang) {
    $("setMobile").textContent += `  ·  stored “${j.stored_lang}” → ${j.lang}`;
  }
}

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

$("accHead").onclick = () => { state.expanded = !state.expanded; render(); };

function render() {
  if (state.mode === "admin") return;
  const box = $("items");
  const n = state.items.length;

  if (!n) {
    $("accHead").hidden = true;
    box.hidden = false;
    box.innerHTML = `<p class="empty">${t("emptyBill")}<br><span class="en">${
      health.asr_configured ? t("emptyHint") : t("emptyHintType")}</span></p>`;
    $("totalRow").hidden = true;
    $("finalize").hidden = true;
    return;
  }

  const pending = state.items.filter((i) => i.pending);
  const total = state.items.reduce((s, i) => s + (i.pending ? 0 : i.amount), 0);
  const last = state.items[n - 1];

  // Collapsed by default so the shopkeeper sees a running total rather than a wall of
  // lines — but never collapsed over something still unresolved.
  const open = state.expanded || pending.length > 0;
  $("accHead").hidden = false;
  $("accCount").textContent = `${n} ${n === 1 ? t("item") : t("items")}`;
  $("accLast").textContent = open ? "" : `${last.name} · ${rupees(last.amount)}`;
  $("accTotal").textContent = rupees(total);
  $("accChev").textContent = open ? "⌃" : "⌄";
  $("accHead").classList.toggle("alert", pending.length > 0);
  box.hidden = !open;

  const fmtQty = (n) => (+n).toFixed(n % 1 ? 2 : 0).replace(/\.?0+$/, "") || "0";
  box.innerHTML = state.items.map((it, i) => {
    // A blend reads as "0.8+0.2 kg" so the shopkeeper can see both parts at a glance,
    // rather than a single 1 kg that hides what was actually weighed.
    const qty = it.combo && it.combo.length
      ? `${it.combo.map((c) => fmtQty(c.qty)).join("+")} ${it.unit}`
      : it.price_led ? `${rupees(it.amount)} worth`
                     : `${fmtQty(it.qty)} ${it.unit}`;
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
    b.onclick = () => { state.items.splice(+b.dataset.del, 1); render(); };
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

async function finalize() {
  if (!state.items.length || state.items.some((i) => i.pending)) return;
  $("finalize").disabled = true;
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
    // The number and the moment, both fixed at finalise. Shown here as well as on the
    // receipt so the shopkeeper can read them back to a customer without printing.
    $("payRef").textContent = d.receipt_no
      ? `${d.receipt_no} · ${stamp(d.receipt && d.receipt.issued_at)}`
      : d.ref;
    show("payment");
  } catch (err) {
    toast(t("notSaved"));
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
function openWhatsApp(mobile) {
  const href = waLink(mobile);
  if (!href) { toast(t("noBillYet"), 3000, true); return false; }
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  return true;
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
  state.expanded = false;
  hidePrompt();
  setMode("billing");
  show("main");
}

