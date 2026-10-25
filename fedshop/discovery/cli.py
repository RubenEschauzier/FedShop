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
    """Restructure FedShop's data into publishers: data/<publisher>.nt, publishers.json, links.tsv, placements.nq."""
    click.echo(json.dumps(ownership.run(DiscoveryConfig(configfile)), indent=2))


@cli.command()
@CONFIG
def load(configfile):
    """(Re)create the Virtuoso containers: one graph per publisher, one service per publisher with a service."""
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
@click.option("--request-log", type=click.Path(dir_okay=False), default=None,
              help="Append every request the proxy forwards to this JSON-lines file")
def serve(configfile, log_level, request_log):
    """Serve the documents and query services behind the counting proxy (keeps running)."""
    dconf = DiscoveryConfig(configfile)
    store.start(dconf)
    css.serve(dconf, load_owners(dconf), log_level, request_log)


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


@cli.command()
@CONFIG
@click.argument("queryfile", type=click.Path(exists=True, dir_okay=False))
def check(configfile, queryfile):
    """Analyse one query (sources, reachability, delegation gain) and print the result."""
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
