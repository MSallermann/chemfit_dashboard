# ChemFit Context Dashboard

A read-only native viewer for JSON evaluation contexts produced by ChemFit 4.0.0.
It understands the current recursive `EvaluateContext.to_summary()` shape and the
root-level shape written by `CheckpointBestParameters`, while retaining generic
views for unfamiliar or partial JSON.

## Install

This package is the dashboard itself. Installing it always installs PySide6;
there is no separate headless or core-only installation.

```bash
python -m pip install -e .
```

For development and tests:

```bash
python -m pip install -e ".[test]"
```

The development command additionally installs `pytest` and `chemfit>=4.0.0` for
the test suite. Compatibility tests construct and serialize contexts with that
installed ChemFit package rather than maintaining a dashboard-owned copy of the
context schema.

## Run

Open the app with no initial files:

```bash
chemfit-dashboard
```

To open the included context immediately:

```bash
chemfit-dashboard examples/step_0_ctx_0.json
```

To exercise folder navigation with six evaluations across three optimizer steps:

```bash
chemfit-dashboard examples/context_collection/
```

The evaluations contain a 34-context, five-level hierarchy with nested parameter
preparation, parallel pair-potential, thermodynamic, and structural models, and a
multi-term loss pipeline. The final evaluation (`step_2_ctx_1.json`) intentionally
fails in its structure-loss component, demonstrating the contexts and error output
captured from an unsuccessful evaluation. Solid hierarchy edges and dashed
dependencies are both visible in the **Graph** tab. The files were produced
through ChemFit's real `SaveMetaData` callback. Regenerate them against the installed ChemFit version with
`python examples/create_example_context.py` and
`python examples/create_example_collection.py`.

Open one or more JSON files or recursively scan directories at startup:

```bash
chemfit-dashboard results/ step_20_ctx_0.json
```

Every argument is an independent location and all supplied locations are loaded.
For example, use `chemfit-dashboard runs/initial_params_01/meta_data` to load only
that directory; `context_collection` is a directory name in the examples above,
not a CLI subcommand.

Directories appear as a folder tree in the evaluation pane. Use the arrow keys to
move between context files and expand or collapse folders; selecting a file loads
its indexed context hierarchy. A selected child component follows across
evaluations using ChemFit's declared child names, occurrences, and structural
indices. Runtime output, error text, status, and static metadata are deliberately
excluded from identity, so the same component remains selected whether it returns
a number or raises an exception. A skipped structural component still has its
declared slot, but its runtime descendants may never be created. In that case the
dashboard selects the deepest available declaration or ancestor while retaining
the full target, then restores it when a later evaluation contains that subtree.
Files use natural alphanumeric filename order, so
`step_2` precedes `step_10`. To order a collection by data instead, select a
numeric inspector value and choose **Order collection by selected number**;
contexts without that value remain visible at the end. **Reverse order** toggles
ascending and descending order while still keeping missing numeric values last.
**Filename order** restores the default. Changing the collection order also
rebuilds an already-open plot, so its context indices and connecting lines always
follow the order shown in the evaluation pane.

To plot an intermediate value across the loaded collection, select any numeric
leaf in a context's Overview, Metadata, Structure, or Raw fields and choose
**Plot selected number across collection**. The Plot tab follows the same semantic
component through evaluations even when skipped branches change runtime
availability. Missing, failed, and nonnumeric values are omitted and reported
rather than being connected to neighboring points. Use **Add selected number to
current plot** to compare several fields; individual series can be removed or
normalized independently to a 0-1 range. Plot controls provide a base-10
logarithmic Y-axis (with nonpositive values explicitly omitted) and reset zooming
after interactive rubber-band navigation. **Export CSV** opens a column editor
for the context index, filename, record ID, and every plotted series before writing
the file.
Column names must be non-empty and unique. The chart can also be exported as PNG
or SVG.

Plot and collection selection are linked in both directions. Clicking a plot
point opens that evaluation, while selecting an evaluation highlights all of its
visible points in the chart.

The header's **Color** selector provides Midnight, Light, Nord, and High Contrast
schemes. Scheme changes include custom graph and plot colors, not only standard
widgets. The selected scheme and the A-/A+ font scale are restored on the next
launch from `settings.ini` in Qt's platform-standard per-user application config
directory. The status bar displays the exact settings path after changing the
scheme, making the configuration easy to locate and copy between installations.

The top-level **Collection overview** workspace is built in the background and
summarizes successful, failed, partial, and unreadable evaluations; total context
nodes; numeric root field ranges and means; and the most frequently skipped or
unrecorded components. Its failure navigator groups primary component failures by
component, exception type, and message. Expand a group to see affected
evaluations, or use **Previous** and **Next** when multiple failures are available
to move through them and jump directly to the failing context.

The hierarchy search supports fuzzy ranking, case-insensitive exact substring
matching, and regular expressions across component breadcrumbs, nested field
paths, and field values throughout the selected evaluation. Matching text is
highlighted in each result. Results are globally ordered by relevance and display
their match score; each field retains its owning context breadcrumb so repeated
names remain distinguishable. **Include keys** and **Include values** independently
control those search dimensions while component hierarchy remains searchable;
the scope selector searches either the current evaluation or the whole loaded
collection. Collection indexing and matching run in the background, and stale
queries are discarded as the search text changes. Selecting a result switches to
its source evaluation, opens its context, and focuses the exact value in the
inspector.

The hierarchy pane offers both a lazy tree and a deferred graph. The graph shows
solid parent/child edges and dashed dependency edges derived directly from
`meta["chemfit.structure"]`; selecting a graph node opens the same detail inspector.
Graph nodes show the structural component name and, when present, a separate
static `meta["name"]` label.

The toolbar can add files or directories later. Files are parsed and indexed on a
worker thread. Each parsed document is cached for the session and automatically
reloaded only if its size or modification time changes.
Use the header's **A-** and **A+** controls to scale typography throughout the
interface.

## Supported data

ChemFit 4.0.0 recursive summaries contain `input`, `output`, `meta`, and ordered
`children`. Structural components additionally place their immediate declaration
in `meta["chemfit.structure"]`, including child names and indices, input sources,
dependencies, and output sources. The dashboard pairs those declarations with the
runtime child contexts and also shows declaration-only children when execution did
not produce a context.

Checkpoint files with root-level `step`, `loss`, `parameters`, `quantities`,
`meta`, and `children` are recognized without converting them to another schema.
Unknown fields remain available in the generic field, metadata, and raw views.

## Test

```bash
QT_QPA_PLATFORM=offscreen pytest
```
