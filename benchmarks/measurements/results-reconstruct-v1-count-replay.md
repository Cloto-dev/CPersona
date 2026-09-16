# Reconstruction v1 count replay

This is a frozen **raw, pre-scene-filter top-20** replay on 500 LongMemEval queries. It is not the scene-filtered NDCG evaluation that produced 81.872540. The ranking dumper records rows before the per-query candidate filter. Consequently these results must not be compared as a change in that score.

The corpus records are whole sessions. Their per-turn source metadata is absent, so all retained candidates are singletons; grouping is qualified separately through real-store conversation tests.

| Item ceiling | Evidence recall, type macro | Full-text item characters, type macro | Queries exhausting available clusters |
| ---: | ---: | ---: | ---: |
| 1 | 7.383% | 1593.8 | 0.0% |
| 2 | 11.325% | 3218.0 | 0.0% |
| 4 | 17.046% | 6463.5 | 0.0% |
| 8 | 23.118% | 13090.7 | 0.0% |
| 10 | 24.206% | 16391.3 | 0.0% |

Every arm had 20 available candidates and clusters. Increasing the ceiling changes retained evidence and serialized characters without changing the candidate pool. All 2,500 replay cases checked selected evidence against the frozen ranking and the head quotation against its source. Payload counts here use full-text library items, not MCP preview bytes and not tokens.

**Decision boundary:** this does not choose an optimal default. No answer reader, scene-controlled count trial, new retrieval run, latency claim, or external-model generalization is included. The omitted-call default of one is a contract choice; the maximum of ten remains experimental. Neither is justified as optimal by this replay.

Reproduction: `PYTHONPATH=. python benchmarks/reconstruct_count_replay.py --rankings <retained-rankings.jsonl> --corpus <LongMemEval/corpus.jsonl> --qrels-root <LongMemEval> --output <result.json>`. Input SHA-256 fingerprints and per-query rows are in the generated JSON.

Rankings SHA-256: `816077826e4882390c6abac372eee53e6a8c44a272fa115191a1cc0646243e05`.
Corpus SHA-256: `c1462e7d8f11cfddec685a67315736c890ebce9da54330aefbf45ba99554148d`.
