"""The query services: one Virtuoso container with a graph per owner and a virtual endpoint per composite.

Every owner's data (data/<owner>.nt) is loaded into the graph named by its
namespace, so the analysis and the reference answers can see who holds what. Owners with a
service get a virtual SPARQL endpoint, as FedShop builds them: a vhost on its own port whose
default graph is the owner's graph, so the endpoint answers for that owner only. Owners without a
service (reviewers, small brands) are only loaded for the analysis.
"""
import os
import subprocess
from pathlib import Path

from .common import LOGGER, sparql_csv, wait_for

ISQL = "/opt/virtuoso-opensource/bin/isql"


def isql(dconf, statements):
    script = "\n".join(statements) + "\ncheckpoint;\n"
    proc = subprocess.run(["docker", "exec", "-i", dconf.virtuoso["container"], ISQL, str(dconf.virtuoso["isql_port"]),
                           "dba", "dba"], input=script, text=True, capture_output=True)
    if proc.returncode != 0 or "*** Error" in proc.stdout + proc.stderr:
        raise RuntimeError(f"isql failed: {(proc.stdout + proc.stderr)[-2000:]}")


def compose(dconf, *args):
    data_dir = str(Path(dconf.path("data", "x")).parent.resolve())
    env = {**os.environ, "FEDSHOP_DISCOVERY_DATA": data_dir}
    subprocess.run(["docker", "compose", "-f", dconf.virtuoso["compose_file"], *args], check=True, env=env)


def start(dconf):
    """Start the container. It keeps the graphs of the config loaded last; `load` switches configs."""
    compose(dconf, "up", "-d")
    wait_for(dconf.endpoint)


def load(dconf, owners):
    """Recreate the container from scratch and load every owner's graph."""
    compose(dconf, "down", "-v")
    start(dconf)
    data_dir = Path(dconf.path("data", "x")).parent
    statements = []
    for name, info in owners.items():
        if Path(data_dir, f"{name}.nt").exists():
            statements.append(f"ld_dir('/usr/share/proj', '{name}.nt', '{info['namespace']}');")
    statements.append("rdf_loader_run();")
    LOGGER.info(f"Loading {len(statements) - 1} files into {dconf.virtuoso['container']}...")
    isql(dconf, statements)

    services = []
    for name, info in owners.items():
        if info["service"]:
            services += [
                f"DB.DBA.VHOST_DEFINE(vhost=>'localhost', lhost=>':{info['port']}', lpath=>'/{name}/sparql', "
                f"ppath=>'/!sparql/', is_dav=>1, vsp_user=>'dba', opts=>vector('browse_sheet', '', 'noinherit', 'yes'));",
                f"INSERT REPLACING DB.DBA.SYS_SPARQL_HOST (SH_HOST, SH_GRAPH_URI) "
                f"VALUES ('localhost:{info['port']}', '{info['namespace']}');",
            ]
    isql(dconf, services)
    return check(dconf, owners)


def check(dconf, owners):
    """Triples per graph against owners.json, and every service answering for its own graph only."""
    counts = {r["g"]: int(r["n"]) for r in sparql_csv(dconf.endpoint, "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g")}
    wrong = {n: (counts.get(i["namespace"], 0), i["triples"]) for n, i in owners.items() if counts.get(i["namespace"], 0) != i["triples"]}
    if wrong:
        raise RuntimeError(f"Graphs disagree with owners.json (loaded, expected): {dict(list(wrong.items())[:5])}")
    for name, info in owners.items():
        if info["service"]:
            n = int(sparql_csv(info["endpoint"], "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])
            if n != info["triples"]:
                raise RuntimeError(f"{info['endpoint']} answers {n} triples, {name} holds {info['triples']}")
    services = sum(i["service"] for i in owners.values())
    LOGGER.info(f"{len(owners)} graphs loaded ({sum(counts.get(i['namespace'], 0) for i in owners.values())} triples), "
                f"{services} services answer for their own graph")
    return {"graphs": len(owners), "services": services}
