from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chemfit_dashboard.source import ContextSourceError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from chemfit_dashboard.source import ContextDocument, ContextSource, RecordRef


class OperationCancelled(RuntimeError):
    """Raised when a cooperative background operation is cancelled."""


class CancellationToken:
    """A lightweight thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        """Initialize an unset cancellation event."""
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled


@dataclass(frozen=True, slots=True)
class TraversalItem:
    record: RecordRef
    document: ContextDocument | None
    error: str | None = None


def iter_collection(
    source: ContextSource,
    records: Iterable[RecordRef],
    *,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
) -> Iterator[TraversalItem]:
    """Load collection documents one at a time and release them after yielding."""
    for index, record in enumerate(records):
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            document = source.load(record)
        except ContextSourceError as error:
            item = TraversalItem(record, None, str(error))
        else:
            item = TraversalItem(record, document)
        yield item
        if progress is not None:
            progress(index + 1, record)
