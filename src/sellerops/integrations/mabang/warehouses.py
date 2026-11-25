"""分页读取马帮管理页的启用仓库目录。"""
import time

from sellerops.integrations.mabang.client import REQUEST_INTERVAL, parse_json, post_warehouse_list


def fetch_enabled_warehouses(cred: dict, *, max_pages: int = 100) -> list[dict]:
    enabled, seen = [], set()
    expected = None
    invalid_rows = False
    row_count = 0
    for page in range(1, max_pages + 1):
        response = post_warehouse_list(cred, {
            'orderBy': 'asc', 'status': 1, 'pageIndex': page, 'pageSize': 100,
        })
        response.raise_for_status()
        result = parse_json(response)
        data = result.get('data')
        if result.get('code') != 200 or result.get('success') is not True or not isinstance(data, dict):
            raise RuntimeError('马帮仓库列表返回失败')
        current, size, total, pages = (data.get(k) for k in ('current', 'size', 'total', 'pages'))
        rows = data.get('records')
        if (any(type(v) is not int for v in (current, size, total, pages))
                or current != page or not 1 <= size <= 100 or total < 0 or pages > max_pages
                or pages not in ((0, 1) if total == 0 else ((total + size - 1) // size,))
                or not isinstance(rows, list)
                or len(rows) != min(size, max(0, total - (page - 1) * size))):
            raise RuntimeError('马帮仓库分页无效或超限')
        pagination = (size, total, pages)
        if expected is not None and expected != pagination:
            raise RuntimeError('马帮仓库分页总数或大小发生变化')
        expected = pagination
        row_count += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows = True
                continue
            warehouse_id, name, status = (row.get(k) for k in ('id', 'name', 'status'))
            if (type(warehouse_id) is not int or warehouse_id < 1 or warehouse_id in seen
                    or not isinstance(name, str) or not name.strip()
                    or type(status) is not int or status not in (1, 2)):
                invalid_rows = True
                continue
            seen.add(warehouse_id)
            if status == 1:
                enabled.append({'id': warehouse_id, 'name': name.strip()})
        if page >= pages:
            if row_count != total:
                raise RuntimeError('马帮仓库列表总数不符')
            if invalid_rows:
                names = '、'.join(row['name'] for row in enabled) or '无'
                raise ValueError(f'马帮仓库列表含无效或重复记录。候选启用仓库（已核实部分）：{names}。')
            return enabled
        time.sleep(REQUEST_INTERVAL)
    raise RuntimeError('马帮仓库列表超出分页上限')
