"""Format-neutral, serializable document structure and exact text-view locations."""

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal

from markdown_it import MarkdownIt


def digest(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Location:
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class Asset:
    asset_id: str
    kind: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    uri: str | None = None


@dataclass(frozen=True)
class ContentBlock:
    block_id: str
    kind: str
    line_start: int
    line_end: int
    locations: tuple[Location, ...] = ()
    asset_ids: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()
    method: str = 'markdown'
    header_end: int | None = None
    table_data: dict | None = None
    context_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Document:
    format: Literal['markdown', 'pdf']
    raw_hash: str
    parser: str
    text: str
    blocks: tuple[ContentBlock, ...]
    assets: tuple[Asset, ...] = ()
    page_count: int | None = None
    issues: tuple[dict, ...] = ()
    raw_file: str | None = None
    schema: int = 1
    attempts: tuple[dict, ...] = ()

    @property
    def parse_id(self):
        import json
        return digest(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False))

    @property
    def status(self):
        return 'partial' if self.issues else 'ready'

    def to_dict(self):
        value = asdict(self)
        # Absent optional extensions must not change the identity of old snapshots.
        for block in value['blocks']:
            if not block['context_ranges']:
                block.pop('context_ranges')
        return value

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        if data.get('schema') != 1 or data.get('format') not in ('markdown', 'pdf'):
            raise ValueError('不支持的文档快照版本。')
        data['blocks'] = tuple(ContentBlock(**{**b,
            'locations': tuple(Location(**{**p, 'bbox': tuple(p['bbox']) if p.get('bbox') else None})
                               for p in b['locations']),
            'asset_ids': tuple(b['asset_ids']), 'heading_path': tuple(b['heading_path']),
            'context_ranges': tuple(tuple(r) for r in b.get('context_ranges', ()))}) for b in data['blocks'])
        data['assets'] = tuple(Asset(**{**a, 'bbox': tuple(a['bbox']) if a.get('bbox') else None}) for a in data['assets'])
        data['issues'] = tuple(data['issues'])
        data['attempts'] = tuple(data.get('attempts', ()))
        result = cls(**data)
        size = len(result.text.splitlines())
        assets = {a.asset_id for a in result.assets}
        if len(assets) != len(result.assets) or len({b.block_id for b in result.blocks}) != len(result.blocks):
            raise ValueError('文档快照存在重复身份。')
        for block in result.blocks:
            if not 1 <= block.line_start <= block.line_end <= size or not set(block.asset_ids) <= assets:
                raise ValueError('文档快照的内容位置或资源引用无效。')
            if block.header_end is not None and not block.line_start <= block.header_end <= block.line_end:
                raise ValueError('表头范围无效。')
            if any(not 1 <= start <= end <= size for start, end in block.context_ranges):
                raise ValueError('表格上下文范围无效。')
            for loc in block.locations:
                if loc.page is not None and not 1 <= loc.page <= (result.page_count or 0):
                    raise ValueError('文档页码无效。')
        return result

    def locations(self, start, end):
        return [asdict(p) for block in self.blocks
                if block.line_start <= end and block.line_end >= start for p in block.locations]


def markdown_document(text: str) -> Document:
    """Keep the original text and line numbers, including blank lines and CRLF."""
    tokens = MarkdownIt('commonmark').enable('table').parse(text)
    blocks, assets, hierarchy = [], [], []
    for i, token in enumerate(tokens):
        if token.level != 0 or token.map is None or token.nesting == -1:
            continue
        start, end = token.map[0] + 1, token.map[1]
        kind = {'heading_open': 'heading', 'table_open': 'table', 'fence': 'code',
                'code_block': 'code', 'bullet_list_open': 'list', 'ordered_list_open': 'list'}.get(token.type, 'paragraph')
        if kind == 'heading':
            level = int(token.tag[1:])
            while hierarchy and hierarchy[-1][0] >= level:
                hierarchy.pop()
            hierarchy.append((level, tokens[i + 1].content))
        refs = []
        if i + 1 < len(tokens):
            for child in tokens[i + 1].children or []:
                if child.type == 'image':
                    uri = child.attrGet('src')
                    identity = digest('linked-image:' + str(uri))
                    if identity not in {a.asset_id for a in assets}:
                        assets.append(Asset(identity, 'linked_image', uri=uri))
                    refs.append(identity)
        blocks.append(ContentBlock(digest(f'markdown:{start}:{end}:{kind}'), kind, start, end,
            (Location(line_start=start, line_end=end),), tuple(refs), tuple(h[1] for h in hierarchy),
            header_end=min(start + 1, end) if kind == 'table' else None))
    return attach_table_context(Document('markdown', digest(text), 'markdown-it-structure-v1', text, tuple(blocks), tuple(assets)))


def attach_table_context(document):
    """Link bounded, actual nearby prose; these links do not assert a unit's meaning."""
    from dataclasses import replace
    import re
    lines = document.text.splitlines()
    blocks = list(document.blocks)
    for index, block in enumerate(blocks):
        if block.kind != 'table':
            continue
        ranges = []
        page = block.locations[0].page if block.locations else None
        if index:
            previous = blocks[index - 1]
            same_page = bool(previous.locations) and previous.locations[0].page == page
            text = '\n'.join(lines[previous.line_start - 1:previous.line_end])
            if same_page and previous.kind in ('heading', 'paragraph') and block.line_start - previous.line_end <= 3 and len(text) <= 400:
                ranges.append((previous.line_start, previous.line_end))
        for following in blocks[index + 1:index + 4]:
            text = '\n'.join(lines[following.line_start - 1:following.line_end]).strip()
            if not following.locations or following.locations[0].page != page or following.line_start - block.line_end > 8:
                break
            if following.kind != 'paragraph' or len(text) > 600 or not re.match(r'^(?:注[：:]?|备注|单位|来源|Note\b|Source\b|Units?\b|\*|†)', text, re.I):
                break
            ranges.append((following.line_start, following.line_end))
        blocks[index] = replace(block, context_ranges=tuple(ranges))
    return replace(document, blocks=tuple(blocks))


def snapshot_metadata(source):
    """Old Markdown archives remain byte-for-byte compatible; PDFs retain structure."""
    return {'document': source.document.to_dict()} if source.document and source.document.format == 'pdf' else {}


def citation_metadata(source, citation):
    if source.document is None or source.document.format != 'pdf':
        return {}
    result = {'format': 'pdf', 'locations': source.document.locations(citation.line_start, citation.line_end)}
    if citation.asset_id is not None:
        asset = next((a for a in source.document.assets if a.asset_id == citation.asset_id), None)
        if asset is not None:
            result.update(asset_id=asset.asset_id, locations=[asdict(Location(asset.page, asset.bbox))],
                          evidence_method='vlm_visual_interpretation')
    return result
