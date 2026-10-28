import json
from pathlib import Path

import click

from . import catalog, css, fragment, ownership, store, workload
from .common import DiscoveryConfig, load_owners


@click.group
@click.option("--overrides", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="YAML file with keys of the config's discovery section, merged over it (repeatable, in order)")
@click.pass_context
def cli(ctx, overrides):
    """Discovery-based FedShop: publishers, their documents and query services, and the workload."""
    ctx.obj = overrides


CONFIG = click.argument("configfile", type=click.Path(exists=True, dir_okay=False))


def config(configfile):
    """The discovery config, with the --overrides given to the command group."""
    return DiscoveryConfig(configfile, click.get_current_context().find_root().obj or ())


@cli.command("catalog")
@CONFIG
@click.option("--force", is_flag=True, default=False, help="Regenerate even if the brand catalog exists")
def catalog_cmd(configfile, force):
    """Generate the brand catalogs with FedShop's product template (catalog/)."""
    catalog.generate(config(configfile), force=force)


@cli.command()
@CONFIG
@click.option("--force", is_flag=True, default=False, help="Regenerate members that already exist")
def members(configfile, force):
    """Generate the shops and review sites with FedShop's vendor and rating-site templates (members/)."""
    catalog.generate_members(config(configfile), force=force)


@cli.command()
@CONFIG
def own(configfile):
    """Restructure FedShop's data into publishers: data/<publisher>.nt, publishers.json, links.tsv, placements.nq."""
    click.echo(json.dumps(ownership.run(config(configfile)), indent=2))


@cli.command()
@CONFIG
def load(configfile):
    """(Re)create the Virtuoso containers: one graph per publisher, one service per publisher with a service."""
    dconf = config(configfile)
    click.echo(json.dumps(store.load(dconf, load_owners(dconf)), indent=2))


@cli.command("load-scripts")
@CONFIG
def load_scripts(configfile):
    """Write the loading of both Virtuoso containers as initdb.d scripts (virtuoso/<main|catalog>/load.sql),
    for running the containers elsewhere (jbr) instead of `load`."""
    dconf = config(configfile)
    click.echo("\n".join(store.write_load_scripts(dconf, load_owners(dconf))))


@cli.command("fragment")
@CONFIG
def fragment_cmd(configfile):
    """Write one document per subject with rdf-dataset-fragmenter (web/), plus storage metadata."""
    dconf = config(configfile)
    click.echo(json.dumps(fragment.run(dconf, load_owners(dconf)), indent=2))


@cli.command()
@CONFIG
@click.option("--log-level", default="warn")
@click.option("--request-log", type=click.Path(dir_okay=False), default=None,
              help="Append every request the proxy forwards to this JSON-lines file")
def serve(configfile, log_level, request_log):
    """Serve the documents and query services behind the counting proxy (keeps running)."""
    dconf = config(configfile)
    store.start(dconf)
    css.serve(dconf, load_owners(dconf), log_level, request_log)


@cli.command("serving-plan")
@CONFIG
@click.option("--documents-host", default="localhost", help="Host the Solid server runs on, as the proxy reaches it")
def serving_plan(configfile, documents_host):
    """Write serving.json: the Solid server's command and the proxy's map, for serving the benchmark elsewhere (jbr)."""
    dconf = config(configfile)
    path = dconf.path("serving.json")
    Path(path).write_text(json.dumps(css.serving_plan(dconf, load_owners(dconf), documents_host), indent=1))
    click.echo(path)


@cli.command("build-workload")
@CONFIG
def build_workload(configfile):
    """Instantiate the templates, analyse the candidates and write the selected workload."""
    dconf = config(configfile)
    store.start(dconf)
    store.check(dconf, load_owners(dconf))      # the container must hold this config's data
    records = workload.build(dconf)
    click.echo(f"{sum(bool(r.get('selected')) for r in records)} instances selected, "
               f"see {dconf.path('workload-summary.csv')}")


@cli.command()
@CONFIG
def plain(configfile):
    """Write the plain form (no SERVICE clauses) of every instance, with its own answers."""
    dconf = config(configfile)
    store.start(dconf)
    store.check(dconf, load_owners(dconf))
    click.echo(f"{len(workload.plain_forms(dconf))} plain forms written")


@cli.command()
@CONFIG
@click.argument("queryfile", type=click.Path(exists=True, dir_okay=False))
def check(configfile, queryfile):
    """Analyse one query (sources, reachability, delegation gain) and print the result."""
    dconf = config(configfile)
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
