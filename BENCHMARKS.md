# Benchmark results

Recorded on 2026-10-29 using Python 3.13.11 on Linux x86-64 with an Intel
Core i7-10750H. The command was:

```bash
PYTHONPATH=src python benchmark/benchmark.py --evaluations 1000 --graph-nodes 2000
```

| Operation | Time (s) | Peak Python memory (MiB) |
|---|---:|---:|
| Directory discovery | 0.316 | 1.42 |
| Context parsing and indexing | 0.329 | 4.95 |
| Collection analysis | 0.468 | 2.51 |
| Collection search | 8.849 | 3.40 |
| Multi-field extraction | 0.415 | 2.52 |
| CSV export | 0.501 | 2.65 |
| JSONL export | 0.493 | 2.52 |
| Data-flow layout | 0.319 | 3.10 |
| Data-flow rendering | 1.414 | 4.66 |

The workload contains many small evaluation contexts and one evaluation with a
2,000-component declared data-flow pipeline. `tracemalloc` reports Python allocations;
it does not include every native Qt allocation. Filesystem caching and hardware
affect results. No trustworthy pre-change run exists for this checkout, so this
table is a reproducible scaling snapshot rather than a before/after claim.
