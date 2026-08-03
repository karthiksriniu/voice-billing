/* Vaakku PoC — push-to-talk billing.
   Two rules drive most of this file:
   1. The button's colour must never lie about whether the mic is live.
   2. A low-confidence item is shown and asked about, never silently added (Principle 2). */

const LATCH_MS = 3000;      // hold this long and the button latches, walkie-talkie style
const MIN_CLIP_MS = 250;    // shorter than this is a mis-tap, not speech

const $ = (id) => document.getElementById(id);
const screens = ["consent", "billing", "payment", "receipt"];
const show = (name) =>
  screens.forEach((s) => $(s).classList.toggle("active", s === name));

const state = {
  shop: { id: "demo", name: "Shop", vpa: "" },
  items: [],
  mode: "billing",
  bill: null,
  askingPrice: null,   // a line waiting on "what's the price?" (D4: ask once, remember)
  proposal: null,      // an admin catalog change waiting to be confirmed
};

let stream = null;
let recorder = null;
let chunks = [];
let pressedAt = 0;
let latched = false;
let busy = false;

const rupees = (n) => "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });

let toastTimer;
function toast(msg, ms = 2200) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

function setStatus(text) { $("status").textContent = text; }

function setTalk(mode) {
  const b = $("talk");
  b.className = "talk " + mode;
  const label = $("talkLabel");
  if (mode === "rec") {
    label.innerHTML = latched
      ? "பதிவாகுது…<br><small>Recording — tap to stop</small>"
      : "பதிவாகுது…<br><small>Recording — release to stop</small>";
  } else if (mode === "busy") {
    label.innerHTML = "கேட்குது…<br><small>Working…</small>";
  } else {
    label.innerHTML = "பேச அழுத்துங்க<br><small>Hold to speak</small>";
  }
}

/* ---------- setup ---------- */

let health = { asr_configured: false };

fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    // Warms the serverless function so the first real utterance doesn't eat a cold start.
    health = h;
    $("healthLine").textContent =
      `ASR: ${h.asr_backend}${h.asr_configured ? "" : " (no key — text mode only)"} · ` +
      `catalog: ${h.products} items · store: ${h.db}`;
  })
  .catch(() => { $("healthLine").textContent = "Backend unreachable."; });

/* A mic button that looks live but has no speech backend behind it is worse than no button:
   it swallows utterances and reports nothing the user will notice. Refuse to present one. */
function applyAsrAvailability() {
  if (health.asr_configured) return;
  const b = $("asrBanner");
  b.hidden = false;
  b.innerHTML =
    "🔇 <b>குரல் இயங்கவில்லை / Voice is off.</b> No speech key is configured on the server, " +
    "so nothing you say can be recognised. Type items below instead, or restart the server " +
    "with <code>SARVAM_API_KEY</code> set.";
  const talk = $("talk");
  talk.disabled = true;
  talk.classList.add("dead");
  $("talkLabel").innerHTML =
    "குரல் இயங்கவில்லை<br><small>Voice unavailable — type below</small>";
  $("typeForm").hidden = false;
  $("typeToggle").hidden = true;
}

$("startBtn").onclick = async () => {
  const name = $("shopName").value.trim() || "Shop";
  const vpa = $("vpa").value.trim();
  const mobile = $("mobile").value.trim();
  if (mobile.replace(/\D/g, "").length < 10) {
    toast("மொபைல் நம்பர் வேணும் / Enter a 10-digit mobile number");
    return;
  }
  if (!vpa || !vpa.includes("@")) {
    toast("UPI ID வேணும் / Enter a UPI ID like name@bank");
    return;
  }
  // The mobile number is the shop id, not a login. It exists so two shops demoing at the
  // same time keep separate catalogs — there is no verification and it proves nothing.
  let shopId = "demo";
  try {
    const r = await fetch("/api/shop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mobile, name, vpa }),
    });
    const j = await r.json();
    shopId = j.shop_id || "demo";
    if (j.priced === 0) {
      toast("புதிய கடை — விலை சொல்லிக்கிட்டே போங்க / New shop: prices are learned as you bill", 4200);
    }
  } catch (err) {
    toast("கடை பதிவு ஆகலை / Could not register shop — running locally", 3500);
  }
  state.shop = { id: shopId, name, vpa };
  $("shopLabel").textContent = name;
  show("billing");
  applyAsrAvailability();
  if (!health.asr_configured) { setStatus("தட்டச்சு செய்யுங்க / Type an item below"); return; }
  try {
    // Acquired once and held for the session: getUserMedia is the slow part, and paying
    // that cost on the first item would blow the latency budget the demo is judged on.
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    setStatus("தயார் / Ready");
  } catch (err) {
    setStatus("மைக் அனுமதி இல்லை / Microphone blocked");
    toast("Allow microphone access, then reload.", 4000);
  }
};

/* ---------- push to talk ---------- */

function pickMime() {
  return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"]
    .find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
}

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
  setStatus("பேசுங்க / Speak");
  if (navigator.vibrate) navigator.vibrate(12);
}

function stopRec() {
  if (recorder && recorder.state === "recording") recorder.stop();
  latched = false;
}

async function handleClip() {
  const ms = Date.now() - pressedAt;
  const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
  if (ms < MIN_CLIP_MS || blob.size < 1200) {
    setTalk("idle");
    setStatus("தயார் / Ready");
    return;
  }
  busy = true;
  setTalk("busy");
  const t0 = performance.now();
  try {
    const fd = new FormData();
    fd.append("audio", blob, "clip.webm");
    fd.append("shop_id", state.shop.id);
    fd.append("mode", state.mode);
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    const data = await res.json();
    apply(data, Math.round(performance.now() - t0));
  } catch (err) {
    toast("இணைப்பு இல்லை / Network problem");
    setStatus("தயார் / Ready");
  } finally {
    busy = false;
    setTalk("idle");
  }
}

const talk = $("talk");
talk.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  if (latched) { stopRec(); return; }          // second tap ends a latched recording
  startRec();
});
talk.addEventListener("pointerup", (e) => {
  e.preventDefault();
  if (!recorder || recorder.state !== "recording") return;
  if (Date.now() - pressedAt >= LATCH_MS) {
    latched = true;                            // held long enough — keep going hands-free
    setTalk("rec");
    setStatus("தொடர்ந்து பதிவு / Latched — tap the button to stop");
  } else {
    stopRec();
  }
});
talk.addEventListener("pointercancel", () => { if (!latched) stopRec(); });
talk.addEventListener("contextmenu", (e) => e.preventDefault());

/* ---------- results ---------- */

function apply(data, roundTripMs) {
  if (data.mode_switch && data.mode_switch !== state.mode) {
    state.mode = data.mode_switch;
    const tag = $("modeTag");
    tag.className = "tag " + (state.mode === "admin" ? "admin" : "billing");
    tag.textContent = state.mode === "admin" ? "விலை / ADMIN" : "பில் / BILLING";
    toast(state.mode === "admin" ? "விலை மோட் / Admin mode" : "பில் மோட் / Billing mode");
  }

  if (data.error) {
    // Surfaced loudly as well as in the status line. A failure that only whispers looks
    // identical to the app simply ignoring the shopkeeper.
    setStatus("காதுல விழலை / " + data.error);
    toast("காதுல விழலை / " + data.error, 3500);
    return;
  }
  if (!data.transcript) { setStatus("காதுல விழலை / Didn't catch that"); return; }

  const timing = data.asr_ms != null
    ? `${roundTripMs} ms (asr ${data.asr_ms}, parse ${data.parse_ms})`
    : `${roundTripMs} ms`;
  setStatus(`“${data.transcript}” · ${timing}`);

  // Answering an outstanding "what's the price?" takes priority over everything else —
  // the shopkeeper is mid-sentence with us, not starting a new thought.
  if (state.askingPrice && data.number != null) { resolvePrice(data.number); return; }

  if (data.admin) { proposeChange(data.admin); return; }

  // A bare mode switch ("விலை வாசி") consumes the whole utterance, leaving nothing to
  // parse. That is success, not failure — don't report it as one.
  if (data.mode_switch && !data.items.length && !data.command) return;

  if (state.mode === "admin") {
    toast("விலை சொல்லுங்க / Say an item name, then its price");
    return;
  }

  if (data.command === "cancel_last" && state.items.length) {
    const gone = state.items.pop();
    toast(`நீக்கியாச்சு / Removed ${gone.name}`);
    render();
    return;
  }
  if (data.command === "clear_all") { state.items = []; render(); toast("பில் காலி / Cleared"); return; }
  if (data.command === "total" && state.items.length) { finalize(); return; }

  let added = 0, asked = 0;
  for (const it of data.items) {
    if (it.verdict === "reject") continue;
    // D4: the catalog ships names, not prices. A known word with no price means ask once,
    // then remember it forever — that is what keeps setup time at zero.
    if (it.needs_price) { askPrice(it); asked++; continue; }
    state.items.push({ ...it, pending: it.verdict === "confirm" });
    it.verdict === "confirm" ? asked++ : added++;
  }
  if (!added && !asked) toast("புரியலை / Didn't get an item — try again");
  render();
}

/* ---------- learning a price (D4) ---------- */

function askPrice(item) {
  state.askingPrice = item;
  const per = item.unit;
  showPrompt({
    kind: "விலை தெரியலை / Price not known",
    main: `${item.name} — என்ன விலை?`,
    note: `Say the price per ${per}. It is remembered from now on.`,
    onCancel: () => { state.askingPrice = null; hidePrompt(); render(); },
  });
  setStatus(`${item.name} — என்ன விலை? / What price per ${per}?`);
}

async function resolvePrice(price) {
  const item = state.askingPrice;
  state.askingPrice = null;
  hidePrompt();
  const saved = await saveProduct({
    id: item.product_id, name: item.name, unit: item.unit, unit_price: price,
  });
  if (!saved) return;
  const qty = item.price_led ? +(item.amount / price).toFixed(3) : item.qty;
  state.items.push({
    ...item, unit_price: price, qty,
    amount: +(item.price_led ? item.amount : qty * price).toFixed(2),
    needs_price: false, pending: false,
  });
  toast(`${item.name} → ${rupees(price)}/${item.unit}`);
  render();
}

/* ---------- admin: confirm before changing the catalog ---------- */

function proposeChange(a) {
  state.proposal = a;
  const isNew = a.action === "create";
  showPrompt({
    kind: isNew ? "புதிய பொருள் / New item" : "விலை மாற்றம் / Price change",
    main: `${a.name} — ${rupees(a.price)}/${a.unit}`,
    // A near miss is shown rather than resolved silently: "maida" scores 0.857 against
    // Wheat Flour, and acting on that would reprice a product nobody mentioned.
    note: isNew
      ? (a.near && a.near_score > 0.7 ? `Not “${a.near}”? Cancel if it is.` : "New item for this shop.")
      : `was ${rupees(a.was)}`,
    warn: isNew && a.near_score > 0.7,
    onOk: async () => {
      hidePrompt();
      const ok = await saveProduct({
        id: a.id || "", name: a.name, unit: a.unit, unit_price: a.price,
      });
      if (ok) toast(`${a.name} → ${rupees(a.price)}/${a.unit}`);
      state.proposal = null;
    },
    onCancel: () => { state.proposal = null; hidePrompt(); },
  });
}

async function saveProduct(body) {
  try {
    const r = await fetch("/api/catalog", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, shop_id: state.shop.id }),
    });
    const j = await r.json();
    // Report the failure. A rejected write used to come back looking like a success.
    if (!j.ok) { toast("சேமிக்க முடியலை / Not saved: " + (j.error || "unknown"), 4500); return false; }
    return true;
  } catch (err) {
    toast("சேமிக்க முடியலை / Not saved: network", 4000);
    return false;
  }
}

/* ---------- prompt panel ---------- */

function hidePrompt() { $("prompt").hidden = true; $("prompt").innerHTML = ""; }

function showPrompt({ kind, main, note, warn, onOk, onCancel }) {
  const box = $("prompt");
  box.hidden = false;
  box.innerHTML = `<div class="promptbody">
      <b>${kind}</b>
      <div class="promptmain">${main}</div>
      ${note ? `<div class="promptnote${warn ? " warn" : ""}">${note}</div>` : ""}
    </div>
    <div class="promptacts">
      ${onOk ? `<button class="yes" data-ok>சரி</button>` : ""}
      <button class="del" data-no aria-label="Cancel">✕</button>
    </div>`;
  const ok = box.querySelector("[data-ok]");
  if (ok) ok.onclick = onOk;
  box.querySelector("[data-no]").onclick = onCancel;
}

async function applyAdmin(data) {
  // Admin mode reuses the same grammar: "sugar nooru rubai" is a price-led utterance,
  // so the spoken amount lands in `amount` and becomes the new unit price.
  const priced = data.items.find((i) => i.price_led);
  if (!priced) { toast("விலை சொல்லுங்க / Say: item name, then the price"); return; }
  const body = {
    shop_id: state.shop.id, name: priced.name, unit: priced.unit,
    unit_price: priced.amount,
  };
  await fetch("/api/catalog", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  toast(`${priced.name} → ${rupees(priced.amount)}/${priced.unit}`);
}

function render() {
  const box = $("items");
  if (!state.items.length) {
    const how = health.asr_configured
      ? "Hold the button and say an item."
      : "Type an item below — voice is off on this server.";
    box.innerHTML = `<p class="empty">பொருள் சொல்லுங்க…<br><span class="en">${how}</span></p>`;
    $("totalRow").hidden = true;
    $("finalize").hidden = true;
    return;
  }
  box.innerHTML = state.items.map((it, i) => {
    const qty = it.price_led
      ? `${rupees(it.amount)} worth`
      : `${(+it.qty).toFixed(it.qty % 1 ? 2 : 0)} ${it.unit}`;
    return `<div class="item ${it.pending ? "confirm" : ""}">
      <span class="qty">${qty}</span>
      <span class="nm">${it.name}${it.pending
        ? `<span class="ask">இதுதானா? / Is this right?</span>` : ""}</span>
      <span class="amt">${rupees(it.amount)}</span>
      ${it.pending ? `<button class="yes" data-ok="${i}">சரி</button>` : ""}
      <button class="del" data-del="${i}" aria-label="Remove">✕</button>
    </div>`;
  }).join("");

  box.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => { state.items.splice(+b.dataset.del, 1); render(); };
  });
  box.querySelectorAll("[data-ok]").forEach((b) => {
    b.onclick = () => { state.items[+b.dataset.ok].pending = false; render(); };
  });

  // Unconfirmed lines are excluded from the running total. The shopkeeper reads this number
  // out to the customer; it must never contain a line he hasn't verified.
  const pending = state.items.filter((i) => i.pending);
  const total = state.items.reduce((s, i) => s + (i.pending ? 0 : i.amount), 0);
  $("runningTotal").innerHTML = pending.length
    ? `${rupees(total)}<span class="pendingnote">+${pending.length} உறுதி செய்ய</span>`
    : rupees(total);
  $("totalRow").hidden = false;
  // Finalise stays hidden while anything is unconfirmed — an unresolved item must never
  // silently make it into an amount the customer is asked to pay.
  $("finalize").hidden = pending.length > 0 || !!state.askingPrice;
  if (pending.length) setStatus("உறுதி செய்யுங்க / Confirm the highlighted item first");
}

/* ---------- finalise, pay, receipt ---------- */

async function finalize() {
  if (!state.items.length || state.items.some((i) => i.pending)) return;
  $("finalize").disabled = true;
  try {
    const res = await fetch("/api/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        shop_id: state.shop.id, items: state.items,
        vpa: state.shop.vpa, payee: state.shop.name,
      }),
    });
    const data = await res.json();
    state.bill = data;
    $("payAmount").textContent = rupees(data.total);
    $("paidAmount").textContent = rupees(data.total);
    $("qr").src = data.qr;
    $("payRef").textContent = data.ref;
    show("payment");
  } catch (err) {
    toast("பில் முடியலை / Could not finalise");
  } finally {
    $("finalize").disabled = false;
  }
}

/* ---------- typed fallback ---------- */

$("typeToggle").onclick = () => {
  const f = $("typeForm");
  f.hidden = !f.hidden;
  $("typeToggle").textContent = f.hidden
    ? "⌨ தட்டச்சு / Type instead"
    : "✕ மறை / Hide typing";
  if (!f.hidden) $("typeInput").focus();
};

$("typeForm").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("typeInput").value.trim();
  if (!text) return;
  $("typeInput").value = "";
  const t0 = performance.now();
  try {
    const res = await fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, shop_id: state.shop.id, mode: state.mode }),
    });
    apply(await res.json(), Math.round(performance.now() - t0));
  } catch (err) {
    toast("இணைப்பு இல்லை / Network problem");
  }
};

$("finalize").onclick = finalize;
$("backToBill").onclick = () => show("billing");

$("received").onclick = async () => {
  if (state.bill) {
    const fd = new FormData();
    fd.append("bill_id", state.bill.bill_id);
    fd.append("shop_id", state.shop.id);
    fetch("/api/confirm", { method: "POST", body: fd }).catch(() => {});
  }
  show("receipt");
};

function newBill() {
  state.items = [];
  state.bill = null;
  state.mode = "billing";
  state.askingPrice = null;
  state.proposal = null;
  hidePrompt();
  $("modeTag").className = "tag billing";
  $("modeTag").textContent = "பில் / BILLING";
  render();
  setStatus("தயார் / Ready");
  show("billing");
}
$("nextCustomer").onclick = newBill;
$("resetBtn").onclick = newBill;
