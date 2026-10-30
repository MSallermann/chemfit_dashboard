from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.source import ContextDocument, NodePath

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from chemfit_dashboard.source import RecordRef

MAX_FAILURE_TREE_ITEMS = 1_000


@dataclass(frozen=True, slots=True)
class NumericSummary:
    path: str
    count: int
    minimum: float
    maximum: float
    mean: float


@dataclass(frozen=True, slots=True)
class FailureOccurrence:
    record_id: str
    record_name: str
    node_path: NodePath
    component: str
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class FailureGroup:
    component: str
    exception_type: str
    message: str
    occurrences: tuple[FailureOccurrence, ...]


@dataclass(frozen=True, slots=True)
class ComponentCount:
    component: str
    count: int


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    total_records: int
    readable_records: int
    successful_records: int
    failed_records: int
    partial_records: int
    unreadable_records: int
    node_count: int
    numeric_fields: tuple[NumericSummary, ...]
    failure_groups: tuple[FailureGroup, ...]
    skipped_components: tuple[ComponentCount, ...]
    missing_components: tuple[ComponentCount, ...]

    @property
    def failure_occurrences(self) -> tuple[FailureOccurrence, ...]:
        return tuple(
            occurrence
            for group in self.failure_groups
            for occurrence in group.occurrences
        )


def analyze_collection(  # noqa: PLR0912
    records_and_documents: Iterable[tuple[RecordRef, ContextDocument | None]],
) -> CollectionSummary:
    """Summarize numeric root fields and primary failures in a collection."""
    numeric_values: dict[str, list[float]] = {}
    failure_occurrences: list[FailureOccurrence] = []
    skipped: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    total = readable = successful = failed = partial = node_count = 0

    for record, document in records_and_documents:
        total += 1
        if document is None:
            continue
        readable += 1
        nodes = document.nodes
        node_count += len(nodes)
        failed_nodes = [node for node in nodes if node.status == "failed"]
        if failed_nodes:
            failed += 1
        elif document.root.status == "complete":
            successful += 1
        else:
            partial += 1

        for node in nodes:
            if node.status == "skipped":
                skipped[document.component_label(node.path)] += 1
            if not node.recorded:
                missing[document.component_label(node.path)] += 1

        failed_paths = {node.path for node in failed_nodes}
        failed_ancestors = {
            path[:depth]
            for path in failed_paths
            for depth in range(len(path))
        }
        primary_candidates = [
            node
            for node in failed_nodes
            if node.path not in failed_ancestors
        ]
        primary_failures = sorted(
            primary_candidates, key=lambda item: (-len(item.path), item.path)
        )[:1]
        for node in primary_failures:
            details = document.details(node.path)
            error_type, message = _exception_parts(details.fields.get("output"))
            failure_occurrences.append(
                FailureOccurrence(
                    record_id=record.id,
                    record_name=record.name,
                    node_path=node.path,
                    component=document.component_label(node.path),
                    exception_type=error_type,
                    message=message,
                )
            )

        for path, value in _numeric_leaves(document.details(()).fields):
            accumulator = numeric_values.get(path)
            if accumulator is None:
                # count, minimum, maximum, sum
                numeric_values[path] = [1.0, value, value, value]
            else:
                accumulator[0] += 1
                accumulator[1] = min(accumulator[1], value)
                accumulator[2] = max(accumulator[2], value)
                accumulator[3] += value

    numeric_fields = tuple(
        NumericSummary(
            path=path,
            count=int(values[0]),
            minimum=values[1],
            maximum=values[2],
            mean=values[3] / values[0],
        )
        for path, values in sorted(
            numeric_values.items(), key=lambda item: (-item[1][0], item[0].casefold())
        )[:40]
    )
    grouped: dict[tuple[str, str, str], list[FailureOccurrence]] = defaultdict(list)
    for occurrence in failure_occurrences:
        grouped[
            (occurrence.component, occurrence.exception_type, occurrence.message)
        ].append(occurrence)
    failure_groups = tuple(
        FailureGroup(component, error_type, message, tuple(occurrences))
        for (component, error_type, message), occurrences in sorted(
            grouped.items(),
            key=lambda item: (
                -len(item[1]),
                item[0][1].casefold(),
                item[0][0].casefold(),
            ),
        )
    )
    return CollectionSummary(
        total_records=total,
        readable_records=readable,
        successful_records=successful,
        failed_records=failed,
        partial_records=partial,
        unreadable_records=total - readable,
        node_count=node_count,
        numeric_fields=numeric_fields,
        failure_groups=failure_groups,
        skipped_components=_component_counts(skipped),
        missing_components=_component_counts(missing),
    )


class CollectionAnalysisPanel(QWidget):
    """Collection health summary and grouped primary-failure navigator."""

    failure_selected = Signal(object)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None):  # noqa: PLR0915
        """Initialize the summary tables and failure navigation controls."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        heading = QHBoxLayout()
        self.summary_label = QLabel("Load a collection to build its summary.")
        self.summary_label.setWordWrap(True)
        heading.addWidget(self.summary_label, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(
            lambda _checked=False: self.refresh_requested.emit()
        )
        heading.addWidget(self.refresh_button)
        layout.addLayout(heading)

        self.issue_label = QLabel()
        self.issue_label.setObjectName("mutedLabel")
        self.issue_label.setWordWrap(True)
        layout.addWidget(self.issue_label)

        numeric_label = QLabel("NUMERIC ROOT FIELDS")
        numeric_label.setObjectName("sectionLabel")
        layout.addWidget(numeric_label)
        self.numeric_table = QTableWidget(0, 5)
        self.numeric_table.setHorizontalHeaderLabels(
            ["Field", "Count", "Minimum", "Maximum", "Mean"]
        )
        self.numeric_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.numeric_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.numeric_table.verticalHeader().hide()
        self.numeric_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, 5):
            self.numeric_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.numeric_table.setMaximumHeight(190)
        layout.addWidget(self.numeric_table)

        failure_heading = QHBoxLayout()
        failure_label = QLabel("PRIMARY FAILURES")
        failure_label.setObjectName("sectionLabel")
        failure_heading.addWidget(failure_label)
        failure_heading.addStretch()
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self._move_failure(-1))
        failure_heading.addWidget(self.previous_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._move_failure(1))
        failure_heading.addWidget(self.next_button)
        layout.addLayout(failure_heading)

        self.failure_tree = QTreeWidget()
        self.failure_tree.setColumnCount(3)
        self.failure_tree.setHeaderLabels(["Component / evaluation", "Error", "Count"])
        self.failure_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.failure_tree.header().resizeSection(0, 165)
        self.failure_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.failure_tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.failure_tree.itemClicked.connect(self._failure_activated)
        layout.addWidget(self.failure_tree, 1)
        self._occurrences: tuple[FailureOccurrence, ...] = ()
        self._current_occurrence = -1

    def show_loading(self, count: int) -> None:
        self.summary_label.setText(
            f"Analyzing {count:,} evaluation{'s' if count != 1 else ''} in the background…"
        )
        self.refresh_button.setEnabled(False)

    def show_error(self, message: str) -> None:
        self.summary_label.setText(message)
        self.refresh_button.setEnabled(True)

    def set_summary(self, summary: CollectionSummary) -> None:
        self.refresh_button.setEnabled(True)
        self.summary_label.setText(
            f"{summary.total_records:,} evaluations · "
            f"{summary.successful_records:,} successful · "
            f"{summary.failed_records:,} failed · "
            f"{summary.partial_records:,} partial · "
            f"{summary.node_count:,} context nodes"
        )
        issue_parts = []
        if summary.unreadable_records:
            issue_parts.append(f"{summary.unreadable_records:,} unreadable")
        if summary.skipped_components:
            issue_parts.append(
                "most skipped: "
                + ", ".join(
                    f"{item.component} ({item.count})"
                    for item in summary.skipped_components[:4]
                )
            )
        if summary.missing_components:
            issue_parts.append(
                "most unrecorded: "
                + ", ".join(
                    f"{item.component} ({item.count})"
                    for item in summary.missing_components[:4]
                )
            )
        self.issue_label.setText(
            " · ".join(issue_parts) or "No skipped or missing components."
        )

        self.numeric_table.setRowCount(len(summary.numeric_fields))
        for row, field in enumerate(summary.numeric_fields):
            values = (
                field.path,
                str(field.count),
                f"{field.minimum:.8g}",
                f"{field.maximum:.8g}",
                f"{field.mean:.8g}",
            )
            for column, value in enumerate(values):
                self.numeric_table.setItem(row, column, QTableWidgetItem(value))

        self.failure_tree.clear()
        tree_items = 0
        displayed_groups = 0
        for group in summary.failure_groups:
            if tree_items >= MAX_FAILURE_TREE_ITEMS - 1:
                break
            group_item = QTreeWidgetItem(
                [group.component, group.exception_type, str(len(group.occurrences))]
            )
            group_item.setToolTip(0, group.message)
            group_item.setData(0, Qt.ItemDataRole.UserRole, group.occurrences[0])
            self.failure_tree.addTopLevelItem(group_item)
            tree_items += 1
            displayed_groups += 1
            available_children = MAX_FAILURE_TREE_ITEMS - tree_items
            visible_limit = (
                available_children
                if len(group.occurrences) <= available_children
                else max(0, available_children - 1)
            )
            visible_occurrences = group.occurrences[:visible_limit]
            for occurrence in visible_occurrences:
                child = QTreeWidgetItem(
                    [occurrence.record_name, occurrence.message, ""]
                )
                child.setToolTip(1, occurrence.message)
                child.setData(0, Qt.ItemDataRole.UserRole, occurrence)
                group_item.addChild(child)
            tree_items += len(visible_occurrences)
            hidden_count = len(group.occurrences) - len(visible_occurrences)
            if hidden_count and tree_items < MAX_FAILURE_TREE_ITEMS:
                group_item.addChild(
                    QTreeWidgetItem(
                        [
                            f"{hidden_count:,} more (use Previous/Next to navigate)",
                            "",
                            "",
                        ]
                    )
                )
                tree_items += 1
            group_item.setExpanded(len(group.occurrences) <= 8)
        omitted_groups = len(summary.failure_groups) - displayed_groups
        if omitted_groups and tree_items < MAX_FAILURE_TREE_ITEMS:
            self.failure_tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        f"{omitted_groups:,} more failure groups "
                        "(use Previous/Next to navigate)",
                        "",
                        "",
                    ]
                )
            )
        self._occurrences = summary.failure_occurrences
        self._current_occurrence = -1
        available = len(self._occurrences) > 1
        self.previous_button.setEnabled(available)
        self.next_button.setEnabled(available)
        if not available:
            empty = QTreeWidgetItem(["No primary failures found", "", ""])
            self.failure_tree.addTopLevelItem(empty)

    def _failure_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        occurrence = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(occurrence, FailureOccurrence):
            return
        try:
            self._current_occurrence = self._occurrences.index(occurrence)
        except ValueError:
            self._current_occurrence = -1
        self.failure_selected.emit(occurrence)

    def _move_failure(self, direction: int) -> None:
        if not self._occurrences:
            return
        self._current_occurrence = (self._current_occurrence + direction) % len(
            self._occurrences
        )
        self.failure_selected.emit(self._occurrences[self._current_occurrence])


def _numeric_leaves(
    value: Any, path: tuple[str | int, ...] = ()
) -> Iterable[tuple[str, float]]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        yield _format_path(path), float(value)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _numeric_leaves(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _numeric_leaves(child, (*path, index))


def _format_path(path: tuple[str | int, ...]) -> str:
    result = ""
    for segment in path:
        if isinstance(segment, int):
            result += f"[{segment}]"
        else:
            result += ("." if result else "") + segment
    return result or "value"


def _exception_parts(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        return "Failure", str(value) if value is not None else "No error message"
    exception_type, separator, message = value.partition(": ")
    if separator and exception_type.endswith(("Error", "Exception")):
        return exception_type, message
    return "Failure", value


def _component_counts(counter: Mapping[str, int]) -> tuple[ComponentCount, ...]:
    return tuple(
        ComponentCount(component, count)
        for component, count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0].casefold())
        )
    )
