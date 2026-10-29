<p align="center">
  <img src="res/chemfit_logo_portable.svg" alt="ChemFit logo" width="180">
</p>

# ChemFit Dashboard

A desktop application for exploring and visualizing evaluation results from [ChemFit](https://github.com/MSallermann/chemfit).

ChemFit can produce detailed records of an optimization, including intermediate results, component metadata, and errors. This dashboard makes those results much easier to inspect without digging through JSON by hand.

Built with Python and PySide6, the dashboard runs locally and works directly with serialized evaluation contexts from ChemFit 4.0.0. It is entirely read-only, so your original results are never modified.

![ChemFit Dashboard screenshot](res/screenshot.png)

## Features

- **Explore evaluations:** Browse individual evaluation contexts or whole collections of results. Inspect nested components, inputs, outputs, metadata, and errors.
- **Visualize component graphs:** View the structure of an evaluation as an interactive graph, including dependencies and parent-child relationships.
- **Track results across evaluations:** Follow the same component through multiple evaluations, even when parts of the execution tree are skipped or fail.
- **Plot numerical data:** Plot any numerical value across a collection of evaluations, compare multiple quantities, normalize curves, and export plots as PNG, SVG, or CSV.
- **Search contexts:** Search components, metadata, and values using plain text or regular expressions, either within one evaluation or across a whole collection.
- **Investigate failures:** Get an overview of failed or incomplete evaluations and jump directly to the relevant components.
- **Extract arbitrary data:** Add scalar or structured inspector values and complete components to a reusable extraction, preview them, and stream the complete collection to CSV or JSONL.
- **Navigate large graphs:** Structural components start collapsed, can be expanded incrementally, and use a deterministic dependency-aware layout.

The dashboard also includes multiple color themes, adjustable font sizes, and keyboard navigation.

## Installation

Requires Python 3.10 or newer.

```bash
git clone https://github.com/MSallermann/chemfit_dashboard.git
cd chemfit_dashboard
pip install -e .
```

## Usage

Start the dashboard with:

```bash
chemfit-dashboard
```

You can also open a JSON evaluation context or an entire directory directly:

```bash
chemfit-dashboard path/to/context.json
chemfit-dashboard path/to/results/
```

To explore the included examples:

```bash
chemfit-dashboard examples/step_0_ctx_0.json
chemfit-dashboard examples/context_collection/
```

Multiple files and directories can be passed at once. Directories are scanned recursively, and additional files can be opened from within the application.

## Data extraction

Select any value in the Overview, Metadata, Structure, or Raw fields inspector,
then choose **Add selected field to extraction**. **Add component to extraction**
captures the component's fields, metadata, and declared structure as one nested
value. Selections remain active while moving between evaluations.

The **Data Extraction** workspace supports editable column names, ordering,
removal, dictionary flattening, single-array expansion, a model-backed preview,
and configuration save/load. The preview is deliberately limited to 200 rows;
CSV and JSONL exports always traverse the complete collection incrementally.

- CSV stores dictionaries and lists as JSON strings. Optional flattening uses
  JSON Pointer escaping (`~0` and `~1`) so keys containing separators cannot
  collide. Companion `__status` columns distinguish unavailable data.
- JSONL preserves structured values under `values` and records an independent
  status for every selection under `status`.
- Only one selected array may expand into long-format rows. Multiple expanded
  arrays are rejected instead of producing an implicit Cartesian product.

Statuses include `available`, `explicit_null`, `missing_component`,
`declared_unexecuted`, `skipped_component`, `missing_field`,
`serialized_exception`, and `unreadable_evaluation`.

### Configuration format

Configurations are human-readable, versioned JSON objects:

```json
{
  "version": 1,
  "include_identifiers": true,
  "include_status_columns": true,
  "selectors": [
    {
      "component": {
        "preferred_path": [1, 0],
        "trail": [
          {"name": "simulation", "occurrence": 0, "declared_index": 1, "declared": true}
        ]
      },
      "section": "fields",
      "path": ["output", "energy"],
      "column": "energy",
      "flatten": false,
      "expand_array": false
    }
  ]
}
```

The semantic trail, rather than raw child indices alone, lets a configuration
resolve repeated and nested declared components across compatible collections.
Unresolved fields retain an explicit unavailable status and are never silently
matched to an unrelated display name.

### Standalone Python API

The extraction core does not import Qt widgets and requires neither Pandas nor
NumPy:

```python
from chemfit_dashboard import FieldSelector, extract_fields
from chemfit_dashboard.source import JsonFileSource

source = JsonFileSource(cache_capacity=32, cache_memory_budget=128 * 1024**2)
records = source.discover(["results/"])
selectors = [
    FieldSelector((), "fields", ("loss",), "loss"),
    FieldSelector((), "metadata", ("dataset",), "dataset"),
]

for row in extract_fields(source, records, selectors):
    print(row.filename, row.values)
```

Use `selector_for_document` from `chemfit_dashboard.extraction` when creating a
selector from a concrete component; it attaches the reusable semantic identity.
`export_csv`, `export_jsonl`, `save_config`, and `load_config` are also public.

## Architecture and scaling

Core JSON loading, semantic component resolution, collection traversal,
extraction, serialization, and graph layout are independent of Qt widgets.
Collection operations load one document at a time through a shared cancellable
traversal. Plotting extracts all selected series in one pass, collection search
retains a bounded global top-K, and parsed documents live in a stat-aware LRU
bounded by entry count and estimated bytes. Search indexes are separately
bounded and contain extracted entries rather than references to documents.

Graph layout is calculated from ordinary Python data structures in
`graph_layout.py`; `graph_view.py` only manages visibility, interaction, and Qt
graphics items. Collapsing nested structures keeps the overview readable, while
selecting a hidden component automatically expands its ancestors.

Generic mapping and mixed mapping/list path operations come from
`pydictnest>=0.3.0`. When developing both repositories before that version is
released, install the corresponding checkout first:

```bash
pip install -e ../pydictnest
pip install -e .
```

## Benchmarks

The opt-in benchmark creates a reproducible synthetic collection and measures
discovery, parsing/indexing, analysis, bounded search, multi-field extraction,
streaming CSV/JSONL, graph layout, graph rendering, and Python peak memory:

```bash
PYTHONPATH=src python benchmark/benchmark.py --evaluations 1000 --graph-nodes 2000
```

Larger collection runs can use `--evaluations 10000` or `100000`. Benchmarks
are intentionally excluded from the unit-test suite. Results depend strongly
on storage, Python, Qt, and whether filesystem data is cached; compare revisions
using the same command and environment. No pre-change measurement was preserved
for this development checkout, so the project does not claim an unsupported
before/after speedup. See [BENCHMARKS.md](BENCHMARKS.md) for the latest recorded
1,000-evaluation and 2,000-component result.

## Development

```bash
pytest -q
ruff check src tests benchmark
pyright
```

The tests use real ChemFit 4.0.0 contexts where semantic compatibility matters
and small synthetic inputs for cache, cancellation, export, and graph edge cases.
