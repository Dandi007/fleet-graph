# Spec ⑫-c（wf-8d9737）· test_d12b 白名单对收割上下文容错（r12 收割受阻修复单）

> 状态：落卷即派（r12 档 P1 停手后的既定修复路径；监督面已在 r12 报告中知悉「dev-dispatch 一张小单或经裁定豁免」两选项，本单为前者）。
> base 钉死：**本地收割 tip 422fac784bf7d04498b4b0bc3302a0badc35c7a4**（branch release/wf-8d9737-harvest-r12，四单合流 merge 链头；origin release/wf-8d9737 仍=7f20b340 未动——本单修复合入后一并 push）。
> 判据锚：r12-harvest-execution-and-canonical-20260904T1005Z.md P1 节逐字诊断；spec-d12b-audit-note-named-targets.md（⑫-b 母单）；R11/R14 收割剥离契约（.dev-dispatch/.dd-evidence 机器件在收割侧剥离，release 树不存在这些路径）。

## 缺陷（只述事实，不规定修法）

`tests/test_d12b_audit_note_targets.py` 的 `TestLegacyPhraseGoneFromRepo::test_full_repo_grep_legacy_phrase_is_whitelisted_only` 在**收割上下文**下 over-eager：

- 白名单 `LEGACY_PHRASE_WHITELIST` 含 `.dev-dispatch/spec/approved.md`（⑫-b 单树内真实存在且含旧措辞 6 处——单树内自洽绿）；
- 但收割链按 R11/R14 契约剥离 `.dev-dispatch/`、`.dd-evidence/` 机器件 → 收割后的树该路径不存在；
- 逐字红样（r12 push 前全量 make verify，清代理 env）：

```
FAILED tests/test_d12b_audit_note_targets.py::TestLegacyPhraseGoneFromRepo::test_full_repo_grep_legacy_phrase_is_whitelisted_only
AssertionError: legacy phrase hits outside the explicit whitelist: []; full set: {'tests/test_d12b_audit_note_targets.py': 1}
Extra items in the right set: '.dev-dispatch/spec/approved.md'
```

即：白名单声明的机器件路径在收割树上缺席时，断言把「路径缺席」当作失败。这是测试对机器件路径的存在性假设与收割剥离契约的交互缺陷，属代码缺陷。**单树内绿 ≠ 收割后绿**——⑫-b 验收在其单树跑（机器件在位）当然绿，缺陷只在收割上下文暴露。

## 要交付的行为（缺陷面，修法自定）

修复 `tests/test_d12b_audit_note_targets.py` 使该断言**在两类上下文下都成立**：

1. **dd 单树上下文**（.dev-dispatch/ 在位；本单 base 422fac7 是收割 tip，.dev-dispatch/ 已剥离——实现者如需复现⑫-b 单树上下文可自行构造，不强制）；
2. **收割上下文**（.dev-dispatch/ 被剥离，路径缺席——base 422fac7 即此形态，红测可直接复现）。

具体修法由实现者自定，两条已知可行路线（不限于）：
- **路线 A（容错缺席）**：白名单机器件路径缺席（文件不存在）时视为满足——白名单语义从「每个白名单路径必须存在且含短语」改为「repo 命中集合 ⊆ 白名单，且白名单中**存在**的路径须确实含短语、缺席的机器件路径（.dev-dispatch/.dd-evidence 前缀）容错」。产品面白名单路径（非机器件）仍须存在。
- **路线 B（收缩白名单）**：白名单移除 `.dev-dispatch/spec/approved.md` 条目。实现者须自行验证路线 B 在单树上下文（若单树 .dev-dispatch/spec/approved.md 含旧措辞，repo 命中会落在白名单外）仍绿的办法，或论证不可行后走路线 A。

无论哪条路线，**测试的防回归意图不得弱化**：产品面（src/ 等）零旧措辞的断言必须原样保留；白名单精确性语义（repo 全部命中都在白名单内、白名单条目须有理由）不得放松为「跳过检查」。

## 验收（冻结）

修复后以下全部成立：

1. `tests/test_d12b_audit_note_targets.py` 在 base 422fac7（收割上下文，.dev-dispatch 缺席）上全绿——含原红测；
2. 若实现含白名单缺席容错逻辑：须有一条用例证明「白名单声明了存在路径但路径不存在且非机器件前缀」仍红（防容错被泛化成放行一切缺席）；
3. 全量 `make verify` 在清代理环境 EXIT=0：**2887 passed + 1 skipped**（即含原红测在内全绿，零 deselect）。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_d12b_audit_note_targets.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
```

## 边界

- 只动 `tests/test_d12b_audit_note_targets.py`（及必要的同文件 fixture）；**零产品码改动**（渲染面/词表/a2.py 均是⑫-b 已验收成品，不得触碰）；
- 零测试删除（本单是修测试断言，不许删测试换绿）；
- 不动 .dev-dispatch/.dd-evidence 机器件；不动收割剥离契约本身（R11/R14 是先例契约）；
- 不动 release 分支模型/push 动作（收割链 push 归 wf-8d9737 线侧流程，非本单验收面）。

> 座位（D8）：implement=glm-5.3-flash，continuous_review=final_review=glm-5.3，经 stage_models 传入。
