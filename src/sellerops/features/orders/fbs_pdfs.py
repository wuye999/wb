"""Render official WB code images at the label stock size."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory


def require_pdf_tools() -> None:
    try:
        import pypdf
        import reportlab
    except ImportError:
        raise ValueError('导出需要 PDF 依赖，请用本命令的 Python 安装 .[fbs-pdf]') from None


def _png_pages(files: list[bytes], width: float, height: float, target: Path) -> None:
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    pdf = Canvas(str(target), pagesize=(width, height))
    for data in files:
        with Image.open(BytesIO(data), formats=('PNG',)) as img:
            img.load()
            if img.height > img.width:
                img = img.transpose(Image.Transpose.ROTATE_90)
            scale = min(width / img.width, height / img.height)
            w, h = img.width * scale, img.height * scale
            pdf.drawImage(ImageReader(img), (width - w) / 2, (height - h) / 2, w, h, mask='auto')
        pdf.showPage()
    pdf.save()


def _pdf_pages(files: list[bytes], width: float, height: float, target: Path) -> None:
    from pypdf import PdfReader, PdfWriter, Transformation

    pdf = PdfWriter()
    for data in files:
        source = PdfReader(BytesIO(data))
        if len(source.pages) != 1:
            raise ValueError('WB 跨境标签应为每单一页')
        page = source.pages[0]
        page.transfer_rotation_to_content()
        if page.mediabox.height > page.mediabox.width:
            page.rotate(90)
            page.transfer_rotation_to_content()
        box = page.mediabox
        scale = min(width / float(box.width), height / float(box.height))
        offset_x = (width - float(box.width) * scale) / 2
        offset_y = (height - float(box.height) * scale) / 2
        transform = (Transformation().translate(-float(box.left), -float(box.bottom))
                     .scale(scale).translate(offset_x, offset_y))
        output_page = pdf.add_blank_page(width=width, height=height)
        output_page.merge_transformed_page(page, transform)
    pdf.write(target)


def _check(path: Path, count: int, width_mm: int, height_mm: int) -> None:
    from pypdf import PdfReader
    from reportlab.lib.units import mm

    with path.open('rb') as stream:
        pages = PdfReader(stream).pages
        if len(pages) != count or any(
            abs(float(page.mediabox.width) - width_mm * mm) > .02
            or abs(float(page.mediabox.height) - height_mm * mm) > .02
            or page.get_contents() is None for page in pages
        ):
            raise ValueError('导出 PDF 页数、尺寸或页面内容不完整')


def check_pair(qr_path: Path, labels_path: Path, count: int) -> None:
    _check(qr_path, 1, 58, 40)
    _check(labels_path, count, 40, 30)


def export_pair(qr_path: Path, labels_path: Path, qr: bytes, labels: list[bytes], *,
                cross_border: bool) -> None:
    from reportlab.lib.units import mm

    directory = qr_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.打印-', dir=directory) as tmp:
        staging = Path(tmp)
        staged_qr, staged_labels = staging / qr_path.name, staging / labels_path.name
        _png_pages([qr], 58 * mm, 40 * mm, staged_qr)
        write_labels = _pdf_pages if cross_border else _png_pages
        write_labels(labels, 40 * mm, 30 * mm, staged_labels)
        check_pair(staged_qr, staged_labels, len(labels))
        staged_qr.replace(qr_path)
        staged_labels.replace(labels_path)
