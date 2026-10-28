"""Serve the documents with the Community Solid Server (tools/css, configured by css-config.json),
behind the counting proxy (tools/proxy).

The proxy holds the public ports: css.port for documents and every query service's port. The Solid
server and Virtuoso's services listen on their public port + proxy.port_offset, so engines reach
them only through the proxy, and every request is counted by source (known or discovered publisher)
and by kind (document, storage root, SPARQL). The proxy also measures CPU and memory of the Solid
server, of each Virtuoso container (catalog endpoints, discovered endpoints) and of itself.
"""
import json
import subprocess
from pathlib import Path


def command(dconf, log_level="warn"):
    server = Path(dconf.discovery_dir, "tools", "css", "node_modules", "@solid", "community-server", "bin", "server.js")
    web_dir = Path(dconf.path("web", "x")).parent.resolve()
    return ["node", str(server.resolve()), "-c", str(Path(dconf.css["config"]).resolve()), "-f", str(web_dir),
            "-p", str(dconf.internal_port(dconf.css["port"])), "-b", dconf.base, "-l", log_level]


def cgroup(container):
    """The cgroup directory of a running container, where its CPU and memory counters are."""
    pid = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", container],
                         check=True, capture_output=True, text=True).stdout.strip()
    path = next(l[3:] for l in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if l.startswith("0::"))
    return f"/sys/fs/cgroup{path}"


def proxy_map(dconf, owners, documents_host="localhost"):
    """The proxy's ports, publishers and upstream hosts, without the components it measures."""
    v = dconf.virtuoso
    return {
        "documents": {"port": int(dconf.css["port"]), "target": dconf.internal_port(dconf.css["port"]),
                      "host": documents_host},
        "services": [{"port": i["port"], "target": dconf.internal_port(i["port"]), "publisher": n,
                      "host": v["catalog_host"] if i["classical"] else v["host"]}
                     for n, i in owners.items() if i["service"]],
        "publishers": {n: {"kind": i["kind"], "catalog": i["classical"]} for n, i in owners.items()},
        "control_port": int(dconf.proxy["control_port"]),
        "max_inflight": int(dconf.proxy["max_inflight"]),
        "monitor": {"sample_ms": int(dconf.proxy["sample_ms"]), "components": []},
    }


def serving_plan(dconf, owners, documents_host):
    """What it takes to serve the benchmark elsewhere (jbr): the Solid server's command and the proxy's
    map. Paths are absolute, as seen in the environment this runs in (the generator image)."""
    return {
        "base_url": dconf.base,
        "proxy_host": dconf.public_host,
        "css_command": command(dconf),
        "web_dir": str(Path(dconf.path("web", "x")).parent.resolve()),
        "proxy_script": str(Path(dconf.discovery_dir, "tools", "proxy", "proxy.js").resolve()),
        "proxy_map": proxy_map(dconf, owners, documents_host),
    }


def write_proxy_map(dconf, owners, css_pid):
    """The proxy's ports, publishers and the components it measures (proxy.json)."""
    plan = proxy_map(dconf, owners)
    plan["monitor"]["components"] = [
        {"name": "css", "pid": css_pid},
        {"name": "catalog_endpoints", "cgroup": cgroup(dconf.virtuoso["catalog_container"])},
        {"name": "discovered_endpoints", "cgroup": cgroup(dconf.virtuoso["container"])},
    ]
    path = dconf.path("proxy.json")
    Path(path).write_text(json.dumps(plan, indent=1))
    return path


def serve(dconf, owners, log_level="warn", request_log=None):
    """Run CSS and the proxy in the foreground until interrupted."""
    css = subprocess.Popen(command(dconf, log_level))
    proxy = None
    try:
        script = Path(dconf.discovery_dir, "tools", "proxy", "proxy.js").resolve()
        args = ["node", str(script), write_proxy_map(dconf, owners, css.pid)]
        proxy = subprocess.Popen(args + ([str(Path(request_log).resolve())] if request_log else []))
        if css.wait() != 0:
            raise RuntimeError(f"The Solid server stopped with exit code {css.returncode}")
    finally:
        for proc in (proxy, css):
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait()
