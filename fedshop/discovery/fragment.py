"""One document per subject, written by rdf-dataset-fragmenter and served by the Community Solid Server.

- The main pass runs the fragmenter's CLI with its subject strategy over every publisher's data: a
  triple goes to the document of its subject (without fragment), so <base>vendor13/Offer7#it is
  described in <base>vendor13/Offer7 and <base>taxonomy/countries holds all countries. With the
  other layouts (layout.py), <base>vendor13/Product42#Offer7 is described in its listing's document,
  <base>ratingsite2/reviews/2007-03-05#Review9 in its day's and <base>producer22/products/2005-06#Product77
  in its month's.
- Triples that belong in another document than their subject's (placements.nq: offers in their
  listing, reviews in their page and products in their brand's record, for traversal) are
  placed in one more fragmenter run (tools/fragmenter/place.js).
- A publisher's graph can state triples about another publisher's resources (a brand's
  `<listing> fs:listingOf <product>`, publishing.py). They are in its service, and placed in its own
  documents only, not in the document of their subject: the main pass reads the graph without them.
- Every publisher's directory is a storage root. Its metadata (.meta, read by CSS) types it as a
  pim:Storage, which CSS advertises with a `Link: <pim:Storage>; rel="type"` header, and, for
  publishers with a query service, names that service with void:sparqlEndpoint. CSS lists the root's
  documents with ldp:contains. CSS includes these triples in the root's own representation, so a
  client that fetches the root reads them there; it never needs the .meta itself.
- With `layout.dataset_links`, the root is also a void:Dataset, and every document links to it:
  `<vendor13/Product42> void:inDataset <vendor13/>`. A client entering any document then finds the
  publisher's dataset, and what answers queries over it, through a link in the data, rather than by
  walking up the url to the root (a Solid convention).
- The server root itself is not readable (its ACL only grants read access to what is below it),
  so it can't be used as an index of all storages.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from .common import LOGGER, PIM_STORAGE, RDF_TYPE, VOID_ENDPOINT

VOID_DATASET = "http://rdfs.org/ns/void#Dataset"
VOID_IN_DATASET = "http://rdfs.org/ns/void#inDataset"

ACL = "http://www.w3.org/ns/auth/acl#"
BATCH = 200   # source files per fragmenter run: Components.js reads a longer list recursively and overflows


def escape_regex(text):
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", text)


def config(dconf, files, web_dir):
    """files: graph files to fragment (absolute paths)."""
    return {
        "@context": "https://linkedsoftwaredependencies.org/bundles/npm/rdf-dataset-fragmenter/^2.0.0/components/context.jsonld",
        "@id": "urn:rdf-dataset-fragmenter:default",
        "@type": "Fragmenter",
        "quadSource": {
            "@type": "QuadSourceComposite",
            "sources": [{"@type": "QuadSourceFile", "filePath": str(f)} for f in files],
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


def own_subjects(dconf, info):
    """A publisher's graph file, or, if it states triples about other publishers' resources, a copy
    without them (fragmenter/own/<file>)."""
    path = Path(dconf.dir("data"), info["file"])
    prefix = f"<{info['storage']}"
    with open(path) as fs:
        lines = fs.readlines()
    own = [l for l in lines if l.startswith(prefix)]
    if len(own) == len(lines):
        return path
    copy = Path(dconf.path("fragmenter", "own", info["file"]))
    copy.write_text("".join(own))
    return copy.resolve()


def run(dconf, publishers):
    shutil.rmtree(dconf.dir("web"))
    web_dir = dconf.dir("web")
    # Each publisher's file holds only its own subjects and the file sink appends, so batches of publishers
    # can be fragmented one after the other into the same documents.
    files = [own_subjects(dconf, info) for info in publishers.values()]
    batches = [files[i:i + BATCH] for i in range(0, len(files), BATCH)]
    LOGGER.info(f"Fragmenting every publisher's data with rdf-dataset-fragmenter ({len(batches)} runs)...")
    for i, batch in enumerate(batches):
        configfile = dconf.path("fragmenter", f"config-{i}.json")
        Path(configfile).write_text(json.dumps(config(dconf, batch, web_dir), indent=2))
        subprocess.run(["node", dconf.tool("fragmenter", "node_modules", "rdf-dataset-fragmenter", "bin", "runner.js"),
                        str(Path(configfile).resolve())], check=True)
    subprocess.run(["node", dconf.tool("fragmenter", "place.js"), str(Path(dconf.path("placements.nq")).resolve()),
                    dconf.base, str(web_dir)], check=True)

    dataset_links = bool(dconf.layout_conf.get("dataset_links"))
    linked = link_datasets(dconf, publishers, web_dir) if dataset_links else 0

    for name, info in publishers.items():
        ns = info["storage"]
        meta = [f"<{ns}> <{RDF_TYPE}> <{PIM_STORAGE}> .\n"]
        if dataset_links:
            meta.append(f"<{ns}> <{RDF_TYPE}> <{VOID_DATASET}> .\n")
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

    documents = sum(1 for _ in web_dir.rglob("*.nq")) - 1          # not the server root's .acl.nq
    LOGGER.info(f"{documents} documents in {len(publishers)} storages under {web_dir}")
    return {"documents": documents, "storages": len(publishers), "dataset_links": linked}


def link_datasets(dconf, publishers, web_dir):
    """Append `<document> void:inDataset <storage root>` to every document of every publisher (after the
    placements, so to their final content). Returns how many documents were linked."""
    linked = 0
    for name, info in publishers.items():
        for path in sorted(Path(web_dir, name).rglob("*.nq")):
            document = dconf.base + path.relative_to(web_dir).as_posix()[:-len(".nq")]
            with open(path, "a") as fs:
                fs.write(f"<{document}> <{VOID_IN_DATASET}> <{info['storage']}> .\n")
            linked += 1
    return linked
