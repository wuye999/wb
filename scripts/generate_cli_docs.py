"""Generate command documentation from the parser, without credentials or network."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sellerops.cli.main import build_parser


def render() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    descriptions = {a.dest: a.help for a in sub._choices_actions}
    lines = ['# 命令参考', '', '由 `python scripts/generate_cli_docs.py` 从实际参数定义生成。', '',
             '使用方法见 [操作流程](USAGE.md)，配置见 [CONFIGURATION](CONFIGURATION.md)，AI 边界见 [AI_GUIDE](AI_GUIDE.md)。', '',
             '源码运行时将下方 usage 的 `sellerops` 换成 `python run.py`。全局 `--data-dir` 放在子命令之前。', '',
             '远端业务写入显式加 `--apply`；部分命令另需 `--yes`，具体以实际参数及操作流程为准。',
             '本地映射、凭证导入等命令会直接写本地数据。dry-run 不等于离线；写命令结束后不追加验证或同步。', '',
             '当前凡涉及仓库的操作默认且仅限各店莫斯科仓库 ID，不回退其他地区仓；全店/全部商品不扩大到全仓。`warehouse-clear` 面向成都仓，当前不使用。',
             'WB 凭证使用一店人工抓包，通过 `cookies-update` 为同一登录下的已配置店铺逐店签发。受限浏览器步骤见 [手动抓包说明](CONFIGURATION.md#手动抓包更新凭证当前方式)。', '',
             f'共 {len(sub.choices)} 个命令。处理器链接用于维护者定位实现，不会在生成文档时加载处理器。', '',
             '| 命令 | 用途 | 实现入口 |', '| --- | --- | --- |']
    for name, command in sub.choices.items():
        module, function, action, channel = command.get_default('handler')
        target = '../src/' + module.replace('.', '/') + '.py'
        details = ', '.join(str(x) for x in (action, channel) if x)
        label = module + ':' + function + (f' ({details})' if details else '')
        lines.append(f'| `{name}` | {descriptions[name].replace("|", "/")} | [{label}]({target}) |')
    for name, command in sub.choices.items():
        lines += ['', '## ' + name, '']
        if name == 'warehouse-clear':
            lines += ['当前不使用：此命令操作成都仓，不能用于仅允许莫斯科仓的日常任务。命令仍被注册，不代表代码已强制禁用。', '']
        lines += ['```text', command.format_help().strip(), '```']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    (ROOT / 'docs/CLI.md').write_text(render(), encoding='utf8')
