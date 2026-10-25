import json
from pathlib import Path

import click

from . import catalog, css, engine, fragment, ownership, store, workload
from .common import DiscoveryConfig, load_owners


@click.group
def cli():
    """Discovery-based FedShop: owners, their documents and query services, and the workload."""


CONFIG = click.argument("configfile", type=click.Path(exists=True, dir_okay=False))


@cli.command("catalog")
@CONFIG
@click.option("--force", is_flag=True, default=False, help="Regenerate even if the brand catalog exists")
def catalog_cmd(configfile, force):
    """Generate the brand catalogs with FedShop's product template (catalog/)."""
    catalog.generate(DiscoveryConfig(configfile), force=force)


@cli.command()
@CONFIG
@click.option("--force", is_flag=True, default=False, help="Regenerate members that already exist")
def members(configfile, force):
    """Generate the shops and review sites with FedShop's vendor and rating-site templates (members/)."""
    catalog.generate_members(DiscoveryConfig(configfile), force=force)


@cli.command()
@CONFIG
def own(configfile):
    """Give every triple its owner: data/<owner>.nt, owners.json, links.tsv, placements.nq."""
    click.echo(json.dumps(ownership.run(DiscoveryConfig(configfile)), indent=2))


@cli.command()
@CONFIG
def load(configfile):
    """(Re)create the discovery Virtuoso container: one graph per owner, one service per composite."""
    dconf = DiscoveryConfig(configfile)
    click.echo(json.dumps(store.load(dconf, load_owners(dconf)), indent=2))


@cli.command("fragment")
@CONFIG
def fragment_cmd(configfile):
    """Write one document per subject with rdf-dataset-fragmenter (web/), plus storage metadata."""
    dconf = DiscoveryConfig(configfile)
    click.echo(json.dumps(fragment.run(dconf, load_owners(dconf)), indent=2))


@cli.command()
@CONFIG
@click.option("--log-level", default="warn")
def serve(configfile, log_level):
    """Serve the documents with the Community Solid Server (keeps running)."""
    dconf = DiscoveryConfig(configfile)
    store.start(dconf)
    css.serve(dconf, log_level)


@cli.command("build-workload")
@CONFIG
def build_workload(configfile):
    """Instantiate the templates, analyse the candidates and write the selected workload."""
    dconf = DiscoveryConfig(configfile)
    store.start(dconf)
    store.check(dconf, load_owners(dconf))      # the container must hold this config's data
    records = workload.build(dconf)
    click.echo(f"{sum(bool(r.get('selected')) for r in records)} instances selected, "
               f"see {dconf.path('workload-summary.csv')}")


@cli.command()
@CONFIG
def plain(configfile):
    """Write the plain form (no SERVICE clauses) of every instance, with its own answers."""
    dconf = DiscoveryConfig(configfile)
    store.start(dconf)
    store.check(dconf, load_owners(dconf))
    click.echo(f"{len(workload.plain_forms(dconf))} plain forms written")


@cli.command("run-comunica")
@CONFIG
@click.option("--name", required=True, help="Run directory: <outdir>/runs/<name>; an existing run with the same settings resumes")
@click.option("--form", type=click.Choice(["plain", "service"]), default="plain",
              help="plain.sparql with the catalog's endpoints, or injected.sparql (SERVICE clauses) with seeds only")
@click.option("--comunica-config", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Comunica config (run through query-dynamic.js); default: the engine's built-in config")
@click.option("--endpoints/--no-endpoints", default=None, help="Give the catalog's endpoints as sources (default: for --form plain)")
@click.option("--timeout", default=180, help="Seconds per instance before its process group is killed")
@click.option("--engine", "engine_dir", default=engine.DEFAULT_ENGINE, help="Comunica engine package (with bin/query.js)")
@click.option("--pattern", default="q*/instance_*", help="Instances to run, as a glob under workload/")
def run_comunica(configfile, name, form, comunica_config, endpoints, timeout, engine_dir, pattern):
    """Run the workload with Comunica, recording each result's arrival time (runs/<name>/)."""
    dconf = DiscoveryConfig(configfile)
    results = engine.run(dconf, name, form=form, comunica_config=comunica_config, endpoints=endpoints,
                         timeout=timeout, engine=engine_dir, pattern=pattern)
    click.echo(Path(dconf.outdir, "runs", name, "summary.md").read_text())
    click.echo(f"{len(results)} instances, see {Path(dconf.outdir, 'runs', name)}")


@cli.command()
@CONFIG
@click.argument("queryfile", type=click.Path(exists=True, dir_okay=False))
def check(configfile, queryfile):
    """Analyse one query (claims, reachability, delegation gain) and print the result."""
    dconf = DiscoveryConfig(configfile)
    store.start(dconf)
    store.check(dconf, load_owners(dconf))
    click.echo(json.dumps(workload.check(dconf, Path(queryfile).read_text()), indent=2))


@cli.command("all")
@CONFIG
@click.pass_context
def all_steps(ctx, configfile):
    """catalog, members, own, load, fragment, build-workload and plain in one go."""
    ctx.invoke(catalog_cmd, configfile=configfile, force=False)
    ctx.invoke(members, configfile=configfile, force=False)
    ctx.invoke(own, configfile=configfile)
    ctx.invoke(load, configfile=configfile)
    ctx.invoke(fragment_cmd, configfile=configfile)
    ctx.invoke(build_workload, configfile=configfile)
    ctx.invoke(plain, configfile=configfile)


if __name__ == "__main__":
    cli()
