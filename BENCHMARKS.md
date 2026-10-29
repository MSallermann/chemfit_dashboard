# Benchmark results

Recorded on 2026-10-29 using Python 3.13.11 on Linux x86-64 with an Intel
Core i7-10750H. The command was:

```bash
PYTHONPATH=src python benchmark/benchmark.py --evaluations 1000 --graph-nodes 2000
```

| Operation | Time (s) | Peak Python memory (MiB) |
|---|---:|---:|
| Directory discovery | 0.355 | 1.42 |
| Context parsing and indexing | 0.332 | 4.28 |
| Collection analysis | 0.449 | 1.84 |
| Collection search | 11.108 | 2.73 |
| Multi-field extraction | 0.406 | 1.85 |
| CSV export | 0.504 | 1.98 |
| JSONL export | 0.529 | 1.86 |
| Graph layout | 0.229 | 3.46 |
| Graph rendering | 1.723 | 4.05 |

The workload contains many small evaluation contexts and one evaluation with a
2,000-component dependency pipeline. `tracemalloc` reports Python allocations;
it does not include every native Qt allocation. Filesystem caching and hardware
affect results. No trustworthy pre-change run exists for this checkout, so this
table is a reproducible scaling snapshot rather than a before/after claim.
