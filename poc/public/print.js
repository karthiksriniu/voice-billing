/* Printing to a 58mm thermal roll from the phone.
 *
 * Two paths, because no single one reaches every printer:
 *
 *   Web Bluetooth. Chrome on Android only, and — this is the part that decides which
 *   printers work — Bluetooth LOW ENERGY only. A great many of the ₹1,500-2,500 ESC/POS
 *   printers speak Bluetooth Classic over the Serial Port Profile, which a browser cannot
 *   open at all, at any price. Those printers are not broken and neither is this: they are
 *   simply out of a web page's reach, and the Android app is where they get supported,
 *   which is a few lines with BluetoothSocket. Newer printers expose a BLE service with a
 *   writable characteristic and those work here today.
 *
 *   The system print dialogue. window.print() with an @page of 58mm, which reaches
 *   anything Android's print service can see — Mopria, a vendor plug-in, a network printer,
 *   or a PDF the shopkeeper keeps. Slower and it costs a dialogue, but it is universal.
 *
 * Raw TCP to port 9100, which is how a WiFi ESC/POS printer is normally driven, is not
 * available to a browser at all. A WiFi printer therefore goes through the print dialogue
 * here, and directly in the Android app later.
 */

(function thermal() {
  // The nearly-universal Nordic UART service, and the two other pairs that turn up on
  // cheap printers. Which one a printer uses is not advertised anywhere but its manual.
  const CANDIDATES = [
    { service: "000018f0-0000-1000-8000-00805f9b34fb", write: "00002af1-0000-1000-8000-00805f9b34fb" },
    { service: "6e400001-b5a3-f393-e0a9-e50e24dcca9e", write: "6e400002-b5a3-f393-e0a9-e50e24dcca9e" },
    { service: "0000ff00-0000-1000-8000-00805f9b34fb", write: "0000ff02-0000-1000-8000-00805f9b34fb" },
    { service: "49535343-fe7d-4ae5-8fa9-9fafd205e455", write: "49535343-8841-43f4-a8d4-ecbe34729bb3" },
  ];

  const ESC = 0x1b, GS = 0x1d;
  const bytes = (...n) => new Uint8Array(n);

  /* ESC/POS is a byte protocol, not a text one. The header resets whatever the last job
     left behind — a printer still in double-height from someone else's receipt will
     cheerfully print half of this one off the edge of the paper. */
  function encode(text) {
    const enc = new TextEncoder();
    const body = enc.encode(text.replace(/\n/g, "\n"));
    const head = bytes(ESC, 0x40,            // initialise
                       ESC, 0x74, 0x00,      // code page 437
                       ESC, 0x61, 0x00);     // align left
    const tail = bytes(0x0a, 0x0a, 0x0a, 0x0a,   // feed clear of the tear bar
                       GS, 0x56, 0x42, 0x00);    // cut, ignored by printers without one
    const out = new Uint8Array(head.length + body.length + tail.length);
    out.set(head, 0);
    out.set(body, head.length);
    out.set(tail, head.length + body.length);
    return out;
  }

  /* BLE writes are capped at the negotiated MTU, which on cheap printers is often the
     minimum 20 bytes. A whole receipt written in one call is silently truncated, so it
     goes in small pieces with a breath between them — without the pause, the printer's
     buffer overruns and prints confetti. */
  async function writeSlowly(ch, data, chunk = 180) {
    for (let i = 0; i < data.length; i += chunk) {
      const piece = data.slice(i, i + chunk);
      if (ch.writeValueWithoutResponse) await ch.writeValueWithoutResponse(piece);
      else await ch.writeValue(piece);
      await new Promise((r) => setTimeout(r, 24));
    }
  }

  async function findCharacteristic(server) {
    for (const c of CANDIDATES) {
      try {
        const svc = await server.getPrimaryService(c.service);
        const ch = await svc.getCharacteristic(c.write);
        if (ch) return ch;
      } catch (err) { /* try the next shape */ }
    }
    return null;
  }

  let printer = null;   // kept between receipts; pairing every sale would be absurd

  async function connect() {
    const dev = printer && printer.device;
    if (dev && dev.gatt.connected && printer.ch) return printer.ch;
    const device = dev || await navigator.bluetooth.requestDevice({
      // Cheap printers advertise almost nothing useful, so the picker shows everything
      // and the shopkeeper picks by name. Filtering by service hides most of them.
      acceptAllDevices: true,
      optionalServices: CANDIDATES.map((c) => c.service),
    });
    const server = await device.gatt.connect();
    const ch = await findCharacteristic(server);
    if (!ch) {
      throw new Error("nochar");
    }
    printer = { device, ch };
    return ch;
  }

  window.btPrint = async function btPrint(text) {
    if (!navigator.bluetooth) { speak(t("btUnsupported")); return false; }
    try {
      const ch = await connect();
      await writeSlowly(ch, encode(text));
      toast(t("printed"), 2500, true);
      return true;
    } catch (err) {
      if (err && err.name === "NotFoundError") return false;   // the picker was dismissed
      // Told apart on purpose: "no printer chosen" is not a fault, a printer that cannot
      // be written to is, and the two need different things from the shopkeeper.
      speak(err && err.message === "nochar" ? t("btClassic") : t("btFailed"));
      return false;
    }
  };

  window.btAvailable = () => !!navigator.bluetooth;
})();
