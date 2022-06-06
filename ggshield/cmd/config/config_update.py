import click


@click.command()
@click.pass_context
def config_update_cmd(ctx: click.Context) -> None:
    """
    Update user config to the latest version
    """
    config = ctx.obj["config"]
    config.user_config.save(config._config_path)
