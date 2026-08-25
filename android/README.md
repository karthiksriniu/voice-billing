# Synthia — native Android shell

The billing UI is still the web app in `poc/`, served. What the APK adds is everything a
browser cannot do:

| | Browser PoC | This |
|---|---|---|
| Wake word | Chrome `SpeechRecognition` — cloud, Google's | 3.3M zipformer KWS, on the phone |
| Microphone | Dies with the screen / on app switch | Foreground service, survives both |
| Endpointing | Silero via onnxruntime-web | Silero natively, same frames |
| Push-to-talk | On-screen only | Also volume-down, phone face-down on the counter |
| Payment confirm | Shopkeeper taps "Received" | Reads the bank's credit SMS (D5) |

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

Keywords match as exact **BPE token sequences**, and the model is gigaspeech — English, and
it has never heard an Indian name. Asked for one it does not fail politely: it decodes the
sound into whatever English subwords fit. So a keyword written from the spelling fires only
by luck.

Three names, measured the same way, are the evidence:

| Name | Result |
|---|---|
| "Vishwa Bill" | **Unspottable.** Heard as FISH WERE BILL (7 of 14 voices), VISHUA BILL, ISSUE A BILL, WISH MY BILL. Thirty-two hand-written spellings tried at every threshold; none ever fired. |
| "Hey Akhila" | Workable but scattered — 11 distinct sequences over 24 clips, the commonest covering only 6. |
| **"Synthia"** | The model knows the word. 17 of 24 clips land on the identical sequence and transcribe as SYNTHIA outright. |

**The method — re-run it whenever the phrase, the model, or the speaker population
changes:**

1. Record the phrase. Many speakers, several speeds.
2. Load the *same* KWS model files as an `OnlineRecognizer` instead of a `KeywordSpotter`.
   The KWS model is a tiny ASR and will happily transcribe.
3. Take the emitted token sequences **verbatim**. Those are your keywords. Register every
   distinct one; they all trigger the same action.
4. Sweep `keywordsScore` / `keywordsThreshold` against positives *and* negatives.

Current numbers, over 24 synthesized positives (8 voices x 3 speeds, Indian-English
included) and 36 negatives of shop speech: **24/24 wake, 0/36 false accepts** at score 2.0 /
threshold 0.15 — and still 23/24 with zero false accepts at the much looser (1.0, 0.35).
The permissive end was chosen deliberately: real audio is harder than synthesized audio, and
measured false accepts are the budget we have to spend.

### A name is not a name-shaped string

A single short word risks colliding with people's names, so that was measured too. The
keyword model was run against 36 clips of **Sandhya, Shanthi, Santhi, Sindhu, Senthil,
Sangeetha, Sunitha, Sathya, Swetha, Santhosh, Sandhiya and Suganya** in three voices. It
fired on **none** of them.

The fuzzy *string* matcher in `wake.js` — a different mechanism, used by the browser build
and to strip a spoken name off a push-to-talk transcript — does not do as well: "synthia"
scores 0.71 against "santhi", just over the 0.70 bar. So a customer called Santhi can wake
the browser build and **cannot** wake the app. That is recorded in `poc/tests/test_wake.js`
as a known collision rather than papered over, and it is why two further spellings
("santhia", "sindhiya") were dropped from the wake list instead of excused.

**It has still never heard a human being.** Through a laptop speaker across the room it
wakes on roughly half the voices — that shortfall is the acoustic path, not the model.
Nothing here is tuned until it has been re-harvested from real recordings and the
false-accept rate measured against hours of real shop noise.

## The microphone follows the window

Hands-free listening starts on `onResume` and stops on `onPause`. The permission the
shopkeeper grants says "while using the app", and this is the literal reading of it: nothing
listens once he has switched away.

A foreground service still holds the mic — it has to, or Android silences the stream as soon
as the Activity stops being visible — but it is told to stand down rather than left running
behind whatever he opened next. The switch itself is remembered in SharedPreferences, not in
the page's localStorage, so it survives a reboot and is correct before the WebView has
loaded.

The cost is real and accepted: a bill cannot be dictated while he is in his UPI app checking
a payment.

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
| `RECEIVE_SMS` / `READ_SMS` | Payment confirmation, from the bank's own credit SMS. |

Reading SMS is **legal for us only because we are not on the Play Store** — this is D5's
own reverse clause firing, and both permissions are Play policy violations for our use case.

The bank's credit SMS is a better source than any one UPI app's notifications, for a reason
that is not about convenience: whichever app the customer paid from, it ends in the same
message from the shopkeeper's own bank. A notification listener would have to know every
payer app in India and be wrong about the ones it did not.

`SmsPayments.kt` reports an amount and nothing else. Nothing is stored — not the message,
not the sender, not a payment log. A message must name a rupee amount **and** say it was
credited **and** not say it was debited, which drops OTPs (they very often quote an amount),
promotions, refunds, payment requests and the shopkeeper's own outgoing payments. It never
settles a bill: the page matches the amount against the open total and a human still says
yes. An SMS is text written by somebody else, and a bill that closes itself on one is
exactly the silent error the product says it will not make.

Settlements claimed this way are recorded as `upi_sms`, deliberately not as `upi`. When a
dispute comes, "the phone saw a matching SMS" and "the PSP confirmed the transfer" have to
be tellable apart.

## Known gaps

- Wake word tuned only against synthesized speech (above).
- The SMS receiver's classification rules are checked against 12 representative bank
  messages, but the receiver itself has never fired on a real SMS — adb cannot inject one
  on a physical device.
- **WhatsApp receipts are not wired.** The customer's number is captured and stored with
  status `requested`; nothing is sent, and the UI says so rather than claiming otherwise.
  Delivery needs a WhatsApp Business account, an approved template and a provider — none of
  which exist yet.
- No in-app updater.
- Battery cost of all-day KWS unmeasured. The Pixel 8 Pro used for development is a
  flagship and will flatter both battery and accuracy versus a ₹8k target phone.
- Cloud ASR still carries transcription (`Transcriber.kt`), which the cost ceiling rules
  out for the product. Unchanged by this work: the on-device backend is the Phase 1 bakeoff.
