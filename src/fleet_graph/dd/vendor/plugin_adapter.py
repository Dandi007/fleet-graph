"""Pinned Dev Dispatch Plugin capability verification and v1 invocation.

Attempt-context v1 admits only a clean, exact Plugin commit whose committed
capability manifest, bundle, lifecycle, artifact contract, workflow, and schemas
all match the configured compatibility lock.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.upstream_constants import (
    ATTEMPT_CONTEXT_CONTRACT_VERSION,
    HANDOFF_CONTRACT_VERSION,
    ReviewPhase,
    canonical_json,
    compute_digest,
    compute_json_digest,
)
from fleet_graph.dd.vendor import external_ops

MATERIALIZE_SCRIPT_CANDIDATES = ("materialize-handoff.py", "materialize-handoff.sh")
DEFAULT_VERIFY_TIMEOUT_SECONDS = 60
CAPABILITY_MANIFEST_PATH = "contracts/attempt-context-capability.json"
CAPABILITY_MANIFEST_VERSION = "dev-dispatch.attempt-context-capability/v1"
CAPABILITY_BUNDLE_ALGORITHM = "sha256-canonical-git-blob-list-b64path/v1"
CAPABILITY_RESOURCE_ROOTS = ["contracts", "workflows", "scripts", "bin", "fixtures"]
CAPABILITY_SCHEMA_PATHS = [
    "contracts/attempt-context.schema.json",
    "contracts/stage-dispatch.schema.json",
    "contracts/implement-handoff-receipt.schema.json",
    "contracts/implement-non-applied-receipt.schema.json",
    "contracts/review-result.schema.json",
    "contracts/review-handoff-receipt.schema.json",
    "contracts/feedback-index.schema.json",
    "contracts/failed-record.schema.json",
]
CAPABILITY_LIFECYCLE_PATH = "contracts/development-lifecycle.json"
CAPABILITY_ARTIFACT_PATH = "contracts/stage-artifacts.json"
CAPABILITY_WORKFLOW_PATH = "workflows/dev-dispatch/stage-graph.yaml"
IMPLEMENT_STAGE_RESOURCE_PATHS = {
    "implement/contracts/implement.output.schema.json": (
        "workflows/dev-dispatch/implement/contracts/"
        "implement.output.schema.json"
    ),
    "implement/personas/implementer.md": (
        "workflows/dev-dispatch/implement/personas/implementer.md"
    ),
    "implement/templates/implement.md": (
        "workflows/dev-dispatch/implement/templates/implement.md"
    ),
    "implement/templates/seal.md": (
        "workflows/dev-dispatch/implement/templates/seal.md"
    ),
    "implement/workflow.yaml": (
        "workflows/dev-dispatch/implement/workflow.yaml"
    ),
}
REVIEW_STAGE_RESOURCE_PATHS = {
    ReviewPhase.CONTINUOUS: {
        "continuous_review/contracts/continuous_review.output.schema.json": (
            "workflows/dev-dispatch/continuous_review/contracts/"
            "continuous_review.output.schema.json"
        ),
        "continuous_review/contracts/"
        "continuous_review.requirement_coverage.schema.json": (
            "workflows/dev-dispatch/continuous_review/contracts/"
            "continuous_review.requirement_coverage.schema.json"
        ),
        "continuous_review/personas/requirement_mapper.md": (
            "workflows/dev-dispatch/continuous_review/personas/"
            "requirement_mapper.md"
        ),
        "continuous_review/personas/verdict_auditor.md": (
            "workflows/dev-dispatch/continuous_review/personas/"
            "verdict_auditor.md"
        ),
        "continuous_review/templates/requirement_mapper.md": (
            "workflows/dev-dispatch/continuous_review/templates/"
            "requirement_mapper.md"
        ),
        "continuous_review/templates/verdict_auditor.md": (
            "workflows/dev-dispatch/continuous_review/templates/"
            "verdict_auditor.md"
        ),
        "continuous_review/templates/verdict_seal.md": (
            "workflows/dev-dispatch/continuous_review/templates/"
            "verdict_seal.md"
        ),
        "continuous_review/workflow.yaml": (
            "workflows/dev-dispatch/continuous_review/workflow.yaml"
        ),
    },
    ReviewPhase.FINAL: {
        "final_review/contracts/final_review.output.schema.json": (
            "workflows/dev-dispatch/final_review/contracts/"
            "final_review.output.schema.json"
        ),
        "final_review/contracts/"
        "final_review.requirement_coverage.schema.json": (
            "workflows/dev-dispatch/final_review/contracts/"
            "final_review.requirement_coverage.schema.json"
        ),
        "final_review/personas/requirement_mapper.md": (
            "workflows/dev-dispatch/final_review/personas/"
            "requirement_mapper.md"
        ),
        "final_review/personas/verdict_auditor.md": (
            "workflows/dev-dispatch/final_review/personas/"
            "verdict_auditor.md"
        ),
        "final_review/templates/requirement_mapper.md": (
            "workflows/dev-dispatch/final_review/templates/"
            "requirement_mapper.md"
        ),
        "final_review/templates/verdict_auditor.md": (
            "workflows/dev-dispatch/final_review/templates/"
            "verdict_auditor.md"
        ),
        "final_review/templates/verdict_seal.md": (
            "workflows/dev-dispatch/final_review/templates/verdict_seal.md"
        ),
        "final_review/workflow.yaml": (
            "workflows/dev-dispatch/final_review/workflow.yaml"
        ),
    },
}
IMPLEMENT_RECEIPT_COMMON_FIELDS = {
    "actor_job_id",
    "artifacts",
    "attempt_id",
    "contract_version",
    "development_id",
    "feedback_digest",
    "input_commit",
    "materialization_intent_id",
    "output_commit",
    "parent_handoff_receipt_digest",
    "spec_digest",
    "work_head_commit",
}
IMPLEMENT_RECEIPT_FIELDS = IMPLEMENT_RECEIPT_COMMON_FIELDS | {
    "verification_record"
}
# 非 APPLIED receipt 无 output_commit（schema SSoT：implement-non-applied-receipt
# additionalProperties:false 且 properties 不含该键——no-op 不产生新产物 commit）。
IMPLEMENT_RECEIPT_NON_APPLIED_COMMON = IMPLEMENT_RECEIPT_COMMON_FIELDS - {
    "output_commit",
}
IMPLEMENT_RECEIPT_DISPUTED_FIELDS = IMPLEMENT_RECEIPT_NON_APPLIED_COMMON | {
    "outcome",
    "rebuttal",
}
IMPLEMENT_RECEIPT_BLOCKED_FIELDS = IMPLEMENT_RECEIPT_NON_APPLIED_COMMON | {
    "outcome",
    "blocker",
}
IMPLEMENT_FAILURE_FIELDS = {
    "detail",
    "failure_code",
    "retryable",
    "verified",
}
REVIEW_RECEIPT_FIELDS = {
    "attempt_id",
    "contract_version",
    "development_id",
    "feedback_index",
    "implementation_handoff_receipt_digest",
    "implementation_subject_commit",
    "input_commit",
    "materialization_intent_id",
    "output_commit",
    "parent_handoff_receipt_digest",
    "review_artifact",
    "review_id",
    "review_phase",
    "reviewer_job_id",
    "subject_commit",
    "verdict",
}

_SECRET_RE = re.compile(r"(?i)(token|authorization|credential|secret|password)\s*[:=]\s*\S+")
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class PluginBinding:
    """The pinned plugin producer identity the MCP is bound to (spec §14.2)."""

    root: str
    expected_commit: str
    bundle_digest: str
    contract_version: int
    verify_timeout_seconds: int
    protocol: str
    capability_manifest_digest: str
    workflow_digest: str
    lifecycle_digest: str
    artifact_digest: str
    schema_digests: dict[str, str]


@dataclass(frozen=True)
class VerifiedPluginCapability:
    """Exact committed capability bytes admitted by the controller."""

    protocol: str
    plugin_commit: str
    capability_manifest_digest: str
    bundle_digest: str
    workflow_digest: str
    lifecycle_digest: str
    artifact_digest: str
    schema_digests: dict[str, str]

    def lock_payload(self, development_id: str) -> dict[str, Any]:
        return {
            "artifact_digest": self.artifact_digest,
            "bundle_digest": self.bundle_digest,
            "capability_manifest_digest": self.capability_manifest_digest,
            "contract_version": self.protocol,
            "development_id": development_id,
            "lifecycle_digest": self.lifecycle_digest,
            "plugin_commit": self.plugin_commit,
            "schema_digests": self.schema_digests,
            "workflow_digest": self.workflow_digest,
        }

    def lock_digest(self, development_id: str) -> str:
        return compute_json_digest(self.lock_payload(development_id))


@dataclass(frozen=True)
class CommittedStageResource:
    """One immutable stage resource read from the pinned Plugin commit."""

    relative_path: str
    content: bytes
    digest: str


def load_plugin_binding(config: dict[str, Any]) -> PluginBinding:
    section = config.get("plugin_producer", {}) or {}
    schema_digests = section.get("schema_digests", {})
    if not isinstance(schema_digests, dict):
        schema_digests = {}
    return PluginBinding(
        root=str(section.get("root", "") or ""),
        expected_commit=str(section.get("expected_commit", "") or ""),
        bundle_digest=str(section.get("bundle_digest", "") or ""),
        contract_version=int(section.get("contract_version", HANDOFF_CONTRACT_VERSION) or HANDOFF_CONTRACT_VERSION),
        verify_timeout_seconds=int(section.get("verify_timeout_seconds", DEFAULT_VERIFY_TIMEOUT_SECONDS) or DEFAULT_VERIFY_TIMEOUT_SECONDS),
        protocol=str(section.get("protocol", "") or ""),
        capability_manifest_digest=str(
            section.get("capability_manifest_digest", "") or ""
        ),
        workflow_digest=str(section.get("workflow_digest", "") or ""),
        lifecycle_digest=str(section.get("lifecycle_digest", "") or ""),
        artifact_digest=str(section.get("artifact_digest", "") or ""),
        schema_digests={
            str(path): str(digest)
            for path, digest in schema_digests.items()
        },
    )


def _compute_capability_digests_for_commit(
    binding: PluginBinding,
) -> dict[str, Any]:
    """Compute all five capability digests from the commit at binding.expected_commit.

    Does NOT perform the full verify_plugin_capability check — only computes the
    digests the manifest declares.  Structural validation (manifest keys, protocol,
    version, schema order) is still enforced so a corrupt commit is never admitted.
    """
    manifest_raw = _committed_capability_bytes(binding, CAPABILITY_MANIFEST_PATH)
    observed_manifest_digest = compute_digest(manifest_raw)
    manifest = _strict_capability_json(
        manifest_raw,
        "plugin capability manifest",
        require_canonical=True,
    )
    _exact_capability_keys(
        manifest,
        {
            "artifact_contract",
            "bundle",
            "lifecycle",
            "manifest_version",
            "protocol",
            "schemas",
            "workflow",
        },
        "plugin capability manifest",
    )
    if manifest["manifest_version"] != CAPABILITY_MANIFEST_VERSION:
        raise ValueError("plugin capability manifest version mismatch")
    if manifest["protocol"] != binding.protocol:
        raise ValueError("plugin capability manifest protocol mismatch")

    schemas = manifest["schemas"]
    if (
        not isinstance(schemas, list)
        or [
            entry.get("path") if isinstance(entry, dict) else None
            for entry in schemas
        ]
        != CAPABILITY_SCHEMA_PATHS
    ):
        raise ValueError("plugin capability schema order/paths mismatch")
    observed_schema_digests: dict[str, str] = {
        path: _verify_digest_entry(
            binding,
            entry,
            path,
            f"schema[{index}]",
        )
        for index, (entry, path) in enumerate(
            zip(schemas, CAPABILITY_SCHEMA_PATHS, strict=True)
        )
    }

    lifecycle_digest = _verify_digest_entry(
        binding,
        manifest["lifecycle"],
        CAPABILITY_LIFECYCLE_PATH,
        "lifecycle",
    )
    artifact_digest = _verify_digest_entry(
        binding,
        manifest["artifact_contract"],
        CAPABILITY_ARTIFACT_PATH,
        "artifact contract",
    )
    workflow_digest = _verify_digest_entry(
        binding,
        manifest["workflow"],
        CAPABILITY_WORKFLOW_PATH,
        "workflow",
    )

    bundle = manifest["bundle"]
    _exact_capability_keys(
        bundle,
        {"algorithm", "digest", "excluded_paths", "resource_roots"},
        "plugin capability bundle",
    )
    if (
        bundle["algorithm"] != CAPABILITY_BUNDLE_ALGORITHM
        or bundle["excluded_paths"] != [CAPABILITY_MANIFEST_PATH]
        or bundle["resource_roots"] != CAPABILITY_RESOURCE_ROOTS
    ):
        raise ValueError("plugin capability bundle contract mismatch")
    observed_bundle = _compute_bundle_digest(binding)
    if bundle["digest"] != observed_bundle:
        raise ValueError("plugin capability bundle digest mismatch")

    return {
        "capability_manifest_digest": observed_manifest_digest,
        "schema_digests": observed_schema_digests,
        "lifecycle_digest": lifecycle_digest,
        "artifact_digest": artifact_digest,
        "workflow_digest": workflow_digest,
        "bundle_digest": observed_bundle,
    }


def load_plugin_binding_with_commit(
    config: dict[str, Any],
    commit: str,
) -> PluginBinding:
    binding = load_plugin_binding(config)
    if not binding.root or not os.path.isdir(binding.root):
        raise ValueError("plugin capability root is not a directory")
    if _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("plugin capability commit must be full lowercase 40-hex")
    if binding.protocol != ATTEMPT_CONTEXT_CONTRACT_VERSION:
        raise ValueError("plugin capability protocol mismatch")

    temp = PluginBinding(
        root=binding.root,
        expected_commit=commit,
        bundle_digest=binding.bundle_digest,
        contract_version=binding.contract_version,
        verify_timeout_seconds=binding.verify_timeout_seconds,
        protocol=binding.protocol,
        capability_manifest_digest=binding.capability_manifest_digest,
        workflow_digest=binding.workflow_digest,
        lifecycle_digest=binding.lifecycle_digest,
        artifact_digest=binding.artifact_digest,
        schema_digests=dict(binding.schema_digests),
    )

    try:
        digests = _compute_capability_digests_for_commit(temp)
    except (ValueError, TypeError, external_ops.ExternalCallTimeout, OSError) as exc:
        raise ValueError(f"plugin capability at {commit[:8]} is invalid: {exc}") from exc

    return PluginBinding(
        root=binding.root,
        expected_commit=commit,
        bundle_digest=digests["bundle_digest"],
        contract_version=binding.contract_version,
        verify_timeout_seconds=binding.verify_timeout_seconds,
        protocol=binding.protocol,
        capability_manifest_digest=digests["capability_manifest_digest"],
        workflow_digest=digests["workflow_digest"],
        lifecycle_digest=digests["lifecycle_digest"],
        artifact_digest=digests["artifact_digest"],
        schema_digests=digests["schema_digests"],
    )


def resolve_binding_at_commit(
    config: dict[str, Any],
    commit: str,
) -> tuple[PluginBinding, str]:
    """Resolve a PluginBinding against a detached checkout of *commit*.

    Returns (binding, checkout_dir) where *checkout_dir* is a temporary
    detached worktree whose HEAD is exactly *commit*.  The caller must
    remove *checkout_dir* (with ``git worktree remove``) after use.

    The returned binding's root is the detached checkout so that
    ``verify_plugin_capability`` with ``verify_worktree_head=True``
    passes and every script read from the binding comes from the locked
    commit rather than the configured worktree.
    """
    base_binding = load_plugin_binding(config)
    if not base_binding.root or not os.path.isdir(base_binding.root):
        raise ValueError("plugin capability root is not a directory")
    if _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("plugin capability commit must be full lowercase 40-hex")
    if base_binding.protocol != ATTEMPT_CONTEXT_CONTRACT_VERSION:
        raise ValueError("plugin capability protocol mismatch")

    checkout_dir = tempfile.mkdtemp(prefix="plugin-checkout-")
    try:
        external_ops.run_process(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=/dev/null",
                "-c", "protocol.ext.allow=never",
                "worktree",
                "add",
                "--detach",
                checkout_dir,
                commit,
            ],
            check=True,
            env=_capability_git_env(),
            cwd=base_binding.root,
            kind="git",
        )
    except (subprocess.CalledProcessError, external_ops.ExternalCallTimeout, OSError) as exc:
        shutil.rmtree(checkout_dir, ignore_errors=True)
        raise ValueError(
            f"plugin capability checkout at {commit[:8]} failed: {exc}"
        ) from exc

    try:
        digests = _compute_capability_digests_for_commit(
            PluginBinding(
                root=checkout_dir,
                expected_commit=commit,
                bundle_digest=base_binding.bundle_digest,
                contract_version=base_binding.contract_version,
                verify_timeout_seconds=base_binding.verify_timeout_seconds,
                protocol=base_binding.protocol,
                capability_manifest_digest=base_binding.capability_manifest_digest,
                workflow_digest=base_binding.workflow_digest,
                lifecycle_digest=base_binding.lifecycle_digest,
                artifact_digest=base_binding.artifact_digest,
                schema_digests=dict(base_binding.schema_digests),
            )
        )
    except (ValueError, TypeError, external_ops.ExternalCallTimeout, OSError) as exc:
        _remove_worktree(checkout_dir)
        raise ValueError(
            f"plugin capability at {commit[:8]} is invalid: {exc}"
        ) from exc

    binding = PluginBinding(
        root=checkout_dir,
        expected_commit=commit,
        bundle_digest=digests["bundle_digest"],
        contract_version=base_binding.contract_version,
        verify_timeout_seconds=base_binding.verify_timeout_seconds,
        protocol=base_binding.protocol,
        capability_manifest_digest=digests["capability_manifest_digest"],
        workflow_digest=digests["workflow_digest"],
        lifecycle_digest=digests["lifecycle_digest"],
        artifact_digest=digests["artifact_digest"],
        schema_digests=digests["schema_digests"],
    )
    return binding, checkout_dir


def _remove_worktree(checkout_dir: str) -> None:
    with contextlib.suppress(OSError, external_ops.ExternalCallTimeout):
        external_ops.run_process(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=/dev/null",
                "-c", "protocol.ext.allow=never",
                "worktree",
                "remove",
                "--force",
                checkout_dir,
            ],
            env=_capability_git_env(),
            kind="git",
        )
    shutil.rmtree(checkout_dir, ignore_errors=True)


def _capability_git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _capability_git(
    binding: PluginBinding,
    *args: str,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return external_ops.run_process(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.ext.allow=never",
            "-C",
            binding.root,
            *args,
        ],
        text=text,
        env=_capability_git_env(),
        kind="git",
    )


def _capability_git_ok(
    binding: PluginBinding,
    *args: str,
    text: bool = True,
) -> str | bytes:
    try:
        proc = _capability_git(binding, *args, text=text)
    except (OSError, external_ops.ExternalCallTimeout) as exc:
        raise ValueError(f"plugin capability Git verification failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr if text else bytes(proc.stderr)
        detail = (
            stderr.decode("utf-8", "replace")
            if isinstance(stderr, bytes)
            else stderr
        )
        raise ValueError(
            f"plugin capability Git {args[0]} failed: {detail.strip()[:300]}"
        )
    if text:
        return str(proc.stdout).strip()
    return bytes(proc.stdout)


def _strict_capability_json(
    raw: bytes,
    label: str,
    *,
    require_canonical: bool = False,
) -> dict[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} has duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if require_canonical and raw != (canonical_json(value) + "\n").encode():
        raise ValueError(f"{label} must be canonical JSON plus one newline")
    return value


def _exact_capability_keys(
    value: Any,
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys do not match the v1 capability contract")


def _committed_capability_bytes(
    binding: PluginBinding,
    path: str,
) -> bytes:
    raw_entry_result = _capability_git_ok(
        binding,
        "ls-tree",
        "-z",
        binding.expected_commit,
        "--",
        path,
        text=False,
    )
    assert isinstance(raw_entry_result, bytes)
    raw_entry = raw_entry_result
    records = [record for record in raw_entry.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise ValueError(f"plugin capability resource {path} is not committed")
    meta, observed_path = records[0].split(b"\t", 1)
    try:
        mode, kind, _oid = meta.decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"plugin capability resource {path} is malformed") from exc
    if observed_path != path.encode() or mode != "100644" or kind != "blob":
        raise ValueError(
            f"plugin capability resource {path} must be one regular committed blob"
        )
    payload = _capability_git_ok(
        binding,
        "show",
        f"{binding.expected_commit}:{path}",
        text=False,
    )
    assert isinstance(payload, bytes)
    return payload


def _verify_digest_entry(
    binding: PluginBinding,
    entry: Any,
    path: str,
    label: str,
) -> str:
    _exact_capability_keys(entry, {"digest", "path"}, label)
    if entry["path"] != path:
        raise ValueError(f"{label} capability path mismatch")
    digest = entry["digest"]
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise ValueError(f"{label} capability digest is malformed")
    observed = compute_digest(_committed_capability_bytes(binding, path))
    if observed != digest:
        raise ValueError(f"{label} capability digest mismatch")
    return digest


def _compute_bundle_digest(binding: PluginBinding) -> str:
    raw_result = _capability_git_ok(
        binding,
        "ls-tree",
        "-r",
        "-z",
        binding.expected_commit,
        "--",
        *CAPABILITY_RESOURCE_ROOTS,
        text=False,
    )
    assert isinstance(raw_result, bytes)
    raw = raw_result
    entries: list[tuple[bytes, dict[str, str]]] = []
    for record in [item for item in raw.split(b"\0") if item]:
        if b"\t" not in record:
            raise ValueError("plugin capability bundle tree is malformed")
        meta, path = record.split(b"\t", 1)
        try:
            _mode, kind, oid = meta.decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("plugin capability bundle entry is malformed") from exc
        if kind != "blob" or path == CAPABILITY_MANIFEST_PATH.encode():
            continue
        entries.append(
            (
                path,
                {
                    "path_b64": base64.b64encode(path).decode("ascii"),
                    "blob_sha": oid,
                },
            )
        )
    canonical = (
        json.dumps(
            [entry for _, entry in sorted(entries)],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def verify_plugin_capability(
    binding: PluginBinding,
    verify_worktree_head: bool = True,
) -> VerifiedPluginCapability:
    """Verify every configured identity against one clean pinned Git commit.

    When *verify_worktree_head* is False the worktree HEAD check and dirty
    check are skipped so that a relock can verify against an arbitrary commit
    that exists in the repository without requiring it to be checked out.
    """

    if not binding.root or not os.path.isdir(binding.root):
        raise ValueError("plugin capability root is not a directory")
    if _FULL_COMMIT_RE.fullmatch(binding.expected_commit) is None:
        raise ValueError("plugin capability commit must be full lowercase 40-hex")
    if binding.protocol != ATTEMPT_CONTEXT_CONTRACT_VERSION:
        raise ValueError("plugin capability protocol mismatch")
    configured_digests = {
        "capability manifest": binding.capability_manifest_digest,
        "bundle": binding.bundle_digest,
        "workflow": binding.workflow_digest,
        "lifecycle": binding.lifecycle_digest,
        "artifact": binding.artifact_digest,
    }
    for label, digest in configured_digests.items():
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(f"plugin capability {label} digest is malformed")
    if set(binding.schema_digests) != set(CAPABILITY_SCHEMA_PATHS):
        raise ValueError("plugin capability schema digest paths are not exact")
    for path, digest in binding.schema_digests.items():
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(f"plugin capability schema digest for {path} is malformed")

    top = _capability_git_ok(binding, "rev-parse", "--show-toplevel")
    if os.path.realpath(str(top)) != os.path.realpath(binding.root):
        raise ValueError("plugin capability root must be the Git worktree root")
    head = _capability_git_ok(binding, "rev-parse", "HEAD")
    if verify_worktree_head and head != binding.expected_commit:
        raise ValueError("plugin capability worktree HEAD disagrees with pinned commit")
    kind = _capability_git_ok(
        binding,
        "cat-file",
        "-t",
        binding.expected_commit,
    )
    if kind != "commit":
        raise ValueError("plugin capability pinned object is not a commit")
    dirty = _capability_git_ok(
        binding,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if verify_worktree_head and dirty:
        raise ValueError("plugin capability worktree is dirty")

    manifest_raw = _committed_capability_bytes(
        binding,
        CAPABILITY_MANIFEST_PATH,
    )
    observed_manifest_digest = compute_digest(manifest_raw)
    if observed_manifest_digest != binding.capability_manifest_digest:
        raise ValueError("plugin capability manifest digest mismatch")
    manifest = _strict_capability_json(
        manifest_raw,
        "plugin capability manifest",
        require_canonical=True,
    )
    _exact_capability_keys(
        manifest,
        {
            "artifact_contract",
            "bundle",
            "lifecycle",
            "manifest_version",
            "protocol",
            "schemas",
            "workflow",
        },
        "plugin capability manifest",
    )
    if manifest["manifest_version"] != CAPABILITY_MANIFEST_VERSION:
        raise ValueError("plugin capability manifest version mismatch")
    if manifest["protocol"] != binding.protocol:
        raise ValueError("plugin capability manifest protocol mismatch")

    schemas = manifest["schemas"]
    if (
        not isinstance(schemas, list)
        or [
            entry.get("path") if isinstance(entry, dict) else None
            for entry in schemas
        ]
        != CAPABILITY_SCHEMA_PATHS
    ):
        raise ValueError("plugin capability schema order/paths mismatch")
    observed_schema_digests = {
        path: _verify_digest_entry(
            binding,
            entry,
            path,
            f"schema[{index}]",
        )
        for index, (entry, path) in enumerate(
            zip(schemas, CAPABILITY_SCHEMA_PATHS, strict=True)
        )
    }
    if observed_schema_digests != binding.schema_digests:
        raise ValueError("plugin capability configured schema digests mismatch")

    lifecycle_digest = _verify_digest_entry(
        binding,
        manifest["lifecycle"],
        CAPABILITY_LIFECYCLE_PATH,
        "lifecycle",
    )
    artifact_digest = _verify_digest_entry(
        binding,
        manifest["artifact_contract"],
        CAPABILITY_ARTIFACT_PATH,
        "artifact contract",
    )
    workflow_digest = _verify_digest_entry(
        binding,
        manifest["workflow"],
        CAPABILITY_WORKFLOW_PATH,
        "workflow",
    )
    if lifecycle_digest != binding.lifecycle_digest:
        raise ValueError("plugin capability configured lifecycle digest mismatch")
    if artifact_digest != binding.artifact_digest:
        raise ValueError("plugin capability configured artifact digest mismatch")
    if workflow_digest != binding.workflow_digest:
        raise ValueError("plugin capability configured workflow digest mismatch")

    bundle = manifest["bundle"]
    _exact_capability_keys(
        bundle,
        {"algorithm", "digest", "excluded_paths", "resource_roots"},
        "plugin capability bundle",
    )
    if (
        bundle["algorithm"] != CAPABILITY_BUNDLE_ALGORITHM
        or bundle["excluded_paths"] != [CAPABILITY_MANIFEST_PATH]
        or bundle["resource_roots"] != CAPABILITY_RESOURCE_ROOTS
    ):
        raise ValueError("plugin capability bundle contract mismatch")
    observed_bundle = _compute_bundle_digest(binding)
    if bundle["digest"] != observed_bundle or observed_bundle != binding.bundle_digest:
        raise ValueError("plugin capability bundle digest mismatch")

    lifecycle = _strict_capability_json(
        _committed_capability_bytes(binding, CAPABILITY_LIFECYCLE_PATH),
        "plugin lifecycle",
    )
    try:
        lifecycle_capability = lifecycle["capabilities"]["attempt_context"]
    except (KeyError, TypeError) as exc:
        raise ValueError("plugin lifecycle capability is missing") from exc
    if (
        not isinstance(lifecycle_capability, dict)
        or lifecycle_capability.get("protocol") != binding.protocol
        or lifecycle_capability.get("schemas") != CAPABILITY_SCHEMA_PATHS
    ):
        raise ValueError("plugin lifecycle capability identity mismatch")

    return VerifiedPluginCapability(
        protocol=binding.protocol,
        plugin_commit=binding.expected_commit,
        capability_manifest_digest=observed_manifest_digest,
        bundle_digest=observed_bundle,
        workflow_digest=workflow_digest,
        lifecycle_digest=lifecycle_digest,
        artifact_digest=artifact_digest,
        schema_digests=observed_schema_digests,
    )


def load_implement_stage_resources(
    binding: PluginBinding,
    *,
    verify_worktree_head: bool = True,
) -> tuple[CommittedStageResource, ...]:
    """Load Implement workflow bytes from the admitted commit, never the index."""

    verify_plugin_capability(binding, verify_worktree_head=verify_worktree_head)
    resources: list[CommittedStageResource] = []
    for relative_path, plugin_path in IMPLEMENT_STAGE_RESOURCE_PATHS.items():
        content = _committed_capability_bytes(binding, plugin_path)
        resources.append(
            CommittedStageResource(
                relative_path=relative_path,
                content=content,
                digest=compute_digest(content),
            )
        )
    return tuple(resources)


def load_review_stage_resources(
    binding: PluginBinding,
    review_phase: ReviewPhase | str,
    *,
    verify_worktree_head: bool = True,
) -> tuple[CommittedStageResource, ...]:
    """Load one Review workflow from the admitted commit, never the index."""

    try:
        phase = ReviewPhase(review_phase)
    except (TypeError, ValueError) as exc:
        raise ValueError("review_phase must be continuous or final") from exc
    verify_plugin_capability(binding, verify_worktree_head=verify_worktree_head)
    resources: list[CommittedStageResource] = []
    for relative_path, plugin_path in REVIEW_STAGE_RESOURCE_PATHS[
        phase
    ].items():
        content = _committed_capability_bytes(binding, plugin_path)
        resources.append(
            CommittedStageResource(
                relative_path=relative_path,
                content=content,
                digest=compute_digest(content),
            )
        )
    return tuple(resources)


def find_script(binding: PluginBinding, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        candidate = os.path.join(binding.root, "scripts", name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _redact(text: str, limit: int = 500) -> str:
    """Redact plugin output before it crosses the controller boundary."""
    redacted = _SECRET_RE.sub(r"\1=[REDACTED]", text)
    if len(redacted) > limit:
        redacted = redacted[:limit] + "…(truncated)"
    return redacted


def _v1_materialization_failure(
    code: str,
    retryable: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "detail": detail,
        "failure_code": code,
        "retryable": retryable,
        "verified": False,
    }


def _implement_actor_result_with_outcome(
    actor_result: dict[str, Any],
) -> dict[str, Any]:
    """Bridge the controller actor result to the current Plugin contract.

    The pinned Plugin seals an explicit ``outcome`` actor-result shape (APPLIED
    carries a schema-required ``verification_record``).  The controller already
    models the full shape and carries the actor's own ``verification_record``
    faithfully, so this adapter only supplies the default APPLIED outcome for a
    legacy three-field result.  It never synthesizes a verification_record.
    """
    expanded = dict(actor_result)
    expanded.setdefault("outcome", "APPLIED")
    return expanded


def invoke_implement_materializer(
    binding: PluginBinding,
    request: dict[str, Any],
    *,
    verify_worktree_head: bool = True,
) -> dict[str, Any]:
    """Invoke the v1 sealer and admit either a direct receipt or exact failure."""

    try:
        verify_plugin_capability(binding, verify_worktree_head=verify_worktree_head)
    except (TypeError, ValueError) as exc:
        return _v1_materialization_failure(
            "PROTOCOL_VERSION_MISMATCH",
            False,
            f"pinned Plugin capability changed: {exc}",
        )
    script = find_script(binding, MATERIALIZE_SCRIPT_CANDIDATES)
    if script is None:
        return _v1_materialization_failure(
            "PLUGIN_CONTRACT_MISMATCH",
            False,
            f"no materialize-handoff entrypoint under {binding.root!r}/scripts",
        )
    adapted_request = dict(request)
    actor_result = request.get("actor_result")
    if isinstance(actor_result, dict):
        adapted_request["actor_result"] = _implement_actor_result_with_outcome(
            actor_result
        )
    result = _invoke_script(binding, script, adapted_request)
    result_keys = set(result)
    if result_keys in (
        IMPLEMENT_RECEIPT_FIELDS,
        IMPLEMENT_RECEIPT_DISPUTED_FIELDS,
        IMPLEMENT_RECEIPT_BLOCKED_FIELDS,
    ):
        return result
    if (
        set(result) == IMPLEMENT_FAILURE_FIELDS
        and result.get("verified") is False
        and isinstance(result.get("failure_code"), str)
        and bool(result["failure_code"])
        and isinstance(result.get("retryable"), bool)
        and isinstance(result.get("detail"), str)
    ):
        return result
    return _v1_materialization_failure(
        "PLUGIN_CONTRACT_MISMATCH",
        False,
        "Implement materializer returned neither a direct v1 receipt nor an exact failure",
    )


def invoke_review_materializer(
    binding: PluginBinding,
    request: dict[str, Any],
    *,
    verify_worktree_head: bool = True,
) -> dict[str, Any]:
    """Invoke the v1 Review sealer and admit a direct receipt or exact failure."""

    try:
        verify_plugin_capability(binding, verify_worktree_head=verify_worktree_head)
    except (TypeError, ValueError) as exc:
        return _v1_materialization_failure(
            "PROTOCOL_VERSION_MISMATCH",
            False,
            f"pinned Plugin capability changed: {exc}",
        )
    script = find_script(binding, MATERIALIZE_SCRIPT_CANDIDATES)
    if script is None:
        return _v1_materialization_failure(
            "PLUGIN_CONTRACT_MISMATCH",
            False,
            f"no materialize-handoff entrypoint under {binding.root!r}/scripts",
        )
    result = _invoke_script(binding, script, request)
    if set(result) == REVIEW_RECEIPT_FIELDS:
        return result
    if (
        set(result) == IMPLEMENT_FAILURE_FIELDS
        and result.get("verified") is False
        and isinstance(result.get("failure_code"), str)
        and bool(result["failure_code"])
        and isinstance(result.get("retryable"), bool)
        and isinstance(result.get("detail"), str)
    ):
        return result
    return _v1_materialization_failure(
        "PLUGIN_CONTRACT_MISMATCH",
        False,
        "Review materializer returned neither a direct v1 receipt nor an exact failure",
    )


def project_feedback(
    binding: PluginBinding,
    *,
    worktree: str,
    state_root: str,
    window: int = 3,
    verify_worktree_head: bool = True,
) -> dict[str, Any]:
    """Build the Plugin-owned non-authoritative feedback projection."""

    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ValueError("feedback projection window must be positive")
    verify_plugin_capability(binding, verify_worktree_head=verify_worktree_head)
    script = os.path.join(binding.root, "scripts", "attempt-context.py")
    if not os.path.isfile(script):
        raise ValueError("pinned Plugin has no attempt-context projection primitive")
    projection_root = os.path.join(state_root, "feedback-projection")
    os.makedirs(projection_root, mode=0o700, exist_ok=True)
    output_path = os.path.join(projection_root, "projection.json")
    markdown_path = os.path.join(projection_root, "projection.md")
    argv = [
        sys.executable,
        script,
        "project-feedback",
        "--index",
        os.path.join(worktree, ".dev-dispatch", "feedback", "index.json"),
        "--root",
        worktree,
        "--window",
        str(window),
        "--output",
        output_path,
        "--markdown",
        markdown_path,
    ]
    try:
        proc = external_ops.run_process(
            argv,
            text=True,
            env=_capability_git_env(),
            kind="plugin",
        )
    except (OSError, external_ops.ExternalCallTimeout) as exc:
        raise ValueError(f"feedback projection invocation failed: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(
            "feedback projection failed: "
            + _redact((proc.stderr or proc.stdout or "").strip())
        )
    try:
        with open(output_path, "rb") as handle:
            raw_projection = handle.read()
        with open(markdown_path, encoding="utf-8") as handle:
            markdown = handle.read()
    except OSError as exc:
        raise ValueError(f"feedback projection output is unreadable: {exc}") from exc
    projection = _strict_capability_json(
        raw_projection,
        "feedback projection",
        require_canonical=True,
    )
    digest = compute_digest(raw_projection)
    if proc.stdout.strip() != digest:
        raise ValueError("feedback projection digest output mismatch")
    if (
        projection.get("contract_version")
        != ATTEMPT_CONTEXT_CONTRACT_VERSION
        or projection.get("authoritative") is not False
        or projection.get("window") != window
    ):
        raise ValueError("feedback projection identity is malformed")
    return {
        "digest": digest,
        "json": projection,
        "markdown": markdown,
    }


def _invoke_script(binding: PluginBinding, script: str, request: dict[str, Any]) -> dict[str, Any]:
    """Invoke the sole v1 materialization primitive and fail closed."""
    argv = [sys.executable, script] if script.endswith(".py") else ["bash", script]
    try:
        proc = external_ops.run_process(
            argv,
            input=json.dumps(request),
            text=True,
            kind="plugin",
        )
    except external_ops.ExternalCallTimeout:
        return _v1_materialization_failure(
            "PROVIDER_UNAVAILABLE",
            True,
            f"{os.path.basename(script)} timed out",
        )
    except OSError as e:
        return _v1_materialization_failure(
            "PROVIDER_UNAVAILABLE",
            True,
            f"{os.path.basename(script)} spawn failed: {e}",
        )
    try:
        receipt: Any = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return _v1_materialization_failure(
            "PLUGIN_CONTRACT_MISMATCH",
            False,
            f"{os.path.basename(script)} returned non-JSON output (exit {proc.returncode}): {_redact(proc.stderr or proc.stdout)}",
        )
    if not isinstance(receipt, dict):
        return _v1_materialization_failure(
            "PLUGIN_CONTRACT_MISMATCH", False, f"{os.path.basename(script)} receipt is not a JSON object"
        )
    return receipt
