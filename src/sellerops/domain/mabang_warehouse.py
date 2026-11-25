"""马帮库存仓配置及启用仓唯一匹配规则。"""
from dataclasses import dataclass

from sellerops.domain.snapshot_values import positive_id


@dataclass(frozen=True)
class WarehouseConfig:
    keyword: str
    warehouse_id: int | None
    issues: tuple[tuple[str, str], ...]

    @property
    def mode(self) -> str:
        if self.issues:
            return 'invalid'
        if self.keyword:
            return 'keyword_and_id' if self.warehouse_id else 'keyword'
        return 'id' if self.warehouse_id else 'missing'


def warehouse_config(settings: dict) -> WarehouseConfig:
    keyword = settings.get('warehouse_keyword', '')
    value = settings.get('warehouse_id')
    issues = []
    if not isinstance(keyword, str):
        issues.append(('warehouse_keyword', 'expected_string'))
        keyword = ''
    try:
        warehouse_id = positive_id(value)
    except ValueError:
        warehouse_id = None
    if value is not None and warehouse_id is None:
        issues.append(('warehouse_id', 'invalid_warehouse_id'))
    return WarehouseConfig(keyword.strip(), warehouse_id, tuple(issues))


def select_enabled_warehouse(rows: list[dict], config: WarehouseConfig) -> dict:
    names = '、'.join(row['name'] for row in rows) or '无'
    candidates = [row for row in rows if config.keyword and config.keyword in row['name']]
    if config.issues:
        reason = '、'.join(f'mabang.{key}' for key, _ in config.issues) + ' 配置格式无效'
    elif not config.keyword:
        reason = '缺少 mabang.warehouse_keyword 或已核实的 mabang.warehouse_id'
    elif len(candidates) != 1:
        reason = f'mabang.warehouse_keyword 命中 {len(candidates)} 个启用仓库'
    elif config.warehouse_id is not None and candidates[0]['id'] != config.warehouse_id:
        reason = 'mabang.warehouse_keyword 命中的仓库与 mabang.warehouse_id 冲突'
    else:
        return candidates[0]
    raise ValueError(f'{reason}。候选启用仓库：{names}。请核对马帮库存仓配置。')
