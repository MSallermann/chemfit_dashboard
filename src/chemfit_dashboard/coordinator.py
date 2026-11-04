from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    Signal,
)

from chemfit_dashboard.context_document import (
    ContextDocument,
    NodePath,
    RecordRef,
)
from chemfit_dashboard.ordered_record_collection import OrderedRecordCollection
from chemfit_dashboard.source import ContextSource
from chemfit_dashboard.workers import GenerationTracker, TaskRunner


@dataclass(frozen=True, slots=True)
class CollectionLoadRequest:
    locations: Iterable[str | Path]
    append: bool


@dataclass(frozen=True, slots=True)
class RecordSelectionRequest:
    ref: RecordRef | None


class DashboardState(QObject):
    """
    Central observable state of the ChemFit Dashboard.

    Signals distinguish between requested operations and committed state
    changes. Requests may involve asynchronous processing, while change
    signals are emitted only after the corresponding state has been updated.

    Results from superseded requests must never modify application state.

    All dashboard features observe the same authoritative state. Changes
    originating from different views therefore follow the same paths.
    """

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    # Emitted when loading, replacing, or extending the evaluation collection
    # is requested. Discovering the available evaluations may take time and
    # is performed asynchronously.
    # The existing collection remains available until the request completes.
    # Payload: tuple[str, ...] containing the requested locations.
    collection_load_requested = Signal(CollectionLoadRequest)

    # Emitted when the collection's contents have changed.
    # This includes adding or removing evaluations, replacing the collection,
    # or closing the currently open collection.
    # The signal is emitted only after the updated collection is committed.
    # Payload: tuple[RecordRef, ...] containing the updated collection.
    collection_changed = Signal(object)

    # ------------------------------------------------------------------
    # Collection ordering
    # ------------------------------------------------------------------

    # Emitted when a new ordering of the collection is requested.
    # Sorting by evaluation quantities may require asynchronously extracting
    # values from multiple documents.
    # The existing ordering remains active until the request completes.
    # Payload: SortSpec describing the requested ordering and direction.
    collection_order_requested = Signal(RecordSelectionRequest)

    # Emitted when a new collection ordering has been committed.
    # This includes sorting by a quantity, changing the sort direction,
    # or restoring the default filename ordering.
    # Collection membership is unaffected.
    # Listeners retrieve the updated ordering from the collection state.
    collection_order_changed = Signal()

    # ------------------------------------------------------------------
    # Record selection
    # ------------------------------------------------------------------

    # Emitted when selecting a different recrod is requested.
    # Requests may originate from the evaluation list, collection plots,
    # search results, extraction previews, or navigation history.
    # Loading the corresponding document may require asynchronous I/O.
    # The previously loaded document remains authoritative until the
    # requested selection is committed.
    # Payload: RecordRef | None, where None requests clearing the selection.
    selected_record_change_requested = Signal(object)

    # Emitted when the requested record has been successfully loaded
    # and installed as the currently selected record.
    # Results belonging to superseded selection requests are discarded.
    # Clearing the selection also emits this signal with None.
    # Listeners can access the new document and its evaluation reference
    # through the dashboard state.
    # Payload: ContextDocument | None
    selected_record_changed = Signal(object)

    # ------------------------------------------------------------------
    # Context selection
    # ------------------------------------------------------------------

    # Emitted when the selected context within the currently loaded
    # evaluation changes.
    # This includes selection from the context hierarchy, data-flow graph,
    # navigation history, or another feature referencing a nested context.
    # The selected context is identified by its path within the document's
    # hierarchy, not by a ChemFit runtime object.
    # Selection may be restored automatically when switching evaluations.
    # Payload: NodePath | None, where None indicates no selected context.
    selected_context_changed = Signal(object)

    # Emitted when an asynchronous operation fails.
    # Payload: operation name and formatted traceback.
    operation_failed = Signal(str, str)

    def __init__(self, source: ContextSource):
        """Initialize the coordinator."""
        super().__init__()
        self._source: ContextSource = source
        self._collection: OrderedRecordCollection = OrderedRecordCollection()
        self._selected_document: ContextDocument | None = None
        self._selected_context: NodePath | None = None
        self._task_runner = TaskRunner()
        self._generations = GenerationTracker()

    @property
    def collection(self) -> Sequence[RecordRef]:
        """The evaluation collection in its current order."""
        return self._collection

    @property
    def selected_document(self) -> ContextDocument | None:
        """The currently loaded evaluation document."""
        return self._selected_document

    @property
    def selected_context(self) -> NodePath | None:
        """Path of the selected context within the current document."""
        return self._selected_context

    def load_collection(
        self, locations: Iterable[str | Path], *, append: bool = False
    ) -> None:
        """Asynchronously discover records and replace or extend the collection."""
        request = CollectionLoadRequest(
            locations=tuple(str(path) for path in locations),
            append=append,
        )

        def task() -> list[RecordRef]:
            return self._source.discover(request.locations)

        def on_success(records: list[RecordRef]) -> None:
            if request.append:
                self._collection.extend(records)
            else:
                self._collection.replace(records)

            self.collection_changed.emit(tuple(self.collection))

        def on_error(details: str) -> None:
            self.operation_failed.emit("Collection loading", details)

        self._task_runner.submit_latest(
            "collection",
            task,
            on_success,
            on_error,
        )
        self.collection_load_requested.emit(request)

    def select_record(self, record: RecordRef | None):
        request = RecordSelectionRequest(ref=record)

        # The requested document is already loaded.
        if (
            record is not None
            and self._selected_document is not None
            and self._selected_document.record == record
        ):
            self._task_runner.invalidate("record")
            return

        # Deselect synchronously and invalidate outstanding loads.
        if record is None:
            self._task_runner.invalidate("record")
            self._selected_document = None
            self._selected_context = None

            self.selected_record_change_requested.emit(request)
            self.selected_record_changed.emit(None)
            self.selected_context_changed.emit(None)
            return

        def task() -> ContextDocument:
            assert record is not None
            return self._source.load(record)

        def on_success(document: ContextDocument) -> None:
            self._selected_document = document
            self._selected_context = ()  # selects the root context

            self.selected_record_changed.emit(self.selected_document)
            self.selected_context_changed.emit(())

        def on_error(details: str) -> None:
            self.operation_failed.emit("Record selection", details)

        self._task_runner.submit_latest(
            "record",
            task,
            on_success,
            on_error,
        )
        self.selected_record_change_requested.emit(request)
