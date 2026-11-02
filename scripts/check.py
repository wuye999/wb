"""Run reproducible offline checks; never read live credentials or call a platform."""
import ast
import builtins
import contextlib
import importlib
import io
import os
from pathlib import Path
import socket
import subprocess
import symtable
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))


def source_checks() -> list[str]:
    errors = []
    built_in_names = set(dir(builtins)) | {'__name__', '__file__', '__package__', '__doc__', '__annotations__'}
    for p in SRC.rglob('*.py'):
        source = p.read_text(encoding='utf8')
        compile(source, str(p), 'exec')
        module = '.'.join(p.relative_to(SRC).with_suffix('').parts)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if module.startswith(('sellerops.features.listings.', 'sellerops.features.pricing.')) and node.func.attr in {
                    'post_write_merge', 'fetch_all', 'fetch_shop', 'sync_shop', 'sync_shops_parallel',
                }:
                    errors.append(f'{module}: write workflows must not chain snapshot sync or merge ({node.func.attr})')
            if isinstance(node, ast.ImportFrom):
                imports = [node.module or '']
            elif isinstance(node, ast.Import):
                imports = [a.name for a in node.names]
            else:
                continue
            forbidden = ['wb_ops']
            if module.startswith('sellerops.integrations.'):
                forbidden += ['sellerops.features', 'sellerops.cli', 'sellerops.persistence']
            if module.startswith('sellerops.persistence.'):
                forbidden += ['sellerops.features', 'sellerops.cli', 'sellerops.integrations']
            if module.startswith('sellerops.core.'):
                forbidden += ['sellerops.features', 'sellerops.cli', 'sellerops.integrations', 'sellerops.persistence']
            if module.startswith('sellerops.domain.'):
                forbidden += ['sellerops.features', 'sellerops.cli', 'sellerops.integrations', 'sellerops.persistence', 'sellerops.settings']
            for dependency in imports:
                if any(dependency == f or dependency.startswith(f + '.') for f in forbidden):
                    errors.append(f'{module}: forbidden dependency {dependency}')
        table = symtable.symtable(source, str(p), 'exec')
        declared = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}

        def check_symbols(scope):
            for symbol in scope.get_symbols():
                if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in declared | built_in_names:
                    errors.append(f'{module}.{scope.get_name()}: undefined {symbol.get_name()}')
            for child in scope.get_children():
                check_symbols(child)

        check_symbols(table)
    return errors


def import_and_cli_checks() -> int:
    from sellerops.settings import credentials
    from sellerops.cli.main import build_parser

    def denied(*args, **kwargs):
        raise AssertionError('Offline checks forbid network, subprocesses and credential reads')

    with patch.object(socket.socket, 'connect', denied), \
            patch.object(socket, 'create_connection', denied), \
            patch.object(subprocess, 'run', denied), \
            patch.object(credentials, 'get', denied), \
            patch.object(os, 'makedirs', denied):
        for p in sorted((SRC / 'sellerops').rglob('*.py')):
            module = '.'.join(p.relative_to(SRC).with_suffix('').parts)
            if module.endswith('.__main__'):
                continue
            if module.endswith('.__init__'):
                module = module[:-9]
            importlib.import_module(module)
            for node in ast.walk(ast.parse(p.read_text(encoding='utf8'))):
                if not isinstance(node, ast.ImportFrom) or not (node.module or '').startswith('sellerops'):
                    continue
                target = importlib.import_module(node.module)
                for alias in node.names:
                    if not hasattr(target, alias.name):
                        importlib.import_module(node.module + '.' + alias.name)
        parser = build_parser()
        subparsers = next(a for a in parser._actions if isinstance(a, __import__('argparse')._SubParsersAction))
        for name, sub in subparsers.choices.items():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    parser.parse_args([name, '--help'])
                except SystemExit as exc:
                    if exc.code:
                        raise
            module, function, _, _ = sub.get_default('handler')
            assert callable(getattr(importlib.import_module(module), function)), name
        return len(subparsers.choices)


def main() -> int:
    scratch = ROOT / '.test-tmp'
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    with tempfile.TemporaryDirectory(prefix='sellerops-check-') as data:
        os.environ['SELLEROPS_DATA_DIR'] = data
        errors = source_checks()
        if errors:
            print('\n'.join(errors))
            return 1
        count = import_and_cli_checks()
        print(f'Syntax, names, architecture, imports and {count} command definitions: OK')
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        def denied(*args, **kwargs):
            raise AssertionError('Unexpected network or external command in offline tests')
        with patch.object(socket.socket, 'connect', side_effect=denied) as network, \
                patch.object(subprocess, 'run', side_effect=denied) as external:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            network.assert_not_called()
            external.assert_not_called()
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
