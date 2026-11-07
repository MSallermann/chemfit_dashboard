from collections.abc import Iterable, Iterator, Sequence
from typing import overload

from chemfit_dashboard.context_document import (
    RecordRef,
    alphanumeric_key,
)
from chemfit_dashboard.workers import TaskRunner


class OrderedRecordCollection(Sequence[RecordRef]):
    """
    An ordered collection of references to serialized evaluation records.

    Each reference is stored exactly once, indexed by its stable ID.
    The collection maintains a natural ordering and a separate active
    ordering, both represented as sequences of IDs.

    The active ordering determines iteration and indexed access.
    Changing the order does not modify collection membership.

    This class is independent of Qt, ChemFit evaluation data, and the
    mechanisms used to compute sorting criteria. It only manages
    references and their ordering.

    Mutations are coordinated externally by DashboardState, which is
    responsible for emitting the corresponding change signals.
    """

    def __init__(self) -> None:
        """Initialize the collection."""
        # Authoritative mapping from stable IDs to context references.
        self._refs: dict[str, RecordRef] = {}

        # Default order, typically natural alphanumeric filename order.
        # Independent of any user-selected sorting criterion.
        self._natural_order: tuple[str, ...] = ()
        self._natural_order_label: str = "natural"

        # Currently active order. Iteration and indexing follow this.
        # Must contain each ID in _refs exactly once.
        self._order: tuple[str, ...] = ()
        self._order_label: str | None = None
        self._order_ascending: bool | None = None

        self._task_runner = TaskRunner()

    def replace(self, refs: Iterable[RecordRef]) -> None:
        """
        Replace all records in the collection.

        The supplied references become the complete collection, replacing
        all previously stored records. Both the natural and active ordering
        are reset to the order in which the references are supplied.

        Each reference must have a unique ID. Duplicate IDs raise a
        ValueError, leaving the existing collection unchanged.

        The caller is responsible for supplying records in their desired
        natural order, typically the alphanumeric filename order established
        by ContextSource.discover().

        This method does not emit signals. DashboardState is responsible
        for notifying observers after the replacement has been committed.
        """
        new_refs = {}

        for ref in refs:
            if ref.id in new_refs:
                msg = f"Duplicate record ID: {ref.id}"
                raise ValueError(msg)
            new_refs[ref.id] = ref

        self._refs = new_refs
        self._natural_order = tuple(new_refs)
        self._order = self._natural_order
        self._order_label = self._natural_order_label
        self._order_ascending = True

    def extend(self, refs: Iterable[RecordRef]) -> None:
        """
        Add new records or update existing records in the collection.

        References are identified by their stable IDs. An incoming reference
        with an existing ID replaces the stored reference without changing
        its position in the active ordering.

        Previously unknown records are appended to the active ordering,
        preserving the relative positions of all existing records. This
        behavior also applies when a custom ordering is active: extending
        the collection does not automatically repeat an expensive sort.

        The natural ordering is recomputed independently using alphanumeric
        filename ordering, including the parent directory and full path as
        tie-breakers.

        Duplicate IDs within the supplied iterable are allowed, with the
        last occurrence determining the stored reference. Each newly
        encountered ID is added to the active ordering only once.

        This method does not emit signals. DashboardState is responsible
        for notifying observers after the collection has been updated.
        """
        updated = dict(self._refs)
        added = []

        for ref in refs:
            if ref.id not in updated:
                added.append(ref.id)
            updated[ref.id] = ref

        natural_order = tuple(
            sorted(
                updated,
                key=lambda ref_id: (
                    alphanumeric_key(updated[ref_id].name),
                    str(updated[ref_id].path.parent).casefold(),
                    str(updated[ref_id].path),
                ),
            )
        )

        self._refs = updated
        self._order += tuple(added)
        self._natural_order = natural_order

    def set_order(self, ids: Sequence[str], label: str | None, ascending: bool) -> bool:
        """
        Replace the active ordering without modifying collection membership.

        The supplied sequence specifies the complete ordering of records
        using their stable IDs. Every record in the collection must appear
        exactly once.

        Missing, duplicated, or unknown IDs are rejected with a ValueError.
        Validation takes place before modifying the active ordering, so an
        invalid request leaves the collection unchanged.

        Only the active ordering is updated. The underlying references and
        their natural ordering remain unchanged, allowing the original
        filename ordering to be restored later.

        This method does not interpret sorting criteria or compute sort
        values. Those responsibilities belong to the caller, which may
        perform asynchronous processing before providing the final order.

        DashboardState is responsible for emitting collection_order_changed
        after a new ordering has been successfully committed.

        """
        new_order = tuple(ids)

        if (
            len(new_order) != len(self._refs)
            or len(set(new_order)) != len(new_order)
            or set(new_order) != self._refs.keys()
        ):
            msg = "Ordering must contain every collection record ID exactly once."
            raise ValueError(msg)

        self._order = new_order
        self._order_label = label
        self._order_ascending = ascending
        return True

    def reset_order(self) -> None:
        """
        Restore the collection's natural ordering.

        Replaces the active ordering with the stored natural ordering,
        typically corresponding to alphanumeric filename order.

        Collection membership and stored references remain unchanged.
        Any custom ordering previously installed through set_order() is
        discarded.

        The natural ordering includes records added through extend(), so
        resetting always restores the default order of the complete
        current collection.

        DashboardState is responsible for emitting collection_order_changed
        if the active ordering actually changes.
        """
        self._order = self._natural_order
        self._order_label = self._natural_order_label

    def invert_order(self) -> None:
        """Inverts the current order."""
        self.set_order(
            self._order[::-1],
            label=self._order_label,
            ascending=not self._order_ascending,
        )

    @property
    def natural_order(self) -> tuple[str, ...]:
        """Return the reference IDs in their default ordering."""
        return self._natural_order

    @property
    def order(self) -> tuple[str, ...]:
        """Return the reference IDs in their current ordering."""
        return self._order

    @property
    def order_label(self) -> str | None:
        """Return the label for the current ordering."""
        return self._order_label

    @property
    def ascending(self) -> bool | None:
        """Return if the orde is ascending."""
        return self._order_ascending

    def __len__(self) -> int:
        """Return the number of references in the collection."""
        return len(self._order)

    @overload
    def __getitem__(self, index: int) -> RecordRef: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RecordRef, ...]: ...

    def __getitem__(self, index: int | slice) -> RecordRef | tuple[RecordRef, ...]:
        """Access references in the active order."""
        if isinstance(index, slice):
            return tuple(self._refs[ref_id] for ref_id in self._order[index])
        return self._refs[self._order[index]]

    def __iter__(self) -> Iterator[RecordRef]:
        """Iterate over references in the active order."""
        return (self._refs[ref_id] for ref_id in self._order)
