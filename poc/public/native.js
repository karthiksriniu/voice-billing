/* The page, when it is running inside the Android app rather than a browser.
 *
 * Everything below the wake_() boundary in wake.js was always meant to be replaced by a
 * real keyword model on the device; this is the other side of that swap. The browser build
 * is untouched — with no bridge present this file does nothing at all and wake.js runs
 * exactly as before.
 *
 * What moves out of the page, and why each one had to:
 *
 *   The wake word. Chrome's recogniser is a cloud service wearing a local API: free, but it
 *   ships the shop's audio to Google all day to listen for one name. The APK carries a
 *   3.3M-parameter keyword model that runs on the phone and cannot transcribe anything but
 *   its own name. That is the privacy claim in D9 becoming literally true rather than
 *   aspirational.
 *
 *   The microphone. A WebView loses the mic when the screen sleeps or the shopkeeper
 *   switches to his UPI app — precisely the two moments a bill is still in progress. A
 *   foreground service does not.
 *
 *   The endpointing. Silero now runs natively on the same frames, so the 800ms/4s/3-clip
 *   arithmetic that was tuned against a real counter recording survives intact, minus the
 *   browser's audio graph.
 *
 * What deliberately did NOT move: the parser, the catalog, the confidence gate, the bill,
 * the QR. Those stay served, so they can be fixed without anybody reinstalling anything —
 * which matters more here than it did on the web, because a sideloaded APK has no Play
 * Store to push an update through.
 *
 * Note on globals: app.js declares `state` and `setStatus` with const, so they are in the
 * global scope but are NOT properties of window. They are referenced bare here, and
 * guarded with typeof rather than window.x, which would always be undefined.
 */
(function nativeShell() {
  const bridge = window.Bolo;
  if (!bridge) return;                    // a plain browser; wake.js keeps the job
  window.BOLO_NATIVE = true;

  const safe = (fn) => { try { return fn(); } catch (e) { console.warn("native:", e); } };

  /* Everything the page says goes out through the phone's voice, not the WebView's.
   *
   * SpeechSynthesis works and sounds identical — both end up at the same Android engine — but
   * the page cannot shut the microphone, and the service can. Spoken through the bridge, the
   * capture loop knows the phone is talking and goes deaf for exactly that long, so an
   * announcement can never be recorded as an item or wake the phone on a name it said itself.
   *
   * The toast is kept: a shop at 70-80 dB(A) swallows a phone speaker, and the screen is
   * still there for the customer even when the shopkeeper cannot see it. */
  /* Ask, then listen — the only path that opens a microphone without the wake word.
   *
   * Exposed as a global rather than called through the bridge directly, so app.js does not
   * have to know whether it is running in a browser. In a browser this stays undefined and
   * the confirmation falls back to the Yes button on screen, which has always worked. */
  window.askThenListen = (text) =>
    safe(() => bridge.askThenListen && bridge.askThenListen(String(text)));

  const pageSpeak = window.speak;
  window.speak = function speakNative(msg) {
    safe(() => typeof toast === "function" && toast(msg, 3600, true));
    try {
      if (bridge.say) { bridge.say(String(msg)); return; }
    } catch (e) { /* fall through to the page's own voice */ }
    if (typeof pageSpeak === "function") pageSpeak(msg);
  };

  /* ---- choosing the microphone ----
   *
   * The page's own picker lists getUserMedia devices, and in the APK the page never opens a
   * stream — the service does — so it listed nothing useful and changing it changed nothing.
   * Replace it with the categories the bridge understands. Deliberately not a device list:
   * the phone reports back which one actually answered, on every clip, which is the number
   * the trial needs and does not require handing the WebView an inventory of the hardware. */
  const MIC_KINDS = [
    ["auto", "Automatic"],
    ["builtin", "Phone microphone"],
    ["wired", "Wired headset"],
    ["usb", "USB / wireless receiver"],
    ["bt", "Bluetooth"],
  ];

  window.renderMicPicker = function renderMicPickerNative() {
    const sel = document.getElementById("setMic");
    if (!sel) return;
    const cur = safe(() => bridge.micPref && bridge.micPref()) || "auto";
    sel.innerHTML = MIC_KINDS.map(([v, label]) =>
      `<option value="${v}"${v === cur ? " selected" : ""}>${label}</option>`).join("");
    sel.onchange = () => {
      safe(() => bridge.setMic && bridge.setMic(sel.value));
      safe(() => typeof toast === "function" &&
        toast(`Microphone: ${(MIC_KINDS.find((k) => k[0] === sel.value) || [])[1]}`, 3000, true));
    };
  };

  /* ---- what the phone tells the page ---- */

  const handlers = {
    voice_ready() { safe(() => setStatus(t("ready"))); },

    capture(p) {
      if (p.state === "start" || p.state === "resume") {
        safe(() => { setTalk("rec"); setStatus(t("speak")); });
      } else if (p.state === "hold") {
        safe(() => setTalk("idle"));
      } else {
        safe(() => { setTalk("idle"); setStatus(t("ready")); });
      }
    },

    working() { safe(() => setTalk("busy")); },

    /* The phone opened a microphone, and it may not be the one that was asked for.
     *
     * Worth a toast rather than a log line: a wireless mic that has gone to sleep drops off
     * the device list, and the next open silently lands on the handset. From the counter
     * that looks like the app breaking for no reason — it still hears him, just much worse,
     * and there is nothing to see. Saying so is the difference between "press the button on
     * the mic" and twenty minutes of guessing. */
    mic(p) {
      safe(() => {
        if (p.honoured) return;
        if (typeof toast === "function") {
          toast(`Recording from ${p.routed || "the phone"} — ${p.pref} not found. ` +
                `Wake the microphone and try again.`, 5000, true);
        }
      });
    },

    /* The native side has already done the round trip, so this is the same object the
       page's own fetch used to produce and it goes to the same place. */
    result(data) {
      safe(() => {
        apply(data, data.native_ms || 0);
        /* The measurement log is the point of the microphone trial, and native clips were
         * missing from it entirely: app.js only calls logAttempt on the round trip it makes
         * itself, and in the APK the round trip happens in Kotlin. So every utterance the
         * shopkeeper actually spoke into the app went unrecorded, and micReport() described
         * the browser build alone. The phone tags each clip with the device it recorded
         * from; pass that through rather than the page's own idea of the microphone, which
         * in native mode is nothing at all. */
        if (typeof logAttempt === "function") {
          logAttempt(data, data.clip_ms || 0, data.native_ms || 0, data.mic || "native");
        }
        setTalk("idle");
      });
    },

    voice_error(p) {
      safe(() => {
        setTalk("idle");
        if (p.reason === "no_permission") toast(t("voiceUnavailable"), 4000, true);
        else if (p.reason === "network") toast(t("network"), 3000, true);
        else toast(`${t("voiceUnavailable")} (${p.reason})`, 4000, true);
      });
    },

    /* The shopkeeper's own bank sent him a credit SMS.
     *
     * It is a REPORT, not a confirmation, and the difference is the whole design. The
     * message was written by his bank, not by us: a refund, a salary credit, a transfer
     * from his brother and a customer's payment all look similar and all carry a rupee
     * amount. So the amount is matched against the open total, and a human still says yes.
     *
     * Matching on the total is doing real work here. It is what separates "the money for
     * THIS bill arrived" from "some money arrived", and it is why an unmatched amount is
     * shown rather than hidden — the shopkeeper needs to see a ₹500 credit land while a
     * ₹120 bill is open, because that is exactly when he should not close it.
     *
     * When shops have used this long enough to trust it, the auto-accept belongs here,
     * behind a setting, and still never for an amount that does not match. */
    payment(p) {
      const bill = (typeof state !== "undefined") && state.bill;
      if (!bill || !bill.total) return;
      const amount = Number(p.amount);
      if (Math.abs(amount - Number(bill.total)) >= 1) {
        // Deliberately loud and deliberately not actionable: money arrived that is not
        // this bill, and the only safe thing the app can do is say so.
        // Spoken, not just shown. Money arriving that is not for this bill is precisely the
        // thing he must not miss while looking at a customer instead of the screen.
        safe(() => speak(`${t("creditNotMatched")}: ${amount} not ${bill.total}`));
        return;
      }
      /* The prompt still needs a tap, and that does not change: an SMS is text written by
       * somebody else, and a bill that closes itself on one is the silent error the product
       * says it will not make. What changes is that he is told out loud that it is waiting —
       * otherwise a payment sits unconfirmed behind him while the customer walks away. */
      safe(() => speak(`${t("confirmReceived")} ${amount}`));
      safe(() => showPrompt({
        kind: "confirm",
        main: `₹${amount}`,
        note: t("confirmReceived") || "Payment received — confirm?",
        onOk: () => upiReceived(amount),
        onCancel: () => {},
      }));
    },
  };

  window.BoloNative = {
    on(event, payload) {
      const h = handlers[event];
      if (h) h(payload || {});
    },
  };

  /* ---- what the page tells the phone ---- */

  // The service needs to know which shop it is posting a clip for. Polling rather than
  // hooking every place the page mutates state: one comparison every two seconds is
  // cheaper than being wrong the one time somebody adds a new write site.
  let last = "";
  setInterval(() => {
    if (typeof state === "undefined" || !state.shop) return;
    const ctx = JSON.stringify({
      shop_id: state.shop.id || "",
      mode: state.mode || "billing",
      lang: state.shop.lang || "en",
      // The phone answers to his name, and the name lives on the shop record.
      owner_name: state.shop.owner_name || "",
    });
    if (ctx !== last) { last = ctx; bridge.setContext(ctx); }
  }, 2000);

  /* ---- the two ways to talk ---- */

  // Push-to-talk. Capture phase plus stopImmediatePropagation so the page's own
  // getUserMedia handlers never run: two recorders on one microphone is the failure
  // wake.js documents at length, and here it would be a native one against a browser one.
  const talkButtons = ["talk", "payTalk"].map((id) => document.getElementById(id)).filter(Boolean);
  for (const btn of talkButtons) {
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault(); e.stopImmediatePropagation(); bridge.pttDown();
    }, true);
    const up = (e) => { e.preventDefault(); e.stopImmediatePropagation(); bridge.pttUp(); };
    btn.addEventListener("pointerup", up, true);
    btn.addEventListener("pointercancel", up, true);
    btn.addEventListener("lostpointercapture", up, true);
  }

  // Hands-free. wake.js has already bailed out (it checks for this bridge), so its menu
  // item is inert and free to re-own — including the dimming it applies when the browser
  // has no recogniser, which in a WebView is always.
  const item = document.getElementById("miWake");
  if (item) {
    // Ask the service, not localStorage: the switch belongs to the thing that holds the
    // microphone, and it has to be right on the first paint after a reboot.
    let on = safe(() => bridge.handsFreeOn()) === true;
    const paint = () => {
      item.setAttribute("aria-checked", String(on));
      const label = document.getElementById("miWakeState");
      if (label) label.textContent = on ? t("on") : t("off");
    };
    item.classList.remove("dim");
    item.onclick = () => {
      on = !on;
      bridge.handsFree(on);
      paint();
      safe(() => toast(on ? t("sayWake") : t("off"), 2200));
    };
    paint();
  }

  /* The safe-area values the page actually resolved, reported once.
   *
   * These are the whole inset story: the shell deliberately does NOT pad the WebView, so
   * if env(safe-area-inset-top) comes back 0px on some handset, every header is under the
   * status bar and the cause is invisible from a screenshot. One line, once, at startup. */
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;top:0;left:0;height:0;width:0;" +
    "padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)";
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const insets = `top=${cs.paddingTop} bottom=${cs.paddingBottom}`;
  probe.remove();

  console.info("Synthia native shell active, apk", safe(() => bridge.version()), "· insets", insets);
})();
