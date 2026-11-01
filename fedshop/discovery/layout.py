"""How shops and review sites lay out their documents, applied to the graph files before they are loaded.

A shop or review site publishes one of two layouts (publishers.json's `layout`):

- `per-resource`: a document per offer or review (<base>vendor0/Offer5), next to its listings;
- `product-pages`: a page per product with its offers or reviews in it, as web shops publish product
  pages. An offer is <listing document>#Offer5 (<base>vendor0/Product125#Offer5), so the subject
  fragmentation (fragment.py) puts it in its listing's document and its IRI resolves there.

The second layout is a matter of IRIs, rewritten by rdf-dataset-fragmenter's
QuadTransformerRemapResourceIdentifier from the offer's dcterms:identifier and its bsbm:product (a
review: bsbm:reviewFor). It rewrites the graph files themselves, so the services, the workload and the
reference answers use the IRIs the documents use.

- Reviewers' profiles name reviews (foaf:made), and the transformer only rewrites a reference once it
  has seen the review, so the input is one file: the product-page publishers first, then the reviewers.
  (A composite source reads its files interleaved.)
- The transformer recognises a resource by its rdf:type, which must be the resource's first triple:
  graph files are written in graph_order.
- Rewriting is idempotent: an offer that already is <listing document>#Offer5 keeps its IRI.
"""
import json
import shutil
import subprocess
from pathlib import Path

from .common import BSBM, DCTERMS_IDENTIFIER, LOGGER, graph_order
from .fragment import escape_regex

PER_RESOURCE = "per-resource"
PRODUCT_PAGES = "product-pages"


def remap(type_name, target):
    """Rewrite each resource of a BSBM type to <document of its target>#<identifier>."""
    return {
        "@type": "QuadTransformerRemapResourceIdentifier",
        "newIdentifierSeparator": "#",
        "typeRegex": f"^{escape_regex(BSBM)}{type_name}$",
        "identifierPredicateRegex": f"^{escape_regex(DCTERMS_IDENTIFIER)}$",
        "targetPredicateRegex": f"^{escape_regex(BSBM)}{target}$",
    }


def config(dconf, input_file, out_dir):
    """Rewrite the IRIs and write every publisher's triples back to one file per publisher."""
    return {
        "@context": "https://linkedsoftwaredependencies.org/bundles/npm/rdf-dataset-fragmenter/^2.0.0/components/context.jsonld",
        "@id": "urn:rdf-dataset-fragmenter:default",
        "@type": "Fragmenter",
        "quadSource": {"@type": "QuadSourceFile", "filePath": str(input_file)},
        "transformers": [remap("Offer", "product"), remap("Review", "reviewFor")],
        "fragmentationStrategy": {"@type": "FragmentationStrategySubject"},
        "quadSink": {
            "@type": "QuadSinkFile",
            "log": False,
            "outputFormat": "application/n-quads",
            "fileExtension": ".nt",
            "iriToPath": {f"^{escape_regex(dconf.base)}([^/]+)/.*$": f"{out_dir}/$1"},
        },
    }


def run(dconf, publishers):
    """Rewrite the IRIs of the product-page publishers' offers and reviews (and the reviewers'
    references to them) in their graph files."""
    pages = [n for n, info in publishers.items() if info.get("layout") == PRODUCT_PAGES]
    shutil.rmtree(dconf.dir("layout"))
    work = dconf.dir("layout")
    if not pages:
        return 0
    names = pages + [n for n, info in publishers.items() if info["kind"] == "reviewer"]
    files = {n: Path(dconf.data_file(n, publishers[n])) for n in names}
    texts = {n: files[n].read_text() for n in names}
    input_file = work / "input.nt"
    input_file.write_text("".join(texts[n] for n in names))
    out_dir = work / "out"
    configfile = work / "config.json"
    configfile.write_text(json.dumps(config(dconf, input_file, out_dir), indent=2))
    LOGGER.info(f"Laying out {len(pages)} shops and review sites as product pages with rdf-dataset-fragmenter...")
    subprocess.run(["node", dconf.tool("fragmenter", "node_modules", "rdf-dataset-fragmenter", "bin", "runner.js"),
                    str(configfile)], check=True)
    for n in names:
        lines = set(Path(out_dir, f"{n}.nt").read_text().splitlines(keepends=True))
        before = texts[n].count("\n")
        if len(lines) != before:
            raise RuntimeError(f"Layout of {n}: {len(lines)} triples instead of {before}")
        files[n].write_text("".join(sorted(lines, key=graph_order)))
    return len(pages)
