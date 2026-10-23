"""Continuous Kindle read-aloud queue plugin."""

def setup_cli(parser):
    from .queue_cli import setup_cli as implementation
    return implementation(parser)

def handle_cli(args):
    from .queue_cli import handle_cli as implementation
    return implementation(args)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="kindle-reader",
        help="Read and manage the Kindle audio queue",
        description="Manage persistent per-book Kindle Reader queues and requests.",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
    )


__all__ = ["register"]
