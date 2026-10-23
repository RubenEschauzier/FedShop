"""Run the workload with a Comunica engine and record what it returns, and when.

Each instance runs in its own process group, through experiments/bsbm/discovery/virtuoso-fetch.js
(Connection: close, a cap on concurrent requests to Virtuoso, retries, request counts). The output
(text/csv) is read as it streams, so every row gets its arrival time. At the timeout the whole
process group is killed, and the rows received until then are kept.

- form "service": injected.sparql; sources are the query's IRIs (its SERVICE clauses name the
  known endpoints).
- form "plain": plain.sparql; sources are the catalog's endpoints and the query's IRIs, and the
  engine selects sources itself.

A run is a directory (runs/<name>/) with run.json (its settings), results.jsonl (one line per
instance, appended as they finish, so an interrupted run resumes where it stopped), outputs/
(each instance's raw output) and logs/ (each instance's stderr).
"""
import csv
import json
import os
import re
import signal
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click

from .common import LOGGER

DEFAULT_ENGINE = os.environ.get("COMUNICA_ENGINE", str(Path.home() / "projects/comunica-feature-link-traversal-default"
                                                       / "engines/query-sparql-link-traversal-solid"))


def norm(v):
    v = v.strip()
    if v.startswith("<") and v.endswith(">"):
        v = v[1:-1]
    try:
        return str(Decimal(v).normalize())
    except InvalidOperation:
        return v


def answer(path):
    with open(path) as f:
        rows = csv.reader(f)
        header = next(rows)
        return header, {tuple(norm(x) for x in row) for row in rows}


def run_instance(dconf, inst, settings, outdir):
    form = settings["form"]
    query = inst / ("plain.sparql" if form == "plain" else "injected.sparql")
    prefix = "plain-" if form == "plain" else ""
    text = query.read_text()
    seeds = list(dict.fromkeys(i.split("#")[0] for i in re.findall(r"<(" + re.escape(dconf.base) + r"[^>]+)>", text)))
    sources = [f"sparql@{e}" for e in settings["endpoints"]] + seeds
    header, ref = answer(inst / f"{prefix}results.csv")
    _, union = answer(inst / f"{prefix}union-results.csv")
    name = f"{inst.parent.name}-{inst.name}"
    res = {"query": inst.parent.name, "instance": int(inst.name.split("_")[1]), "reference": len(ref),
           "seeds": len(seeds), "sources": len(sources)}
    binary = "query-dynamic.js" if settings["comunica_config"] else "query.js"
    env = {**os.environ, "VIRTUOSO_LIMIT": str(settings["virtuoso_limit"])}
    if settings["comunica_config"]:
        env["COMUNICA_CONFIG"] = settings["comunica_config"]
    cmd = ["node", f"--max-old-space-size={settings['heap_mb']}", "-r", settings["preload"],
           str(Path(settings["engine"], "bin", binary)), "--idp", "void", "--lenient", *sources,
           "-f", str(query), "-t", "text/csv"]
    lines, arrivals = [], []
    start = time.time()
    with open(outdir / "logs" / f"{name}.log", "w") as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, env=env, start_new_session=True)

        def read():
            for line in p.stdout:
                lines.append(line)
                arrivals.append(time.time() - start)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            p.wait(timeout=settings["timeout"])
            status = "ok" if p.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            status = "killed"
        reader.join(5)
    res["time"] = round(time.time() - start, 2)
    log = (outdir / "logs" / f"{name}.log").read_text(errors="replace")
    counts = [l for l in log.splitlines() if l.startswith("COUNTS ")]
    if counts:
        res.update(json.loads(counts[-1][len("COUNTS "):]))
    got, at = set(), {}
    for row, t in zip(list(csv.reader(lines))[1:], arrivals[1:]):
        key = tuple(norm(x) for x in row)
        if len(key) == len(header) and key not in got:
            got.add(key)
            at[key] = t
    correct = got & ref
    res.update(status=status, rows=len(got), correct=len(correct), complete=correct == ref,
               claim_violations=len((got - ref) & union), other_rows=len(got - union),
               first_result=round(min(at.values()), 3) if at else None,
               complete_at=round(max(at[k] for k in correct), 3) if correct and correct == ref else None,
               arrivals=sorted(round(at[k], 3) for k in correct))
    if status == "error":
        msg = [l for l in log.splitlines() if l.strip() and not l.startswith("COUNTS")]
        res["error"] = msg[-1][:300] if msg else ""
    (outdir / "outputs" / f"{name}.csv").write_text("".join(lines))
    return res


def run(dconf, name, form="plain", comunica_config=None, endpoints=None, timeout=180, engine=DEFAULT_ENGINE,
        pattern="q*/instance_*", virtuoso_limit=16, heap_mb=8192):
    """Run (or resume) the workload; returns the results of the whole run."""
    outdir = Path(dconf.outdir, "runs", name)
    (outdir / "logs").mkdir(parents=True, exist_ok=True)
    (outdir / "outputs").mkdir(parents=True, exist_ok=True)
    catalog = [l.split("\t")[2].strip() for l in open(dconf.path("sources", "catalog-endpoints.tsv"))]
    settings = {"form": form, "comunica_config": str(Path(comunica_config).resolve()) if comunica_config else None,
                "endpoints": catalog if (endpoints if endpoints is not None else form == "plain") else [],
                "timeout": timeout, "engine": str(engine), "virtuoso_limit": virtuoso_limit, "heap_mb": heap_mb,
                "preload": str(Path(dconf.discovery_dir, "virtuoso-fetch.js").resolve()), "pattern": pattern}
    settings_file = outdir / "run.json"
    if settings_file.exists():
        old = json.loads(settings_file.read_text())
        if {k: v for k, v in old.items() if k != "started"} != settings:
            raise click.ClickException(f"{outdir} holds a run with other settings: {old}")
    settings_file.write_text(json.dumps({**settings, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))

    results_file = outdir / "results.jsonl"
    done = {}
    if results_file.exists():
        for line in results_file.read_text().splitlines():
            r = json.loads(line)
            done[(r["query"], r["instance"])] = r
    insts = sorted(Path(dconf.outdir, "workload").glob(pattern), key=lambda p: (p.parent.name, int(p.name.split("_")[1])))
    with open(results_file, "a") as f:
        for inst in insts:
            key = (inst.parent.name, int(inst.name.split("_")[1]))
            if key in done:
                continue
            r = run_instance(dconf, inst, settings, outdir)
            done[key] = r
            f.write(json.dumps(r) + "\n")
            f.flush()
            LOGGER.info(f"{key[0]}#{key[1]}: {r['status']} {r['time']} s, {r['correct']}/{r['reference']} correct, "
                        f"{r['rows']} rows, {r.get('docs')} documents, {r.get('endpoint')} service requests")
    (outdir / "summary.md").write_text(summary(dconf, list(done.values()), settings))
    return list(done.values())


def summary(dconf, results, settings):
    """A per-template table of a run, as Markdown."""
    def span(values, fmt):
        values = [v for v in values if v is not None]
        if not values:
            return "–"
        lo, hi = min(values), max(values)
        return fmt.format(lo) if lo == hi else f"{fmt.format(lo)}–{fmt.format(hi)}"

    by = {}
    for r in results:
        by.setdefault(r["query"], []).append(r)
    out = [f"Form: {settings['form']}; config: {settings['comunica_config'] or 'default'}; "
           f"{len(settings['endpoints'])} endpoints as sources; timeout {settings['timeout']} s.", "",
           "| Query | Complete | Recall | Time (s) | First result (s) | 10th result (s) | Documents | Service requests | Claim violations | Status |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for q, v in sorted(by.items()):
        status = {}
        for r in v:
            status[r["status"]] = status.get(r["status"], 0) + 1
        out.append(f"| {q} | {sum(r['complete'] for r in v)}/{len(v)} | {span([r['correct'] / max(r['reference'], 1) for r in v], '{:.0%}')} "
                   f"| {span([r['time'] for r in v], '{:.1f}')} | {span([r['first_result'] for r in v], '{:.1f}')} "
                   f"| {span([r['arrivals'][9] if len(r['arrivals']) >= 10 else None for r in v], '{:.1f}')} "
                   f"| {span([r.get('docs') for r in v], '{:,}')} | {span([r.get('endpoint') for r in v], '{:,}')} "
                   f"| {sum(r['claim_violations'] for r in v)} | {', '.join(f'{k} {n}' for k, n in sorted(status.items()))} |")
    out += ["", f"{sum(r['complete'] for r in results)}/{len(results)} complete, "
            f"{sum(r['status'] == 'killed' for r in results)} killed, {sum(r['status'] == 'error' for r in results)} errors, "
            f"{sum(r['other_rows'] for r in results)} rows outside the union answer."]
    return "\n".join(out) + "\n"
