/* ============================================================================
   The cost of ordering more, or less, than the recommendation.

   This is the argument the whole ordering layer makes, and it had never been shown.
   Two products with an identical forecast get different recommended stock levels,
   because the cost of being wrong is not symmetric and not the same for both. A
   high-margin item with a long shelf life should be overstocked; a thin-margin item
   that spoils in a week should not. Stated in words that is a paragraph nobody
   reads. Drawn as a shape it is obvious, and it stays obvious after the planner has
   forgotten the words.

   The shape is the expected cost of the ordering decision, plotted against the
   quantity ordered, with the recommendation at the bottom of it. A curve that falls
   steeply on the left and rises gently on the right says "running out hurts more
   than holding stock" without naming a single mechanism.

   ---- why the minimum is exactly on the recommendation

   A drawn minimum that sits beside the recommended number rather than on it would
   discredit both. So the curve is not fitted to look right; it is constructed so
   that its minimum is the recommendation.

   The expected cost of ordering Q against demand D is

       C(Q) = u . E[(D-Q)+]  +  o . E[(Q-D)+]

   for underage cost u and overage cost o, and dC/dQ = 0 at F(Q) = u/(u+o), the
   critical fractile. So two things are needed and both are exported: the demand
   distribution over the cover window, and the fractile.

   The distribution is not exported directly -- the bundle carries per-day means and
   spreads, not the distribution of the 9-day total -- so it is reconstructed from
   two constraints the bundle does state exactly:

       mean of the total     = sum of the per-day means
       F(orderUpTo)          = targetQ

   Two constraints, two parameters, so a lognormal is determined. That is feasible
   for 809 of 1,000 series; the rest need a spread larger than a lognormal with that
   mean can carry, and there the mean constraint is dropped in favour of the spread
   implied by the per-day figures. The quantile constraint is kept in both branches,
   because it is the one that puts the minimum in the right place.

   The cost weights come from `targetQ` rather than from the raw underage and overage,
   for the same reason: `targetQ` is the fractile the recommendation was actually
   computed at, after the configured floor and ceiling were applied. Using the raw
   ratio would move the drawn minimum off the recommendation for any product whose
   fractile was clamped, and off by a rounding error for nearly all of them, since
   the exported figures are single precision.

   ---- what is plotted

   Excess cost above the best available choice, not absolute cost. A planner does not
   need to know that nine days of this product carries $340 of expected cost; they
   need to know that ordering 30% less than recommended would cost about $18 more.
   So the curve sits on zero at the recommendation and rises on both sides, and the
   asymmetry -- which side rises faster, and how fast -- is the entire message.
   ============================================================================ */

window.Curve = (() => {

  /* ---- the two normal functions this needs ------------------------------ */

  /** Abramowitz and Stegun 7.1.26. Maximum absolute error 1.5e-7, which is four
      orders of magnitude finer than a 112-pixel glyph can express. */
  function erf(x) {
    const sign = x < 0 ? -1 : 1;
    x = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t
                    - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return sign * y;
  }

  const PHI = (z) => 0.5 * (1 + erf(z / Math.SQRT2));

  /** Acklam's inverse normal. Needed once per product, to turn the target service
      level into the position of the recommendation in standard deviations. */
  function probit(p) {
    if (!(p > 0 && p < 1)) return 0;
    const a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
               1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00];
    const b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
               6.680131188771972e+01, -1.328068155288572e+01];
    const c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
               -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00];
    const d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
               3.754408661907416e+00];
    const pl = 0.02425;
    if (p < pl) {
      const q = Math.sqrt(-2 * Math.log(p));
      return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
             ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
    }
    if (p > 1 - pl) {
      const q = Math.sqrt(-2 * Math.log(1 - p));
      return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
              ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
    }
    const q = p - 0.5, r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }

  /* ---- the demand distribution over the cover window -------------------- */

  /** Reconstruct a lognormal for total demand over `cover` days.

      Returns null when the product has nothing to build one from -- a new product
      with no forecast, most obviously -- so callers can omit the graphic rather than
      draw a fabricated one. */
  function fit(s, cover) {
    const Q = s.orderUpTo;
    if (!(Q > 0) || !s.mean || !s.mean.length) return null;

    const n = Math.min(cover, s.mean.length);
    let mu = 0;
    for (let i = 0; i < n; i++) mu += s.mean[i] || 0;
    if (!(mu > 0)) return null;

    // Spread of the total, treating days as independent. Only used by the fallback.
    let varD = 0;
    for (let i = 0; i < n && s.sd; i++) varD += (s.sd[i] || 0) ** 2;

    const tq = Math.min(0.999, Math.max(0.001, s.targetQ || 0.5));
    const z = probit(tq);

    /* Branch 1: match the mean and the quantile.
         m = ln(mu) - sg^2/2  and  ln Q = m + sg z
         => sg^2/2 - z sg + ln(Q/mu) = 0 */
    const k = Math.log(Q) - Math.log(mu);
    const disc = z * z - 2 * k;
    let sg = null, method = "mean+service";
    if (disc >= 0) {
      const roots = [z - Math.sqrt(disc), z + Math.sqrt(disc)]
        .filter((v) => v > 1e-6).sort((x, y) => x - y);
      if (roots.length) sg = roots[0];
    }

    /* Branch 2: the spread this product's own daily figures imply, with the mean
       constraint dropped. Keeps the quantile, so the minimum stays on the
       recommendation, which is the property that matters. */
    if (sg == null) {
      const cv = Math.sqrt(varD) / mu;
      sg = Math.sqrt(Math.log(1 + Math.max(1e-4, cv * cv)));
      method = "service+spread";
    }
    const m = method === "mean+service"
      ? Math.log(mu) - (sg * sg) / 2
      : Math.log(Q) - sg * z;

    /* Cost weights that reproduce the fractile actually used. Total per-unit cost is
       preserved so the money on the axis stays this product's own. */
    const scale = (s.underage || 0) + (s.overage || 0);
    return {
      m, sg, mu, cover: n, tq, z, opt: Q, method,
      u: scale > 0 ? scale * tq : tq,
      o: scale > 0 ? scale * (1 - tq) : 1 - tq,
    };
  }

  /** Expected units short, if Q is ordered. Closed form for the lognormal, so the
      curve costs no integration. */
  function shortfall(f, Q) {
    if (Q <= 0) return Math.exp(f.m + (f.sg * f.sg) / 2);
    const lnQ = Math.log(Q);
    const mean = Math.exp(f.m + (f.sg * f.sg) / 2);
    return mean * PHI((f.m + f.sg * f.sg - lnQ) / f.sg)
           - Q * (1 - PHI((lnQ - f.m) / f.sg));
  }

  /** Expected cost of ordering Q. E[(Q-D)+] = E[(D-Q)+] + Q - E[D] identically, so
      one closed form covers both tails. */
  function cost(f, Q) {
    const short = shortfall(f, Q);
    const mean = Math.exp(f.m + (f.sg * f.sg) / 2);
    const over = short + Q - mean;
    return f.u * short + f.o * Math.max(0, over);
  }

  /** The curve, as points ready to draw, plus the readings the caption needs.

      `span` is a fraction of the recommendation: 0.4 plots from 40% below it to 40%
      above, because "what if I order a fifth less" is the question a planner
      actually asks and a unit range is not comparable across products. */
  function points(s, cover, { span = 0.4, n = 41 } = {}) {
    const f = fit(s, cover);
    if (!f) return null;

    const lo = Math.max(0, f.opt * (1 - span));
    const hi = f.opt * (1 + span);
    const base = cost(f, f.opt);

    const pts = [];
    let peak = 0;
    for (let i = 0; i < n; i++) {
      const q = lo + ((hi - lo) * i) / (n - 1);
      const excess = Math.max(0, cost(f, q) - base);
      pts.push({ q, excess });
      if (excess > peak) peak = excess;
    }

    /* Where the drawn minimum actually fell. Reported so a caller -- or a test --
       can assert it landed on the recommendation rather than trusting that it did. */
    let argmin = pts[0];
    for (const p of pts) if (p.excess < argmin.excess) argmin = p;

    return {
      fit: f, pts, peak, lo, hi, opt: f.opt, base,
      argmin: argmin.q,
      // The two readings the caption quotes.
      lessCost: Math.max(0, cost(f, lo) - base),
      moreCost: Math.max(0, cost(f, hi) - base),
      span,
    };
  }

  return { fit, cost, shortfall, points, PHI, probit };
})();
