/* The cost curve's one load-bearing property, checked against the real module.

   The curve is drawn beside the recommended stock level, so its minimum must BE that
   level. If the low point of the drawing sat a few pixels off the number printed
   above it, a reader would be right to distrust both.

   That is not achieved by fitting a nice-looking shape. The newsvendor optimum is
   F^-1 of the critical fractile, so the demand distribution is reconstructed from two
   things the bundle states exactly -- the mean of the cover window, and
   F(orderUpTo) = targetQ -- which determines it. This script asserts the property
   actually holds, for every series, rather than trusting the derivation.

   Run directly, or via tests/test_cost_curve.py which skips if node is absent.
   Prints a summary and exits non-zero on any violation. */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
global.window = {};
eval(fs.readFileSync(path.join(ROOT, "ui/js/curve.js"), "utf8"));
const { Curve } = global.window;

const D = JSON.parse(fs.readFileSync(path.join(ROOT, "ui/data/bundle.json"), "utf8"));
const COVER = (D.run.reviewPeriod || 7) + (D.run.leadTime || 2);
const SPAN = 0.4, N = 41;
const gridStep = (2 * SPAN) / (N - 1);

const bad = [];
let drawn = 0, noFit = 0, flat = 0;
const byMethod = {};
let worstOffset = 0, worstId = null, worstF = 0;

for (const s of D.series) {
  const c = Curve.points(s, COVER, { span: SPAN, n: N });
  if (!c) {
    noFit++;
    // A series with no curve must genuinely have nothing to build one from.
    if (s.orderUpTo > 0 && (s.mean || []).some((v) => v > 0)) {
      bad.push(`${s.id}: no curve, but it has both a level and a forecast`);
    }
    continue;
  }
  drawn++;
  byMethod[c.fit.method] = (byMethod[c.fit.method] || 0) + 1;

  // 1. the analytic property: F(recommendation) == the service level used
  const F = Curve.PHI((Math.log(c.opt) - c.fit.m) / c.fit.sg);
  const fErr = Math.abs(F - c.fit.tq);
  if (fErr > 1e-5) bad.push(`${s.id}: F(level)=${F.toFixed(6)} but service=${c.fit.tq.toFixed(6)}`);
  if (fErr > worstF) worstF = fErr;

  // 2. the drawn property: the lowest plotted point is the recommendation
  const offset = Math.abs(c.argmin - c.opt) / c.opt;
  if (offset > gridStep) {
    bad.push(`${s.id}: drawn minimum at ${c.argmin.toFixed(1)}, level is ${c.opt}`);
  }
  if (offset > worstOffset) { worstOffset = offset; worstId = s.id; }

  // 3. cost rises away from the minimum in both directions, so the shape reads as a
  //    trade-off rather than as noise
  const lowest = c.pts.reduce((b, p, i) => (p.excess < c.pts[b].excess ? i : b), 0);
  for (let i = lowest; i + 1 < c.pts.length; i++) {
    if (c.pts[i + 1].excess < c.pts[i].excess - 1e-9) {
      bad.push(`${s.id}: cost falls again above the level`); break;
    }
  }
  for (let i = lowest; i > 0; i--) {
    if (c.pts[i - 1].excess < c.pts[i].excess - 1e-9) {
      bad.push(`${s.id}: cost falls again below the level`); break;
    }
  }

  // 4. drawable: finite, and not a flat line that would render as an empty box
  for (const p of c.pts) {
    if (!isFinite(p.q) || !isFinite(p.excess)) { bad.push(`${s.id}: non-finite point`); break; }
  }
  if (!(c.peak > 0)) flat++;
}

console.log(`curves drawn                ${drawn}`);
console.log(`no curve (nothing to fit)   ${noFit}`);
console.log(`fit branch                  ${JSON.stringify(byMethod)}`);
console.log(`flat curves                 ${flat}`);
console.log(`worst service-level error   ${worstF.toExponential(2)}`);
console.log(`worst minimum offset        ${(worstOffset * 100).toFixed(3)}% of the level ` +
            `(one grid step is ${(gridStep * 100).toFixed(1)}%)${worstId ? `, on ${worstId}` : ""}`);
console.log(`violations                  ${bad.length}`);
for (const b of bad.slice(0, 10)) console.log("  " + b);
if (bad.length > 10) console.log(`  ... and ${bad.length - 10} more`);
process.exit(bad.length || flat ? 1 : 0);
