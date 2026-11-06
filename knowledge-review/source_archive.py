"""Version-2 immutable archives: text and structure are queried, not materialized."""
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from document_model import Asset, ContentBlock, Location


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)


def decode_block(data):
    b=json.loads(data)
    return ContentBlock(**{**b,'locations':tuple(Location(**{**p,'bbox':tuple(p['bbox']) if p.get('bbox') else None})
        for p in b['locations']), 'asset_ids':tuple(b['asset_ids']),'heading_path':tuple(b['heading_path']),
        'context_ranges':tuple(tuple(r) for r in b.get('context_ranges',()))})


class Records:
    """Compatibility iterable; iteration holds only the current serialized record."""
    def __init__(self,path,table): self.path,self.table=path,table

    def __len__(self):
        with closing(connect(self.path)) as db:
            return db.execute(f'SELECT COUNT(*) FROM {self.table}').fetchone()[0]

    def __iter__(self):
        with closing(connect(self.path)) as db:
            for row in db.execute(f'SELECT payload FROM {self.table} ORDER BY seq'):
                yield self.decode(row[0])

    def decode(self,payload):
        if self.table=='blocks': return decode_block(payload)
        a=json.loads(payload)
        return Asset(**{**a,'bbox':tuple(a['bbox']) if a.get('bbox') else None})


class ArchiveDocument:
    def __init__(self,path,data):
        self.archive=path
        for key in ('format','raw_hash','parser','page_count','raw_file'):
            setattr(self,key,data[key])
        self.status=data['status']
        self.blocks=Records(path,'blocks'); self.assets=Records(path,'assets')

    @property
    def issues(self):
        # Page-local diagnostics are queried by readers. This property preserves
        # old consumers; it contains diagnostics, never the document body.
        with closing(connect(self.archive)) as db:
            return tuple(json.loads(row[0]) for row in db.execute('SELECT payload FROM issues ORDER BY seq'))

    def read_blocks(self,start,end):
        with closing(connect(self.archive)) as db:
            return [decode_block(row[0]) for row in db.execute(
                'SELECT payload FROM blocks WHERE line_start<=? AND line_end>=? ORDER BY seq',(end,start))]

    def read_assets(self,page):
        with closing(connect(self.archive)) as db:
            return [self.assets.decode(row[0]) for row in db.execute(
                'SELECT payload FROM assets WHERE page=? ORDER BY seq',(page,))]

    def asset(self,identity):
        # Identity is stored in the JSON payload; page lists remain small and
        # are queried by page where known. No image bytes are loaded here.
        with closing(connect(self.archive)) as db:
            row=db.execute("SELECT payload FROM assets WHERE json_extract(payload,'$.asset_id')=?",(identity,)).fetchone()
        return self.assets.decode(row[0]) if row else None

    def page(self,number):
        if type(number) is not int or not 1<=number<=self.page_count:
            raise ValueError('页码不在资料范围内。')
        with closing(connect(self.archive)) as db:
            row=db.execute('SELECT line_start,line_end FROM pages WHERE number=?',(number,)).fetchone()
        if row is None: raise ValueError('页面存档缺失。')
        return tuple(row)

    def page_at(self,line):
        with closing(connect(self.archive)) as db:
            row=db.execute('SELECT number FROM pages WHERE line_start<=? AND line_end>=?',(line,line)).fetchone()
        if row is None: raise ValueError('原文位置没有对应页面。')
        return row[0]

    def locations(self,start,end):
        return [asdict(p) for b in self.read_blocks(start,end) for p in b.locations]


def blocks_for(source,start,end):
    doc=source.document
    if not doc: return []
    if isinstance(doc,ArchiveDocument): return doc.read_blocks(start,end)
    return [b for b in doc.blocks if b.line_start<=end and b.line_end>=start]
