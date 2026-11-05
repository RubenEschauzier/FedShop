"""The query services: two Virtuoso containers with a graph per publisher and a virtual endpoint per
publisher with a service.

- The main container holds every publisher's data (data/, at publishers.json's `file`), each in the
  graph named by its storage, so the analysis and the reference answers can see who holds what (the
  analysis endpoint). It runs the services of the discoverable publishers: the discovered endpoints.
- The known container holds the known publishers' graphs and runs their services: the endpoints
  known up front. Keeping them apart lets the proxy report CPU and memory for each.

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
    """(container, isql port, http port, host) of the main and the known container."""
    v = dconf.virtuoso
    return {"main": (v["container"], v["isql_port"], v["http_port"], v["host"]),
            "known": (v["known_container"], v["known_isql_port"], v["known_http_port"], v["known_host"])}


def isql(dconf, statements, which="main"):
    container, port, _, _ = containers(dconf)[which]
    script = "\n".join(statements) + "\ncheckpoint;\n"
    proc = subprocess.run(["docker", "exec", "-i", container, ISQL, str(port), "dba", "dba"],
                          input=script, text=True, capture_output=True)
    if proc.returncode != 0 or "*** Error" in proc.stdout + proc.stderr:
        raise RuntimeError(f"isql failed: {(proc.stdout + proc.stderr)[-2000:]}")


def compose(dconf, *args):
    env = {**os.environ, "FEDSHOP_DISCOVERY_DATA": str(dconf.dir("data"))}
    subprocess.run(["docker", "compose", "-f", dconf.virtuoso["compose_file"], *args], check=True, env=env)


def start(dconf):
    """Start the containers and wait for them. They keep the graphs of the config loaded last; `load`
    switches configs. With virtuoso.external, the containers are run by someone else (jbr): only wait."""
    if not dconf.virtuoso["external"]:
        compose(dconf, "up", "-d")
    for _, _, http_port, host in containers(dconf).values():
        wait_for(f"http://{host}:{http_port}/sparql", timeout=int(dconf.virtuoso["wait_timeout"]))


def statements(dconf, publishers, which):
    """The SQL that loads a container's graphs (from /usr/share/proj) and defines its services."""
    data_dir = dconf.dir("data")
    names = list(publishers) if which == "main" else [n for n, i in publishers.items() if i["known"]]
    files = {n: Path(publishers[n]["file"]) for n in names if Path(data_dir, publishers[n]["file"]).exists()}
    load = [f"ld_dir('{Path('/usr/share/proj', f.parent)}', '{f.name}', '{publishers[n]['storage']}');"
            for n, f in files.items()]
    # Queries with VALUES (as engines send for bind joins) run through this view, which the
    # anonymous SPARQL user can't read by default
    services = ['GRANT SELECT ON DB.DBA.SPARQL_SINV_2 TO "SPARQL";',
                'GRANT EXECUTE ON DB.DBA.SPARQL_SINV_IMP TO "SPARQL";']
    for name in names:
        info = publishers[name]
        if info["service"] and (which == "known") == info["known"]:
            port = dconf.internal_port(info["port"])        # the proxy serves the public port
            services += [
                f"DB.DBA.VHOST_DEFINE(vhost=>'localhost', lhost=>':{port}', lpath=>'/{name}/sparql', "
                f"ppath=>'/!sparql/', is_dav=>1, vsp_user=>'dba', opts=>vector('browse_sheet', '', 'noinherit', 'yes'));",
                f"INSERT REPLACING DB.DBA.SYS_SPARQL_HOST (SH_HOST, SH_GRAPH_URI) "
                f"VALUES ('localhost:{port}', '{info['storage']}');",
            ]
    return load + ["rdf_loader_run();"], services


def load(dconf, publishers):
    """Recreate both containers from scratch, load the graphs and define the services."""
    compose(dconf, "down", "-v")
    start(dconf)
    for which in ("main", "known"):
        load_sql, services = statements(dconf, publishers, which)
        LOGGER.info(f"Loading {len(load_sql) - 1} files into {containers(dconf)[which][0]}...")
        isql(dconf, load_sql, which)
        isql(dconf, services, which)
    return check(dconf, publishers)


def write_load_scripts(dconf, publishers):
    """virtuoso/<main|known>/load.sql: the same loading as `load`, as a script for the container's
    initdb.d (run once, when the container creates its database), with the data at /usr/share/proj."""
    paths = []
    for which in ("main", "known"):
        load_sql, services = statements(dconf, publishers, which)
        path = Path(dconf.path("virtuoso", which, "load.sql"))
        path.write_text("\n".join(load_sql + services + ["checkpoint;"]) + "\n")
        paths.append(str(path))
    return paths


def check(dconf, publishers):
    """Triples per graph in both containers against publishers.json, and every service answering for its own graph only."""
    for which, names in (("main", list(publishers)), ("known", [n for n, i in publishers.items() if i["known"]])):
        container, _, http_port, host = containers(dconf)[which]
        counts = {r["g"]: int(r["n"]) for r in sparql_csv(f"http://{host}:{http_port}/sparql",
                                                          "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g")}
        wrong = {n: (counts.get(publishers[n]["storage"], 0), publishers[n]["triples"]) for n in names
                 if counts.get(publishers[n]["storage"], 0) != publishers[n]["triples"]}
        if wrong:
            raise RuntimeError(f"{container}: graphs disagree with publishers.json (loaded, expected): {dict(list(wrong.items())[:5])}")
        if which == "main":
            loaded = sum(counts.get(i["storage"], 0) for i in publishers.values())
    for name, info in publishers.items():
        if info["service"]:
            host, port = containers(dconf)["known" if info["known"] else "main"][3], dconf.internal_port(info["port"])
            internal = f"http://{host}:{port}/{name}/sparql"
            n = int(sparql_csv(internal, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", host=f"localhost:{port}")[0]["n"])
            if n != info["triples"]:
                raise RuntimeError(f"{info['endpoint']} answers {n} triples, {name} holds {info['triples']}")
    services = sum(i["service"] for i in publishers.values())
    LOGGER.info(f"{len(publishers)} graphs loaded ({loaded} triples), {services} services answer for their own graph")
    return {"graphs": len(publishers), "services": services}
