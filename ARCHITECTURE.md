# ChemFit Dashboard architecture

## Overview

The dashboard is a Qt desktop application around one central data boundary:
`ContextDocument`. A source discovers serialized evaluations as lightweight
`RecordRef` objects, then loads and indexes a document only when a feature needs
it. Feature modules consume documents directly or convert them into small,
immutable result objects before updating Qt widgets.

`DashboardCoordinator` is the authoritative runtime state. It owns the source,
ordered collection, current document/context/field selection, component-label
setting, and shared task runner. `MainWindow` assembles panels and controllers
and connects cross-feature requests; panels retain only feature-local
presentation state.

```text
local file / folder
          |
          v
 JsonFileSource.discover -> RecordRef -> DashboardCoordinator
                                              |
                           +------------------+------------------+
                           |                                     |
                  EvaluationListModel                    record selected
                                                                 |
                                                                 v
 JsonFileSource.load -------------------------------> ContextDocument
                                           |
                    +----------------------+----------------------+
                    |                      |                      |
             ContextTreeModel        feature processors      search indexes
                    |                      |                      |
                Inspector       analysis / flow / plot /      Search panel
                                 extraction result objects
```

## Module map

### Application shell

- `cli.py`, `app.py`, and `__main__.py`: command-line parsing, optional startup
  extraction-configuration loading, Qt startup, and signal-aware event-loop
  shutdown.
- `main_window.py`: widget assembly and cross-feature coordination. Start here
  for actions that affect more than one panel.
- `coordinator.py`, `ordered_record_collection.py`: authoritative collection,
  ordering, selection, and asynchronous load/sort requests.
- `activity.py`: task-lifecycle presentation, progress state, and the expandable
  status footer.
- `appearance.py`: color palettes, persistent appearance settings, and the
  application stylesheet.
- `ui_components.py`: shared action rows, muted labels, panel layouts,
  detachable-panel controls, and interaction constants. Repeated GUI roles
  belong here rather than in feature-local widget factories.
- `chart_style.py`: shared Qt Charts palette, axis, grid, and legend policy.
- `workers.py`, `traversal.py`: shared thread/process executors, GUI-thread
  lifecycle callbacks, progress transport, cooperative cancellation, and
  consistent incremental collection loading.

### Data and identity

- `source.py`: local discovery, JSON loading, and bounded document caching.
- `context_document.py`: indexed ChemFit hierarchy, lazy node details, and
  lightweight `RecordRef` metadata.
- `models.py`: lazy Qt models for records, context nodes, and nested JSON.
- `semantic.py`: low-level component-trail helpers. Current plot and extraction
  selectors use direct integer node paths rather than these helpers.

### Features

- `inspector.py`: fields, metadata, structure, and raw-value views plus actions
  that create plot or extraction selectors.
- `data_flow.py`: converts one `chemfit.structure` declaration into graph data.
  `graph_layout.py` computes positions without Qt; `graph_view.py` renders and
  navigates the scene.
- `search_core.py`: bounded document indexes, matching, ranking, and the local
  index LRU. `process_search.py` coordinates whole-collection search in a
  persistent spawn-based process pool. `context_search.py` renders hits.
- `collection_analysis.py`: pure collection summarization and its overview UI.
- `collection_plot.py`: numeric selectors, pure series construction, chart,
  histogram/statistics modes, zoom, and exports.
- `extraction.py`: selectors, missing-value semantics, configuration format,
  and streaming CSV/JSONL export. `extraction_ui.py` edits configurations and
  shows a bounded preview. `extraction_cli.py` reuses the same core headlessly.
- `example.py`: example ChemFit context generation used by documentation and
  tests; it is not part of the runtime architecture.

## From JSON to the inspector

1. `DashboardCoordinator.load_collection` submits `ContextSource.discover` as a
   latest-only task. Discovery is non-recursive: a directory contributes only
   its immediate JSON files.
2. The coordinator commits discovered `RecordRef` objects to
   `OrderedRecordCollection`. `EvaluationListModel` observes collection/order
   signals and rebuilds its folder/file presentation.
3. Selecting a record submits `JsonFileSource.load` as another latest-only
   task; superseded results are invalidated before they can update state.
4. `JsonFileSource` decodes JSON and creates or reuses a bounded cached
   `ContextDocument`. The document indexes structural node paths and summaries;
   full `NodeDetails` are produced lazily.
5. A successful GUI-thread callback commits the document to the coordinator,
   which emits selection signals consumed by the hierarchy, Inspector, Data
   Flow, search, plot, and extraction panels.
6. `ContextTreeModel` creates child Qt items in batches. Selecting one asks the
   document for `NodeDetails` and routes the path back through the coordinator.

When `-c`/`--extraction-config` is supplied, `cli.py` validates the file with
the same `load_config` function used by the GUI and headless extractor. `app.py`
passes the resulting immutable configuration into `MainWindow`, which installs
it only after panel signals and tab badges exist. Collection discovery remains
asynchronous; the extraction panel refreshes its preview when the collection is
committed.

ChemFit 4.0.0 serializes executed children in `children` and declared structure
in `meta["chemfit.structure"]`. `ContextDocument` merges both: an unexecuted
declaration still appears as a node with `recorded=False`. Status handling for
skipped, failed, incomplete, and missing components originates here and is
preserved by analysis and extraction.

## Selection and navigation

`DashboardCoordinator` is the authority for the current record, context path,
and selected field path.

- Tree selection updates Inspector and Data Flow.
- Data Flow selection calls back into the same tree-path selection routine.
- Search hits, failure rows, and plot points may target another evaluation.
  They first select that record and store one pending navigation object. The
  requested node/value is focused only after the matching document load wins
  its generation check.
- `DashboardCoordinator._preferred_context_path` retains the selected integer
  node path while moving between evaluations. If the exact path is unavailable,
  selection falls back to its deepest available ancestor.
- Back/forward history stores `(record_id, node_path)` entries. A replay flag
  prevents the resulting programmatic selection from creating duplicate history.

The selection methods and signals in `coordinator.py` are the best entry points
when debugging unexpected cross-view navigation. `main_window.py` handles only
cross-panel destinations such as opening a plot point or extraction row.

## Feature data paths

- **Data Flow:** `ContextDocument` -> `build_data_flow` -> `data_flow_layout` ->
  `DataFlowView`. Large layouts run on a worker; Qt scene items are always
  created on the GUI thread in batches.
- **Search:** current-evaluation search uses `ContextSearchCache` and a
  background worker thread. Whole-collection search sends serializable records/options to a
  process pool, then deterministically merges per-chunk top results. The UI
  receives only `SearchHit` objects.
- **Analysis:** `iter_collection` -> `analyze_collection` ->
  `CollectionSummary` -> `CollectionAnalysisPanel`.
- **Plotting/order:** Inspector emits a `FieldSelector`; one traversal builds
  `CollectionSeries`. Plotting, ordering, and point-to-record navigation share
  that selector rather than storing raw widget indexes.
- **Extraction:** Inspector emits a `(FieldSelector, OutputDescriptor)` pair.
  `FieldSelector` stores a direct component `node_path` and raw-JSON
  `value_path`; missing paths become explicit extraction statuses.
  `OutputDescriptor` owns naming and CSV shaping. Object flattening and
  per-selector array depth both derive output columns through
  `column_paths_for_value`, shared by preview and CSV export. They never
  multiply evaluation rows. Preview reads at most 200 rows, while export
  streams the complete collection and does not depend on the preview model.
  GUI exports write a temporary sibling and atomically replace the requested
  destination only after success. Local `JsonFileSource` collections with at
  least 200 records use the spawned process pool; smaller or source-specific
  exports use the thread pool.

## Background work and stale results

Qt widgets are touched only on the GUI thread. `TaskRunner` runs ordinary Python
callables on a shared `ThreadPoolExecutor` or a lazily created, spawn-based
`ProcessPoolExecutor`, then routes lifecycle events, results, and tracebacks back
through Qt signals. `submit_latest` invalidates the previous task for one key so
its eventual callback cannot mutate current state. `GenerationTracker` provides
the same freshness rule for Data Flow's incremental layout/render generations.

The shared thread count and process count are persisted independently and exposed
under **Settings > Workers**. Whole-collection search and large local extraction
exports share the process executor. Resizing it replaces the persistent executor
and invalidates any active search.

`ActivityStatus` is presentation-only: it consumes labeled `TaskEvent` lifecycle
updates and renders concurrent operations with indeterminate or determinate
progress. `TaskRunner` owns task cancellation. Keyed submissions invalidate and
cooperatively cancel their predecessor; workers using `iter_collection` stop
between records, while search and graph layout use their own periodic checks.
Closing a collection invalidates all observed request families and cancels the
corresponding outstanding work.

Thread tasks report progress directly through the Qt bridge. Process tasks use a
small per-task shared-memory state containing a sequence-protected progress pair
and cancellation flag. The GUI polls that state every 40 ms, so progress updates
are coalesced instead of queueing one cross-process message per record. Only the
shared-memory name is submitted with the process job; no live pipe or socket
handle is serialized through the process pool. The state is closed and unlinked
when the task finishes or the runner shuts down.

Whole-collection search uses processes because JSON indexing and fuzzy matching
are Python CPU work and can monopolize the GIL. The pool uses the portable
`spawn` context, keeps at most the worker count of chunks in flight, and checks
cancellation every 50 ms while coordinating futures. Running process calls
cannot be forcibly interrupted; their results are ignored after cancellation.

Large local exports use one job in the same pool because parsing and
serialization would otherwise contend with the GUI under the GIL. The worker
reconstructs a `JsonFileSource` from serializable `RecordRef` values and streams
directly to the temporary output. Flattened/expanded CSV uses two traversals—one
to discover columns and one to write—while ordinary CSV and JSONL use one.

## State and cache ownership

| Owner | Mutable state |
| --- | --- |
| `DashboardCoordinator` | Source, ordered records, current document/context/field, label key, shared task runner |
| `MainWindow` | Panel assembly, menus, detachable views, and cross-panel routing |
| `ActivityStatus` | Visible task labels, progress, and footer rows |
| `JsonFileSource` | Bounded document LRU and its cache lock |
| `ContextDocument` | Immutable node index, small lazy details LRU, active label key |
| Feature panels | Widget-local options, selected rows/series, render state |
| `ContextSearchCache` | Recent current-evaluation indexes |
| Process worker | Export-local source cache; bounded search document/index caches when serving search |

State follows three rules throughout the GUI:

1. Values that share a lifecycle or invariant live behind one owner. For
   example, `ContextNavigationHistory` owns replay state,
   `DashboardSessionController` owns persisted collection/layout state, and
   `_RenderedPointLookup` owns the plot's rendered-point indexes.
2. Repeated widget lifecycles are controllers, not copied fields and methods.
   `DetachablePanelController` owns a detached dialog, placeholder, and restore
   index for both Data Flow and Plot.
3. Comments explain ownership, invariants, asynchronous boundaries, and Qt
   workarounds. Field names and type annotations carry ordinary value meaning;
   comments should not merely repeat an assignment.

The important retained safeguards are:

- The source cache is bounded by 64 documents and an estimated 256 MiB. It
  prevents repeated parsing while avoiding unbounded retention.
- Each document retains only 256 computed `NodeDetails`; hierarchy summaries are
  indexed once because every major feature needs them.
- Local search keeps 32 bounded indexes. Each process worker keeps at most eight
  documents/64 MiB and 32 indexes/40,000 entries. Process caches are independent
  of the GUI source cache by design.
- Context/JSON models fetch 200 children at a time. Graphs above 180 nodes lay
  out off-thread and graphs above 800 nodes render in event-loop batches. Data
  Flow refuses more than 5,000 components. These limits prevent observed GUI
  freezes rather than optimizing a marginal benchmark case.
- Plot rendering and exported plot images use at most 4,000 displayed points per
  series. Extraction preview is independently bounded to 200 rows, while data
  export still streams the complete collection.
- GUI extraction export is streaming and atomic. The process path transfers
  only record metadata/configuration plus shared progress state; extracted rows
  are written in the worker and are never copied back into the GUI process.

## Where to make common changes

- Add an inspector action or value view: `inspector.py`; define a path-based
  selector in `collection_plot.py` or `extraction.py` if it applies
  collection-wide.
- Change context parsing/status rules: `source.py`, then test against serialized
  ChemFit 4.0.0 fixtures.
- Change graph meaning: `data_flow.py`; change placement/routing:
  `graph_layout.py`; change painting/interaction: `graph_view.py`.
- Change search matching or ranking: `search_core.py`; change parallelism or
  cancellation: `process_search.py`; change result presentation:
  `context_search.py`.
- Add a collection statistic: calculate it in `analyze_collection` and render it
  in `CollectionAnalysisPanel`.
- Add a plot mode or export: `CollectionPlotPanel` in `collection_plot.py`.
- Add an extraction option: extend `FieldSelector`/`ExtractionConfig` and core
  behavior in `extraction.py`, then expose it in `extraction_ui.py`. Keep the
  headless CLI on the same core path.
- Change task lifecycle, thread/process execution, progress IPC, or cooperative
  cancellation: `workers.py`; keep `activity.py` limited to presentation.
- Change cross-view navigation, background task startup, saved session state, or
  collection ordering: `main_window.py`.

For a first code-reading pass, read `context_document.py`, `source.py`, and
`coordinator.py`, then `models.py` and `main_window.py`. After that, follow only
the feature module relevant to the change. This avoids having to understand
plotting, search, extraction, and Data Flow simultaneously.
