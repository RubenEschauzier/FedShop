// Places triples in documents other than their subject's: cached specs in the listing that carries
// them, the copies for traversal (offers in their listing, reviews in their page, a brand's products
// in its category pages).
//
// The input is N-Quads whose graph names the target document. rdf-dataset-fragmenter writes them
// with a strategy that sends each quad to its graph's document and drops the graph. The file sink
// appends, so documents written by the main (subject) pass keep their own triples.
//
// Usage: node place.js <placements.nq> <base URL> <output directory>
const path = require('node:path');
const { DataFactory } = require('rdf-data-factory');
const { Fragmenter, QuadSinkFile, QuadSourceFile } = require('rdf-dataset-fragmenter');
const { FragmentationStrategyStreamAdapter } = require('rdf-dataset-fragmenter/lib/strategy/FragmentationStrategyStreamAdapter');

const DF = new DataFactory();
const escapeRegex = text => text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');

class FragmentationStrategyGraph extends FragmentationStrategyStreamAdapter {
  async handleQuad(quad, quadSink) {
    await quadSink.push(quad.graph.value, DF.quad(quad.subject, quad.predicate, quad.object));
  }
}

async function main() {
  const [placements, base, outDir] = process.argv.slice(2);
  await new Fragmenter({
    quadSource: new QuadSourceFile({ filePath: path.resolve(placements) }),
    transformers: [],
    fragmentationStrategy: new FragmentationStrategyGraph(),
    quadSink: new QuadSinkFile({
      outputFormat: 'application/n-quads',
      fileExtension: '.nq',
      iriToPath: { [`^${escapeRegex(base)}`]: `${path.resolve(outDir)}/` },
      log: false,
    }),
  }).fragment();
  process.stdout.write(`Placed the triples of ${placements}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exit(1);
});
