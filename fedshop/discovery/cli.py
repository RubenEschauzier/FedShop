import json
from pathlib import Path

import click

from . import catalog, css, fragment, ownership, store, workload
from .common import DiscoveryConfig, load_owners


@click.group
def cli():
    """Discovery-based FedShop: owners, their documents and query services, and the workload."""


CONFIG = click.argument("configfile", type=click.Path(exists=True, dir_okay=False))


@cli.command("catalog")
@CONFIG
@click.option("--force", is_flag=True, default=False, help="Regenerate even if the brand catalog exists")
def catalog_cmd(configfile, force):
    """Generate the brand catalogs, shops and review sites with FedShop's WatDiv templates (catalog/, members/)."""
    dconf = DiscoveryConfig(configfile)
    catalog.generate(dconf, force=force)
    catalog.generate_members(dconf, force=force)


@cli.command()
@CONFIG
def own(configfile):
    """Give every triple its owner: data/<owner>.nt, owners.json, links.tsv, replicas.json."""
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
    """catalog, own, load, fragment and build-workload in one go."""
    ctx.invoke(catalog_cmd, configfile=configfile, force=False)
    ctx.invoke(own, configfile=configfile)
    ctx.invoke(load, configfile=configfile)
    ctx.invoke(fragment_cmd, configfile=configfile)
    ctx.invoke(build_workload, configfile=configfile)


if __name__ == "__main__":
    cli()
