"""报告的商品名称与 VC 归属。"""
from sellerops.domain.product_reports import ReportCatalog
from sellerops.persistence.mappings import MappingRepository


def load_report_catalog() -> ReportCatalog:
    products = MappingRepository.current_products()
    entries = MappingRepository.load_vc_entries(products)
    _, names = MappingRepository.build_vc_resolver(entries=entries)
    return ReportCatalog(price_names={row['cn'] for row in products}, mapped_names=names,
                         historical_vcs={vc for vc, row in entries.items()
                                         if row['status'] == 'mapped' and not row['current_product']})
