"""Bind parser metadata to a lazily imported command handler."""
from argparse import ArgumentParser, Namespace
from importlib import import_module


def bind(parser: ArgumentParser, module: str, function: str, *,
         action: str | None = None, channel: str | None = None) -> None:
    parser.set_defaults(handler=(module, function, action, channel))


def dispatch(args: Namespace) -> int:
    module, function, action, channel = args.handler
    handler = getattr(import_module(module), function)
    if action:
        result = handler(action, args, **({'channel': channel} if channel else {}))
    else:
        result = handler(args)
    return result if isinstance(result, int) else 0
