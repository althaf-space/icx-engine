# Graph extractor evaluation harness

Measures precision and recall of the icx graph parser against hand-annotated
ground-truth edges on real-world projects. Without this harness, accuracy
claims about the graph builder are unverifiable.

## Layout

```
tests/graph/eval/
  score.py                       # precision + recall scorer (entry point)
  fixtures/<name>/               # source files for a fixture project
  ground_truth/<name>.json       # hand-annotated expected edges
```

## Adding a fixture

1. Copy ~30-50 source files of a representative project under
   `fixtures/<name>/`. Keep the directory layout the project would have on disk.
2. Create `ground_truth/<name>.json` with the format below.
3. Hand-annotate cross-file edges you expect the parser to detect.
   Only annotate edges a human would reasonably expect - not every transitive
   reference. Aim for ~30 edges per fixture; that is enough to get statistical
   signal without burning days on annotation.

## Ground-truth schema

```json
{
  "fixture": "fastapi_sample",
  "language": "python",
  "framework": "fastapi",
  "edges": [
    {
      "source": "app/main.py:read_users",
      "target": "app/services/user_service.py:list_users",
      "kind": "call",
      "notes": "endpoint delegates to service layer"
    },
    {
      "source": "app/main.py",
      "target": "app/db.py:get_db",
      "kind": "import"
    }
  ]
}
```

- `source` / `target` use `relative_path:symbol` form, or just
  `relative_path` for file-level edges.
- `kind` is one of `import`, `call`, `inherit`, `reference`, `route`,
  `di` (dependency injection), `relation` (ORM).
- `notes` is optional human commentary; ignored by the scorer.

## Running

```
python -m tests.graph.eval.score fastapi_sample
```

Scorer:
1. Builds a fresh graph over the fixture directory using the same code path as
   `icx graph build`.
2. Loads `ground_truth/<name>.json`.
3. Computes precision (correct predicted edges / all predicted edges) and
   recall (correct predicted edges / all ground-truth edges).
4. Prints a per-edge-kind breakdown.

## Calibration

Treat the first 2-3 fixtures as calibration: their precision / recall scores
inform what realistic accuracy ceilings look like for the supported stacks.
Avoid moving the goalposts after the harness is locked.
