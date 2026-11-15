"""Compare reach.py runs: median (min–max) documents reached per template, one column per run.

usage: python compare.py name=out.jsonl [name=out.jsonl ...] [--markets markets.json:workload-dir]
Each run also gives its total documents (web/ size) when written as name=out.jsonl:benchmark-dir.
"""
import collections
import json
import os
import statistics
import sys


def load(path):
    rows = collections.defaultdict(list)
    for line in open(path):
        r = json.loads(line)
        rows[r["q"]].append(r["fetched"])
    return rows


def total(bench):
    return sum(1 for _, _, fs in os.walk(os.path.join(bench, "web")) for f in fs if f.endswith(".nq")) - 1


def fmt(values):
    if not values:
        return "–"
    med = int(statistics.median(values))
    k = lambda v: f"{v / 1000:.1f}k" if v >= 1000 else str(v)
    return f"{k(med)} ({k(min(values))}–{k(max(values))})" if min(values) != max(values) else k(med)


runs = []
for arg in sys.argv[1:]:
    name, spec = arg.split("=", 1)
    path, _, bench = spec.partition(":")
    runs.append((name, load(path), total(bench) if bench else None))
queries = sorted(set().union(*(r.keys() for _, r, _ in runs)))
print("| Query | " + " | ".join(n for n, _, _ in runs) + " |")
print("|---|" + "---|" * len(runs))
for q in queries:
    print(f"| {q} | " + " | ".join(fmt(r.get(q, [])) for _, r, _ in runs) + " |")
if any(t for _, _, t in runs):
    print("| documents in total | " + " | ".join(f"{t / 1000:.0f}k" if t else "" for _, _, t in runs) + " |")
