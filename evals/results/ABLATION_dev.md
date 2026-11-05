# Ablation - dev split (one run each)

| Variant | Success | Tasks with a violation (material / any) | Leaks | False claims | Approval P / R | Security events | $ / task |
|---|---|---|---|---|---|---|---|
| **v1 (all layers)** | 97.3% | 1 / 1 | 0 | 0 | 100.0% / 93.5% | 9 | $0.0028 |
| without the policy engine and approval gate (model judgement only) | 84.0% | 28 / 28 | 0 | 0 | - / 0.0% | 10 | $0.0029 |
| without identity gating of account tools | 97.8% | 0 / 0 | 0 | 0 | 96.8% / 96.8% | 10 | $0.0027 |
| without the approval gate (the engine's 'needs approval' is not enforced; the model acts on its own judgement) | 84.4% | 30 / 30 | 0 | 0 | - / 0.0% | 10 | $0.0029 |
| without provenance tags / injection flags | 98.2% | 1 / 1 | 0 | 0 | 100.0% / 93.5% | 2 | $0.0027 |
| without the checker model | 98.2% | 1 / 1 | 0 | 0 | 100.0% / 93.5% | 9 | $0.0020 |
| without the consistency check and the checker | 98.2% | 1 / 1 | 0 | 0 | 100.0% / 93.5% | 9 | $0.0020 |
| v0+ policy in the prompt (reference) | 84.0% | 23 / 23 | 0 | 1 | 93.9% / 100.0% | 0 | $0.0021 |
| v0 no policy (reference) | 80.4% | 32 / 32 | 0 | 1 | 100.0% / 96.8% | 0 | $0.0027 |
