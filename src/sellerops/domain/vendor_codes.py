"""Vendor identifiers used in existing marketplace records."""
import re

VC_PREFIX_RE = r"^BCS-([A-Z]{4})-(?:(?:ozon-card-)?\d+|[^/]+/\d+)(?:-\d+)*$"

def extract_vc_prefix(vc):
    """从现有 vendorCode 提取 4 位前缀码。
    支持：
      BCS-QQNN-1078999444 -> QQNN
      BCS-QQNN-ozon-card-1078999444 -> QQNN
      BCS-QQNN-WRLINWI/1078999444 -> QQNN
    未提取到返回 None
    """
    s = str(vc or "").strip()
    m = re.match(VC_PREFIX_RE, s)
    if m:
        return m.group(1).upper()
    m = re.match(r"^BCS-([A-Za-z]{4})(?:-|$|/)", s)
    if m:
        return m.group(1).upper()
    return None
