"""Generate command documentation from the parser, without credentials or network."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sellerops.cli.main import build_parser, leaf_commands


def render() -> str:
    """按参数元数据生成参考，输出与终端宽度、argparse 换行版本无关。"""
    lines = ["# 命令参考", "", "由实际命令定义生成。使用方法见 [操作流程](USAGE.md)。",
             "全局参数 `--data-dir` 放在命令组之前。业务提交使用 `--apply`。", ""]
    parser = build_parser()
    for name, command in leaf_commands(parser):
        module, function = command.get_default("handler")
        parent = parser
        for word in name.split():
            subcommands = next(a for a in parent._actions if isinstance(a, argparse._SubParsersAction))
            summary = next((a.help for a in subcommands._choices_actions if a.dest == word), "")
            parent = subcommands.choices[word]
        lines += ["## " + name, "", summary.replace("%%", "%"), "", f"实现入口 [{function}](../src/{module.replace('.', '/')}.py)", ""]
        for group in command._mutually_exclusive_groups:
            names = " / ".join(a.option_strings[0] for a in group._group_actions)
            lines.append(("- 必选其一 " if group.required else "- 最多选一项 ") + names)
        for action in command._actions:
            if action.dest == "help":
                continue
            label = ", ".join(action.option_strings) or action.dest
            choices = "（" + " / ".join(map(str, action.choices)) + "）" if action.choices else ""
            description = (action.help or "").replace("%%", "%")
            lines.append(f"- `{label}` {description}{choices}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == '__main__':
    (ROOT / 'docs/CLI.md').write_text(render(), encoding='utf8')
