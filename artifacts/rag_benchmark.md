Questions: 61 (35 answerable, 26 should abstain), documents: 27, dataset sha256 `e5b75852df75…`

| Model | Mode | Recall@3 | MRR | Paraphrase R@3 | Exact-ID R@3 | Abstention | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| hash-128 | bm25 | 0.914 | 0.838 | 0.727 | 1.000 | 0.115 | 0.42 | 0.53 | 0 |
| hash-128 | dense | 0.657 | 0.605 | 0.182 | 0.625 | 0.077 | 0.57 | 0.62 | 0 |
| hash-128 | hybrid | 0.743 | 0.724 | 0.364 | 0.750 | 0.077 | 0.75 | 0.79 | 0 |
| all-MiniLM-L6-v2 | bm25 | 0.914 | 0.838 | 0.727 | 1.000 | 0.115 | 0.44 | 0.49 | 0 |
| all-MiniLM-L6-v2 | dense | 0.886 | 0.843 | 0.818 | 0.750 | 0.500 | 9.12 | 9.93 | 0 |
| all-MiniLM-L6-v2 | hybrid | 0.943 | 0.929 | 0.818 | 1.000 | 0.500 | 9.60 | 14.06 | 0 |
