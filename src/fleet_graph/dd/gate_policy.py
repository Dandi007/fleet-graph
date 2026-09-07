"""审单协议在准入时冻结；旧单缺字段保留原专项检查。"""

import re

STANDARD = "dd-standard-v1"
LEGACY = "legacy-six-v1"
STANDARD_EVIDENCE = (
    "acceptance_frozen",
    "review_chain_approved",
    "personally_rerun",
    "zero_test_deletion",
)


def policy_from_spec(spec: bytes) -> str:
    declared = re.findall(r"```dd-gate-policy\s*\n(.*?)\n```", spec.decode(), re.DOTALL)
    if len(declared) > 1:
        raise ValueError("SPEC 只能声明一个 dd-gate-policy")
    policy = declared[0].strip() if declared else STANDARD
    if policy not in {STANDARD, LEGACY}:
        raise ValueError(f"未知审单协议 {policy!r}")
    return policy
