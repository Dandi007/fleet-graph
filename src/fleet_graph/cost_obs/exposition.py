"""Prometheus text exposition for the cost-observability data plane.

The data plane is producer-shaped: every source fact the five
`cost-observability` recording rules consume is emitted here as a labelled
sample, rendered to the Prometheus text format, and -- in the acceptance
fixture -- written to a file and parsed back so the scrape -> query roundtrip
is what gets tested, not an in-memory shortcut.

The format is the one node_exporter's textfile collector and Prometheus's own
scrape endpoint agree on, so the same bytes could be served over an HTTP
`/metrics` or dropped in a `*.prom` textfile without any translation. Label
values are escaped per the exposition wire format; values are rendered as
integers when integral and as floats otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

_ESCAPE_TABLE = str.maketrans({"\\": r"\\", "\n": r"\n", '"': r"\""})


def escape_label_value(value: str) -> str:
    """Backslash, newline and double-quote, as the exposition format demands."""
    return str(value).translate(_ESCAPE_TABLE)


@dataclass(frozen=True)
class Sample:
    """One emitted source fact: a metric name plus its label set and value.

    Labels are kept as a sorted tuple of pairs so a Sample is hashable and
    comparable -- which makes both idempotent emission and query-side grouping
    deterministic.
    """

    name: str
    labels: tuple[tuple[str, str], ...] = ()
    value: float = 0.0

    def label_map(self) -> dict[str, str]:
        return dict(self.labels)

    def label_block(self) -> str:
        if not self.labels:
            return ""
        inner = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in self.labels)
        return "{" + inner + "}"


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def render(samples: Iterable[Sample]) -> str:
    """Serialize samples to Prometheus text exposition.

    Ordering is deterministic (metric name, then label-set order as emitted)
    so the file is a reproducible artifact, not a hash of iteration order.
    """
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.name, []).append(sample)
    lines: list[str] = []
    for name in sorted(grouped):
        for sample in grouped[name]:
            lines.append(f"{name}{sample.label_block()} {_format_value(sample.value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def parse(text: str) -> list[Sample]:
    """Parse the exposition text back into samples.

    Only the subset of the format this data plane renders is required: a
    metric name, an optional ``{label="value", ...}`` block, and one numeric
    value. This is what the acceptance fixture feeds into the query engine,
    so an emitted fact that cannot roundtrip through this parser is a
    scrape-wiring bug and fails loudly.
    """
    samples: list[Sample] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        brace = line.find("{")
        space = line.find(" ")
        if brace != -1 and (space == -1 or brace < space):
            name = line[:brace].strip()
            rest = line[brace + 1 :]
            close = rest.rfind("}")
            labels_text = rest[:close]
            value_text = rest[close + 1 :].strip()
        else:
            name = line[:space].strip()
            labels_text = ""
            value_text = line[space + 1 :].strip()
        if not name or not value_text:
            raise ValueError(f"exposition line {lineno} is not a sample: {line!r}")
        labels = _parse_labels(labels_text, lineno)
        try:
            value = float(value_text)
        except ValueError as exc:
            raise ValueError(
                f"exposition line {lineno} has a non-numeric value: {value_text!r}"
            ) from exc
        samples.append(Sample(name=name, labels=labels, value=value))
    return samples


def _parse_labels(text: str, lineno: int) -> tuple[tuple[str, str], ...]:
    labels: list[tuple[str, str]] = []
    if not text.strip():
        return ()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"exposition line {lineno} has a malformed label: {part!r}")
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not (value.startswith('"') and value.endswith('"')):
            raise ValueError(f"exposition line {lineno} label {key!r} is not quoted: {value!r}")
        labels.append((key, _unescape_label(value[1:-1])))
    return tuple(sorted(labels))


def _unescape_label(value: str) -> str:
    """Undo `escape_label_value` so a round-tripped label matches the original."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            out.append({"\\": "\\", '"': '"', "n": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


__all__ = ["Sample", "escape_label_value", "parse", "render"]
