"""Build exact source chunks without depending on a generation service."""
from hashlib import sha256
from chunking import Chunk
from source_reader import read_lines


def excerpt(source, start, end, path=(), lines=None):
    return Chunk(sha256(f'{source.source_id}:{start}:{end}'.encode()).hexdigest(), source.source_id,
                 tuple(path), start, end, '\n'.join(lines[start - 1:end]) if lines is not None else read_lines(source,start,end))

