# 开发检查实际输出

以下命令在本组 checkout 执行；未启动真实引擎、模型或联合集成/E2E。

## make verify

退出码：0

```text
uv run ruff check src tests scripts
All checks passed!
uv run ruff format --check src tests scripts
17 files already formatted
uv run pytest
........................................................................ [ 63%]
.........................................                                [100%]
113 passed in 1.84s
python3 -m compileall -q src
```

## uv run fleet-graph check-config --config config/codex.json

退出码：0

```text
{"valid": true, "roles": ["goal", "impl", "cr", "fr", "scribe"]}
```

## git diff --check

退出码：0

```text
```

# References
- Makefile、config/codex.json 与 tests/。
- 本组 WF wf-53a584 开发阶段边界。
