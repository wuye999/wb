"""Check documentation links and parse examples without running business commands."""
import argparse
import ast
import importlib.util
import json
from pathlib import Path
import re
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sellerops.cli.main import build_parser

REQUIRED = (
    'CONFIGURATION.md', 'DATA.md', 'USAGE.md', 'AI_GUIDE.md',
    'DEVELOPMENT.md', 'TROUBLESHOOTING.md', 'CLI.md',
    'PRODUCT_REPORT.md',
)


def headings(text: str) -> set[str]:
    anchors = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if line.startswith('```'):
            in_fence = not in_fence
        if in_fence or not re.match(r'^#{1,6}\s+', line):
            continue
        title = re.sub(r'^#{1,6}\s+', '', line).strip().lower()
        title = re.sub(r'[^\w -]', '', title).replace(' ', '-')
        count = seen.get(title, 0)
        anchors.add(title + (f'-{count}' if count else ''))
        seen[title] = count + 1
    return anchors


def run() -> int:
    errors: list[str] = []
    for name in REQUIRED:
        if not (ROOT / 'docs' / name).is_file():
            errors.append('Missing documentation: docs/' + name)
    documents = [ROOT / 'README.md', ROOT / 'AGENTS.md', ROOT / 'data/README.md',
                 ROOT / '_scratch/README.md', *sorted((ROOT / 'docs').rglob('*.md'))]
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    links = examples = 0
    for document in documents:
        text = document.read_text(encoding='utf8')
        label = document.relative_to(ROOT)
        for match in re.finditer(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)', text):
            target = match.group(1).strip().strip('<>')
            if re.match(r'[a-zA-Z][\w+.-]*:', target):
                continue
            file_part, _, anchor = target.partition('#')
            resolved = (document.parent / unquote(file_part)).resolve() if file_part else document
            links += 1
            if not resolved.exists():
                errors.append(f'{label}: missing link target {target}')
            elif anchor and resolved.suffix == '.md':
                if unquote(anchor) not in headings(resolved.read_text(encoding='utf8')):
                    errors.append(f'{label}: missing heading anchor {target}')
        for block in re.finditer(r'^```([^\n]*)\n(.*?)^```\s*$', text, flags=re.M | re.S):
            language, body = block.group(1).strip().lower(), block.group(2)
            if language == 'json':
                try:
                    json.loads(body)
                except json.JSONDecodeError as exc:
                    errors.append(f'{label}: invalid JSON example ({exc.msg})')
            if language == 'python':
                try:
                    ast.parse(body)
                except SyntaxError as exc:
                    errors.append(f'{label}: invalid Python example ({exc.msg})')
            if language not in {'powershell', 'bash', 'shell', 'console'}:
                continue
            for line in body.splitlines():
                # Inspect only our CLI examples. Never invoke a shell or dispatch.
                command = re.match(r'^(?:python|\.\\\.venv\\Scripts\\python(?:\.exe)?)\s+(?:run\.py|-m\s+sellerops)(?:\s+(.*))?$', line.strip())
                if not command:
                    continue
                try:
                    argv = shlex.split(command.group(1) or '', comments=True)
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        try:
                            parser.parse_args(argv)
                        except SystemExit as exc:
                            if exc.code != 0:
                                raise ValueError('arguments were rejected')
                    examples += 1
                except (ValueError, TypeError) as exc:
                    errors.append(f'{label}: invalid command example: {line.strip()} ({exc})')

    spec = importlib.util.spec_from_file_location('sellerops_cli_doc_generator', ROOT / 'scripts/generate_cli_docs.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    actual = (ROOT / 'docs/CLI.md').read_text(encoding='utf8')
    if actual != generator.render():
        errors.append('CLI.md is out of date; run python scripts/generate_cli_docs.py')
    for name in sub.choices:
        if f'\n## {name}\n' not in actual:
            errors.append('CLI.md missing command: ' + name)

    source_examples = 0
    for file in (ROOT / 'src/sellerops').rglob('*.py'):
        tree = ast.parse(file.read_text(encoding='utf8'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for line in (ast.get_docstring(node) or '').splitlines():
                command = re.match(r'^(?:python\s+)(?:run\.py|-m\s+sellerops)\s+(.*)$', line.strip())
                if command and not any(marker in command.group(1) for marker in ('...', '<', '>', 'xxx', '[', ']')):
                    try:
                        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                            try:
                                parser.parse_args(shlex.split(command.group(1), comments=True))
                            except SystemExit as exc:
                                if exc.code:
                                    raise ValueError('arguments were rejected')
                        source_examples += 1
                    except ValueError as exc:
                        errors.append(f'{file.relative_to(ROOT)}: invalid docstring command ({line.strip()}): {exc}')
                if re.match(r'^from sellerops\.[\w.]+ import ', line.strip()):
                    example = ast.parse(line.strip()).body[0]
                    target = ROOT / 'src' / Path(*example.module.split('.'))
                    module_file = target.with_suffix('.py')
                    if not module_file.exists():
                        module_file = target / '__init__.py'
                    if not module_file.exists():
                        errors.append(f'{file.relative_to(ROOT)}: docstring imports missing module {example.module}')
                        continue
                    symbols = set()
                    for item in ast.parse(module_file.read_text(encoding='utf8')).body:
                        if isinstance(item, (ast.FunctionDef, ast.ClassDef)):
                            symbols.add(item.name)
                        elif isinstance(item, (ast.Import, ast.ImportFrom)):
                            symbols.update(alias.asname or alias.name for alias in item.names)
                        elif isinstance(item, ast.Assign):
                            symbols.update(n.id for n in item.targets if isinstance(n, ast.Name))
                    for alias in example.names:
                        if alias.name not in symbols and not (target / (alias.name + '.py')).exists():
                            errors.append(f'{file.relative_to(ROOT)}: docstring imports missing symbol {example.module}.{alias.name}')
                    source_examples += 1

    for file in (ROOT / 'examples').glob('*.json'):
        try:
            value = json.loads(file.read_text(encoding='utf8'))
            if file.name == 'credentials.example.json':
                def check_secrets(data):
                    if isinstance(data, dict):
                        for key, item in data.items():
                            if key in {'token', 'limit_key', 'cookie_extra', 'cookie', 'authorizev3',
                                       'wb_seller_lk', 'api_key', 'api_bearer', 'www_cookie', 'aamz_cookie'}:
                                if item:
                                    errors.append(f'{file.name}: credential field {key} must be empty')
                            check_secrets(item)
                    elif isinstance(data, list):
                        for item in data:
                            check_secrets(item)
                check_secrets(value)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append(f'{file.name}: invalid JSON ({exc})')

    if errors:
        print('\n'.join(errors))
        return 1
    print(f'Documentation: {len(documents)} files, {links} local links, {examples} CLI examples, '
          f'{len(sub.choices)} command references: OK')
    print(f'Source documentation: {source_examples} concrete command/import examples: OK')
    print('Examples were parsed only; no business handlers, credentials or platform APIs were used.')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
