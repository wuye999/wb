"""按店从本地快照反查 nmId 和完整 VC。"""
from typing import Any

from sellerops.domain.snapshot_values import positive_id
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository


MISS_LABEL = '本地真源未收录'


class NmResolver:
    def __init__(self, shop_id: Any, with_cn: bool = True):
        self.nm2vc: dict[int, str] = {}
        self._cn = lambda vc: ''
        try:
            rows = ProductSnapshotRepository.load_shop_rows(shop_id) or {}
            for vc, item in rows.items():
                nm_id = positive_id(item.get('nmId') or item.get('nmID'))
                if nm_id:
                    self.nm2vc[nm_id] = vc
            if with_cn:
                resolve_cn, _ = MappingRepository.build_vc_resolver()
                self._cn = resolve_cn
        except Exception as error:
            print(f'  [警告] 本店商品资料读取失败（{type(error).__name__}）：{error}')

    def resolve(self, nm_id: Any) -> dict[str, str]:
        vc = self.nm2vc.get(positive_id(nm_id), '')
        return {'vc': vc, 'cn': self._cn(vc) if vc else '', 'src': '店快照' if vc else ''}
