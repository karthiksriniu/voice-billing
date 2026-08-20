# Vendored, not built

Third-party binaries the hands-free endpointer needs, committed rather than fetched at
runtime so the shop is not one CDN outage away from a feature that decides when to stop
recording. Nothing here loads until hands-free is switched on (see `public/vad.js`), and
after the first time it is served from Cache Storage, which is what makes it work offline.

| file | from | version | why |
|---|---|---|---|
| `silero_vad_v5.onnx` | `@ricky0123/vad-web` | 0.0.24 | Silero VAD v5 — 16 kHz, 512-sample frames |
| `ort/ort.wasm.min.mjs` | `onnxruntime-web` | 1.20.1 | loader, wasm backend only |
| `ort/ort-wasm-simd-threaded.mjs` | `onnxruntime-web` | 1.20.1 | glue for the binary below |
| `ort/ort-wasm-simd-threaded.wasm` | `onnxruntime-web` | 1.20.1 | the runtime, ~2.8 MB gzipped |

The threaded binary is the only one ORT 1.20 ships. Without cross-origin isolation it runs
single-threaded, which is what we want anyway — a 2 MB model on a 32 ms frame is not work
worth splitting, and COOP/COEP headers are not something this app should have to set.

To update, re-pack from npm and copy the same four files:

    npm pack onnxruntime-web@<version>
    npm pack @ricky0123/vad-web@<version>

The model's graph signature is `input`/`state`/`sr` in, `output`/`stateN` out. If a future
version changes that, `vad.js` fails to load and hands-free falls back to the decibel gate
rather than breaking — but check it, because the fallback is the thing being replaced.
