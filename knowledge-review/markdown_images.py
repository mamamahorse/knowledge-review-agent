"""Frozen Markdown image resources and source-bound visual crops."""
import base64
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from urllib.parse import unquote,urlsplit

from document_model import Asset,markdown_document,digest

MAX_BYTES=8*1024*1024
MAX_PIXELS=16_000_000


def relative_name(value):
    value=unquote(value).replace('\\','/')
    if urlsplit(value).scheme or value.startswith('/') or ':' in value or '..' in PurePosixPath(value).parts:
        raise ValueError('图片只允许资料资源目录内的相对路径。')
    return str(PurePosixPath(value))


def checked_image(data):
    import warnings
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            return _checked_image(data)
    except Exception as exc:
        raise ValueError('图片格式、像素或解码检查未通过；仅支持受限静态图片。') from exc


def _checked_image(data):
    from PIL import Image,ImageOps
    if len(data)>MAX_BYTES: raise ValueError('单张图片不能超过8MB。')
    with Image.open(BytesIO(data)) as image:
        if image.format not in ('PNG','JPEG','WEBP') or getattr(image,'n_frames',1)!=1:
            raise ValueError('目前仅支持静态PNG、JPEG、WebP图片。')
        if image.width*image.height>MAX_PIXELS: raise ValueError('图片像素超过资源上限。')
        image.load()
        return ImageOps.exif_transpose(image).convert('RGB')


def import_images(path,text,uploads):
    """Only explicitly uploaded files; no URL fetching or disk-directory scan."""
    path=Path(path); root=path.parent.resolve()
    normalized={relative_name(name):data for name,data in uploads.items()}
    if len(normalized)>20 or sum(map(len,normalized.values()))>32*1024*1024:
        raise ValueError('图片最多20张，总计不能超过32MB。')
    for data in normalized.values(): checked_image(data)
    doc=markdown_document(text); manifest={}
    for asset in doc.assets:
        try: name=relative_name(asset.uri)
        except ValueError: continue
        if name not in normalized: continue
        data=normalized[name]; checked_image(data)
        identity=sha256(data).hexdigest(); folder=root/'.images'; folder.mkdir(exist_ok=True)
        target=folder/identity
        if not target.resolve().is_relative_to(root): raise ValueError('图片存储路径越界。')
        if not target.exists(): target.write_bytes(data)
        manifest[asset.uri]=dict(asset_id='mdimg-'+identity,uri='.images/'+identity)
    from pdf_parse_cache import write_record
    write_record(root/'image-manifest.json',manifest)


def bind_source(path,text,base_id):
    from sources import Source
    path=Path(path); record=path.parent/'image-manifest.json'
    if not record.exists(): return Source(base_id,path,text)
    manifest=json.loads(record.read_text(encoding='utf-8')); doc=markdown_document(text)
    assets=[]; mapping={}; issues=[]
    for asset in doc.assets:
        frozen=manifest.get(asset.uri)
        if frozen:
            mapping[asset.asset_id]=frozen['asset_id']
            assets.append(Asset(frozen['asset_id'],'linked_image',uri=frozen['uri']))
        else:
            assets.append(asset); issues.append(dict(code='image_not_imported',asset_id=asset.asset_id))
    blocks=tuple(replace(b,asset_ids=tuple(mapping.get(a,a) for a in b.asset_ids)) for b in doc.blocks)
    doc=replace(doc,assets=tuple({a.asset_id:a for a in assets}.values()),blocks=blocks,issues=tuple(issues))
    identity=digest(base_id+'\0'+json.dumps(manifest,sort_keys=True))
    return Source(identity,path,text,doc)


def resolve(source,identity):
    if not isinstance(identity,str) or len(identity)>2048: raise ValueError('图片身份无效。')
    box=None; original=identity
    if identity.startswith('mdcrop-'):
        try:
            payload=json.loads(base64.urlsafe_b64decode(identity[7:]+'='*(-len(identity[7:])%4)))
            version,original,box=payload
            if version!=source.source_id: raise ValueError('图片裁剪属于其他资料版本。')
        except Exception as exc: raise ValueError('图片裁剪身份无效。') from exc
    asset=next((a for a in source.document.assets if a.asset_id==original),None)
    if not asset or not asset.asset_id.startswith('mdimg-'): raise ValueError('图片尚未导入，请附带图片重新导入Markdown。')
    if box is not None:
        from pdf_regions import checked_box
        box=checked_box(box,normalized=True)
    return replace(asset,asset_id=identity,bbox=box),original


def render(source,identity,bbox=None):
    asset,original=resolve(source,identity)
    base=next(a for a in source.document.assets if a.asset_id==original)
    root=source.path.parent.resolve(); path=(root/base.uri).resolve()
    if not path.is_relative_to(root): raise ValueError('图片资源路径越界。')
    with path.open('rb') as stream: data=stream.read(MAX_BYTES+1)
    if sha256(data).hexdigest()!=original[6:]: raise ValueError('图片内容与冻结版本不一致。')
    image=checked_image(data)
    current=asset.bbox or (0,0,1,1)
    if bbox is not None:
        from pdf_regions import checked_box
        x0,y0,x1,y1=checked_box(bbox,normalized=True); a,b,c,d=current
        current=(a+x0*(c-a),b+y0*(d-b),a+x1*(c-a),b+y1*(d-b))
        payload=json.dumps([source.source_id,original,current],separators=(',',':')).encode()
        asset=replace(asset,asset_id='mdcrop-'+base64.urlsafe_b64encode(payload).decode().rstrip('='),bbox=current)
    if asset.bbox:
        a,b,c,d=current; w,h=image.size
        image=image.crop((int(a*w),int(b*h),max(int(a*w)+1,int(c*w)),max(int(b*h)+1,int(d*h))))
    from pdf_parser import MAX_RENDER_PIXELS
    if image.width*image.height>MAX_RENDER_PIXELS:
        scale=(MAX_RENDER_PIXELS/(image.width*image.height))**.5
        image.thumbnail((max(1,int(image.width*scale)),max(1,int(image.height*scale))))
    output=BytesIO(); image.save(output,format='PNG')
    return output.getvalue(),asset,original


def inspect(source,identity,question,*,bbox=None,include_page=False):
    if include_page: raise ValueError('Markdown图片没有PDF整页；请用read_source补读图题或段落。')
    from pdf_vision import vision_request,VisualReading
    image,asset,original=render(source,identity,bbox)
    block=next(b for b in source.document.blocks if original in b.asset_ids)
    reading=vision_request([image],'Read this Markdown image as evidence. Preserve labels, units and conditions; '
        'report unreadable details and avoid inferring a complete table from a crop. Question: '+question,VisualReading)
    viewed=dict(source_id=source.source_id,asset_id=asset.asset_id,resource_id=original,page=None,bbox=asset.bbox,
                line_start=block.line_start,line_end=block.line_end)
    suggestions=[]
    for region in reading.suggested_regions:
        _,crop,_=render(source,asset.asset_id,region.bbox)
        suggestions.append(dict(asset_id=crop.asset_id,label=region.label,reason=region.reason,viewed=False))
    return dict(**viewed,observation=reading.observation,limitations=reading.limitations,viewed_assets=[viewed],
                formulas=[f.model_dump() for f in reading.formulas],
                suggested_regions=suggestions,next_read=reading.next_read,method='vlm_visual_interpretation'),block
