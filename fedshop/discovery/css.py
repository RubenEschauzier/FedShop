"""Serve the documents with the Community Solid Server (tools/css, configured by css-config.json)."""
import subprocess
from pathlib import Path


def command(dconf, log_level="warn"):
    server = Path(dconf.discovery_dir, "tools", "css", "node_modules", "@solid", "community-server", "bin", "server.js")
    web_dir = Path(dconf.path("web", "x")).parent.resolve()
    return ["node", str(server.resolve()), "-c", str(Path(dconf.css["config"]).resolve()), "-f", str(web_dir),
            "-p", str(dconf.css["port"]), "-b", dconf.base, "-l", log_level]


def serve(dconf, log_level="warn"):
    """Run CSS in the foreground until interrupted."""
    subprocess.run(command(dconf, log_level), check=True)
