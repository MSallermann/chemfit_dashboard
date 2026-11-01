# ChemFit Dashboard architecture

## Overview

The dashboard is a Qt desktop application around one central data boundary:
`ContextDocument`. A source discovers serialized evaluations as lightweight
`RecordRef` objects, then loads and indexes a document only when a feature needs
it. Feature modules consume documents directly or convert them into small,
immutable result objects before updating Qt widgets.

`MainWindow` is the application coordinator. It owns the open collection,
current document, cross-view selection, history, collection order, and
asynchronous request generations. It does not parse JSON, calculate graph
layouts, rank search hits, or implement extraction formats. Panels own their
presentation state and communicate with `MainWindow` through Qt signals.

```text
file / folder / remote URI
          |
          v
 JsonFileSource.discover -> RecordRef -> EvaluationListModel
          |                                  |
          |                           evaluation selected
          v                                  v
 JsonFileSource.load --------------> ContextDocument
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

- `cli.py`, `app.py`, and `__main__.py`: command-line parsing, Qt startup, and
  signal-aware event-loop shutdown.
- `main_window.py`: widget assembly and cross-feature coordination. Start here
  for actions that affect more than one panel.
- `activity.py`: named background-operation tokens, progress state, and the
  expandable status footer.
- `appearance.py`: color palettes, persistent appearance settings, and the
  application stylesheet.
- `ui_components.py`: shared action rows, muted labels, panel layouts,
  detachable-panel controls, and interaction constants. Repeated GUI roles
  belong here rather than in feature-local widget factories.
- `chart_style.py`: shared Qt Charts palette, axis, grid, and legend policy.
- `workers.py`, `traversal.py`: Qt thread-pool callbacks, cooperative
  cancellation, and consistent incremental collection loading.

### Data and identity

- `source.py`: discovery, remote GIO materialization, JSON loading, document
  caching, and `ContextDocument`'s indexed ChemFit hierarchy.
- `models.py`: lazy Qt models for records, context nodes, and nested JSON.
- `semantic.py`: component trails that resolve a logical component when integer
  node paths differ between evaluations.

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

1. `MainWindow.add_locations` submits `ContextSource.discover` to `TaskRunner`.
   Discovery is deliberately non-recursive: a directory contributes only its
   immediate JSON files.
2. `EvaluationListModel` receives `RecordRef` objects and builds the folder/file
   tree. The model also owns the visible collection order.
3. Selecting a record increments the document-load generation, clears the old
   views, and submits `JsonFileSource.load` to a worker thread.
4. `JsonFileSource` deduplicates concurrent loads, decodes JSON, and creates a
   `ContextDocument`. The document indexes structural node paths and summaries;
   full `NodeDetails` are produced lazily.
5. The GUI-thread completion callback first checks its captured generation. If
   it is stale, it does nothing. Otherwise it installs the document in
   `ContextTreeModel`, Data Flow, search availability, and the selection logic.
6. `ContextTreeModel` creates child Qt items in batches. Selecting one asks the
   document for `NodeDetails`, updates `Inspector`, and selects the same path in
   Data Flow.

ChemFit 4.0.0 serializes executed children in `children` and declared structure
in `meta["chemfit.structure"]`. `ContextDocument` merges both: an unexecuted
declaration still appears as a node with `recorded=False`. Status handling for
skipped, failed, incomplete, and missing components originates here and is
preserved by analysis and extraction.

## Selection and navigation

`MainWindow` is the authority for the current record and node path.

- Tree selection updates Inspector and Data Flow.
- Data Flow selection calls back into the same tree-path selection routine.
- Search hits, failure rows, and plot points may target another evaluation.
  They first select that record and store one pending navigation object. The
  requested node/value is focused only after the matching document load wins
  its generation check.
- `_sticky_node_path` and its semantic `NodeTrail` retain the logical component
  while moving between evaluations. A partial resolution selects the deepest
  available ancestor instead of silently choosing another component.
- Back/forward history stores `(record_id, node_path)` entries. A replay flag
  prevents the resulting programmatic selection from creating duplicate history.

The `*_selected` methods in `main_window.py` are the best entry points when
debugging unexpected cross-view navigation.

## Feature data paths

- **Data Flow:** `ContextDocument` -> `build_data_flow` -> `data_flow_layout` ->
  `DataFlowView`. Large layouts run on a worker; Qt scene items are always
  created on the GUI thread in batches.
- **Search:** current-evaluation search uses `ContextSearchCache` and a Qt worker
  thread. Whole-collection search sends serializable records/options to a
  process pool, then deterministically merges per-chunk top results. The UI
  receives only `SearchHit` objects.
- **Analysis:** `iter_collection` -> `analyze_collection` ->
  `CollectionSummary` -> `CollectionAnalysisPanel`.
- **Plotting/order:** Inspector emits a `NumberSelector`; one traversal builds
  `CollectionSeries`. Plotting, ordering, and point-to-record navigation share
  that selector rather than storing raw widget indexes.
- **Extraction:** Inspector emits a `FieldSelector`; semantic trails resolve it
  per document. Object flattening and per-selector array depth both derive
  output columns through `column_paths_for_value`, shared by preview and CSV
  export. They never multiply evaluation rows. Preview reads at most 200 rows,
  while export streams the complete collection and does not depend on the
  preview model.

## Background work and stale results

Qt widgets are touched only on the GUI thread. `TaskRunner` runs ordinary Python
callables on Qt's global thread pool and routes their result or traceback back
through signals. Each feature has a monotonically increasing generation counter
in `MainWindow`. A callback must match the current generation before changing
the UI; cancellation alone is insufficient because already-running work can
finish after a newer request.

The shared Qt thread count and the separate whole-collection search process count
are persisted independently and exposed under **Settings > Workers**. Resizing the
search pool replaces its persistent executor and invalidates any active search.

`ActivityStatus` owns named `CancellationToken` objects. Reusing an operation
name cancels its predecessor. Workers using `iter_collection` stop between
records; search and graph layout use their own periodic checks. Closing a
collection increments every relevant generation and cancels outstanding work.

Whole-collection search uses processes because JSON indexing and fuzzy matching
are Python CPU work and can monopolize the GIL. The pool uses the portable
`spawn` context, keeps at most the worker count of chunks in flight, and checks
cancellation every 50 ms while coordinating futures. Running process calls
cannot be forcibly interrupted; their results are ignored after cancellation.

## State and cache ownership

| Owner | Mutable state |
| --- | --- |
| `MainWindow` | Open records, current document, selection/history, collection order, session state, feature generations |
| `ActivityStatus` | Operation tokens, labels, progress, footer rows |
| `JsonFileSource` | Document LRU, in-flight load deduplication, temporary remote-file directory |
| `ContextDocument` | Immutable node index, small lazy details LRU, active label key |
| Feature panels | Widget-local options, selected rows/series, render state |
| `ContextSearchCache` | Recent current-evaluation indexes |
| Search worker process | Its own bounded document and index caches |

The important retained safeguards are:

- The source cache is bounded by 64 documents and an estimated 256 MiB, and
  invalidates entries when modification time or file size changes. It prevents
  repeated parsing while avoiding unbounded retention.
- Each document retains only 64 computed `NodeDetails`; hierarchy summaries are
  indexed once because every major feature needs them.
- Local search keeps 32 bounded indexes. Each process worker keeps at most eight
  documents/64 MiB and 32 indexes/40,000 entries. Process caches are independent
  of the GUI source cache by design.
- Context/JSON models fetch 200 children at a time. Graphs above 180 nodes lay
  out off-thread and graphs above 800 nodes render in event-loop batches. Data
  Flow refuses more than 5,000 components. These limits prevent observed GUI
  freezes rather than optimizing a marginal benchmark case.
- Plot rendering samples at most 4,000 displayed points per series, while export
  still uses complete data. Extraction preview is similarly bounded and does
  not change export semantics.

## Where to make common changes

- Add an inspector action or value view: `inspector.py`; define a semantic
  selector in `collection_plot.py` or `extraction.py` if it applies collection-wide.
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
- Change cross-view navigation, background task startup, saved session state, or
  collection ordering: `main_window.py`.

For a first code-reading pass, read `source.py`, then `models.py`, then the
`MainWindow` class docstring and its discovery/load/selection methods. After
that, follow only the feature module relevant to the change. This avoids having
to understand plotting, search, extraction, and Data Flow simultaneously.
