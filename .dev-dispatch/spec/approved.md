# E4b: Gate verdict input normalization

## Goal

Make the DD human-gate decision boundary **wide in input, strict in output**.
Humans and approved publishers may express the two existing verdicts with a
small, documented set of transport/Markdown wrappers. The gate must
mechanically remove only those wrappers and emit exactly `APPROVE` or
`REJECT`. Every other input remains fail-closed as
`GATE_VERDICT_UNRECOGNIZED`.

This is the follow-up deliberately excluded by the still-running replay fix
`dev-fg-a964e9a77251`. That development ID remains unchanged and in flight;
E4b must not modify, resume, or otherwise operate on it.

## Current Boundary And Defect

`Board.decision_for()` correctly establishes provenance: only
`work.decision.v1`/`work.decision.v2` messages that reference the exact gate
question are candidates, and the newest candidate wins. `BoardGate.act()` then
does `decision.decision.strip().upper()` and compares the resulting arbitrary
string to `APPROVE`/`REJECT`.

That preserves the safety property but rejects common non-semantic shells such
as a quoted token, a single-token Markdown code span, a single-token fenced
block, or an explicit `decision:`/`verdict:` label. Conversely, accepting a
sentence because it contains an approval word would turn a parser into a
policy engine and can accidentally release a merge.

## Contract

### Trusted input remains narrow

Normalization is attempted only after the existing board read path has proved
all of the following:

1. The message kind is one of the existing decision kinds.
2. The message references the exact `question_note_id` of this gate.
3. The selected message is the current newest matching decision.

No note, card, message body, `rationale`, `question`, actor argument, or MCP
tool parameter is an alternate verdict input. The gate tool continues to take
no verdict argument. Existing v1/v2 schema validation, ref validation, and
newest-message semantics are unchanged.

### Mechanical de-shelling grammar

The implementation shall expose one pure normalizer used by `BoardGate` (not
by an agent or publisher). It receives the raw `payload.decision` string and
returns either the canonical token or no value. It must not consult identity,
prose, locale, rationale, or a language model.

Apply these transformations in this exact order, at most once per numbered
wrapper; after every transformation trim only leading/trailing ASCII
whitespace (`space`, `tab`, `CR`, `LF`).

1. Start with the complete field. An absent/non-string decision is invalid;
   coercion of arbitrary JSON values is forbidden.
2. Remove one outer Markdown quote prefix only when every non-empty line has
   exactly one `>` followed by at most one space. Remove that prefix from every
   line. Mixed quoted/unquoted lines are invalid.
3. Remove one matching inline code shell only when the complete value is one
   backtick-delimited token: `` `...` ``. Interior backticks are invalid.
4. Otherwise remove one matching fenced-code shell only when the complete
   value has an opening line of exactly three backticks (optional ASCII info
   string is not allowed), one non-empty content line, and a closing line of
   exactly three backticks. Extra content lines are invalid.
5. Remove one ASCII label prefix, case-insensitively, only when the complete
   remaining value matches `decision:` or `verdict:` followed by whitespace.
6. ASCII-uppercase the complete remaining value. Accept only the exact bytes
   `APPROVE` or `REJECT`; no suffix, prefix, punctuation, Unicode lookalike,
   synonym, or second token is accepted.

Steps 3 and 4 are alternatives, not recursive unwrapping. A label can follow a
quote or a code shell only if that resulting complete value itself meets step
5. This makes the accepted language finite and reviewable. The normalizer must
not split lines, select a first word, scan for a token, strip punctuation,
repair spelling, or interpret natural language.

### Canonical output and audit record

On success, downstream gate logic and the sealed gate receipt use only the
canonical uppercase `APPROVE` or `REJECT`. For an approval, the existing
receipt fields remain byte-compatible and `decision` is the canonical token.
Add provenance sufficient to reproduce the normalization decision without
changing authorization semantics: the decision message ID is retained and the
record includes the raw decision field plus a stable normalization form name
(`bare`, `quote`, `inline_code`, `fenced_code`, or `label`) when a shell was
removed. Do not persist a transformed token as the raw value.

On failure, keep the current `GATE_VERDICT_UNRECOGNIZED` refusal code and
include a safely represented raw value in the error detail. Failure produces
no gate receipt, merge action, or inferred rejection. A canonical `REJECT`
continues to produce the existing `GATE_REJECTED` behavior.

## Defect-Family Samples

| Family | Raw `decision` | Required result |
| --- | --- | --- |
| Bare casing/space | `  approve\r\n` | `APPROVE`, form `bare` |
| Quoted transport copy | `> reject` | `REJECT`, form `quote` |
| Inline Markdown | `` `Approve` `` | `APPROVE`, form `inline_code` |
| Single-token fence | ` ```\nREJECT\n``` ` | `REJECT`, form `fenced_code` |
| Explicit label | `Verdict: approve` | `APPROVE`, form `label` |
| Shell plus label | `> decision: REJECT` | `REJECT`, form `label` (raw retained) |
| Narrative approval | `APPROVE because checks pass` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Ambiguous prose | `I approve this` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Multiple choices | `APPROVE\nREJECT` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Mixed quote | `> APPROVE\nreason` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Fenced prose | ` ```\nAPPROVE\nreason\n``` ` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Punctuation/lookalike | `APPROVE!` / `ΑPPROVE` | refuse `GATE_VERDICT_UNRECOGNIZED` |
| Non-decision reply | `work.note.v1` with `APPROVE` text | still no decision |
| Wrong-question decision | valid `APPROVE` referencing another question | still no decision |

## Minimal Implementation Scope

1. Add the pure normalizer adjacent to the gate/board decision boundary, with
   a typed result that distinguishes canonical token, raw value, and form.
2. Change `BoardGate.act()` to consume that result before its existing allowed
   decision check and receipt construction.
3. Add focused unit tests for every row above plus existing plain
   `APPROVE`/`REJECT`, v1 and v2 candidates, newest-wins, and refusal codes.
4. Update only documentation required to describe the exact accepted grammar.

Do not change bus protocol registration, decision publication authority,
`development_gate` tool arguments, ref selection, card state, replay logic,
attempt-context behavior, deployment configuration, or production services.

## Executable Acceptance

The development must make the following commands pass in its isolated
worktree. Tests must assert behavior, not merely helper implementation
details: at least one path must construct a board decision and execute
`BoardGate.act()` through approval, rejection, and unrecognized refusal.

```dd-acceptance
uv sync --frozen
make verify
```

Acceptance is complete only when `make verify` includes the new focused tests
and all pre-existing board/gate tests remain green. No deployment, restart, or
production-checkout operation is part of this development.

## Delivery Constraints

All business-code changes and all code review are owned by dev-dispatch. Git
inspection, H0 construction, and acceptance execution occur only in an
independent `/data/worktrees/` worktree. The production checkout must not be
checked out, switched, reset, detached, written, tested, deployed, or restarted.
