"""Feishu CLI transport and complete, bounded NDJSON pagination."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any


def invoke(args: list[str], payload: Any = None, timeout: float = 120) -> dict:
    executable = os.environ.get('SELLEROPS_LARK_CLI') or shutil.which('lark-cli') or shutil.which('lark-cli.cmd')
    if not executable:
        raise RuntimeError('未找到 lark-cli，请配置 PATH 或 SELLEROPS_LARK_CLI')
    with tempfile.TemporaryDirectory(prefix='sellerops-lark-') as directory:
        command = [executable, 'base', *args, '--as', 'user']
        if payload is not None:
            payload_path = Path(directory) / 'payload.json'
            payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf8')
            command += ['--json', '@' + str(payload_path)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf8',
                                errors='replace', timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(f'lark-cli 执行失败，退出码 {result.returncode}')
        try:
            document = json.loads(result.stdout or '')
        except json.JSONDecodeError as exc:
            raise RuntimeError('lark-cli 返回非 JSON 数据') from exc
        if not isinstance(document, dict):
            raise RuntimeError('lark-cli 返回数据必须是对象')
        if document.get('ok') is False:
            raise RuntimeError('lark-cli 报告请求失败')
        return document['data'] if isinstance(document.get('data'), dict) else document


def resolve_base(url: str) -> str:
    result = invoke(['+url-resolve', '--url', url])
    if result.get('resource_type') != 'bitable':
        raise RuntimeError('链接不是飞书多维表格')
    return result['base_token']


def resolve_table(base_token: str, table_name: str) -> str:
    result = invoke(['+table-list', '--base-token', base_token])
    for table in result.get('tables') or []:
        if table.get('name') == table_name:
            return table['id']
    raise RuntimeError(f'表格不存在: {table_name}')


def list_records(base_token: str, table_id: str, field_ids: list[str] | None = None,
                 max_pages: int = 10000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    with tempfile.TemporaryDirectory(prefix='sellerops-records-') as directory:
        for page in range(max_pages):
            output = Path(directory) / f'page_{page}.ndjson'
            command = ['+record-list', '--base-token', base_token, '--table-id', table_id,
                       '--limit', '2000', '--offset', str(offset), '--format', 'ndjson',
                       '--output', str(output), '--overwrite']
            for field in field_ids or []:
                command += ['--field-id', field]
            manifest = invoke(command)
            before = len(rows)
            if output.exists():
                for line in output.read_text(encoding='utf-8-sig').splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            if not manifest.get('has_more'):
                return rows
            try:
                next_offset = int(manifest['next_offset'])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError('record-list 缺少有效 next_offset，已中止以避免漏读') from exc
            if next_offset <= offset or len(rows) == before:
                raise RuntimeError('record-list 分页没有前进，已中止以避免重复或漏读')
            offset = next_offset
            time.sleep(0.3)
    raise RuntimeError(f'record-list 超出分页上限 {max_pages}')
