"""Disposable test directories with inherited Windows workspace permissions."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def temporary_notes():
    # tempfile's Windows mode 0700 directories are inaccessible in this sandbox.
    root = Path(__file__).resolve().parent / f".test-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        for path in root.iterdir():
            path.unlink()
        root.rmdir()
