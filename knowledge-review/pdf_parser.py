"""Local PDF conversion, immutable parse cache, quality flags and bounded rendering."""

from dataclasses import replace
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv

from document_model import Asset, ContentBlock, Document, Location, digest, attach_table_context

PARSER_VERSION = 'pdf-structure-v2-table-context'
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200
MAX_RENDER_PIXELS = 4_000_000


def pdf_library():
    try:
        import pymupdf
    except ImportError as exc:
        raise ValueError('PDF 支持需要安装 requirements-pdf.txt 中的依赖。') from exc
    return pymupdf


def profile():
    load_dotenv(Path(__file__).resolve().parent / '.env')
    engine = os.getenv('PDF_PARSER', 'pymupdf').strip().lower()
    if engine not in ('pymupdf', 'docling'):
        raise ValueError('PDF_PARSER 必须为 pymupdf 或 docling。')
    value = {'algorithm': PARSER_VERSION, 'engine': engine,
             'pymupdf': pdf_library().VersionBind, 'ocr': engine == 'docling' and os.getenv('PDF_DO_OCR', '0') == '1'}
    if engine == 'docling':
        try:
            value['docling'] = importlib.metadata.version('docling')
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError('Docling 尚未安装，请先安装 requirements-docling.txt 并准备本地模型。') from exc
        value['artifacts'] = os.getenv('DOCLING_ARTIFACTS_PATH', '')
        if not Path(value['artifacts']).is_dir() or not value['artifacts']:
            raise ValueError('请设置已准备好的 DOCLING_ARTIFACTS_PATH；解析不会自动下载模型。')
    value['vlm'] = os.getenv('PDF_VLM_ENABLED', '0') == '1'
    if value['vlm']:
        from pdf_vision import vision_settings
        model, options = vision_settings(required=False)
        value['vlm_model'] = model
        value['vlm_endpoint'] = options.get('base_url', 'https://api.openai.com/v1')
        value['vlm_protocol'] = 'page-transcription-v2-chat-default'
    return value


def load_pdf(path: Path, *, force=False, use_vlm=None) -> Document:
    data = path.read_bytes()
    if not data or len(data) > MAX_PDF_BYTES:
        raise ValueError('PDF 不能为空，且不能超过 20 MB。')
    raw_hash = sha256(data).hexdigest()
    config = profile()
    if use_vlm is not None:
        config['vlm'] = use_vlm
    parser = json.dumps(config, sort_keys=True)
    cache = path.parent / '.parsed' / (digest(raw_hash + parser) + '.json')
    if cache.is_file() and not force:
        try:
            result = Document.from_dict(json.loads(cache.read_text(encoding='utf-8')))
            requested = json.loads(result.parser)
            requested.pop('effective_engine', None)
            if result.raw_hash == raw_hash and requested == config and result.raw_file == path.name:
                return result
        except (ValueError, KeyError, TypeError):
            pass
    result = parse_pdf(data, raw_file=path.name, config=config)
    cache.parent.mkdir(parents=True, exist_ok=True)
    pending = cache.with_name(uuid4().hex + '.tmp')
    try:
        pending.write_text(json.dumps(result.to_dict(), ensure_ascii=False), encoding='utf-8')
        pending.replace(cache)
    finally:
        pending.unlink(missing_ok=True)
    return result


def table_text(rows):
    def cell(value):
        return str('' if value is None else value).replace('|', '\\|').replace('\r', '').replace('\n', '<br>')
    if not rows:
        return ''
    width = max(len(row) for row in rows)
    lines = ['| ' + ' | '.join(cell(row[i]) if i < len(row) else '' for i in range(width)) + ' |' for row in rows]
    return '\n'.join([lines[0], '| ' + ' | '.join('---' for _ in range(width)) + ' |', *lines[1:]])


def parse_pdf(data: bytes, *, raw_file: str, config=None) -> Document:
    if not data or len(data) > MAX_PDF_BYTES:
        raise ValueError('PDF 不能为空，且不能超过 20 MB。')
    config = profile() if config is None else config
    pdf = pdf_library()
    raw_hash = sha256(data).hexdigest()
    try:
        handle = pdf.open(stream=data, filetype='pdf')
    except Exception as exc:
        raise ValueError('PDF 损坏或无法打开。') from exc
    with handle:
        if handle.needs_pass or not 1 <= len(handle) <= MAX_PAGES:
            raise ValueError(f'PDF 需要密码、没有页面或超过 {MAX_PAGES} 页。')
        pages, issues, attempts = [], [], []
        for page in handle:
            items, table_boxes = [], []
            try:
                for table in page.find_tables().tables:
                    rows = table.extract()
                    if rows and len(rows) >= 2:
                        bbox = tuple(table.bbox)
                        table_boxes.append(pdf.Rect(bbox))
                        # External headers are real page text, not an invented first row.
                        if table.header.external and any(table.header.names):
                            rows.insert(0, table.header.names)
                        if any(value is None for row in rows for value in row):
                            issues.append({'page': page.number + 1, 'code': 'table_structure_needs_review'})
                        items.append({'kind': 'table', 'text': table_text(rows), 'bbox': bbox,
                                      'table_data': {'rows': rows, 'cells': table.cells,
                                                     'header_names': table.header.names,
                                                     'header_external': table.header.external}})
            except Exception:
                issues.append({'page': page.number + 1, 'code': 'table_detection_failed'})
            for block in page.get_text('dict', sort=True)['blocks']:
                if block['type'] != 0:
                    continue
                rect = pdf.Rect(block['bbox'])
                if any((rect & box).get_area() / max(rect.get_area(), 1) > .65 for box in table_boxes):
                    continue
                text = '\n'.join(''.join(span['text'] for span in line['spans']) for line in block['lines']).strip()
                if text:
                    items.append({'kind': 'paragraph', 'text': text, 'bbox': tuple(rect)})
            regions = [pdf.Rect(info['bbox']) for info in page.get_image_info()]
            try:
                regions += [pdf.Rect(box) for box in page.cluster_drawings() if pdf.Rect(box).width >= 60 and pdf.Rect(box).height >= 40]
            except Exception:
                pass  # The full page asset always retains vector graphics.
            figures = []
            for rect in regions:
                if rect.is_empty or any((rect & box).get_area() / max(rect.get_area(), 1) > .65 for box in table_boxes + figures):
                    continue
                figures.append(rect)
                items.append({'kind': 'figure', 'text': '', 'bbox': tuple(rect)})
            text = page.get_text()
            if '\ufffd' in text or text.count('\x00'):
                issues.append({'page': page.number + 1, 'code': 'garbled_text'})
            if len(text.strip()) < 20 and figures:
                issues.append({'page': page.number + 1, 'code': 'visual_content_needs_review'})
            paragraphs = [i for i in items if i['kind'] == 'paragraph']
            if any(a['bbox'][2] <= b['bbox'][0] and min(a['bbox'][3], b['bbox'][3]) - max(a['bbox'][1], b['bbox'][1]) > 25
                   for a in paragraphs for b in paragraphs):
                issues.append({'page': page.number + 1, 'code': 'reading_order_needs_review'})
            pages.append(sorted(items, key=lambda i: (i['bbox'][1], i['bbox'][0])))

        if config['engine'] == 'docling':
            try:
                pages = docling_pages(data, config, len(handle))
                issues = [i for i in issues if i['code'] not in ('reading_order_needs_review', 'table_detection_failed')]
            except Exception as exc:
                # Keep the already extracted candidate, but never label it Docling success.
                config = {**config, 'effective_engine': 'pymupdf'}
                for number in range(1, len(handle) + 1):
                    issues.append({'page': number, 'code': 'docling_failed', 'error_type': type(exc).__name__})
        if config.get('vlm'):
            from pdf_vision import transcribe_page, VisionUnsupported
            eligible = sorted({i['page'] for i in issues})
            unsupported = False
            attempted_pages = set()
            for number in eligible[:3]:
                attempted_pages.add(number)
                original_items = pages[number - 1]
                try:
                    image = render_page(handle[number - 1])
                    result = transcribe_page(image)
                    attempts.append({'page': number, 'method': 'vlm_transcription', 'status': 'returned',
                                     'original_items': original_items, 'candidate': result.model_dump()})
                    if result.blocks and any(b.text.strip() for b in result.blocks):
                        # Keep visual region references, replace only this page's text candidate.
                        figures = [i for i in pages[number - 1] if i['kind'] == 'figure']
                        pages[number - 1] = [{'kind': b.kind, 'text': b.text, 'bbox': tuple(handle[number - 1].rect),
                                              'method': 'vlm_transcription'} for b in result.blocks if b.text.strip()] + figures
                        issues = [i for i in issues if i['page'] != number]
                        if result.unresolved:
                            issues.append({'page': number, 'code': 'vlm_unresolved', 'details': result.unresolved})
                    else:
                        issues.append({'page': number, 'code': 'vlm_no_reliable_text'})
                except VisionUnsupported:
                    issues.append({'page': number, 'code': 'vlm_unsupported'})
                    attempts.append({'page': number, 'method': 'vlm_transcription', 'status': 'unsupported'})
                    unsupported = True
                    break
                except Exception as exc:
                    issues.append({'page': number, 'code': 'vlm_failed', 'error_type': type(exc).__name__})
                    attempts.append({'page': number, 'method': 'vlm_transcription', 'status': 'failed',
                                     'error_type': type(exc).__name__})
            for number in eligible:
                if number not in attempted_pages:
                    issues.append({'page': number, 'code': 'vlm_unsupported' if unsupported else 'vlm_page_budget_exhausted'})

        lines, blocks, assets = [], [], [Asset('raw-' + raw_hash, 'pdf', uri=raw_file)]
        for number, items in enumerate(pages, 1):
            lines.extend([f'## 第 {number} 页', ''])
            page_asset = f'page-{number}'
            assets.append(Asset(page_asset, 'page', number))
            # A page navigation block enables page viewing, but is never indexed as original prose.
            blocks.append(ContentBlock(f'page-{number}', 'page', len(lines) - 1, len(lines) - 1,
                                       (Location(number),), (page_asset,), (f'第 {number} 页',), 'navigation'))
            for index, item in enumerate(items):
                kind, bbox = item['kind'], tuple(float(x) for x in item['bbox'])
                refs = ()
                if kind in ('table', 'figure'):
                    identity = f'{kind}-{number}-{index}'
                    assets.append(Asset(identity, kind, number, bbox))
                    refs = (identity,)
                text = item['text'] if kind != 'figure' else f'图像区域（第 {number} 页；可查看原图）'
                start = len(lines) + 1
                lines.extend(text.splitlines())
                end = len(lines)
                if end < start:
                    continue
                positions = tuple(Location(p['page'], tuple(p['bbox'])) for p in item['locations']) if item.get('locations') else (Location(number, bbox),)
                blocks.append(ContentBlock(digest(f'{raw_hash}:{number}:{index}'), kind, start, end,
                    positions, refs, (f'第 {number} 页',), item.get('method', config.get('effective_engine', config['engine'])),
                    min(start + 1, end) if kind == 'table' else None, item.get('table_data')))
                lines.append('')
            if not items:
                lines.extend(['本页没有提取到可检索正文；可查看原始页面。', ''])
        result = Document('pdf', raw_hash, json.dumps(config, sort_keys=True), '\n'.join(lines), tuple(blocks),
                          tuple(assets), len(handle), tuple(issues), raw_file, attempts=tuple(attempts))
        return Document.from_dict(attach_table_context(result).to_dict())


def docling_pages(data, config, count):
    from io import BytesIO
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    options = PdfPipelineOptions(artifacts_path=Path(config['artifacts']), do_ocr=config['ocr'])
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(DocumentStream(name='document.pdf', stream=BytesIO(data)), max_num_pages=MAX_PAGES)
    if str(result.status).lower().split('.')[-1] != 'success':
        raise ValueError('Docling 未完整完成解析；未发布不完整快照。')
    pages = [[] for _ in range(count)]
    for item, _ in result.document.iterate_items():
        if not getattr(item, 'prov', None):
            continue
        label = str(item.label).lower().split('.')[-1]
        for prov in item.prov[:1]:
            number = prov.page_no
            box = prov.bbox.to_top_left_origin(result.document.pages[number].size.height)
            bbox = (box.l, box.t, box.r, box.b)
            if label == 'table':
                text, kind = item.export_to_markdown(doc=result.document), 'table'
            elif label == 'picture':
                text, kind = '', 'figure'
            else:
                text = getattr(item, 'text', '')
                kind = {'section_header': 'heading', 'title': 'heading', 'formula': 'formula',
                        'code': 'code', 'list_item': 'list'}.get(label, 'paragraph')
            if text or kind == 'figure':
                locations = []
                for position in item.prov:
                    locbox = position.bbox.to_top_left_origin(result.document.pages[position.page_no].size.height)
                    locations.append({'page': position.page_no, 'bbox': (locbox.l, locbox.t, locbox.r, locbox.b)})
                pages[number - 1].append({'text': text, 'kind': kind, 'bbox': bbox, 'locations': locations,
                    'table_data': item.data.model_dump(mode='json') if label == 'table' else None})
    return pages


def raw_path(source):
    document = source.document
    if document is None or document.format != 'pdf' or not document.raw_file:
        raise ValueError('当前快照没有 PDF 原件。')
    root = source.path.parent.resolve()
    path = (root / document.raw_file).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != '.pdf':
        raise ValueError('PDF 资源路径越界。')
    if sha256(path.read_bytes()).hexdigest() != document.raw_hash:
        raise ValueError('PDF 原件与当前解析快照不一致。')
    return path


def render_page(page, bbox=None):
    pdf = pdf_library()
    # Asset boxes are unrotated PDF coordinates; transform to rendered page space.
    clip = pdf.Rect(bbox) * page.rotation_matrix if bbox else page.rect
    clip &= page.rect
    if clip.is_empty or clip.get_area() <= 0:
        raise ValueError('页面区域无效。')
    scale = min(2.0, math.sqrt(MAX_RENDER_PIXELS / clip.get_area()))
    return page.get_pixmap(matrix=pdf.Matrix(scale, scale), clip=clip, alpha=False).tobytes('png')


def render_asset(source, asset_id):
    asset = next((a for a in source.document.assets if a.asset_id == asset_id), None) if source.document else None
    if asset is None or asset.page is None:
        raise ValueError('找不到可查看的页面或区域。')
    with pdf_library().open(raw_path(source)) as handle:
        return render_page(handle[asset.page - 1], asset.bbox)
