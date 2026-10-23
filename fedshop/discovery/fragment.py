"""One document per subject, written by rdf-dataset-fragmenter and served by the Community Solid Server.

- The main pass runs the fragmenter's CLI with its subject strategy over every owner's data: a
  triple goes to the document of its subject (without fragment), so <base>vendor13/Offer7#it is
  described in <base>vendor13/Offer7 and <base>taxonomy/countries holds all countries.
- Triples that belong in another document than their subject's (placements.nq: cached specs in
  their listing; offers in their listing, reviews in their page and products in their brand's
  category pages, for traversal) are placed in one more fragmenter run (tools/fragmenter/place.js).
- Every owner's namespace is a storage root. Its metadata (.meta, read by CSS) types it as a
  pim:Storage, which CSS advertises with a `Link: <pim:Storage>; rel="type"` header, and, for
  owners with a query service, names that service with void:sparqlEndpoint. CSS lists the root's
  documents with ldp:contains.
- The server root itself is not readable (its ACL only grants read access to what is below it),
  so it can't be used as an index of all storages.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from .common import LOGGER, PIM_STORAGE, RDF_TYPE, VOID_ENDPOINT

ACL = "http://www.w3.org/ns/auth/acl#"


def escape_regex(text):
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", text)


def tool(dconf, *parts):
    return str(Path(dconf.discovery_dir, "tools", *parts).resolve())


BATCH = 200   # source files per fragmenter run: Components.js reads a longer list recursively and overflows


def config(dconf, owners, web_dir):
    data_dir = Path(dconf.path("data", "x")).parent.resolve()
    return {
        "@context": "https://linkedsoftwaredependencies.org/bundles/npm/rdf-dataset-fragmenter/^2.0.0/components/context.jsonld",
        "@id": "urn:rdf-dataset-fragmenter:default",
        "@type": "Fragmenter",
        "quadSource": {
            "@type": "QuadSourceComposite",
            "sources": [{"@type": "QuadSourceFile", "filePath": str(data_dir / f"{name}.nt")} for name in owners],
        },
        "transformers": [],
        "fragmentationStrategy": {"@type": "FragmentationStrategySubject"},
        "quadSink": {
            "@type": "QuadSinkFile",
            "log": False,
            "outputFormat": "application/n-quads",
            "fileExtension": ".nq",
            "iriToPath": {f"^{escape_regex(dconf.base)}": f"{web_dir}/"},
        },
    }


def run(dconf, owners):
    web_dir = Path(dconf.path("web", "x")).parent.resolve()
    shutil.rmtree(web_dir, ignore_errors=True)
    web_dir.mkdir(parents=True)
    # Each owner's file holds only its own subjects and the file sink appends, so batches of owners
    # can be fragmented one after the other into the same documents.
    names = list(owners)
    batches = [names[i:i + BATCH] for i in range(0, len(names), BATCH)]
    LOGGER.info(f"Fragmenting every owner's data with rdf-dataset-fragmenter ({len(batches)} runs)...")
    for i, batch in enumerate(batches):
        configfile = dconf.path("fragmenter", f"config-{i}.json")
        Path(configfile).write_text(json.dumps(config(dconf, batch, web_dir), indent=2))
        subprocess.run(["node", tool(dconf, "fragmenter", "node_modules", "rdf-dataset-fragmenter", "bin", "runner.js"),
                        str(Path(configfile).resolve())], check=True)
    subprocess.run(["node", tool(dconf, "fragmenter", "place.js"), str(Path(dconf.path("placements.nq")).resolve()),
                    dconf.base, str(web_dir)], check=True)

    for name, info in owners.items():
        ns = info["namespace"]
        meta = [f"<{ns}> <{RDF_TYPE}> <{PIM_STORAGE}> .\n"]
        if info["service"]:
            meta.append(f"<{ns}> <{VOID_ENDPOINT}> <{info['endpoint']}> .\n")
        Path(web_dir, name).mkdir(exist_ok=True)
        Path(web_dir, name, ".meta").write_text("".join(meta))
    root = f"{dconf.base}.acl#children"
    acl = [(f"<{root}>", f"<{RDF_TYPE}>", f"<{ACL}Authorization>"),
           (f"<{root}>", f"<{ACL}agentClass>", "<http://xmlns.com/foaf/0.1/Agent>"),
           (f"<{root}>", f"<{ACL}default>", f"<{dconf.base}>"),
           (f"<{root}>", f"<{ACL}mode>", f"<{ACL}Read>")]
    Path(web_dir, ".acl.nq").write_text("".join(f"{s} {p} {o} .\n" for s, p, o in acl))

    documents = sum(1 for _ in web_dir.rglob("*.nq")) - 1
    LOGGER.info(f"{documents} documents in {len(owners)} storages under {web_dir}")
    return {"documents": documents, "storages": len(owners)}
