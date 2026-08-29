# Step 7 drill scratch line roster entry

Add one line entry to `config/ronin-lines.json` `lines[]` for the step-7 seat-swap drill scratch folder, without touching any existing entry or any other top-level key.

- folder_id: wf-a8c7b5
- seat: opencode-dsv4pro
- alias: ronin-step7-drill
- max_rounds: 1
- enabled: false

Constraints:
- Do not modify any other line entry, `run_root`, `line_environment`, or any top-level key.
- Keep the JSON valid; preserve existing `_comment`/`_provenance` fields.
- `enabled` MUST be false (parked drill line; the scheduler must refuse to auto-ignite it).

```dd-acceptance
python3 -c 'import json,pathlib; d=json.loads(pathlib.Path("config/ronin-lines.json").read_text()); e=[l for l in d["lines"] if l.get("folder_id")=="wf-a8c7b5"]; assert len(e)==1; s=e[0]; assert s["seat"]=="opencode-dsv4pro" and s["enabled"] is False and s.get("alias")=="ronin-step7-drill"; print("SCRATCH_LINE_OK")'
```
