"""Continuous Kindle read-aloud queue plugin."""
from .queue_cli import handle_cli, setup_cli

def register(ctx) -> None:
    ctx.register_cli_command(
        name="kindle-reader",
        help="Read the live Kindle audio queue",
        description="JSON queue used by the Kindle Reader desktop pane.",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
    )

__all__ = ["register"]
