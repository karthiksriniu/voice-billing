/* The grams/kilos boundary in the recipe editor.
 *
 * This one line of arithmetic has now produced two separate wrong-by-1000 bugs, and
 * neither showed up as an error. The first flipped a field's unit as the shopkeeper
 * typed, so clearing "18 g" and entering 20 saved twenty kilos of beans per cup. The
 * second was quieter and worse: opening a recipe, fixing one line and saving divided every
 * line you did NOT touch by a thousand, so beans stopped depleting and the shrinkage
 * screen — the entire point of the feature — went silent.
 *
 * Both passed a hand check, because a hand check touches the field it is checking. So the
 * helpers are pulled out of app.js and exercised directly, including the case nobody
 * performs on purpose: a save with nothing edited.
 */

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "public", "app.js"), "utf8");

/* Lifted from the running file rather than copied, so a change to app.js is a change to
   what this tests. A copy would keep passing after the original broke. */
function lift(name) {
  const start = src.search(new RegExp(`^const ${name} = `, "m"));
  if (start < 0) throw new Error(`could not find ${name} in app.js`);
  // Scanned rather than regexed: these are arrow functions whose bodies contain their own
  // semicolons, so "up to the first ;" stops in the middle of one.
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if ("({[".includes(ch)) depth++;
    else if (")}]".includes(ch)) depth--;
    else if (ch === ";" && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`${name} is not terminated`);
}
const fmtNum = (n) => { const v = +n || 0; return v % 1 ? String(+v.toFixed(3)) : String(Math.round(v)); };
/* Rebound to globalThis because a `const` inside eval stays inside eval. */
const NAMES = ["smallUnit", "displayUnit", "inSmall", "displayQty", "toStockQty"];
eval(NAMES.map((n) => lift(n).replace(`const ${n} =`, `globalThis.${n} =`)).join("\n"));

let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? "ok  " : "FAIL"} ${label}` +
    (ok ? "" : `\n     got  ${JSON.stringify(got)}\n     want ${JSON.stringify(want)}`));
};

// The catalog stores canonical units: "kg", "g", "l", "ml" — never "litre". Keying on the
// spelled-out word meant milk, the one thing a café measures by volume, was edited in
// litres while beans were edited in grams.
check("kilos are edited in grams", smallUnit("kg"), "g");
check("litres are edited in millilitres, by the canonical key", smallUnit("l"), "ml");
check("the spelled-out form still works", smallUnit("litre"), "ml");
check("pieces are left alone", smallUnit("piece"), "piece");
check("grams are already small", smallUnit("g"), "g");

// Opening the sheet: stock units in, display units out.
check("18 g of beans", displayQty({ qty: 0.018, unit: "kg" }), 18);
check("150 ml of milk", displayQty({ qty: 0.15, unit: "l" }), 150);
check("one cup stays one", displayQty({ qty: 1, unit: "piece" }), 1);
// A recipe really can call for more than a kilo — a caterer's rice, a bar's ice — and the
// box must not switch scale just because the number got big.
check("1.2 kg is 1200 g, not 1.2", displayQty({ qty: 1.2, unit: "kg" }), 1200);
check("zero is zero", displayQty({ qty: 0, unit: "kg" }), 0);

// Saving: display units in, stock units out.
check("20 g saves as 0.02 kg", toStockQty({ component_id: "b", unit: "kg", shown: "20" }),
      { component_id: "b", qty: 0.02 });
check("150 ml saves as 0.15 l", toStockQty({ component_id: "m", unit: "l", shown: "150" }),
      { component_id: "m", qty: 0.15 });
check("2 pieces save as 2", toStockQty({ component_id: "c", unit: "piece", shown: "2" }),
      { component_id: "c", qty: 2 });
check("an emptied box is zero, not NaN",
      toStockQty({ component_id: "b", unit: "kg", shown: "" }), { component_id: "b", qty: 0 });
check("typed nonsense is zero, not NaN",
      toStockQty({ component_id: "b", unit: "kg", shown: "abc" }), { component_id: "b", qty: 0 });

// The bug. A part the shopkeeper never touched has to survive a save unchanged — which
// only holds if the sheet converts once on open and reads `shown` alone on save.
for (const [qty, unit] of [[0.018, "kg"], [0.15, "l"], [1, "piece"], [1.2, "kg"], [0.5, "ml"]]) {
  const opened = { component_id: "x", unit, qty, shown: displayQty({ qty, unit }) };
  check(`${qty} ${unit} survives an untouched save`, toStockQty(opened).qty, qty);
}

// And twice, because a sheet gets opened and closed more than once.
let part = { component_id: "x", unit: "kg", qty: 0.018 };
for (let i = 0; i < 3; i++) {
  part = { ...part, shown: displayQty(part) };
  part = { ...part, qty: toStockQty(part).qty };
}
check("three open-and-save rounds do not drift", part.qty, 0.018);

// A newly added component starts at zero in display units like every other line.
check("a fresh component saves as zero, not undefined",
      toStockQty({ component_id: "n", unit: "kg", qty: 0, shown: 0 }).qty, 0);

console.log(fails ? `\n${fails} FAILED` : "\nall unit-conversion checks passed");
process.exit(fails ? 1 : 0);
