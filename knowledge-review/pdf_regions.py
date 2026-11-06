"""Version-bound PDF region locators; locators alone never grant citation rights."""

import base64
from hashlib import sha256
import json
import math

from document_model import Asset

PREFIX = 'crop1-'


def checked_box(box, *, normalized=False):
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in box):
        raise ValueError('区域必须是四个有限数值 [x0, y0, x1, y1]。')
    x0, y0, x1, y1 = map(float, box)
    if not x0 < x1 or not y0 < y1:
        raise ValueError('区域的右下角必须位于左上角之后。')
    if normalized and not all(0 <= v <= 1 for v in box):
        raise ValueError('裁剪坐标相对于目标图像，必须位于 0 到 1 之间。')
    return (x0, y0, x1, y1)


def region_asset(source, page, box):
    if type(page) is not int or not 1 <= page <= (source.document.page_count or 0):
        raise ValueError('区域页码不属于当前 PDF。')
    box = checked_box(tuple(round(v, 6) for v in checked_box(box)))
    payload = json.dumps([page, box], separators=(',', ':'), allow_nan=False).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    version = sha256(source.source_id.encode()).hexdigest()[:24]
    return Asset(f'{PREFIX}{version}-{encoded}', 'region', page, box)


def resolve_asset(source, identity):
    document = source.document
    if document is None or document.format != 'pdf' or not isinstance(identity, str):
        raise ValueError('这份资料没有可查看的 PDF 页面。')
    asset = document.asset(identity) if hasattr(document,'asset') else next((a for a in document.assets if a.asset_id == identity and a.page), None)
    if asset:
        return asset
    if not identity.startswith(PREFIX) or len(identity) > 400:
        raise ValueError('视觉资源不属于当前资料版本。')
    try:
        version, encoded = identity[len(PREFIX):].split('-', 1)
        payload = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        page, box = json.loads(payload)
        asset = region_asset(source, page, box)
        if asset.asset_id != identity:
            raise ValueError('区域身份或资料版本不匹配。')
        return asset
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('区域身份无效或属于其他资料版本。') from exc


def page_anchor(source, page):
    document=source.document
    blocks=document.read_blocks(*document.page(page)) if hasattr(document,'page') else document.blocks
    block = next((b for b in blocks
                  if b.kind == 'page' and f'page-{page}' in b.asset_ids), None)
    if block is None:
        raise ValueError('视觉资源没有可追溯的页面位置。')
    return block


def displayed_rect(page, asset):
    """Map unrotated PDF positions into the actual rendered page orientation."""
    from pdf_parser import pdf_library
    pdf = pdf_library()
    rect = pdf.Rect(asset.bbox) * page.rotation_matrix if asset.bbox else page.rect
    clipped = rect & page.rect
    if clipped.is_empty or clipped.get_area() <= 0:
        raise ValueError('区域不在页面内。')
    if asset.kind == 'region' and any(abs(a - b) > .00001 for a, b in zip(rect, clipped)):
        raise ValueError('区域超出页面边界。')
    return clipped


def select_region(source, identity, bbox=None):
    """bbox is relative to the selected image, not guessed original PDF coordinates."""
    from pdf_parser import pdf_library, raw_path
    asset = resolve_asset(source, identity)
    if bbox is None:
        return asset
    x0, y0, x1, y1 = checked_box(bbox, normalized=True)
    pdf = pdf_library()
    with pdf.open(raw_path(source)) as handle:
        page = handle[asset.page - 1]
        rect = displayed_rect(page, asset)
        crop = pdf.Rect(rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                        rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height)
        return region_asset(source, asset.page, tuple(crop * page.derotation_matrix))


def navigation(source, asset):
    """Only positions and identities: listing an object is not reading its contents."""
    doc=source.document
    assets=doc.read_assets(asset.page) if hasattr(doc,'read_assets') else doc.assets
    regions = [dict(asset_id=a.asset_id, kind=a.kind, page=a.page, bbox=a.bbox)
               for a in assets if a.page == asset.page and a.bbox]
    return dict(page=asset.page, page_count=source.document.page_count,
                page_asset_id=f'page-{asset.page}',
                previous_page=f'page-{asset.page-1}' if asset.page > 1 else None,
                next_page=f'page-{asset.page+1}' if asset.page < source.document.page_count else None,
                known_regions=regions,
                coordinate_notice='Known bbox values are unrotated PDF positions for navigation only. '
                    'Use asset_id to read them. Tool bbox inputs are instead normalized to the selected image.',
                notice='Navigation is not evidence. No neighboring page or listed region is marked as viewed.')
