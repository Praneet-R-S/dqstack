"""Load test: many tills sending paid bills at once (section 15: "200 tills sending
sales at once"), through the POS connector API.

    python scripts/loadtest.py --base-url https://staging.example.in \
        --api-key dqk_... --store-code PUN1 --sku RICE-SNA-5 --sku DAL-TOOR-1 \
        --tills 200 --bills 25

Each till sends its bills one after another, as a real till would; all tills run at
once. Every bill has its own id, and at the end the whole set is sent a second time,
which must change nothing. Reports throughput, latency percentiles and errors, and
exits non-zero when any request failed, any bill was lost or duplicated, or the 95th
percentile is above --p95-ms.

Run it against staging on PostgreSQL. SQLite on a laptop is not a fair measure: it
takes one writer at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid

import httpx


async def till(client: httpx.AsyncClient, n: int, args, latencies: list, errors: list,
               run: str) -> list[dict]:
    sent = []
    for b in range(args.bills):
        msg = {"id": f"load-{run}-{n}-{b}", "type": "sale", "store_code": args.store_code,
               "bill_number": f"L{run[:6]}-{n}-{b}",
               "lines": [{"sku": args.sku[(n + b) % len(args.sku)], "qty": 1, "price": 10}],
               "payment_method": "upi"}
        t0 = time.perf_counter()
        try:
            r = await client.post("/api/pos/v1/messages", json={"messages": [msg]})
            latencies.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}: {r.text[:200]}")
            else:
                body = r.json()
                if body["refused"]:
                    errors.append(str(body["results"][0].get("error")))
        except httpx.HTTPError as e:
            errors.append(type(e).__name__)
        sent.append(msg)
    return sent


async def main(args) -> int:
    run = uuid.uuid4().hex
    headers = {"Authorization": f"Bearer {args.api_key}"}
    limits = httpx.Limits(max_connections=args.tills, max_keepalive_connections=args.tills)
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=60,
                                 limits=limits) as client:
        latencies: list[float] = []
        errors: list[str] = []
        t0 = time.perf_counter()
        batches = await asyncio.gather(*[till(client, n, args, latencies, errors, run)
                                         for n in range(args.tills)])
        took = time.perf_counter() - t0
        total = args.tills * args.bills
        lat = sorted(latencies)
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else float("nan")
        print(f"{total} bills from {args.tills} tills in {took:.1f}s "
              f"({total / took:.0f} bills/s)")
        print(f"latency ms: p50 {p(0.50):.0f}  p95 {p(0.95):.0f}  p99 {p(0.99):.0f}  "
              f"max {lat[-1] if lat else float('nan'):.0f}  mean {statistics.fmean(lat):.0f}")
        print(f"errors: {len(errors)}" + (f" (first: {errors[0]})" if errors else ""))
        # The same bills again: every one must be recognised as a repeat.
        msgs = [m for batch in batches for m in batch]
        dup = 0
        for i in range(0, len(msgs), 500):
            r = await client.post("/api/pos/v1/messages", json={"messages": msgs[i:i + 500]})
            dup += r.json().get("duplicates", 0) if r.status_code == 200 else 0
        print(f"resent {len(msgs)}: {dup} recognised as repeats")
        ok = not errors and dup == len(msgs) and p(0.95) <= args.p95_ms
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--store-code", required=True)
    ap.add_argument("--sku", action="append", required=True)
    ap.add_argument("--tills", type=int, default=200)
    ap.add_argument("--bills", type=int, default=25)
    ap.add_argument("--p95-ms", type=float, default=2000.0,
                    help="fail if the 95th percentile is slower (section 12: usable in 2 s)")
    sys.exit(asyncio.run(main(ap.parse_args())))
