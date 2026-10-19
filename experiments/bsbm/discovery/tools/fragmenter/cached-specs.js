// Places each cached spec sheet in the document of the listing that carries it.
//
// A cached spec has the brand's product as subject, so rdf-dataset-fragmenter's subject strategy
// would put it in the brand's document. Each one is fragmented on its own with the constant
// strategy instead, which writes every quad to one given document; the file sink appends, so the
// listing document written by the main pass keeps its own triples.
//
// Usage: node cached-specs.js <cached-specs.json> <base URL> <output directory>
const fs = require('node:fs');
const path = require('node:path');
const { Fragmenter, FragmentationConstant, QuadSinkFile, QuadSourceFile } = require('rdf-dataset-fragmenter');

const escapeRegex = text => text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');

async function main() {
  const [specsFile, base, outDir] = process.argv.slice(2);
  const specs = JSON.parse(fs.readFileSync(specsFile, 'utf8'));
  const scratch = fs.mkdtempSync(path.join(path.dirname(path.resolve(specsFile)), 'cached-specs-'));
  try {
    for (const [i, spec] of specs.entries()) {
      const file = path.join(scratch, `${i}.nt`);
      fs.writeFileSync(file, spec.triples);
      await new Fragmenter({
        quadSource: new QuadSourceFile({ filePath: file }),
        transformers: [],
        fragmentationStrategy: new FragmentationConstant(spec.doc),
        quadSink: new QuadSinkFile({
          outputFormat: 'application/n-quads',
          fileExtension: '.nq',
          iriToPath: { [`^${escapeRegex(base)}`]: `${path.resolve(outDir)}/` },
          log: false,
        }),
      }).fragment();
    }
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
  process.stdout.write(`Placed ${specs.length} cached specs\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exit(1);
});
