"""A minimal PromQL evaluator for the cost-observability recording rules.

This is deliberately a *subset*, not a re-implementation. It evaluates the
expressions the five recording rules use and nothing else, so the acceptance
fixture can query the rules directly rather than against a hand-rolled result.
The subset is chosen from the correctness risks the spec names -- duplicate
events, ordering, missing labels, and PromQL vector matching -- and each is
exercised by a rule or a test:

- instant vector selectors with ``=`` / ``!=`` / ``=~`` / ``!~`` label matches
  (regex matches are anchored, as Prometheus anchors them);
- ``sum`` / ``count`` aggregation, with ``by`` / ``without`` grouping;
- binary ``/``, scalar when both sides aggregate to a single sample, and
  vector-matching otherwise via ``on(...)`` / ``ignoring(...)``.

Anything outside that subset raises `PromQLError` instead of silently guessing
-- a silent wrong answer is exactly the failure mode this data plane exists to
end.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from fleet_graph.cost_obs.exposition import Sample

_MATCH_OPS = frozenset({"=", "!=", "=~", "!~"})
_BINARY_OPS = frozenset({"+", "-", "*", "/"})
_AGGREGATORS = frozenset({"sum", "count"})
_KEYWORDS = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})


class PromQLError(ValueError):
    """The expression steps outside the evaluated subset, or fails to parse."""


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(expr: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buffer: list[str] = []
            while j < n and expr[j] != '"':
                if expr[j] == "\\" and j + 1 < n:
                    buffer.append(expr[j + 1])
                    j += 2
                else:
                    buffer.append(expr[j])
                    j += 1
            if j >= n:
                raise PromQLError("unterminated string")
            tokens.append(_Token("STRING", "".join(buffer), i))
            i = j + 1
            continue
        two = expr[i : i + 2]
        if two in _MATCH_OPS or two in _BINARY_OPS:
            tokens.append(_Token("OP", two, i))
            i += 2
            continue
        if ch in "+-*/=":
            tokens.append(_Token("OP", ch, i))
            i += 1
            continue
        if ch in "{}()":
            kind = {"{": "LBRACE", "}": "RBRACE", "(": "LPAREN", ")": "RPAREN"}[ch]
            tokens.append(_Token(kind, ch, i))
            i += 1
            continue
        if ch == ",":
            tokens.append(_Token("COMMA", ch, i))
            i += 1
            continue
        if ch.isalpha() or ch == "_" or ch == ":":
            j = i
            while j < n and (expr[j].isalnum() or expr[j] in "_:"):
                j += 1
            tokens.append(_Token("NAME", expr[i:j], i))
            i = j
            continue
        raise PromQLError(f"unexpected character {ch!r} at position {i}")
    tokens.append(_Token("EOF", "", n))
    return tokens


@dataclass
class _Selector:
    name: str
    matchers: list[tuple[str, str, str]]


@dataclass
class _Agg:
    op: str
    inner: object
    modifier: tuple[str, tuple[str, ...]] | None


@dataclass
class _Binary:
    op: str
    left: object
    right: object
    matching: tuple[str, tuple[str, ...]] | None


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> _Token:
        return self.tokens[self.pos]

    def next(self) -> _Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str) -> _Token:
        if self.peek().kind != kind:
            raise PromQLError(
                f"expected {kind} at position {self.peek().pos}, got {self.peek().text!r}"
            )
        return self.next()

    def parse(self) -> object:
        node = self.parse_primary()
        while self.peek().kind == "OP" and self.peek().text in _BINARY_OPS:
            op = self.next().text
            matching = self.parse_matching_modifier()
            right = self.parse_primary()
            node = _Binary(op, node, right, matching)
        return node

    def parse_matching_modifier(self) -> tuple[str, tuple[str, ...]] | None:
        if self.peek().kind == "NAME" and self.peek().text in {"on", "ignoring"}:
            keyword = self.next().text
            self.expect("LPAREN")
            labels = self.parse_label_list()
            self.expect("RPAREN")
            return (keyword, labels)
        if self.peek().kind == "NAME" and self.peek().text in {"group_left", "group_right"}:
            raise PromQLError("group_left/group_right is outside the evaluated subset")
        return None

    def parse_label_list(self) -> tuple[str, ...]:
        labels: list[str] = []
        while self.peek().kind == "NAME":
            labels.append(self.next().text)
            if self.peek().kind == "COMMA":
                self.next()
        return tuple(labels)

    def parse_primary(self) -> object:
        token = self.peek()
        if token.kind == "NAME" and token.text in _AGGREGATORS:
            op = self.next().text
            self.expect("LPAREN")
            inner = self.parse()
            self.expect("RPAREN")
            modifier = self.parse_agg_modifier()
            return _Agg(op, inner, modifier)
        return self.parse_selector()

    def parse_agg_modifier(self) -> tuple[str, tuple[str, ...]] | None:
        if self.peek().kind == "NAME" and self.peek().text in {"by", "without"}:
            keyword = self.next().text
            self.expect("LPAREN")
            labels = self.parse_label_list()
            self.expect("RPAREN")
            return (keyword, labels)
        return None

    def parse_selector(self) -> _Selector:
        name = self.expect("NAME").text
        matchers: list[tuple[str, str, str]] = []
        if self.peek().kind == "LBRACE":
            self.next()
            while self.peek().kind != "RBRACE":
                label = self.expect("NAME").text
                op = self.peek()
                if op.kind != "OP" or op.text not in _MATCH_OPS:
                    raise PromQLError(f"expected a label matcher at position {op.pos}")
                self.next()
                value = self.expect("STRING").text
                matchers.append((label, op.text, value))
                if self.peek().kind == "COMMA":
                    self.next()
            self.next()
        return _Selector(name, matchers)


def _matcher_matches(value: str | None, op: str, expected: str) -> bool:
    if value is None:
        return op in {"!=", "!~"}
    if op == "=":
        return value == expected
    if op == "!=":
        return value != expected
    if op == "=~":
        return re.fullmatch(expected, value) is not None
    if op == "!~":
        return re.fullmatch(expected, value) is None
    raise PromQLError(f"unknown matcher {op!r}")


@dataclass
class _Eval:
    samples: list[Sample]
    _by_name: dict[str, list[Sample]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for sample in self.samples:
            self._by_name.setdefault(sample.name, []).append(sample)

    def eval(self, node: object) -> list[Sample]:
        if isinstance(node, _Selector):
            return self._eval_selector(node)
        if isinstance(node, _Agg):
            return self._eval_agg(node)
        if isinstance(node, _Binary):
            return self._eval_binary(node)
        raise PromQLError(f"unknown node {node!r}")

    def _eval_selector(self, node: _Selector) -> list[Sample]:
        result: list[Sample] = []
        for sample in self._by_name.get(node.name, []):
            labels = sample.label_map()
            if all(
                _matcher_matches(labels.get(label), op, expected)
                for label, op, expected in node.matchers
            ):
                result.append(sample)
        return result

    def _eval_agg(self, node: _Agg) -> list[Sample]:
        vector = self.eval(node.inner)
        if not vector:
            # Aggregating no series yields no series -- an absent source fact
            # leaves its recording rule silent, which is exactly the failure
            # mode this data plane exists to surface rather than hide as a 0.
            return []
        if node.modifier is None:
            # Aggregation without grouping collapses to a single scalar sample.
            value = sum(m.value for m in vector) if node.op == "sum" else float(len(vector))
            return [Sample(name="", labels=(), value=value)]
        grouped: dict[tuple[tuple[str, str], ...], list[Sample]] = {}
        for sample in vector:
            key = self._group_key(sample.label_map(), node.modifier)
            grouped.setdefault(key, []).append(sample)
        result: list[Sample] = []
        for key, members in grouped.items():
            value = sum(m.value for m in members) if node.op == "sum" else float(len(members))
            result.append(Sample(name="", labels=key, value=value))
        return result

    def _group_key(
        self, labels: dict[str, str], modifier: tuple[str, tuple[str, ...]] | None
    ) -> tuple[tuple[str, str], ...]:
        if modifier is None:
            return ()
        keyword, keep = modifier
        if keyword == "by":
            return tuple((k, labels[k]) for k in keep if k in labels)
        if keyword == "without":
            return tuple((k, v) for k, v in sorted(labels.items()) if k not in keep)
        raise PromQLError(f"unknown aggregation modifier {keyword!r}")

    def _eval_binary(self, node: _Binary) -> list[Sample]:
        left = self.eval(node.left)
        right = self.eval(node.right)
        if node.op != "/":
            raise PromQLError("only '/' binary arithmetic is in the evaluated subset")
        if not left or not right:
            return []
        left_scalar = len(left) == 1 and not left[0].labels
        right_scalar = len(right) == 1 and not right[0].labels
        if left_scalar and right_scalar:
            return [Sample(name="", labels=(), value=left[0].value / right[0].value)]
        result: list[Sample] = []
        for l_sample in left:
            for r_sample in right:
                if self._match_pair(l_sample.label_map(), r_sample.label_map(), node.matching):
                    merged = dict(l_sample.label_map())
                    for k, v in r_sample.label_map().items():
                        merged.setdefault(k, v)
                    key = tuple(sorted(merged.items()))
                    result.append(
                        Sample(name="", labels=key, value=l_sample.value / r_sample.value)
                    )
        return result

    @staticmethod
    def _match_pair(
        left: dict[str, str], right: dict[str, str], matching: tuple[str, tuple[str, ...]] | None
    ) -> bool:
        if matching is None:
            common = set(left) & set(right)
            return all(left[k] == right[k] for k in common)
        keyword, labels = matching
        if keyword == "on":
            return all(left.get(k) == right.get(k) for k in labels)
        if keyword == "ignoring":
            all_keys = (set(left) | set(right)) - set(labels)
            return all(left.get(k) == right.get(k) for k in all_keys)
        raise PromQLError(f"unknown matching keyword {keyword!r}")


def query(expr: str, samples: Iterable[Sample]) -> list[Sample]:
    """Evaluate a PromQL expression against samples and return the result vector."""
    tokens = _tokenize(expr)
    parser = _Parser(tokens)
    node = parser.parse()
    if parser.peek().kind != "EOF":
        trailing = parser.peek()
        raise PromQLError(f"trailing content at position {trailing.pos}: {trailing.text!r}")
    return _Eval(list(samples)).eval(node)


__all__ = ["PromQLError", "query"]
