"""Serve the documents with the Community Solid Server (tools/css, configured by css-config.json),
behind the counting proxy (tools/proxy).

The proxy holds the public ports: css.port for documents and every query service's port. The Solid
server and Virtuoso's services listen on their public port + proxy.port_offset, so engines reach
them only through the proxy, and every request is counted by source (known or discovered publisher)
and by kind (document, storage root, SPARQL).
"""
import json
import subprocess
from pathlib import Path


def command(dconf, log_level="warn"):
    server = Path(dconf.discovery_dir, "tools", "css", "node_modules", "@solid", "community-server", "bin", "server.js")
    web_dir = Path(dconf.path("web", "x")).parent.resolve()
    return ["node", str(server.resolve()), "-c", str(Path(dconf.css["config"]).resolve()), "-f", str(web_dir),
            "-p", str(dconf.internal_port(dconf.css["port"])), "-b", dconf.base, "-l", log_level]


def write_proxy_map(dconf, owners):
    """The proxy's ports and publishers (proxy.json)."""
    proxy_map = {
        "documents": {"port": int(dconf.css["port"]), "target": dconf.internal_port(dconf.css["port"])},
        "services": [{"port": i["port"], "target": dconf.internal_port(i["port"]), "publisher": n}
                     for n, i in owners.items() if i["service"]],
        "publishers": {n: {"kind": i["kind"], "catalog": i["classical"]} for n, i in owners.items()},
        "control_port": int(dconf.proxy["control_port"]),
        "max_inflight": int(dconf.proxy["max_inflight"]),
    }
    path = dconf.path("proxy.json")
    Path(path).write_text(json.dumps(proxy_map, indent=1))
    return path


def serve(dconf, owners, log_level="warn", request_log=None):
    """Run the proxy and CSS in the foreground until interrupted."""
    proxy = Path(dconf.discovery_dir, "tools", "proxy", "proxy.js").resolve()
    args = ["node", str(proxy), write_proxy_map(dconf, owners)] + ([str(Path(request_log).resolve())] if request_log else [])
    proc = subprocess.Popen(args)
    try:
        subprocess.run(command(dconf, log_level), check=True)
    finally:
        proc.terminate()
        proc.wait()
