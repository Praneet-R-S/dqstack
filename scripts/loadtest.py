"""Load test: many tills sending paid bills at once (section 15: "200 tills sending
sales at once"), through the POS connector API.

    python scripts/loadtest.py --base-url https://staging.example.in \
        --api-key dqk_... --store-code PUN1 --sku RICE-SNA-5 --sku DAL-TOOR-1 \
        --tills 200 --bills 25

    # A whole chain: tills spread over many stores, each store with its own key,
    # bills drawn from the full catalogue (scripts/scale_seed.py writes this file).
    python scripts/loadtest.py --base-url http://127.0.0.1:8000 \
        --targets scale-targets.json --tills 200 --bills 25 --lines 3 \
        --think-ms 1000 --hot-skus 20 --hot-share 0.2 --json summary.json

Each till sends its bills one after another, as a real till would; all tills run at
once. Every bill has its own id, and at the end the whole set is sent a second time,
which must change nothing. Reports throughput, latency percentiles and errors, and
exits non-zero when any request failed, any bill was lost or duplicated, or the 95th
percentile is above --p95-ms. --json writes the same as a summary that CI checks
against the database (scripts/scale_seed.py verify).

The targets file is {"skus": [...], "stores": [{"store_code": ..., "api_key": ...}]}:
till n sends for store n mod the number of stores, with that store's key.

Run it against staging on PostgreSQL. SQLite on a laptop is not a fair measure: it
takes one writer at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass

import httpx


@dataclass
class Target:
    store_code: str
    headers: dict


class Plan:
    """Which store each till sends for, with which key, and which products it sells."""

    def __init__(self, args):
        self.lines = max(1, args.lines)
        self.hot_share = args.hot_share
        if args.targets:
            with open(args.targets, encoding="utf-8") as f:
                spec = json.load(f)
            self.targets = [Target(s["store_code"], {"Authorization": f"Bearer {s['api_key']}"})
                            for s in spec["stores"]]
            self.skus = list(spec["skus"])
            self.spread = True
        else:
            self.targets = [Target(args.store_code, {"Authorization": f"Bearer {args.api_key}"})]
            self.skus = list(args.sku)
            self.spread = False
        if not self.targets or not self.skus:
            raise SystemExit("the plan needs at least one store and one product")
        self.hot = self.skus[:max(0, min(args.hot_skus, len(self.skus)))]

    def target(self, n: int) -> Target:
        return self.targets[n % len(self.targets)]

    def pick(self, rng: random.Random, n: int, b: int) -> list[str]:
        k = min(self.lines, len(self.skus))
        if not self.spread:
            # The original rotation (one line: the same SKUs as before this option).
            return [self.skus[(n + b + i) % len(self.skus)] for i in range(k)]
        chosen: list[str] = []
        while len(chosen) < k:
            pool = self.hot if self.hot and rng.random() < self.hot_share else self.skus
            sku = rng.choice(pool)
            if sku not in chosen:
                chosen.append(sku)
        return chosen


async def till(client: httpx.AsyncClient, n: int, args, plan: Plan, latencies: list,
               errors: list, run: str, counts: Counter) -> list[tuple[Target, dict]]:
    target = plan.target(n)
    rng = random.Random(f"{run}-{n}")
    sent = []
    for b in range(args.bills):
        if b and args.think_ms > 0:
            await asyncio.sleep(args.think_ms / 1000 * rng.uniform(0.5, 1.5))
        skus = plan.pick(rng, n, b)
        msg = {"id": f"load-{run}-{n}-{b}", "type": "sale", "store_code": target.store_code,
               "bill_number": f"L{run[:8]}-{n}-{b}",
               "lines": [{"sku": s, "qty": 1, "price": 10} for s in skus],
               "payment_method": "upi"}
        counts["lines"] += len(skus)
        t0 = time.perf_counter()
        try:
            r = await client.post("/api/pos/v1/messages", json={"messages": [msg]},
                                  headers=target.headers)
            latencies.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}: {r.text[:200]}")
            else:
                body = r.json()
                counts["accepted"] += body.get("accepted", 0)
                if body["refused"]:
                    errors.append(str(body["results"][0].get("error")))
        except httpx.HTTPError as e:
            errors.append(type(e).__name__)
        sent.append((target, msg))
    return sent


async def main(args) -> int:
    plan = Plan(args)
    run = uuid.uuid4().hex
    limits = httpx.Limits(max_connections=args.tills, max_keepalive_connections=args.tills)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        latencies: list[float] = []
        errors: list[str] = []
        counts: Counter = Counter()
        t0 = time.perf_counter()
        batches = await asyncio.gather(*[till(client, n, args, plan, latencies, errors, run,
                                              counts)
                                         for n in range(args.tills)])
        took = time.perf_counter() - t0
        total = args.tills * args.bills
        lat = sorted(latencies)
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else float("nan")
        mean = statistics.fmean(lat) if lat else float("nan")
        stores = len({plan.target(n).store_code for n in range(args.tills)})
        print(f"{total} bills ({counts['lines']} lines) from {args.tills} tills in {stores} "
              f"stores in {took:.1f}s ({total / took:.0f} bills/s)")
        print(f"latency ms: p50 {p(0.50):.0f}  p95 {p(0.95):.0f}  p99 {p(0.99):.0f}  "
              f"max {lat[-1] if lat else float('nan'):.0f}  mean {mean:.0f}")
        print(f"errors: {len(errors)}" + (f" (first: {errors[0]})" if errors else ""))
        # The same bills again: every one must be recognised as a repeat. Each store's
        # bills go with that store's key, as its till would resend them.
        by_key: dict[str, tuple[Target, list[dict]]] = {}
        for batch in batches:
            for target, m in batch:
                by_key.setdefault(target.headers["Authorization"], (target, []))[1].append(m)
        resent = dup = 0
        resend_errors: list[str] = []
        for target, msgs in by_key.values():
            for i in range(0, len(msgs), 500):
                chunk = msgs[i:i + 500]
                resent += len(chunk)
                try:
                    r = await client.post("/api/pos/v1/messages", json={"messages": chunk},
                                          headers=target.headers)
                except httpx.HTTPError as e:
                    resend_errors.append(type(e).__name__)
                    continue
                if r.status_code == 200:
                    dup += r.json().get("duplicates", 0)
                else:
                    resend_errors.append(f"HTTP {r.status_code}: {r.text[:200]}")
        print(f"resent {resent}: {dup} recognised as repeats")
        ok = (not errors and counts["accepted"] == total and dup == resent == total
              and p(0.95) <= args.p95_ms)
        print("PASS" if ok else "FAIL")
        if args.json:
            summary = {
                "run": run, "bill_prefix": f"L{run[:8]}-", "message_prefix": f"load-{run}-",
                "tills": args.tills, "stores": stores, "bills_per_till": args.bills,
                "bills_sent": total, "lines_sent": counts["lines"],
                "accepted": counts["accepted"], "seconds": round(took, 2),
                "bills_per_second": round(total / took, 1) if took else None,
                "think_ms": args.think_ms,
                "latency_ms": {k: round(v, 1) for k, v in
                               {"p50": p(0.50), "p95": p(0.95), "p99": p(0.99),
                                "max": lat[-1] if lat else float("nan"), "mean": mean}.items()},
                "p95_limit_ms": args.p95_ms,
                "errors": len(errors), "error_rate": len(errors) / total if total else 0,
                "error_kinds": dict(Counter(e[:120] for e in errors).most_common(10)),
                "resent": resent, "recognised_repeats": dup, "resend_errors": resend_errors[:10],
                "pass": ok,
            }
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str)
        return 0 if ok else 1


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key")
    ap.add_argument("--store-code")
    ap.add_argument("--sku", action="append")
    ap.add_argument("--targets", help="JSON file of stores, their keys, and the SKUs to sell "
                                      "(instead of --api-key, --store-code and --sku)")
    ap.add_argument("--tills", type=int, default=200)
    ap.add_argument("--bills", type=int, default=25)
    ap.add_argument("--lines", type=int, default=1, help="items on each bill")
    ap.add_argument("--think-ms", type=float, default=0.0,
                    help="average pause between one till's bills (0: flat out)")
    ap.add_argument("--hot-skus", type=int, default=0,
                    help="the first N SKUs are best-sellers that many tills sell at once")
    ap.add_argument("--hot-share", type=float, default=0.0,
                    help="share of lines that are best-sellers (with --targets)")
    ap.add_argument("--p95-ms", type=float, default=2000.0,
                    help="fail if the 95th percentile is slower (section 12: usable in 2 s)")
    ap.add_argument("--json", help="write a summary here")
    args = ap.parse_args(argv)
    if not args.targets and not (args.api_key and args.store_code and args.sku):
        ap.error("give --targets, or --api-key, --store-code and at least one --sku")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
