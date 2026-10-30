# Benchmark results

## Methodology and environment

Measurements below were recorded on 2026-10-30 with Python 3.13.11 and
PySide6 on Linux x86-64, using an Intel Core i7-10750H. Qt used the offscreen
platform plugin. Each command is a fresh process. Within one command, phase
times are independent but the reported RSS is the process high-water mark and
therefore cumulative. Python allocation peaks are per phase when
`--tracemalloc` is enabled; Qt/native allocations are visible only in RSS.

Representative timings should be collected without allocation tracing:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile standard
```

Use a second run for Python allocation peaks:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile standard --tracemalloc
```

The harness measures dataset generation, discovery, parsing/indexing, cold and
warm context switching, analysis, search-index construction and querying,
streaming extraction and export, graph construction/layout/rendering,
evaluation-model sorting, and collection plotting. Rendering has an enforced
deadline and a 10 ms event-loop heartbeat; `max_gui_stall` is the largest
heartbeat delay beyond that interval. Extraction rows are consumed rather than
retained solely for benchmarking.

## Before/after standard profile

The baseline was recorded before the optimizations using the same traced
1,000-evaluation / 2,000-node profile. The optimized column is a traced run, so
the comparison includes the same substantial `tracemalloc` overhead.

| Operation | Baseline (s) | Optimized (s) | Speedup | Optimized Python peak (MiB) |
|---|---:|---:|---:|---:|
| Directory discovery | 0.289 | 0.300 | 0.96× | 1.42 |
| Context parsing and indexing | 0.394 | 0.342 | 1.15× | 6.64 |
| Collection analysis | 0.485 | 0.318 | 1.53× | 0.11 |
| Collection search (1 query) | 15.599 | 2.268 | 6.88× | 6.25 |
| Multi-field extraction | 0.395 | 0.434 | 0.91× | 3.88 |
| CSV export | 0.480 | 0.508 | 0.94× | 4.02 |
| JSONL export | 0.478 | 0.494 | 0.97× | 3.89 |
| Data-flow layout | 0.282 | 0.295 | 0.96× | 3.10 |
| Data-flow rendering | 1.328 | 0.901 | 1.47× | 3.98 |

Small differences around 1.0× are timing noise and were not treated as wins.
The optimized traced process reached 112.6 MiB peak RSS after all phases. The
same optimized run without tracing completed collection search in 0.69s and
graph rendering in 0.30s; traced and untraced values should not be mixed in a
speedup claim.

## Scaling profile

The final untraced implementation was also measured with the stress data shape
at two sizes:

| Operation | 500 evals / 1,000 nodes | 5,000 evals / 5,000 nodes | Scale factor |
|---|---:|---:|---:|
| Parsing and indexing | 0.196s | 2.260s | 11.5× |
| Collection analysis | 0.215s | 2.256s | 10.5× |
| Search-index construction | 0.878s | 8.221s | 9.4× |
| Collection search (3 queries) | 4.709s | 46.782s | 9.9× |
| 42-field extraction | 0.353s | 2.986s | 8.5× |
| CSV export | 0.395s | 3.684s | 9.3× |
| JSONL export | 0.367s | 3.430s | 9.3× |
| Evaluation model build/sort | 0.018s | 0.162s | 9.0× |
| Collection plotting | 0.020s | 0.147s | 7.4× |
| Graph layout | 0.018s | 0.084s | 4.7× |
| Graph rendering | 0.148s | 0.829s | 5.6× |
| Maximum GUI stall | 32.2ms | 157.0ms | — |
| Process peak RSS | 95.5 MiB | 245.2 MiB | 2.6× |

The tenfold collection-size increase is approximately linear. Search remains
the dominant phase, but no superlinear growth was observed. The 5,000-node
scene stays interactive because node creation is split into event-loop batches;
total rendering also improves because edges/arrowheads are batched and node
decoration no longer creates child graphics objects.

## Full stress profile

The opt-in maximum profile contains 50,000 evaluation files, 40 runtime
components per evaluation, a 25,000-component first context, 80 numeric root
fields (82 exported fields), three searches, and a 25,000-node construction and
layout. The GUI phase verifies that the built-in 5,000-component rendering
limit rejects the oversized scene immediately:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile stress
```

It can require several gigabytes of memory and substantial temporary storage.
Start with the reproducible smaller profile used above:

```bash
PYTHONPATH=src python benchmark/benchmark.py --profile stress \
  --evaluations 5000 --graph-nodes 5000 \
  --components-per-evaluation 20 --root-fields 40
```

The full 50,000/25,000 run was not executed on this laptop; extrapolations are
not reported as measurements.

## Library evaluations and remaining limits

- RapidFuzz 3.14.6 reduced final end-to-end three-query collection search by a
  further 16% (55.8s to 46.8s at 5,000 evaluations) with identical ordering and
  scores in representative exact/fuzzy regression comparisons. It is now a
  runtime dependency.
- `orjson` improved isolated parsing by about 14% and raw wide serialization by
  7.4×, but preserving stdlib behavior for `NaN`, infinity, and oversized
  integers required a Python compatibility scan that made the safe exporter
  slower. It was not adopted.
- Graphviz `dot` took 0.226s for a 1,000-node linear layout versus 0.016s for the
  existing layout, before integration/IPC overhead. It was not adopted.
- A data-flow level above 5,000 components is intentionally not rendered.
  Smaller graphs retain one interactive Qt item per node; edges and decoration
  are batched, but native Qt memory remains material.
- Search indexes are cached for the 32 most recent documents and scoring caches
  are bounded at 16,384 entries, keeping retained memory predictable.
