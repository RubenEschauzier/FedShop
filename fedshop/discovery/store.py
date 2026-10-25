"""The query services: two Virtuoso containers with a graph per publisher and a virtual endpoint per
publisher with a service.

- The main container holds every publisher's data (data/<publisher>.nt), each in the graph named by
  its storage, so the analysis and the reference answers can see who holds what (the analysis
  endpoint). It runs the services of the publishers outside the catalog: the discovered endpoints.
- The catalog container holds the catalog's graphs and runs their services: the endpoints known up
  front. Keeping them apart lets the proxy report CPU and memory for each.

A service is a virtual SPARQL endpoint, as FedShop builds them: a vhost on its own port (the public
port + the proxy's offset) whose default graph is the publisher's graph. Publishers without a
service (reviewers, small brands) are only loaded for the analysis.
"""
import os
import subprocess
from pathlib import Path

from .common import LOGGER, sparql_csv, wait_for

ISQL = "/opt/virtuoso-opensource/bin/isql"


def containers(dconf):
    """(container, isql port, http port) of the main and the catalog container."""
    v = dconf.virtuoso
    return {"main": (v["container"], v["isql_port"], v["http_port"]),
            "catalog": (v["catalog_container"], v["catalog_isql_port"], v["catalog_http_port"])}


def isql(dconf, statements, which="main"):
    container, port, _ = containers(dconf)[which]
    script = "\n".join(statements) + "\ncheckpoint;\n"
    proc = subprocess.run(["docker", "exec", "-i", container, ISQL, str(port), "dba", "dba"],
                          input=script, text=True, capture_output=True)
    if proc.returncode != 0 or "*** Error" in proc.stdout + proc.stderr:
        raise RuntimeError(f"isql failed: {(proc.stdout + proc.stderr)[-2000:]}")


def compose(dconf, *args):
    data_dir = str(Path(dconf.path("data", "x")).parent.resolve())
    env = {**os.environ, "FEDSHOP_DISCOVERY_DATA": data_dir}
    subprocess.run(["docker", "compose", "-f", dconf.virtuoso["compose_file"], *args], check=True, env=env)


def start(dconf):
    """Start the containers. They keep the graphs of the config loaded last; `load` switches configs."""
    compose(dconf, "up", "-d")
    for _, _, http_port in containers(dconf).values():
        wait_for(f"http://localhost:{http_port}/sparql")


def load(dconf, owners):
    """Recreate both containers from scratch, load the graphs and define the services."""
    compose(dconf, "down", "-v")
    start(dconf)
    data_dir = Path(dconf.path("data", "x")).parent
    for which, names in (("main", list(owners)), ("catalog", [n for n, i in owners.items() if i["classical"]])):
        statements = [f"ld_dir('/usr/share/proj', '{n}.nt', '{owners[n]['storage']}');"
                      for n in names if Path(data_dir, f"{n}.nt").exists()]
        LOGGER.info(f"Loading {len(statements)} files into {containers(dconf)[which][0]}...")
        isql(dconf, statements + ["rdf_loader_run();"], which)

        services = []
        for name in names:
            info = owners[name]
            if info["service"] and (which == "catalog") == info["classical"]:
                port = dconf.internal_port(info["port"])        # the proxy serves the public port
                services += [
                    f"DB.DBA.VHOST_DEFINE(vhost=>'localhost', lhost=>':{port}', lpath=>'/{name}/sparql', "
                    f"ppath=>'/!sparql/', is_dav=>1, vsp_user=>'dba', opts=>vector('browse_sheet', '', 'noinherit', 'yes'));",
                    f"INSERT REPLACING DB.DBA.SYS_SPARQL_HOST (SH_HOST, SH_GRAPH_URI) "
                    f"VALUES ('localhost:{port}', '{info['storage']}');",
                ]
        isql(dconf, services, which)
    return check(dconf, owners)


def check(dconf, owners):
    """Triples per graph in both containers against publishers.json, and every service answering for its own graph only."""
    for which, names in (("main", list(owners)), ("catalog", [n for n, i in owners.items() if i["classical"]])):
        container, _, http_port = containers(dconf)[which]
        counts = {r["g"]: int(r["n"]) for r in sparql_csv(f"http://localhost:{http_port}/sparql",
                                                          "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g")}
        wrong = {n: (counts.get(owners[n]["storage"], 0), owners[n]["triples"]) for n in names
                 if counts.get(owners[n]["storage"], 0) != owners[n]["triples"]}
        if wrong:
            raise RuntimeError(f"{container}: graphs disagree with publishers.json (loaded, expected): {dict(list(wrong.items())[:5])}")
        if which == "main":
            loaded = sum(counts.get(i["storage"], 0) for i in owners.values())
    for name, info in owners.items():
        if info["service"]:
            internal = f"http://localhost:{dconf.internal_port(info['port'])}/{name}/sparql"
            n = int(sparql_csv(internal, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])
            if n != info["triples"]:
                raise RuntimeError(f"{info['endpoint']} answers {n} triples, {name} holds {info['triples']}")
    services = sum(i["service"] for i in owners.values())
    LOGGER.info(f"{len(owners)} graphs loaded ({loaded} triples), {services} services answer for their own graph")
    return {"graphs": len(owners), "services": services}
