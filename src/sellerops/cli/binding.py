"""Bind parser metadata to a lazily imported command handler."""
from argparse import ArgumentParser, Namespace
from importlib import import_module


def bind(parser: ArgumentParser, module: str, function: str) -> None:
    parser.set_defaults(handler=(module, function))


def dispatch(args: Namespace) -> int:
    module, function = args.handler
    result = getattr(import_module(module), function)(args)
    return 0 if result is None else result
