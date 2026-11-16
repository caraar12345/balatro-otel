# Bot observation events

Autoplay emits structured OpenTelemetry log events for play/discard observations and debug
interventions. They use the existing `balatro-autoplay` Logs SDK, resource, batching, OTLP endpoint
and Collector pipeline. Start autoplay normally; no application file writer or extra output flag
is involved. Storage and training-data extraction are downstream concerns.

## Events and correlation

| Event | Occurrence and data |
| --- | --- |
| `balatro.bot.episode.start` | Episode begins; `balatro.bot.run.config` contains policy identity, seed, deck, stake, instrumentation version and force/round limits. |
| `balatro.bot.action.requested` | Before a `play`/`discard` request: method, request ID, exact parameters and preceding observation. |
| `balatro.bot.action.result` | API response or failure: request sequence, resolution status and resulting observation when available. |
| `balatro.bot.intervention.requested` | Before a debug `set`/`add`: requested changes and preceding observation. |
| `balatro.bot.intervention.result` | Debug response or failure and updated assistance provenance. |
| `balatro.bot.episode.outcome` | Bot stop reason and final observation, captured before menu cleanup. |
| `balatro.bot.episode.end` | Normal termination or exception, including episode assistance/completeness flags. |

The [generated reference](bot-signals.md) defines their attributes. Registry schema `0.4.1` adds
these events; instrumentation/package version remains `0.4.0`. Structured observation format version
1 is identified by `balatro.bot.observation.version`.

Each log has an episode ID and monotonically increasing event sequence. A result's
`balatro.bot.request.sequence` refers to its requested event in that episode. The Logs SDK attaches
the active trace/span context: action requests/results belong to the RPC client span; episode events
belong to the bot run. Follow the client's server child and linked game spans to inspect scoring.
`balatro.bot.decision.span_id` also identifies the enclosing loop iteration in the same trace.
These references are log attributes/context, not metric labels.

Structured fields use native OTLP map/array attributes, not JSON strings. This follows the pinned
[event conventions](https://github.com/open-telemetry/semantic-conventions/blob/v1.44.0/docs/general/events.md),
which reserve the body for display text and put structured event details in attributes. The registry
uses `type: any` for observations/configuration/parameters; their detailed contract is documented
here and exercised through the real SDK encoder in tests. Weaver checks the event/attribute
definitions; it does not validate every nested card property or game rule.

## Observation projection

`balatro.bot.observation.before`, `.after` and `.final` contain an explicit projection of BalatroBot
1.5.2 responses: held cards, ordered Jokers/consumables, visible offers, card values/modifiers/effect
descriptions, money, round counters, hand levels, blind descriptions, used vouchers and area limits.
Absent fields remain absent. The remaining deck's contents/order and the seed are excluded from
observations; deck count/limit remain. Face-down cards retain only their API ID and state flags.
Unknown future API fields are excluded.

`balatro.bot.command.parameters.cards` preserves zero-based indices into the preceding observation's
`hand.cards` array in **request order**, not necessarily engine scoring order. API card IDs are
BalatroBot `sort_id` values, not the scoring plugin's `balatro.card.instance.id`; observation positions
distinguish duplicates. Shared card identity is future work.

The latest successful state-bearing response supplies the next preceding observation, including
successful forced mutations. Capture introduces no additional RPCs or game calculations. The API
does not expose every evolving ability counter as machine-readable data, and the projection is not
a complete legal-action mask or simulator state. The heuristic still receives the original API
response; observation redaction does not prove that its decisions used only player-visible data.
Seed and debug parameters are provenance, not policy input features.

## Results, assistance and loss

`balatro.bot.result.status=completed` means a state was returned after the API completion condition.
Transport/protocol/RPC failures and missing state results produce `unresolved`; the latter uses
`error.type=missing_state`. RPC/transport/protocol failures retain their existing error categories.
An RPC error may follow partial execution, so it is not treated as guaranteed rejection. Recovery
reads update the next observation without resolving or retrying the uncertain command. Completion
events use INFO; unresolved results and episode exceptions use WARN with `error.type`.

`balatro.bot.ever_assisted` stays true after a debug `set`/`add` returns a state successfully.
`balatro.bot.assistance_uncertain` stays true after an unresolved debug mutation or an observer
failure around one. Requests carry prior provenance; result/end events include its consequences.
For unassisted evaluation, exclude entire episodes if either flag is true on the end event. Only
mutations through this process's RPC helper are tracked; external changes are not detected.

Episode outcome records separate `game_over`/`max_rounds`/`step_limit` from the final observation's
`won`, when available. Episode-end `finished` means normal player exit and includes configured
truncation; it does not imply a won game or a naturally completed run.

Observer failures preserve command results/exceptions, report a Python warning, clear stale cached
state and set `balatro.bot.observation.incomplete` on subsequent events. Sequence gaps, unmatched
request/results and missing end events help identify incomplete episodes. They cannot prove complete
delivery: logs use the existing SDK queue, batching and shutdown lifecycle and can be dropped during
overflow, exporter failures or hard termination. Trace sampling is independent of log emission;
linked spans may be absent. Backends must support structured log attributes to retain nested data.

## Verification

- [x] Registry manifest is versioned `definition_manifest/2.0`; local signals/attributes have `balatro.*` names and stability declarations.
- [x] Standard RPC/JSON-RPC/error attributes are referenced from semantic conventions v1.44.0, not redeclared.
- [x] Existing counters retain names without `_total`; duration histograms retain seconds.
- [x] Templates use v2 grouped helpers; resolved event shape was inspected before extending the event selection.
- [x] CI already runs registry check, generated-reference drift checks, baseline diff and live-check on SDK-encoded logs.
- [~] Generated language-code formatting is inapplicable: the generated artifact is Markdown; source Python uses pinned Ruff.
- [x] Tests cover structured OTLP values, correlation, redaction, assistance, unresolved commands, observer failure and full autoplay-loop capture.
- [x] Twelve Python tests and all three Lua suites pass; pinned Ruff and diff whitespace checks pass.
- [x] Weaver registry check and SDK-sample live-check pass with zero violations; an invalid sequence-type fixture is rejected.
- [x] Regenerated bot reference matches a second generation, scoring reference is unchanged, and registry baseline diff completes.
- [x] Changed source, registry, templates and generated reference were re-read; remaining delivery and nested-schema limits are stated above.
- [ ] Live game delivery for these observation events has not been exercised.
