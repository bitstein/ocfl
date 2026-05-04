"""Typed CLI context accessors."""

import click
import pytest
import typer

from ocfl_cli._appctx import AppCtxError, app_ctx, command_path, json_mode, root
from ocfl_cli.config import ArchiveConfig, Config


def _factory(_cfg: Config) -> object:
    return object()


def test_app_ctx_builds_typed_view_from_ctx_obj() -> None:
    archive = ArchiveConfig(name="main", root="/archive")
    cfg = Config(archives={"main": archive})
    ctx = typer.Context(
        click.Command("probe"),
        obj={
            "root": "/archive",
            "config": cfg,
            "archive": archive,
            "json": True,
            "quiet": False,
            "verbose": True,
            "no_color": False,
            "sp_client_factory": _factory,
            "btc_rpc_factory": _factory,
        },
    )

    typed = app_ctx(ctx)

    assert typed.root == "/archive"
    assert typed.config is cfg
    assert typed.archive is archive
    assert typed.json is True
    assert typed.quiet is False
    assert typed.verbose is True
    assert typed.no_color is False
    assert typed.sp_client_factory is _factory
    assert typed.btc_rpc_factory is _factory


def test_app_ctx_reports_missing_required_keys() -> None:
    ctx = typer.Context(click.Command("probe"), obj={})

    with pytest.raises(AppCtxError) as exc_info:
        app_ctx(ctx)

    assert "ctx.obj['root']" in str(exc_info.value)


def test_app_ctx_reports_malformed_ctx_obj() -> None:
    ctx = typer.Context(click.Command("probe"), obj=object())

    with pytest.raises(AppCtxError) as exc_info:
        app_ctx(ctx)

    assert "expected ctx.obj to be a dict" in str(exc_info.value)


def test_single_value_accessors_only_require_their_key() -> None:
    ctx = typer.Context(click.Command("probe"), obj={"root": "/archive", "json": True})

    assert root(ctx) == "/archive"
    assert json_mode(ctx) is True


def test_command_path_strips_click_root_program_name() -> None:
    root_ctx = typer.Context(click.Command("ocfl"), info_name="ocfl")
    stage_ctx = typer.Context(
        click.Command("stage"), info_name="stage", parent=root_ctx
    )
    content_ctx = typer.Context(
        click.Command("content"), info_name="content", parent=stage_ctx
    )

    assert content_ctx.command_path == "ocfl stage content"
    assert command_path(content_ctx) == "stage content"
