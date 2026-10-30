from __future__ import annotations

import os
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
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
from chemfit_dashboard.source import (
    ContextDocument,
    NodeInfo,
    RecordRef,
    alphanumeric_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

ModelIndex = QModelIndex | QPersistentModelIndex
EMPTY_INDEX = QModelIndex()
DISPLAY_ROLE = Qt.ItemDataRole.DisplayRole.value
TOOLTIP_ROLE = Qt.ItemDataRole.ToolTipRole.value
FOREGROUND_ROLE = Qt.ItemDataRole.ForegroundRole.value


@dataclass(slots=True)
class _EvaluationItem:
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
    """Hierarchical folder model containing discovered context files."""

    RecordRole = Qt.ItemDataRole.UserRole.value + 1

    def __init__(self, parent: QObject | None = None):
        """Initialize an empty evaluation folder tree."""
        super().__init__(parent)
        self._records: list[RecordRef] = []
        self._record_order: dict[str, int] | None = None
        self._items_by_record_id: dict[str, _EvaluationItem] = {}
        self._root = _EvaluationItem("", None, None, None)

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return 1

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        parent_item = cast("_EvaluationItem", index.internalPointer()).parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def hasChildren(self, parent: ModelIndex = EMPTY_INDEX) -> bool:  # noqa: N802
        return bool(self._item(parent).children)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):  # noqa: PLR0911
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
        if role == FOREGROUND_ROLE and item.record is None:
            return QColor(active_color_scheme().muted)
        return None

    def set_records(self, records: list[RecordRef]) -> None:
        merged = {record.id: record for record in self._records}
        merged.update({record.id: record for record in records})
        ordered = self._ordered_records(merged.values())
        self.beginResetModel()
        self._records = ordered
        self._items_by_record_id.clear()
        self._root = _EvaluationItem("", None, None, None)
        self._build_tree()
        self.endResetModel()

    def clear_records(self) -> None:
        """Remove every record and restore the empty model state."""
        self.beginResetModel()
        self._records = []
        self._record_order = None
        self._items_by_record_id.clear()
        self._root = _EvaluationItem("", None, None, None)
        self.endResetModel()

    def record(self, index: ModelIndex) -> RecordRef | None:
        if not index.isValid():
            return None
        return cast("_EvaluationItem", index.internalPointer()).record

    def first_record_index(self) -> QModelIndex:
        pending = list(reversed(self._root.children))
        while pending:
            item = pending.pop()
            if item.record is not None:
                return self.createIndex(item.row(), 0, item)
            pending.extend(reversed(item.children))
        return QModelIndex()

    @property
    def records(self) -> tuple[RecordRef, ...]:
        return tuple(self._records)

    @property
    def filename_ordered_records(self) -> tuple[RecordRef, ...]:
        return tuple(sorted(self._records, key=_record_filename_key))

    def set_record_order(self, record_ids: Iterable[str] | None) -> None:
        """Apply a custom record order or restore natural filename ordering."""
        self._record_order = (
            {record_id: rank for rank, record_id in enumerate(record_ids)}
            if record_ids is not None
            else None
        )
        self.beginResetModel()
        self._records = self._ordered_records(self._records)
        self._items_by_record_id.clear()
        self._root = _EvaluationItem("", None, None, None)
        self._build_tree()
        self.endResetModel()

    def index_for_record_id(self, record_id: str) -> QModelIndex:
        item = self._items_by_record_id.get(record_id)
        return (
            self.createIndex(item.row(), 0, item)
            if item is not None
            else QModelIndex()
        )

    def _ordered_records(self, records: Iterable[RecordRef]) -> list[RecordRef]:
        if self._record_order is None:
            return sorted(records, key=_record_filename_key)
        order = self._record_order
        return sorted(
            records,
            key=lambda record: (
                0,
                order[record.id],
            )
            if record.id in order
            else (1, _record_filename_key(record)),
        )

    def _build_tree(self) -> None:
        if not self._records:
            return
        common_path = Path(
            os.path.commonpath([str(record.path.parent) for record in self._records])
        )
        common_name = common_path.name or str(common_path)
        visible_root = _EvaluationItem(common_name, common_path, None, self._root)
        self._root.children = [visible_root]
        folders = {common_path: visible_root}

        for record in self._records:
            relative = record.path.relative_to(common_path)
            parent_item = visible_root
            folder_path = common_path
            for folder_name in relative.parts[:-1]:
                folder_path /= folder_name
                folder_item = folders.get(folder_path)
                if folder_item is None:
                    folder_item = _EvaluationItem(
                        folder_name, folder_path, None, parent_item
                    )
                    parent_item.children.append(folder_item)
                    folders[folder_path] = folder_item
                parent_item = folder_item
            record_item = _EvaluationItem(
                record.name,
                record.path,
                record,
                parent_item,
            )
            parent_item.children.append(record_item)
            self._items_by_record_id[record.id] = record_item

        for folder in folders.values():
            folder.children.sort(key=self._evaluation_item_sort_key)
            for row, child in enumerate(folder.children):
                child.row_index = row

    def _evaluation_item_sort_key(self, item: _EvaluationItem):
        if item.record is None:
            return (0, 0, alphanumeric_key(item.name), item.name.casefold())
        if self._record_order is not None and item.record.id in self._record_order:
            rank = self._record_order[item.record.id]
            return (1, 0, (), rank)
        return (1, 1, alphanumeric_key(item.name), item.name.casefold())

    def _item(self, index: ModelIndex) -> _EvaluationItem:
        if not index.isValid():
            return self._root
        return cast("_EvaluationItem", index.internalPointer())


@dataclass(slots=True)
class _ContextItem:
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
    """Lazy Qt model backed by a pre-indexed ContextDocument."""

    columns = ("Component", "Status", "Output")
    NodeRole = Qt.ItemDataRole.UserRole.value + 1

    def __init__(self, parent: QObject | None = None):
        """Initialize an empty context hierarchy."""
        super().__init__(parent)
        self._document: ContextDocument | None = None
        self._root = _ContextItem(info=None, parent=None, fetched=True)

    @property
    def document(self) -> ContextDocument | None:
        return self._document

    def set_document(self, document: ContextDocument | None) -> None:
        self.beginResetModel()
        self._document = document
        self._root = _ContextItem(info=None, parent=None, fetched=True)
        if document is not None:
            self._root.children.append(
                _ContextItem(info=document.root, parent=self._root)
            )
        self.endResetModel()

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return len(self.columns)

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        item = cast("_ContextItem", index.internalPointer())
        parent_item = item.parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):  # noqa: PLR0911
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
        if not parent.isValid():
            return bool(self._root.children)
        item = self._item(parent)
        return bool(item.info and item.info.child_count)

    def canFetchMore(self, parent: ModelIndex) -> bool:  # noqa: N802
        if not parent.isValid():
            return False
        item = self._item(parent)
        return bool(item.info and item.info.child_count and not item.fetched)

    def fetchMore(self, parent: ModelIndex) -> None:  # noqa: N802
        if self._document is None or not self.canFetchMore(parent):
            return
        item = self._item(parent)
        if item.info is None:
            return
        children = self._document.children(item.info.path)
        item.fetched = True
        if not children:
            return
        qt_parent = QModelIndex(parent)  # type: ignore[arg-type]
        self.beginInsertRows(qt_parent, 0, len(children) - 1)
        item.children = [
            _ContextItem(info=child, parent=item, row_index=row)
            for row, child in enumerate(children)
        ]
        self.endInsertRows()

    def node_info(self, index: ModelIndex) -> NodeInfo | None:
        if not index.isValid():
            return None
        return cast("_ContextItem", index.internalPointer()).info

    def index_for_path(self, path: tuple[int, ...]) -> QModelIndex:
        """Fetch ancestors as needed and return the index for a context path."""
        current = self.index(0, 0, QModelIndex())
        if not current.isValid() or not path:
            return current
        prefix: tuple[int, ...] = ()
        for child_index in path:
            if self.canFetchMore(current):
                self.fetchMore(current)
            prefix = (*prefix, child_index)
            item = self._item(current)
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
        if not index.isValid():
            return self._root
        return cast("_ContextItem", index.internalPointer())


@dataclass(slots=True)
class _JsonItem:
    key: str
    value: Any
    parent: _JsonItem | None
    path: tuple[str | int, ...]
    children: list[_JsonItem] = field(default_factory=list)
    fetched: bool = False

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


class JsonValueModel(QAbstractItemModel):
    """Generic, lazily expanded mapping/list viewer."""

    columns = ("Field", "Value", "Type")

    def __init__(self, value: Any = None, parent: QObject | None = None):
        """Initialize the model with one JSON-compatible value."""
        super().__init__(parent)
        self._root = _JsonItem("", value, None, ())
        self._populate(self._root)

    def set_value(self, value: Any) -> None:
        self.beginResetModel()
        self._root = _JsonItem("", value, None, ())
        self._populate(self._root)
        self.endResetModel()

    def columnCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        del parent
        return len(self.columns)

    def rowCount(self, parent: ModelIndex = EMPTY_INDEX) -> int:  # noqa: N802
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self._item(parent).children)

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = EMPTY_INDEX,
    ) -> QModelIndex:
        parent_item = self._item(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: ModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        parent_item = cast("_JsonItem", index.internalPointer()).parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def data(self, index: ModelIndex, role: int = DISPLAY_ROLE):
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
        item = self._item(parent)
        return isinstance(item.value, (dict, list)) and bool(item.value)

    def canFetchMore(self, parent: ModelIndex) -> bool:  # noqa: N802
        return (
            parent.isValid()
            and not self._item(parent).fetched
            and self.hasChildren(parent)
        )

    def fetchMore(self, parent: ModelIndex) -> None:  # noqa: N802
        if not self.canFetchMore(parent):
            return
        item = self._item(parent)
        count = len(item.value)
        if count:
            qt_parent = QModelIndex(parent)  # type: ignore[arg-type]
            self.beginInsertRows(qt_parent, 0, count - 1)
            self._populate(item)
            self.endInsertRows()

    def value(self, index: ModelIndex) -> Any:
        """Return the unformatted JSON value represented by an index."""
        return self._item(index).value if index.isValid() else None

    def value_path(self, index: ModelIndex) -> tuple[str | int, ...]:
        """Return mapping keys and list indices from the root to an item."""
        return self._item(index).path if index.isValid() else ()

    def index_for_value_path(self, path: tuple[str | int, ...]) -> QModelIndex:
        """Fetch nested values as needed and locate an exact JSON path."""
        parent = QModelIndex()
        prefix: tuple[str | int, ...] = ()
        for segment in path:
            if parent.isValid() and self.canFetchMore(parent):
                self.fetchMore(parent)
            prefix = (*prefix, segment)
            match = QModelIndex()
            for row in range(self.rowCount(parent)):
                candidate = self.index(row, 0, parent)
                if self.value_path(candidate) == prefix:
                    match = candidate
                    break
            if not match.isValid():
                return QModelIndex()
            parent = match
        return parent

    def _populate(self, item: _JsonItem) -> None:
        if item.fetched:
            return
        item.fetched = True
        if isinstance(item.value, dict):
            item.children = [
                _JsonItem(str(key), value, item, (*item.path, str(key)))
                for key, value in item.value.items()
            ]
        elif isinstance(item.value, list):
            item.children = [
                _JsonItem(f"[{index}]", value, item, (*item.path, index))
                for index, value in enumerate(item.value)
            ]

    def _item(self, index: ModelIndex) -> _JsonItem:
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
