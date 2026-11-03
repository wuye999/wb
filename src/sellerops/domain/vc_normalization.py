"""历史 VC 的明确归属与跨店编码分配；不从旧前缀推断商品。"""
from dataclasses import dataclass, field
import re

MAX_VC_LENGTH = 72


@dataclass
class NormalizationCatalog:
    prefixes: dict[str, list[str]] = field(default_factory=dict)
    owners: dict[str, set[str]] = field(default_factory=dict)
    overrides: dict[str, set[str]] = field(default_factory=dict)
    excluded: set[str] = field(default_factory=set)

    def names(self, vc: str) -> set[str]:
        key = vc.upper()
        return self.overrides.get(key) or self.owners.get(key, set())

    def resolve(self, vc: str) -> tuple[str, str]:
        names = self.names(vc)
        if len(names) != 1:
            raise ValueError('缺少明确商品归属' if not names else '已有商品归属存在分歧')
        cn = next(iter(names))
        values = self.prefixes.get(cn, [])
        if len(values) != 1:
            raise ValueError('商品价格表未找到对应商品' if not values else '商品价格表中文名重复')
        prefix = values[0]
        if not re.fullmatch(r'[A-Z]{4}', prefix):
            raise ValueError('价格表前缀必须为四位英文字母')
        if any(name != cn and prefix in prefixes for name, prefixes in self.prefixes.items()):
            raise ValueError('价格表前缀被多个商品共用')
        return cn, prefix


@dataclass(frozen=True)
class RenameAttempt:
    shop_id: int
    nm_id: int
    old_vc: str
    new_vc: str


def replacement_base(vc: str, prefix: str) -> str:
    """只替换 BCS- 后的四位字母，完整保留后续字符。"""
    if not re.fullmatch(r'[A-Z]{4}', prefix):
        raise ValueError('目标前缀必须为四位大写英文字母')
    if not re.match(r'^BCS-[A-Za-z]{4}(?=-|/|$)', vc):
        raise ValueError('旧 VC 不含可安全替换的 BCS 四位前缀')
    result = vc[:4] + prefix + vc[8:]
    if len(result) > MAX_VC_LENGTH:
        raise ValueError('新 VC 超过 72 字符，不截断原编码')
    return result


def allocate_codes(bases: dict[str, str], occupied: set[str],
                   history: list[RenameAttempt]) -> tuple[dict[str, str], dict[str, str]]:
    """保留原尾部，冲突时追加最小序号；历史提交固定分配且跨店一致。"""
    used = {vc.upper() for vc in occupied}
    prior: dict[str, set[str]] = {}
    reserved_by: dict[str, set[str]] = {}
    for attempt in history:
        prior.setdefault(attempt.old_vc, set()).add(attempt.new_vc)
        reserved_by.setdefault(attempt.new_vc.upper(), set()).add(attempt.old_vc)
        used.add(attempt.new_vc.upper())
    result, errors = {}, {}
    for vc, base in sorted(bases.items()):
        if len(base) > MAX_VC_LENGTH:
            errors[vc] = '新 VC 超过 72 字符，不截断原编码'
            continue
        previous = prior.get(vc, set())
        if previous:
            if len(previous) != 1:
                errors[vc] = '历史提交对同一旧 VC 记录了不同新码，需人工处理'
                continue
            target = next(iter(previous))
            if len(reserved_by[target.upper()]) != 1:
                errors[vc] = '历史提交将不同旧 VC 分配到同一新码，需人工处理'
                continue
            if (len(target) > MAX_VC_LENGTH
                    or (target != base and not re.fullmatch(re.escape(base) + r'-[1-9][0-9]*', target))):
                errors[vc] = '历史提交的新码与当前价格表或原编码不一致，需人工处理'
                continue
        else:
            target, suffix = base, 0
            while target.upper() in used:
                suffix += 1
                target = f'{base}-{suffix}'
                if len(target) > MAX_VC_LENGTH:
                    errors[vc] = '追加避重序号后新 VC 超过 72 字符'
                    break
            if vc in errors:
                continue
        used.add(target.upper())
        result[vc] = target
    return result, errors
