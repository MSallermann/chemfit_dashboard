"""
Qt models connecting dashboard data to Qt tree views.

A QTreeView asks its model for rows, columns, parent/child indexes, and data
for specific display roles; it does not navigate ContextDocument directly.
A QModelIndex refers to one cell and carries a private ``_...Item`` instance
as its ``internalPointer()``. That item links to its parent and materialized
children, while RecordRef, NodeInfo, and JSON values remain the actual data.

Every model uses an invisible ``_root`` item. Qt represents this root with an
*invalid* QModelIndex; valid indexes refer to visible cells, with tree
relationships conventionally expressed through column 0.

- EvaluationListModel eagerly builds the folder/record hierarchy and controls
  its display order.
- ContextTreeModel materializes context items in batches from an already
  indexed ContextDocument.
- JsonValueModel lazily exposes a nested dict/list for the inspector.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor

from chemfit_dashboard.appearance import active_color_scheme
from chemfit_dashboard.context_document import (
    ContextDocument,
    NodeInfo,
    RecordRef,
    alphanumeric_key,
)
from chemfit_dashboard.coordinator import DashboardCoordinator

if TYPE_CHECKING:
    from pathlib import Path


ModelIndex = QModelIndex | QPersistentModelIndex
EMPTY_INDEX = QModelIndex()
DISPLAY_ROLE = Qt.ItemDataRole.DisplayRole.value
TOOLTIP_ROLE = Qt.ItemDataRole.ToolTipRole.value
FOREGROUND_ROLE = Qt.ItemDataRole.ForegroundRole.value


@dataclass(slots=True)
class _EvaluationItem:
    """
    One Qt tree item: either a folder (``record=None``) or an evaluation.

    ``row_index`` is its position among its parent's displayed children;
    Qt needs this to reconstruct the parent's QModelIndex.
    """

    name: str
    path: Path | None
    record: RecordRef | None
    parent: _EvaluationItem | None
    children: list[_EvaluationItem] = field(default_factory=list)
    row_index: int = 0

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.row_index


class EvaluationListModel(QAbstractItemModel):
    """
    Eagerly built grouping of evaluations by their immediate parent folder.

    Folder entries are display-only; leaf items hold RecordRef objects.
    Reordering resets the Qt model,
    while stable record IDs let MainWindow restore selection.
    """

    RecordRole = Qt.ItemDataRole.UserRole.value + 1
    FolderPathRole = Qt.ItemDataRole.UserRole.value + 2

    def __init__(
        self, parent: QObject | None = None, *, coordinator: DashboardCoordinator
    ):
        """Initialize an empty evaluation folder tree."""
        super().__init__(parent)
        self.coordinator = coordinator

        self.coordinator.collection_changed.connect(self._rebuild_tree)
        self.coordinator.collection_order_changed.connect(self._rebuild_tree)

        self._root = _EvaluationItem("", None, None, None)
        self._items_by_record_id: dict[str, _EvaluationItem] = {}
        self._folders_by_path: dict[Path, _EvaluationItem] = {}

        self._rebuild_tree()

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return 1

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        """Count rows beneath ``parent`` (only column 0 owns children)."""
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        if column != 0 or (parent.isValid() and parent.column() != 0):
            return QModelIndex()

        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()

        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        """Return a child's parent index; top-level items have no Qt parent."""
        if not index.isValid():
            return QModelIndex()
        parent_item = cast("_EvaluationItem", index.internalPointer()).parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def hasChildren(self, parent: ModelIndex = EMPTY_INDEX) -> bool:  # noqa: N802
        return bool(self._item(parent).children)

    def index_for_record_id(self, record_id: str) -> QModelIndex:
        """Return the model index for a record, or an invalid index if absent."""
        item = self._items_by_record_id.get(record_id)

        if item is None:
            return QModelIndex()

        return self.createIndex(item.row(), 0, item)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):  # noqa: PLR0911
        """
        Answer Qt's role-specific requests for a folder or evaluation row.

        ``RecordRole`` exposes RecordRef to application code without depending
        on the displayed name or filename.
        """
        if not index.isValid():
            return None
        item = cast("_EvaluationItem", index.internalPointer())
        if role == DISPLAY_ROLE:
            return item.name
        if role == TOOLTIP_ROLE:
            if item.record is not None:
                return f"{item.record.path}\n{_format_size(item.record.size)}"
            return str(item.path) if item.path is not None else None
        if role == self.RecordRole:
            return item.record
        if role == self.FolderPathRole and item.record is None:
            return item.path
        if role == FOREGROUND_ROLE and item.record is None:
            return QColor(active_color_scheme().muted)
        return None

    def _rebuild_tree(self, *_args: object) -> None:
        self.beginResetModel()
        try:
            self._root = _EvaluationItem("", None, None, None)
            self._items_by_record_id.clear()
            self._folders_by_path.clear()
            self._build_tree()
        finally:
            self.endResetModel()

    def _item(self, index: ModelIndex) -> _EvaluationItem:
        if not index.isValid():
            return self._root
        return cast("_EvaluationItem", index.internalPointer())

    def _build_tree(self) -> None:
        """Group evaluations beneath only their immediate parent folders."""

        records = self.coordinator.collection
        if not records:
            return

        # Insert records in coordinator order.
        for record in records:
            folder_path = record.path.parent
            parent = self._folders_by_path.get(folder_path)
            if parent is None:
                parent = _EvaluationItem(
                    folder_path.name or str(folder_path),
                    folder_path,
                    None,
                    self._root,
                )
                self._root.children.append(parent)
                self._folders_by_path[folder_path] = parent

            # Create the evaluation leaf.
            item = _EvaluationItem(
                record.name,
                record.path,
                record,
                parent,
            )
            parent.children.append(item)
            self._items_by_record_id[record.id] = item

        self._root.children.sort(
            key=lambda item: (
                alphanumeric_key(item.name),
                str(item.path).casefold(),
            )
        )
        for row, child in enumerate(self._root.children):
            child.row_index = row

        # Retain the coordinator's ordering of evaluation leaves within each folder.
        for folder in self._folders_by_path.values():
            for row, child in enumerate(folder.children):
                child.row_index = row


@dataclass(slots=True)
class _ContextItem:
    """
    Qt-facing wrapper for a NodeInfo from ContextDocument.

    ``children`` contains only items materialized so far, not all children
    known to ContextDocument. Parent pointers and row indexes serve Qt's
    index/parent protocol; the document remains the source of node data.
    """

    info: NodeInfo | None
    parent: _ContextItem | None
    children: list[_ContextItem] = field(default_factory=list)
    fetched: bool = False
    row_index: int = 0

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.row_index


class ContextTreeModel(QAbstractItemModel):
    """
    Lazy Qt adapter for one pre-indexed ``ContextDocument``.

    When a document is set, the invisible model root has one visible child:
    the document root context. NodeInfo.child_count gives the complete child count, while ``_ContextItem``
    holds only the children fetched into Qt (at most FETCH_BATCH_SIZE per call).
    The document owns the actual hierarchy and its path lookup.
    """

    columns = ("Component", "Status", "Output")
    NodeRole = Qt.ItemDataRole.UserRole.value + 1
    FETCH_BATCH_SIZE = 200

    def __init__(self, parent: QObject | None = None):
        """Initialize an empty context hierarchy."""
        super().__init__(parent)
        self._document: ContextDocument | None = None
        self._root = _ContextItem(info=None, parent=None, fetched=True)

    @property
    def document(self) -> ContextDocument | None:
        return self._document

    def set_document(self, document: ContextDocument | None) -> None:
        """Switch evaluations and invalidate the previous Qt item hierarchy."""
        self.beginResetModel()
        self._document = document
        self._root = _ContextItem(info=None, parent=None, fetched=True)
        # Unlike JsonValueModel, the context's own root is a visible row.
        if document is not None:
            self._root.children.append(
                _ContextItem(info=document.root, parent=self._root)
            )
        self.endResetModel()

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return len(self.columns)

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        """Count rows beneath ``parent`` (only column 0 owns children)."""
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        """Wrap an already fetched child in a QModelIndex with its item pointer."""
        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        """Recover the wrapper's parent, using an invalid index at the top."""
        if not index.isValid():
            return QModelIndex()
        item = cast("_ContextItem", index.internalPointer())
        parent_item = item.parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):  # noqa: PLR0911
        """
        Return cell text, tooltip, colors, or NodeInfo for the requested role.

        Qt asks for each role separately. The custom NodeRole lets other GUI
        code retrieve the underlying NodeInfo directly from a selected index.
        """
        if not index.isValid():
            return None
        item = cast("_ContextItem", index.internalPointer())
        info = item.info
        if info is None:
            return None
        if role == self.NodeRole:
            return info
        if role == DISPLAY_ROLE:
            label = (
                self._document.component_label(info.path)
                if self._document is not None
                else info.label
            )
            return (label, info.status, info.output_preview)[index.column()]
        if role == TOOLTIP_ROLE:
            return f"{info.kind} · {info.path_text}"
        if role == FOREGROUND_ROLE and not info.recorded:
            return QColor(active_color_scheme().status_missing)
        if role == FOREGROUND_ROLE and index.column() == 1:
            return QColor(_status_color(info.status))
        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = DISPLAY_ROLE,
    ):
        if orientation == Qt.Orientation.Horizontal and role == DISPLAY_ROLE:
            return self.columns[section]
        return None

    def hasChildren(self, parent: ModelIndex = EMPTY_INDEX) -> bool:  # noqa: N802
        """Tell Qt an item is expandable even before its children are fetched."""
        if not parent.isValid():
            return bool(self._root.children)
        item = self._item(parent)
        return bool(item.info and item.info.child_count)

    def canFetchMore(self, parent: ModelIndex) -> bool:  # noqa: N802
        """Compare the document's child count with items created for Qt."""
        if not parent.isValid():
            return False
        item = self._item(parent)
        return bool(item.info and item.info.child_count > len(item.children))

    def fetchMore(self, parent: ModelIndex) -> None:  # noqa: N802
        """
        Materialize one bounded child batch for Qt, without reindexing data.

        beginInsertRows()/endInsertRows() tell attached views exactly which
        rows became available; mutating ``item.children`` alone is not enough.
        """
        if self._document is None or not self.canFetchMore(parent):
            return
        item = self._item(parent)
        if item.info is None:
            return
        start = len(item.children)
        stop = min(start + self.FETCH_BATCH_SIZE, item.info.child_count)
        children = self._document.children_slice(item.info.path, start, stop)
        item.fetched = stop >= item.info.child_count
        if not children:
            return
        qt_parent = QModelIndex(parent)  # type: ignore[arg-type]
        self.beginInsertRows(qt_parent, start, stop - 1)
        item.children.extend(
            _ContextItem(info=child, parent=item, row_index=row)
            for row, child in enumerate(children, start=start)
        )
        self.endInsertRows()

    def node_info(self, index: ModelIndex) -> NodeInfo | None:
        if not index.isValid():
            return None
        return cast("_ContextItem", index.internalPointer()).info

    def index_for_path(self, path: tuple[int, ...]) -> QModelIndex:
        """
        Find a context node by path, fetching its ancestor branches as needed.

        Children are stored in path order. Fetch batches until the desired
        prefix is in range, then bisect the materialized siblings. This also
        supports jumping to nodes that the user has not expanded manually.
        """
        current = self.index(0, 0, QModelIndex())
        if not current.isValid() or not path:
            return current
        prefix: tuple[int, ...] = ()
        for child_index in path:
            prefix = (*prefix, child_index)
            item = self._item(current)
            while self.canFetchMore(current) and (
                not item.children
                or item.children[-1].info is None
                or item.children[-1].info.path < prefix
            ):
                self.fetchMore(current)
            row = bisect_left(
                item.children,
                prefix,
                key=lambda child: child.info.path if child.info is not None else (),
            )
            match = self.index(row, 0, current)
            info = self.node_info(match)
            if info is None or info.path != prefix:
                match = QModelIndex()
            if not match.isValid():
                return QModelIndex()
            current = match
        return current

    def _item(self, index: ModelIndex) -> _ContextItem:
        """Map Qt's invalid root index or a valid internal pointer to an item."""
        if not index.isValid():
            return self._root
        return cast("_ContextItem", index.internalPointer())


@dataclass(slots=True)
class _JsonItem:
    """
    One materialized entry in a nested dict/list inspector tree.

    ``path`` stores mapping keys and list indexes for selection restoration.
    A child retains its raw value; the model formats that value for display.
    """

    key: str
    value: Any
    parent: _JsonItem | None
    path: tuple[str | int, ...]
    children: list[_JsonItem] = field(default_factory=list)
    row_index: int = 0

    def row(self) -> int:
        return self.row_index


class JsonValueModel(QAbstractItemModel):
    """
    Generic, lazily expanded dict/list viewer for the raw-value inspector.

    Unlike ContextTreeModel, this model has no ContextDocument or NodeInfo.
    Its invisible root holds the inspected JSON value; the visible rows are
    that value's keys or list elements. The top-level entries start with one
    batch; deeper entries are created on demand as branches are expanded.
    """

    columns = ("Field", "Value", "Type")
    FETCH_BATCH_SIZE = 200

    def __init__(self, value: Any = None, parent: QObject | None = None):
        """Initialize the model with one JSON-compatible value."""
        super().__init__(parent)
        # The root value itself is not a displayed row; its entries are.
        self._root = _JsonItem("", value, None, ())
        self._populate_batch(self._root)

    def set_value(self, value: Any) -> None:
        """Replace the inspected value and invalidate old QModelIndexes."""
        self.beginResetModel()
        self._root = _JsonItem("", value, None, ())
        self._populate_batch(self._root)
        self.endResetModel()

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return len(self.columns)

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        """Count rows beneath ``parent`` (only column 0 owns children)."""
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        """Wrap an already fetched child in a QModelIndex with its item pointer."""
        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        """Return the parent JSON entry, or an invalid index for root entries."""
        if not index.isValid():
            return QModelIndex()
        parent_item = cast("_JsonItem", index.internalPointer()).parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):
        """Show an entry's key/index, abbreviated value, and Python/JSON type."""
        if not index.isValid() or role != DISPLAY_ROLE:
            return None
        item = cast("_JsonItem", index.internalPointer())
        if index.column() == 0:
            return item.key
        if index.column() == 1:
            return _json_summary(item.value)
        return _json_type(item.value)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = DISPLAY_ROLE,
    ):
        if orientation == Qt.Orientation.Horizontal and role == DISPLAY_ROLE:
            return self.columns[section]
        return None

    def hasChildren(self, parent: ModelIndex = EMPTY_INDEX) -> bool:  # noqa: N802
        """Report expandable containers, including ones not yet materialized."""
        item = self._item(parent)
        return isinstance(item.value, (dict, list)) and bool(item.value)

    def canFetchMore(self, parent: ModelIndex) -> bool:  # noqa: N802
        """Check whether the container has more entries than Qt items."""
        item = self._item(parent)
        return isinstance(item.value, (dict, list)) and len(item.children) < len(
            item.value
        )

    def fetchMore(self, parent: ModelIndex) -> None:  # noqa: N802
        """Append one batch and notify Qt around the insertion."""
        if not self.canFetchMore(parent):
            return
        item = self._item(parent)
        count = min(self.FETCH_BATCH_SIZE, len(item.value) - len(item.children))
        if count:
            qt_parent = QModelIndex(parent)  # type: ignore[arg-type]
            first = len(item.children)
            self.beginInsertRows(qt_parent, first, first + count - 1)
            self._populate_batch(item)
            self.endInsertRows()

    def value(self, index: ModelIndex) -> Any:
        """Return the unformatted JSON value represented by an index."""
        return self._item(index).value if index.isValid() else None

    def value_path(self, index: ModelIndex) -> tuple[str | int, ...]:
        """Return mapping keys and list indices from the root to an item."""
        return self._item(index).path if index.isValid() else ()

    def index_for_value_path(self, path: tuple[str | int, ...]) -> QModelIndex:
        """
        Locate a nested key/index, fetching unseen entries along the path.

        Each level scans the currently materialized siblings, fetching more
        batches until the requested entry is found or the level is exhausted.
        """
        parent = QModelIndex()
        prefix: tuple[str | int, ...] = ()
        for segment in path:
            prefix = (*prefix, segment)
            match = QModelIndex()
            while True:
                for row in range(self.rowCount(parent)):
                    candidate = self.index(row, 0, parent)
                    if self.value_path(candidate) == prefix:
                        match = candidate
                        break
                if match.isValid() or not self.canFetchMore(parent):
                    break
                self.fetchMore(parent)
            if not match.isValid():
                return QModelIndex()
            parent = match
        return parent

    def _populate_batch(self, item: _JsonItem) -> None:
        """
        Create the next batch of wrappers from a dict or list value.

        Called both during reset (for the invisible root) and from fetchMore()
        (inside Qt's row-insertion notifications for expanded items).
        """
        start = len(item.children)
        stop = (
            min(start + self.FETCH_BATCH_SIZE, len(item.value))
            if isinstance(item.value, (dict, list))
            else start
        )
        if isinstance(item.value, dict):
            # Slice the iterator rather than copying a potentially huge mapping.
            entries = islice(item.value.items(), start, stop)
            item.children.extend(
                _JsonItem(str(key), value, item, (*item.path, str(key)), row_index=row)
                for row, (key, value) in enumerate(entries, start=start)
            )
        elif isinstance(item.value, list):
            item.children.extend(
                _JsonItem(
                    f"[{index}]",
                    item.value[index],
                    item,
                    (*item.path, index),
                    row_index=index,
                )
                for index in range(start, stop)
            )

    def _item(self, index: ModelIndex) -> _JsonItem:
        """Recover the entry pointer, or the invisible root for an invalid index."""
        if not index.isValid():
            return self._root
        return cast("_JsonItem", index.internalPointer())


def _record_filename_key(record: RecordRef):
    return (
        alphanumeric_key(record.name),
        str(record.path.parent).casefold(),
        str(record.path),
    )


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _status_color(status: str) -> str:
    scheme = active_color_scheme()
    return {
        "complete": scheme.status_complete,
        "skipped": scheme.status_skipped,
        "not recorded": scheme.status_missing,
        "partial": scheme.status_partial,
    }.get(status, scheme.text)


def _json_summary(value: Any) -> str:
    if isinstance(value, dict):
        return f"{{{len(value)} fields}}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _json_type(value: Any) -> str:  # noqa: PLR0911
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__
