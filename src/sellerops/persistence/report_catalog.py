"""报告的商品名称与 VC 归属。"""
from sellerops.domain.product_reports import ReportCatalog
from sellerops.persistence.mappings import MappingRepository


def load_mapped_names() -> dict[str, str]:
    return MappingRepository.build_vc_resolver()[1]


def load_report_catalog() -> ReportCatalog:
    products = MappingRepository.load_price_book(require_price=False)
    return ReportCatalog(price_names={row['cn'] for row in products}, mapped_names=load_mapped_names())
