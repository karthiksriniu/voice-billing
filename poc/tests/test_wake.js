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
 * The wake phrase is now "Vishwa Bill", and the reason it is two words is measured, not
 * stylistic: a bare "Vishwa" is a name stem, so Vishal, Vishnu, Vishwas, Vishwanath and
 * Bishwa all land inside the bar, and a carrier does not rescue it because the whole
 * window is compared — "hey vishal" scores 0.80 against "hey vishwa". Those names are
 * negatives below and must stay there.
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
  // Customers, called across a counter. This is what rules out a bare personal name.
  "vishal", "vishwas", "vishnu", "vishwanath", "viswanathan", "bishwa", "vishesh",
  "hey vishal", "hey vishwas", "hey vishnu", "hey vishwanath", "hey bishwa",
  "வishal", "விஷால்", "விஷ்ணு", "விஸ்வநாதன்",
  // "bill" is said constantly; only the pair may wake it.
  "bill", "bill kodu", "bill please", "bill podu", "final bill", "bill amount",
  "the bill", "my bill", "give me the bill", "bill ready", "பில்", "பில் கொடு",
  "wish", "fish", "dish", "world", "build", "still bill", "small bill",
  "two kilo sugar", "one filter coffee", "thank you", "okay sir", "how much is this",
  "give me change", "anna", "enna venum", "vanakkam", "sari sari", "ille ille",
  "நன்றி", "சரி", "என்ன வேணும்", "ஒரு டீ",
];

/* Collisions that are inherent to the NAME, not to a bad entry — they come from spellings
   the recogniser genuinely produces for "Chitti", so removing them would cost real wakes.
   Reported rather than failed, because the fix is a different wake word, not a different
   list: "Chitti" is two syllables and sits one edit from several ordinary words. This
   block is the standing evidence for changing it. */
const KNOWN_COLLISIONS = ["world bill", "vishwa billa", "wish bill"];

/* Must still wake, in every spelling the recogniser is known to produce. */
const IS_A_SUMMONS = [
  "vishwa bill", "viswa bill", "vishva bill", "vishwabill", "vishwa bil",
  "wishwa bill", "vishwa build", "vishwa pill",
  // Said naturally, and said mid-sentence — both have to work.
  "vishwa bill podu", "vishwa, bill", "um vishwa bill", "vishwa bill two kilo sugar",
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
