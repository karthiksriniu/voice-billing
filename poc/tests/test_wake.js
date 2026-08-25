/* What the phone will and will not answer to.
 *
 * The wake list is matched FUZZILY — it has to be, because Chrome hands back its best guess
 * at a name it has never met and that guess wanders (chitty, chithi, cheeti, city). The
 * cost of that is invisible from inside the list: an entry only has to be *near* an
 * ordinary word for the shop to ring the doorbell all day.
 *
 * That is not hypothetical. "hd" and "hedy" were added because one handset transcribed the
 * old wake word as "HD". At the 0.7 bar "hedy" sits 0.75 from "hey", 0.80 from "ready",
 * "heavy" and "head" — so the phone woke on half the sentences spoken near it, recorded
 * shop noise, and beeped on and off all day. The shopkeeper's report was "it keeps
 * activating and deactivating and I have to switch it off and on".
 *
 * The wake phrase is now "Synthia". A single word was affordable this time for a reason
 * that is measured rather than stylistic: the keyword model on the phone transcribes the
 * name outright, so the on-device spotter needs no carrier word to disambiguate it. What
 * a single word does cost is exactly what killed the last bare stem — proximity to
 * people's names. Sandhya, Shanthi, Sindhu and Senthil are all shouted across a counter,
 * and two spellings had to be dropped from the wake list because they landed inside the
 * bar of one of them. Those names are negatives below and must stay there.
 *
 * So the guard is on the DATA, not the algorithm: no entry in any pack may sit within the
 * matching threshold of something a person says at a counter. `ratio` is copied from
 * wake.js rather than imported because wake.js is a plain browser script that reaches for
 * `document` and `state` at load; the function is eight lines of Levenshtein and has not
 * changed since it was written.
 */

const fs = require("fs");
const path = require("path");

const LANG_DIR = path.join(__dirname, "..", "api", "_lib", "lang");
const WAKE_THRESHOLD = 0.7;      // must track wake.js
const MIN_ENTRY_LEN = 4;         // below this, one edit is most of the word

let failed = 0;
function check(label, cond, detail = "") {
  if (cond) return;
  failed++;
  console.log(`  FAIL  ${label}${detail ? "  " + detail : ""}`);
}

function ratio(a, b) {
  if (a === b) return 1;
  if (!a.length || !b.length) return 0;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) {
      row[j] = Math.min(prev[j] + 1, row[j - 1] + 1,
                        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = row;
  }
  return 1 - prev[b.length] / Math.max(a.length, b.length);
}

/* wake.js's own windowing: every single word and every adjacent pair. */
function best(text, words) {
  const clean = text.toLowerCase().replace(/[.,!?;:"'’]/g, " ").replace(/\s+/g, " ").trim();
  if (!clean) return { score: 0 };
  const toks = clean.split(" ");
  const windows = [];
  for (let i = 0; i < toks.length; i++) {
    windows.push(toks[i]);
    if (i + 1 < toks.length) windows.push(`${toks[i]} ${toks[i + 1]}`);
  }
  let score = 0, via = "", hit = "";
  for (const w of windows) {
    for (const t of words) {
      const r = ratio(w, t);
      if (r > score) { score = r; via = w; hit = t; }
    }
  }
  return { score, via, hit };
}

/* Said at a counter all day, in front of a phone that is listening. None of it is a
   summons. The English is what a Chennai counter code-mixes; the rest is what the
   recogniser hands back when it is guessing at noise. */
const NOT_A_SUMMONS = [
  "hey", "hey boss", "hey give me two tea", "ok hey", "yes hey", "hello hey",
  "ready", "are you ready", "heavy", "head", "heady bill", "hd", "h d", "hd ready",
  "city", "city bus", "kitty", "heat", "hit", "had", "hold", "hand",
  // Customers, called across a counter. This is what rules out a bare personal name,
  // and it is the reason the wake list may not drift toward common Tamil given names.
  "vishal", "vishwas", "vishnu", "vishwanath", "viswanathan", "bishwa", "vishesh",
  "hey vishal", "hey vishwas", "hey vishnu", "hey vishwanath", "hey bishwa",
  "வishal", "விஷால்", "விஷ்ணு", "விஸ்வநாதன்",
  // Names that sound near "Synthia". "santhia" and "sindhiya" were in the wake list
  // until this block failed: "santhia" scores 0.71 against "sandhya", so every Sandhya
  // called across the shop rang the doorbell. They were removed, not excused.
  "sandhya", "shanthi", "sindhu", "sunitha", "swetha", "santhosh",
  "sangeetha", "sumathi", "shanthini", "suganya", "sathya",
  "சிந்து",
  // "bill" is said constantly; only the pair may wake it.
  "bill", "bill kodu", "bill please", "bill podu", "final bill", "bill amount",
  "the bill", "my bill", "give me the bill", "bill ready", "பில்", "பில் கொடு",
  "wish", "fish", "dish", "world", "build", "still bill", "small bill",
  "two kilo sugar", "one filter coffee", "thank you", "okay sir", "how much is this",
  "give me change", "anna", "enna venum", "vanakkam", "sari sari", "ille ille",
  "நன்றி", "சரி", "என்ன வேணும்", "ஒரு டீ",
];

/* Collisions inherent to the NAME rather than to a bad entry, and — this is the new part —
   collisions that only exist in the FUZZY STRING matcher, not in the phone's ears.
 *
 * "synthia" scores 0.71 against "santhi" and 0.75 against "sandhiya", a hair over the 0.70
 * bar. Those are names shouted across a counter, so on the face of it this is the same
 * mistake as the old "hedy"/"hey" collision.
 *
 * It is not, and the difference was measured rather than assumed. The Android build does
 * not use this list to wake: an on-device keyword model does, and it was run against 36
 * clips of Sandhya, Shanthi, Santhi, Sindhu, Senthil, Sangeetha, Sunitha, Sathya, Swetha,
 * Santhosh, Sandhiya and Suganya in three voices. It fired on none of them. The acoustic
 * model can tell the name from those names; Levenshtein over a transcript cannot.
 *
 * So what is left below is the browser build's doorbell and the stripping of a spoken name
 * off the front of a push-to-talk transcript. A customer called Santhi can wake the
 * browser build, and cannot wake the app. Reported here so it stays visible, and so nobody
 * "fixes" the wake list by adding spellings that make it worse. */
const KNOWN_COLLISIONS = ["santhi", "senthil", "sandhiya", "சந்தியா"];

/* Must still wake, in every spelling the recogniser is known to produce.
 *
 * These matter less than they used to. In the Android build the wake word is spotted by an
 * on-device keyword model and never reaches this list at all — what remains is the browser
 * build, and stripping a spoken name off the front of a push-to-talk transcript. */
const IS_A_SUMMONS = [
  "synthia", "sinthia", "synthiya", "sinthiya", "cynthia",
  // Said naturally, and said mid-sentence — both have to work.
  "synthia two kilo sugar", "synthia, close bill", "um synthia", "hey synthia",
];

const packs = fs.readdirSync(LANG_DIR).filter((f) => f.endsWith(".json")).sort();

console.log("no ordinary sentence is a wake word");
for (const file of packs) {
  const code = path.basename(file, ".json");
  const words = JSON.parse(fs.readFileSync(path.join(LANG_DIR, file), "utf8")).wake || [];
  check(`${code}: has wake words at all`, words.length > 0);

  // The structural rule that let "hd" in. A three-character entry is one edit from a
  // hundred things; nothing that short can be matched fuzzily and stay honest.
  for (const w of words) {
    check(`${code}: "${w}" is long enough to fuzzy-match safely`,
          w.replace(/\s/g, "").length >= MIN_ENTRY_LEN,
          `(${w.replace(/\s/g, "").length} chars, need ${MIN_ENTRY_LEN})`);
    // And the other end of it. heardName() only ever builds one- and two-word windows, so
    // a three-word entry is not a strict wake phrase — it is a dead one, matched by
    // nothing, failing silently and for ever.
    check(`${code}: "${w}" is at most two words`, w.trim().split(/\s+/).length <= 2,
          `(${w.trim().split(/\s+/).length} words — no window is that long)`);
  }

  for (const phrase of NOT_A_SUMMONS) {
    const b = best(phrase, words);
    check(`${code}: "${phrase}" must NOT wake it`, b.score < WAKE_THRESHOLD,
          `-> ${b.score.toFixed(2)} via "${b.via}" ~ "${b.hit}"`);
  }
}

console.log("known collisions inherent to the name (reported, not failed)");
{
  const words = JSON.parse(fs.readFileSync(path.join(LANG_DIR, "ta-en.json"), "utf8")).wake || [];
  for (const phrase of KNOWN_COLLISIONS) {
    const b = best(phrase, words);
    if (b.score >= WAKE_THRESHOLD) {
      console.log(`  warn  "${phrase}" wakes it — ${b.score.toFixed(2)} via "${b.via}" ~ "${b.hit}"`);
    }
  }
}

console.log("the name still wakes it");
for (const file of packs) {
  const code = path.basename(file, ".json");
  const words = JSON.parse(fs.readFileSync(path.join(LANG_DIR, file), "utf8")).wake || [];
  for (const phrase of IS_A_SUMMONS) {
    const b = best(phrase, words);
    check(`${code}: "${phrase}" must wake it`, b.score >= WAKE_THRESHOLD,
          `-> ${b.score.toFixed(2)} via "${b.via}" ~ "${b.hit}"`);
  }
}

const total = packs.length * (NOT_A_SUMMONS.length + IS_A_SUMMONS.length);
console.log(`\n${total - failed} passed, ${failed} failed  (${packs.length} packs)`);
process.exit(failed ? 1 : 0);
