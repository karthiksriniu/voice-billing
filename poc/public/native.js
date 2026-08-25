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

    /* The native side has already done the round trip, so this is the same object the
       page's own fetch used to produce and it goes to the same place. */
    result(data) {
      safe(() => { apply(data, data.native_ms || 0); setTalk("idle"); });
    },

    voice_error(p) {
      safe(() => {
        setTalk("idle");
        if (p.reason === "no_permission") toast(t("voiceUnavailable"), 4000, true);
        else if (p.reason === "network") toast(t("network"), 3000, true);
        else toast(`${t("voiceUnavailable")} (${p.reason})`, 4000, true);
      });
    },

    /* A UPI app on this phone posted a notification that money arrived.
     *
     * It is a REPORT, not a confirmation, and the difference is the whole design. The
     * notification was written by another application; a refund alert, a request, or a
     * payment to somebody else can all carry a rupee amount, and a bill that settles
     * itself on one of those is exactly the silent error the product says it will not
     * make. So the amount is matched against the open total and a human still says yes.
     *
     * When shops have used this long enough to trust it, the auto-accept belongs here —
     * behind a setting, and still never for an amount that does not match. */
    payment(p) {
      const bill = (typeof state !== "undefined") && state.bill;
      if (!bill || !bill.total) return;
      const matches = Math.abs(Number(p.amount) - Number(bill.total)) < 1;
      if (!matches) {
        safe(() => toast(`₹${p.amount} — ≠ ₹${bill.total}`, 5000, true));
        return;
      }
      safe(() => {
        showPrompt({
          kind: "confirm",
          main: `₹${p.amount}`,
          note: t("confirmReceived") || "Payment received — confirm?",
          onOk: () => cashReceived(),
          onCancel: () => {},
        });
      });
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
      lang: state.shop.lang || "ta",
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

  console.info("Bolo Bill native shell active, apk", safe(() => bridge.version()));
})();
