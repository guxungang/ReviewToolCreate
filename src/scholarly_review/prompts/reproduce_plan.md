## What to do: reproduction plan (not executed)

Code execution is denied by default and not implemented in this version. Produce **plans only**.

For the core empirical claims that are worth reproducing, draft one `ExperimentPlan` each:

- `run_variant`: `original` (as published), `environment_adapted` (only environment changes) or `patched`
  (algorithm/config changes). Never present a patched run as an original reproduction.
- `entrypoint` + `arguments` as an argument array (never a shell string), `working_directory`.
- `resources`: cpu_cores, memory_bytes, disk_bytes, cpu_seconds, gpu_seconds (0 unless the user approved GPU),
  max_processes.
- `metrics`: name, definition, unit, reported_value, `comparison` (exact, rounded_interval, absolute, relative,
  statistical, descriptive) and tolerances **only if** the paper or a declared protocol gives them - never a
  blanket 1-2%.
- `timeout_seconds`, `rationale`. Put static observations (missing seeds, unpinned dependencies) in `static_notes`.

```json
{"output": {"plans": [{"key": "x1", "claim_ids": ["<claim_id>"], "run_variant": "original",
  "entrypoint": "python", "arguments": ["eval.py", "--config", "configs/main.yaml"], "working_directory": ".",
  "resources": {"cpu_cores": 4, "memory_bytes": 17179869184, "disk_bytes": 10737418240, "cpu_seconds": 3600,
                "gpu_seconds": 0, "max_processes": 16},
  "metrics": [{"name": "accuracy", "definition": "top-1 on test split", "unit": "%", "reported_value": "84.0",
               "comparison": "rounded_interval"}],
  "timeout_seconds": 3600, "rationale": "core claim C1"}], "static_notes": []},
 "usage": {"basis": "unknown"}}
```
