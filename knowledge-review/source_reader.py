"""Immutable on-disk source snapshots; range reads retain original global lines.

Legacy Source remains unchanged. StoredSource deliberately has no .text property:
callers must request a range instead of accidentally materializing the book.
"""

from contextlib import closing
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from document_model import Asset, ContentBlock, Location


def file_digest(path):
    value=sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):
            value.update(part)
    return value.hexdigest()


def line_count(source):
    return source.line_count if isinstance(source, StoredSource) else len(source.text.splitlines())


def read_lines(source, start, end):
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= line_count(source):
        raise ValueError('原文读取范围无效。')
    if isinstance(source, StoredSource):
        with closing(sqlite3.connect(source.archive.as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT number,text,digest FROM lines WHERE number BETWEEN ? AND ? ORDER BY number',
                            (start,end)).fetchall()
        if len(rows)!=end-start+1 or any(sha256(text.encode()).hexdigest()!=digest for _,text,digest in rows):
            raise ValueError('原文分片缺失或损坏。')
        return '\n'.join(text for _,text,_ in rows)
    return '\n'.join(source.text.splitlines()[start-1:end])


def snapshot(source):
    if isinstance(source, StoredSource):
        return dict(source_id=source.source_id,path=str(source.path),reader=dict(
            version=source.version,archive=str(source.archive),line_count=source.line_count,file_hash=source.file_hash))
    return dict(source_id=source.source_id,path=str(source.path),text=source.text,
                **({'document':source.document.to_dict()} if source.document else {}))


@dataclass(frozen=True)
class DocumentMetadata:
    format: str
    raw_hash: str
    parser: str
    blocks: tuple
    assets: tuple
    page_count: int | None
    issues: tuple
    raw_file: str | None

    @property
    def status(self):
        return 'partial' if self.issues else 'ready'

    def locations(self,start,end):
        return [asdict(p) for b in self.blocks if b.line_start<=end and b.line_end>=start for p in b.locations]


@dataclass(frozen=True)
class StoredSource:
    source_id: str
    path: Path
    archive: Path
    line_count: int
    document: DocumentMetadata | None
    file_hash: str | None = None
    version: int = 1


def open_snapshot(value):
    ref=value['reader']
    if ref['version'] not in (1,2):
        raise ValueError('不支持的按需文档版本。')
    archive=Path(ref['archive'])
    # A missing archive must not create an empty SQLite file during a read.
    with closing(sqlite3.connect(archive.as_uri()+'?mode=ro',uri=True)) as db:
        data=json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
    if data['source_id']!=value['source_id'] or data['line_count']!=ref['line_count']:
        raise ValueError('文档分片与来源身份不匹配。')
    if ref['version']==2:
        if data.get('version')!=2 or data.get('state')!='sealed':
            raise ValueError('解析快照尚未封存。')
        from source_archive import ArchiveDocument
        return StoredSource(value['source_id'],Path(value['path']),archive,ref['line_count'],
                            ArchiveDocument(archive,data['document']),data['file_hash'],2)
    doc=data['document']
    if doc:
        blocks=tuple(ContentBlock(**{**b,'locations':tuple(Location(**{**p,'bbox':tuple(p['bbox']) if p.get('bbox') else None}) for p in b['locations']),
            'asset_ids':tuple(b['asset_ids']),'heading_path':tuple(b['heading_path']),
            'context_ranges':tuple(tuple(r) for r in b.get('context_ranges',()))}) for b in doc['blocks'])
        assets=tuple(Asset(**{**a,'bbox':tuple(a['bbox']) if a.get('bbox') else None}) for a in doc['assets'])
        doc=DocumentMetadata(doc['format'],doc['raw_hash'],doc['parser'],blocks,assets,doc.get('page_count'),
                             tuple(doc['issues']),doc.get('raw_file'))
    return StoredSource(value['source_id'],Path(value['path']),archive,ref['line_count'],doc,data.get('file_hash'))


def freeze(source, directory):
    """Publish once by atomic rename; existing immutable versions are reused."""
    if isinstance(source, StoredSource):
        return source
    root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    path=root/(sha256(source.source_id.encode()).hexdigest()+'.sqlite3')
    value=dict(source_id=source.source_id,path=str(source.path),reader=dict(
        version=1,archive=str(path.resolve()),line_count=line_count(source)))
    if path.exists():
        return open_snapshot(value)
    pending=path.with_name(uuid4().hex+'.tmp')
    try:
        document=source.document.to_dict() if source.document else None
        if document:
            document.pop('text')
        data=dict(source_id=source.source_id,line_count=line_count(source),document=document,
                  file_hash=file_digest(source.path) if source.path.is_file() else None)
        with closing(sqlite3.connect(pending)) as db, db:
            db.executescript('CREATE TABLE metadata(payload TEXT NOT NULL); '
                'CREATE TABLE lines(number INTEGER PRIMARY KEY,text TEXT NOT NULL,digest TEXT NOT NULL);')
            db.execute('INSERT INTO metadata VALUES (?)',(json.dumps(data,ensure_ascii=False),))
            db.executemany('INSERT INTO lines VALUES (?,?,?)',((n,text,sha256(text.encode()).hexdigest())
                for n,text in enumerate(source.text.splitlines(),1)))
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return open_snapshot(value)


def range_chunks(source, start, end, max_chars=1200):
    """Retrieval is confined to this batch; explicit reading may reach other ranges."""
    from chunking import block_chunk
    from source_archive import blocks_for
    blocks=blocks_for(source,start,end)
    if not blocks:
        from document_model import ContentBlock
        blocks=(ContentBlock('body','text',1,line_count(source)),)
    result=[]
    for block in blocks:
        left,right=max(start,block.line_start),min(end,block.line_end)
        if left>right or block.kind in ('page','figure'):
            continue
        text=read_lines(source,left,right)
        # Keep tables structurally intact; the existing embedding layer can window them.
        if block.kind in ('table','formula'):
            result.append(block_chunk(source,block,left,right)); continue
        cursor,size=left,0
        for number,line in enumerate(text.splitlines(),left):
            if size and size+len(line)>max_chars:
                result.append(block_chunk(source,block,cursor,number-1)); cursor,size=number,0
            size+=len(line)+1
        result.append(block_chunk(source,block,cursor,right))
    return result
