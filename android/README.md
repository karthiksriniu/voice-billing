# Bolo Bill — native Android shell

The billing UI is still the web app in `poc/`, served. What the APK adds is everything a
browser cannot do:

| | Browser PoC | This |
|---|---|---|
| Wake word | Chrome `SpeechRecognition` — cloud, Google's | 3.3M zipformer KWS, on the phone |
| Microphone | Dies with the screen / on app switch | Foreground service, survives both |
| Endpointing | Silero via onnxruntime-web | Silero natively, same frames |
| Push-to-talk | On-screen only | Also volume-down, phone face-down on the counter |
| Payment confirm | Shopkeeper taps "Received" | Reads the UPI app's own notification (D5) |

`poc/public/native.js` is the other half. With no bridge present it does nothing and
`wake.js` runs exactly as before, so **the browser build is unaffected**.

## Toolchain

Installed under `~/Library`, no sudo, no Homebrew, no Android Studio:

- JDK 17 — `~/Library/Java/temurin-17`
- Android SDK (platform-35, build-tools 35, platform-tools) — `~/Library/Android/sdk`
- Gradle 8.9 — `~/Library/Gradle/gradle-8.9`

```bash
export JAVA_HOME="$HOME/Library/Java/temurin-17/Contents/Home" && ~/Library/Gradle/gradle-8.9/bin/gradle -p android :app:assembleDebug
```

```bash
~/Library/Android/sdk/platform-tools/adb install -r android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk
```

## Dev loop

The debug build points at `http://localhost:8077` and reaches the Mac through `adb
reverse`, so the page and the APK are iterated together without deploying anything.
Cleartext is permitted **only** for loopback (`res/xml/network_security_config.xml`).

```bash
cd poc && ./bolo dev
```

```bash
~/Library/Android/sdk/platform-tools/adb reverse tcp:8077 tcp:8077
```

Release builds point at the deployed PoC. Note that the deployment must be carrying
`native.js` for the APK to do anything — an older deployment will load, and the phone will
simply never wake.

## The wake word is harvested, not spelled

The single most important thing in this directory, and the least obvious.

The KWS model is trained on gigaspeech — English, no Indian names. Asked for a name it has
never heard, it does not fail politely: it decodes the sound into whatever English subwords
fit. Keywords are matched as **exact BPE token sequences**, so a keyword written from the
spelling will never fire.

This killed the first wake word. Across 14 synthesized voices the model heard "Vishwa Bill"
as `FISH WERE BILL` (7), `VISHUA BILL` (2), `ISSUE A BILL` (2), `WISH MY BILL`,
`VISHUA BARRELL`. Thirty-two hand-written spellings of "Vishwa" were tried at every
threshold; none fired. "Hey Akhila" was chosen instead and clusters far better, because
"hey" is a word the model knows cold.

**The method — re-run this whenever the phrase, the model, or the speaker population
changes:**

1. Record the phrase. Many speakers, several speeds.
2. Load the *same* KWS model files as an `OnlineRecognizer` instead of a `KeywordSpotter`.
   The KWS model is a tiny ASR and will happily transcribe.
3. Take the emitted token sequences **verbatim**. Those are your keywords. Register every
   distinct one; they all trigger the same action.
4. Sweep `keywordsScore` / `keywordsThreshold` against the clips you collected.

The current list is in `Kws.kt` and came from 24 synthesized clips (8 voices x 3 speeds,
including the Indian-English ones). It fires 24/24 at score 2.0 / threshold 0.15.

**It has never heard a human being.** Through a laptop speaker across the room it woke on
4 of 8 — the shortfall is the acoustic path, not the model. Nothing here is tuned until it
has been re-harvested from real recordings and the false-accept rate measured against hours
of real shop noise.

## Signing

Nothing has been distributed yet, which makes this the moment to get it right: **a
sideloaded app's upgrade path breaks permanently if the signing key changes.** Debug-signed
APKs must never go on a shopkeeper's phone.

Create the keystore yourself — the password is yours and should not pass through anyone
else's hands:

```bash
~/Library/Java/temurin-17/Contents/Home/bin/keytool -genkeypair -v -keystore android/bolobill-release.jks -keyalg RSA -keysize 4096 -validity 10000 -alias bolobill
```

Then write `android/keystore.properties` (gitignored, alongside the `.jks` which is also
gitignored):

```
storeFile=bolobill-release.jks
storePassword=<yours>
keyAlias=bolobill
keyPassword=<yours>
```

Back both up somewhere you will still have in five years. With them present,
`assembleRelease` signs properly; without them it silently falls back to the debug key.

## Distribution

`assembleRelease` produces per-ABI APKs. Hand out **arm64-v8a** (45 MB) — every Android
phone since roughly 2017. `armeabi-v7a` (36 MB) is there for anything genuinely ancient,
and the universal APK (66 MB) is the no-thinking fallback.

Per-phone friction, all one-time: "Install unknown apps" must be granted to whatever
delivers the file, and Play Protect shows a warning on install. Fine with you standing
there; a wall for the self-serve, install-to-first-bill-in-5-minutes goal in `CLAUDE.md`.

**There is no update channel.** Because the UI is served rather than bundled, most fixes
land without reinstalling — but anything in the APK (the wake word, the audio pipeline,
the notification reader) needs a manual reinstall until an in-app updater exists. Five
shops for a week is fine. By week three it will not be.

## Permissions, and why each

| Permission | Why |
|---|---|
| `RECORD_AUDIO` | The microphone. Held only while hands-free is on or PTT is down. |
| `FOREGROUND_SERVICE_MICROPHONE` | Mandatory from API 34 to hold a mic outside the UI. |
| `POST_NOTIFICATIONS` | The ongoing notification is how the shopkeeper sees the mic is on. |
| `REQUEST_INSTALL_PACKAGES` | Reserved for the updater that does not exist yet. |
| `BIND_NOTIFICATION_LISTENER_SERVICE` | Payment confirmation. Granted by hand in Settings. |

The notification listener is **legal for us only because we are not on the Play Store** —
this is D5's own reverse clause firing. `PaymentListener.kt` reports an amount and nothing
else: it never confirms a bill, never stores anything, never forwards the payer's name, and
ignores every package that is not a known UPI app. A notification is text written by
another application; a bill that settles itself on one is exactly the silent error the
product says it will not make.

## Known gaps

- Wake word tuned only against synthesized speech (above).
- No in-app updater.
- Battery cost of all-day KWS unmeasured. The Pixel 8 Pro used for development is a
  flagship and will flatter both battery and accuracy versus a ₹8k target phone.
- Cloud ASR still carries transcription (`Transcriber.kt`), which the cost ceiling rules
  out for the product. Unchanged by this work: the on-device backend is the Phase 1 bakeoff.
