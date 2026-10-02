"""Exception context helpers."""

import traceback
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def error_context(context: str) -> Iterator[None]:
    """Annotate failures while preserving the original exception."""
    try:
        yield
    except BaseException as exc:
        exc.add_note(context)
        raise


def format_error(exc: BaseException) -> str:
    """Render an exception and its notes on one line."""
    return " | ".join(
        part.strip()
        for line in traceback.format_exception_only(exc)
        for part in line.splitlines()
        if part.strip()
    )
