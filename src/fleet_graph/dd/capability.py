"""Capability lock: verify the contract bundle before dispatching anything.

dd pins every contract file by sha256 in `attempt-context-capability.json`.
Before an attempt is dispatched, the bundle it will be given is checked against
that manifest, and a mismatch stops the dispatch.

The reason is supply chain: an attempt runs with a set of contracts that tell
it what shape its output must take and what it is allowed to touch. If those
can be altered between being written and being handed over, the guarantees
they encode are worth nothing. Pinning them makes tampering loud.

**Fail-closed is the whole point.** Every failure mode here -- a missing file,
an unreadable manifest, an unparseable digest, an algorithm we do not
recognise -- refuses the dispatch. A verifier that falls back to "allow" when
confused is not a verifier; it is a delay before the same outcome.

plan.md lists this under what must not be cut during the refactor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).parent / "contracts"
MANIFEST_NAME = "attempt-context-capability.json"
SUPPORTED_DIGEST_ALGORITHMS = frozenset({"sha256"})


class CapabilityError(RuntimeError):
    """Verification could not be completed, or did not pass. Do not dispatch."""


@dataclass(frozen=True)
class PinnedFile:
    path: str
    digest: str

    @property
    def algorithm(self) -> str:
        return self.digest.split(":", 1)[0] if ":" in self.digest else ""

    @property
    def hexdigest(self) -> str:
        return self.digest.split(":", 1)[1] if ":" in self.digest else ""


@dataclass
class VerificationResult:
    matched: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatched and not self.missing and bool(self.matched)

    def raise_if_failed(self) -> VerificationResult:
        if self.ok:
            return self
        raise CapabilityError(
            "capability lock failed -- refusing to dispatch: "
            f"{len(self.mismatched)} mismatched {sorted(self.mismatched)}, "
            f"{len(self.missing)} missing {sorted(self.missing)}"
        )


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    if algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
        raise CapabilityError(f"unsupported digest algorithm {algorithm!r}")
    return f"{algorithm}:{hashlib.new(algorithm, path.read_bytes()).hexdigest()}"


class CapabilityLock:
    def __init__(self, manifest: dict[str, Any], contracts_dir: Path) -> None:
        self.manifest = manifest
        self.contracts_dir = contracts_dir

    @classmethod
    def load(cls, contracts_dir: Path | str = CONTRACTS_DIR) -> CapabilityLock:
        directory = Path(contracts_dir)
        manifest_path = directory / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Cannot read the manifest -> cannot verify -> do not dispatch.
            raise CapabilityError(f"cannot read capability manifest: {exc}") from exc
        return cls(manifest, directory)

    @property
    def manifest_version(self) -> str:
        return str(self.manifest.get("manifest_version", ""))

    @property
    def protocol(self) -> str:
        return str(self.manifest.get("protocol", ""))

    def pinned_files(self) -> list[PinnedFile]:
        """Every file the manifest pins, from all three places it lists them."""
        pinned: list[PinnedFile] = []
        for key in ("artifact_contract", "lifecycle"):
            entry = self.manifest.get(key)
            if isinstance(entry, dict) and entry.get("path") and entry.get("digest"):
                pinned.append(PinnedFile(entry["path"], entry["digest"]))
        for entry in self.manifest.get("schemas", []):
            if isinstance(entry, dict) and entry.get("path") and entry.get("digest"):
                pinned.append(PinnedFile(entry["path"], entry["digest"]))
        return pinned

    def verify(self) -> VerificationResult:
        pinned = self.pinned_files()
        if not pinned:
            raise CapabilityError(
                "capability manifest pins no files; an empty allowlist verifies nothing"
            )

        result = VerificationResult()
        for entry in pinned:
            if entry.algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
                raise CapabilityError(
                    f"{entry.path} pinned with unsupported algorithm {entry.algorithm!r}"
                )
            target = self.contracts_dir / Path(entry.path).name
            if not target.is_file():
                result.missing.append(entry.path)
                continue
            if file_digest(target, entry.algorithm) == entry.digest:
                result.matched.append(entry.path)
            else:
                result.mismatched.append(entry.path)
        return result

    def require(self) -> VerificationResult:
        """Verify, or raise. This is what a dispatch path should call."""
        return self.verify().raise_if_failed()


__all__ = [
    "CONTRACTS_DIR",
    "MANIFEST_NAME",
    "CapabilityError",
    "CapabilityLock",
    "PinnedFile",
    "VerificationResult",
    "file_digest",
]
