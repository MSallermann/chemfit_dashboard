<p align="center">
  <img src="res/chemfit_logo_portable.svg" alt="ChemFit logo" width="500">
</p>

# ChemFit Dashboard

A desktop application for exploring, visualizing, and extracting evaluation data from [ChemFit](https://github.com/MSallermann/chemfit).

ChemFit records inputs, outputs, metadata, and intermediate results throughout an evaluation. The dashboard makes it easy to explore this information without digging through JSON files by hand.

Built with Python and PySide6, it works directly with serialized evaluation contexts from ChemFit 4.0.0. Everything runs locally, and the original files are never modified.

![ChemFit Dashboard screenshot](res/screenshot.png)

## Features

- **Explore evaluations:** Browse individual contexts or entire collections, including nested components, inputs, outputs, metadata, and errors.
- **Follow declared data flow:** See how data moves from a structural component's input, through its immediate children, to its output.
- **Search contexts:** Find components, fields, and values using text searches or regular expressions, within an evaluation or across a collection.
- **Plot numerical data:** Follow numerical values across evaluations, compare multiple quantities, and export plots as PNG, SVG, or CSV.
- **Investigate failures:** Identify failed evaluations and navigate directly to the components responsible.
- **Extract data:** Collect arbitrary fields from multiple evaluations and export them as CSV or JSONL for further analysis.

The dashboard also includes several color themes, adjustable font sizes, and keyboard navigation. Large collections are processed in the background to keep the interface responsive.

## Installation

ChemFit Dashboard requires Python 3.10 or newer.

Clone the repository:

```bash
git clone https://github.com/MSallermann/chemfit_dashboard.git
cd chemfit_dashboard
```

Install the required development version of `pydictnest`, followed by the dashboard:

```bash
python -m pip install git+https://github.com/MSallermann/pydictnest.git
python -m pip install -e .
```

The separate `pydictnest` installation is only necessary until version 0.3.0 is available on PyPI.

ChemFit itself is not required to view previously saved evaluation contexts.

## Getting started

Start the dashboard with:

```bash
chemfit-dashboard
```

You can open JSON files and directories from within the application, or specify them directly on the command line:

```bash
chemfit-dashboard path/to/context.json
chemfit-dashboard path/to/results/
```

To explore the included examples, open a single evaluation:

```bash
chemfit-dashboard examples/step_0_ctx_0.json
```

Or load a collection containing several evaluations:

```bash
chemfit-dashboard examples/context_collection/
```

You can also pass multiple files and directories at once. Directories are scanned recursively.

### Exploring evaluations

Select an evaluation from the file browser to inspect its component hierarchy. Clicking a component shows its inputs, outputs, metadata, and other recorded information.

The **Data Flow** view shows how data moves between the immediate children of a structural component. Double-click a nested component to explore its internal flow, and use the breadcrumbs to navigate back. Selecting a component stays synchronized with the tree and inspector.

Use the search field to find particular components or values. You can search the current evaluation or the entire loaded collection.

When working with multiple evaluations, the **Collection Overview** summarizes their execution status, numerical results, and failures.

### Plotting results

To plot a numerical value across evaluations, select it in the inspector and choose **Plot selected number across collection**.

The dashboard follows the same component through the collection, even when some evaluations contain skipped or missing branches.

You can add additional values to the plot, normalize them, use logarithmic scaling, and export the result.

Clicking a plotted point opens the corresponding evaluation.

## Data extraction

The dashboard can also collect arbitrary values from evaluation contexts and export them for analysis in Python, spreadsheets, or other tools.

Unlike plotting, data extraction is not limited to numerical values. You can extract strings, lists, dictionaries, metadata, or complete component information.

### Extracting data

1. Open an evaluation and select a value in the inspector.
2. Choose **Add selected field to extraction**. You can also use **Add component to extraction** to include a component's fields, metadata, and structure.
3. Repeat for any other values you want to collect.
4. Open the **Data Extraction** tab to preview the results.
5. Export the collected data as CSV or JSONL.

By default, each evaluation becomes one row, and each selected field becomes a column.

In the extraction tab, you can rename, reorder, or remove columns. **Flatten all** recursively turns nested dictionary leaves into separate columns, while **Expand** turns a selected array into multiple rows.

The preview shows up to 200 evaluations, but exports include the complete collection.

### Export formats

**CSV** is useful for spreadsheets, Pandas, and other tabular analysis tools. Dictionaries and lists are stored as JSON strings unless you choose to flatten them.

**JSONL** preserves nested values as JSON objects and arrays, making it better suited for structured data.

The dashboard also records whether a value was available, missing, skipped, or associated with an error, so these cases can be distinguished during analysis.

### Reusing field selections

If you frequently extract the same fields, you can save your selections using **Save configuration**.

A configuration is simply a list of the fields you've selected, their column names, and any extraction options. It does not contain the extracted data.

You can later choose **Load configuration** to restore those selections, including when working with another collection.

The dashboard uses ChemFit's component structure to recognize corresponding components across evaluations, so you don't have to select the same fields in every file.

## Using the extraction API from Python

Data extraction is also available directly from Python, without launching the graphical interface.

For example, the following script extracts the Lennard-Jones parameter `epsilon` and the objective value from the included example collection.

```python
from chemfit_dashboard import (
    ExtractionConfig,
    FieldSelector,
    export_csv,
    extract_fields,
)
from chemfit_dashboard.source import JsonFileSource

# Find the evaluation files
source = JsonFileSource()
records = source.discover(["examples/context_collection/"])

# Select the values we want
columns = (
    FieldSelector(
        node_path=(),
        section="fields",
        value_path=("input", "epsilon"),
        column="epsilon",
    ),
    FieldSelector(
        node_path=(),
        section="fields",
        value_path=("output",),
        column="loss",
    ),
)

# Inspect the extracted values
for row in extract_fields(source, records, columns):
    epsilon = row.values[0].value
    loss = row.values[1].value
    print(row.filename, epsilon, loss)

# Export the collection to CSV
export_csv(
    "results.csv",
    source,
    records,
    ExtractionConfig(columns),
)
```

Here, each `FieldSelector` describes a value to extract:

- `node_path=()` selects the root evaluation context.
- `section="fields"` selects the regular context fields.
- `value_path` identifies the value within those fields.
- `column` specifies the output column name.

For instance, `("input", "epsilon")` selects the `epsilon` entry from the root context's input dictionary.

The resulting `results.csv` contains the extracted values for every evaluation, along with evaluation identifiers and status information.

Each value returned by `extract_fields()` also has a `.status` attribute, which can be used to check whether the value was available or why it could not be extracted.

For nested components, the `selector_for_document()` function in `chemfit_dashboard.extraction` can attach the component's identity to a selector, allowing it to be reused across evaluations with the same logical structure.

The extraction API does not require Pandas or NumPy.

## Development

Install the test dependencies:

```bash
python -m pip install -e ".[test]"
```

Run the tests:

```bash
pytest -q
```

The repository also uses Ruff and Pyright for linting and type checking:

```bash
ruff check src tests benchmark
pyright
```

### Benchmarks

The benchmark suite evaluates collection processing, searching, extraction,
wide exports, and Data Flow layout/rendering. Run the standard profile with:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile standard
```

An intentionally large stress profile is also included:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile stress
```

The stress profile generates 50,000 evaluation files, indexes roughly two
million runtime component nodes, builds and renders a 25,000-node declared
graph, searches several dimensions, and exports 82 selected fields. It can use
substantial CPU time, memory, and temporary disk space. Every scale dimension
can be overridden independently; see `--help`.

See [BENCHMARKS.md](BENCHMARKS.md) for recorded benchmark results.

## Related projects

- [ChemFit](https://github.com/MSallermann/chemfit) — A Python framework for automated, high-dimensional model parameter optimization.
- [pydictnest](https://github.com/MSallermann/pydictnest) — Utilities for working with nested Python data structures.
