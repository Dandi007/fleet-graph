# minimal 协议层：全部 Stop schema 定义 + 信封校验器

# DD-101 · minimal 协议层：Stop schema 与信封校验

## 背景（自含）

本仓 `release/loopx-minimal` 分支正在按 `docs/specs/minimal/` 的设计正本重建一个最小系统：一个常驻引擎（LangGraph）+ 一个程序化校验节点 + 五个流程 agent（Goal / Impl / CR / FR / Merge）+ 一个只读书记员（Scribe）。

设计正本必读：`docs/specs/minimal/protocol.md`（§0 通用规则、§2 goal.turn、§3 goal.review、§4 impl、§5 review、§6 merge、§7 DD 结果对象、§12 scribe）与 `docs/specs/minimal/golden-order.md`。**冲突时以 golden-order 第 26–36 段为准**（见 `docs/specs/minimal/README.md` 的优先级说明）。

系统的核心纪律是：**每个 agent 的输出必须是恰好一个 JSON 对象**，含 `schema` 与 `stop` 两个键；缺键、schema 不匹配、stop 不在枚举内，一律判「无效输出」（protocol §0.1）。今天这条纪律在代码里没有任何落点——本单就是把它变成一个可被后续所有节点复用的纯函数模块。

## 要改什么

新建包 `src/fleet_graph/minimal/`（含 `__init__.py`），在其中新建 `protocol.py`，只用标准库（可用 dev 依赖 `jsonschema`，已在 pyproject 的 dev group 里；若用它，import 失败时不要让生产路径炸——建议干脆手写校验，不引入新运行时依赖）。

实现内容：

1. **schema 常量表**。为下列每个输出协议定义其 `schema` 字面量、允许的 `stop` 枚举、以及每个 stop 分支的必填字段：
   - `goal.turn/1`：stop ∈ `dispatch | done | blocked`。`dispatch` 必带非空 `summary` 与 `dispatch` 对象；`done` 必带 `summary`；`blocked` 必带 `summary` 与 `blocked.kind` ∈ `needs_human | external | contradiction` 且 `blocked.detail` 非空。
   - `goal.review/1`：stop ∈ `approve | reject`。`reject` 必带非空 `message`（protocol §3：缺 message 判无效）。
   - `impl/1`：stop ∈ `committed | failed`。`committed` 必带 40 位十六进制 `commit` 与非空 `summary`；`failed` 必带非空 `detail`。
   - `review/1`：stop ∈ `pass | fail`，另有必填 `role` ∈ `cr | fr`。`fail` 时 `findings` 至少一条 `severity` ∈ `blocker | major`（protocol §5，防无理由打回）；`findings[].severity` 只认 `blocker | major | minor | note`。
   - `merge/1`：stop ∈ `merged | rebased | failed`。`merged` 必带 40 位 sha `merged_commit`；`rebased` 必带 40 位 sha `new_head`；`failed` 必带非空 `detail`。
   - `scribe/1`：stop 只有 `observed`。`observations` 必须是数组（**允许空数组**，protocol §12 明确空数组合法）；每条 observation 必带 `kind` ∈ `progress | anomaly | cost | quality | decision | pattern`、`severity` ∈ `info | warn | high`、非空 `title`、非空 `summary`、以及**至少一条** `evidence`。
   - `runtime.error/1`：stop `invalid_output`，必带 `detail`（protocol §0.2，agent-runtime 失败时的输出）。

2. **`validate(obj: dict, expected_schema: str) -> ValidationResult`**。返回一个 dataclass（`ok: bool`、`errors: list[str]`），错误信息必须是**字段级**的、可直接落进 event payload 的字符串（例如 `"review/1: stop=fail requires at least one finding with severity in {blocker, major}"`）。不要抛异常做控制流。

3. **`extract_protocol_object(text: str, schema_prefix: str) -> dict | None`**。按 `docs/specs/000-smoke.md` §3 已经写死的契约实现：从一段含大量工具轨迹与 prose 的 stdout 里扫描所有平衡的 `{...}` 候选，取**最后一个** `schema` 以给定前缀开头且 `json.loads` 通过的对象；若无这样的候选，回退取「最后一个能平衡解析的 JSON 对象」；再无则返回 None。必须能正确处理字符串字面量里的花括号与转义引号（例如 `{"detail": "用了 {} 占位"}`）。

4. **`ENVELOPE_KEYS = ("schema", "stop")`** 与一个 `describe_schema(name) -> dict` 之类的只读导出，供后续 DD 生成 `--output-schema` 参数用（本单不需要真的生成 JSON Schema 文件，但要让 stop 枚举与必填字段可被程序读取，不要把它们埋在 if/else 里）。

## 不改什么

- **不碰 `src/fleet_graph/` 下任何已有模块**（acceptance.py / dd/ / graphs/ / scheduler/ / arbiter/ / supervise/ 等一律不动），不删旧代码——旧引擎按 GO-14 B5 仍在跑，下线是后续批次的事。
- 不引入新的运行时依赖（不要往 `[project].dependencies` 加东西）。
- 不写引擎、不写 LangGraph 图、不起子进程、不碰 git、不碰网络、不读写 events.jsonl——那些是同批其它 DD 与后续批次的范围。
- 不修改 `docs/specs/minimal/` 下的设计正本（那是 WF 的逐字节镜像）。
- 不改 Makefile。

## 怎么验收

`make verify` 必须绿（注意 ruff line-length=100、select 含 E/F/I/UP/B/SIM/RUF，写完跑 `make fmt`）。

另建 `tests/test_minimal_protocol.py`，至少覆盖：
- 六个 schema 各自的 happy path 各一条；
- `stop` 不在枚举内 → ok=False 且 errors 指出 stop；
- 缺 `schema` / 缺 `stop` → ok=False；
- `goal.review/1` reject 缺 message → ok=False；
- `review/1` fail 只带 minor/note findings → ok=False；fail 带一条 blocker → ok=True；
- `impl/1` committed 带非 40 位 sha → ok=False；
- `scribe/1` observations 为空数组 → ok=True；某条 observation 无 evidence → ok=False；
- `extract_protocol_object`：多个 JSON 候选时取最后一个匹配前缀的；prose 与代码围栏混杂时仍能提出；字符串里含 `{`/`}` 与转义引号时不误判；无候选返回 None。
