"""Structured OpenTelemetry events for bot observations and interventions.

Only the Python autoplay process is observed. No additional RPCs or game calculations run.
"""

import contextlib
import contextvars
import copy
import logging
import uuid

import bot_telemetry as telemetry
from opentelemetry._logs import SeverityNumber
from opentelemetry.semconv._incubating.attributes import jsonrpc_attributes as JSONRPC
from opentelemetry.semconv._incubating.attributes import rpc_attributes as RPC
from opentelemetry.semconv.attributes import error_attributes as ERROR

OBSERVATION_VERSION = 1
_active = contextvars.ContextVar("bot_observations", default=None)


def pick(value, keys):
    return {k: copy.deepcopy(value[k]) for k in keys if k in value}


def card_observation(card):
    state = pick(card.get("state", {}), ("debuff", "hidden", "highlight"))
    result = {"state": state, **pick(card, ("id",))}
    if not state.get("hidden"):
        result.update(pick(card, ("key", "set", "label")))
        for field, keys in (
            ("value", ("rank", "suit", "effect")),
            ("modifier", ("seal", "edition", "enhancement", "eternal", "perishable", "rental")),
            ("cost", ("buy", "sell")),
        ):
            if field in card:
                result[field] = pick(card[field], keys)
    return result


def observation(state):
    """Project BalatroBot 1.5.2 responses; never copy the ordered remaining deck."""
    result = pick(state, ("state", "round_num", "ante_num", "money", "won", "deck", "stake"))
    if "round" in state:
        result["round"] = pick(
            state["round"], ("hands_left", "hands_played", "discards_left", "discards_used", "reroll_cost", "chips")
        )
    for area in ("hand", "jokers", "consumables", "shop", "vouchers", "packs", "pack"):
        if isinstance(state.get(area), dict):
            result[area] = pick(state[area], ("count", "limit", "highlighted_limit"))
            result[area]["cards"] = [card_observation(c) for c in state[area].get("cards", [])]
    if isinstance(state.get("cards"), dict):
        result["remaining_deck"] = pick(state["cards"], ("count", "limit"))
    if "hands" in state:
        result["hands"] = {
            name: pick(hand, ("order", "level", "chips", "mult", "played", "played_this_round"))
            for name, hand in state["hands"].items()
        }
    if "blinds" in state:
        result["blinds"] = {
            slot: pick(blind, ("type", "status", "name", "effect", "score", "tag_name", "tag_effect"))
            for slot, blind in state["blinds"].items()
        }
    if "used_vouchers" in state:
        result["used_vouchers"] = {
            key: value for key, value in state["used_vouchers"].items() if isinstance(value, str)
        }
    return result


class Recorder:
    def __init__(self):
        self.episode_id = str(uuid.uuid4())
        self.sequence = 0
        self.current = None
        self.ever_assisted = False
        self.assistance_uncertain = False
        self.incomplete = False

    def emit(self, kind, attributes=None):
        self.sequence += 1
        attrs = {
            "balatro.bot.episode.id": self.episode_id,
            "balatro.bot.event.sequence": self.sequence,
            "balatro.bot.observation.version": OBSERVATION_VERSION,
            "balatro.bot.ever_assisted": self.ever_assisted,
            "balatro.bot.assistance_uncertain": self.assistance_uncertain,
            "balatro.bot.observation.incomplete": self.incomplete,
            **(attributes or {}),
        }
        severity = SeverityNumber.WARN if ERROR.ERROR_TYPE in attrs else SeverityNumber.INFO
        telemetry.event("balatro.bot." + kind, attrs, severity)

    def begin(self, method, params, request_id, decision_span_id):
        if method not in ("play", "discard", "set", "add"):
            return None
        kind = "intervention" if method in ("set", "add") else "action"
        token = {"kind": kind, "request_sequence": self.sequence + 1}
        attributes = {
            RPC.RPC_METHOD: method,
            JSONRPC.JSONRPC_REQUEST_ID: str(request_id),
            "balatro.bot.command.parameters": copy.deepcopy(params),
        }
        if self.current is not None:
            attributes["balatro.bot.observation.before"] = copy.deepcopy(self.current)
        if decision_span_id:
            attributes["balatro.bot.decision.span_id"] = decision_span_id
        self.emit(kind + ".requested", attributes)
        return token

    def finish(self, token, result=None, error=None):
        after = observation(result) if isinstance(result, dict) and isinstance(result.get("state"), str) else None
        if token:
            completed = error is None and after is not None
            if token["kind"] == "intervention":
                self.ever_assisted |= completed
                self.assistance_uncertain |= not completed
            attributes = {
                "balatro.bot.request.sequence": token["request_sequence"],
                "balatro.bot.result.status": "completed" if completed else "unresolved",
            }
            if not completed:
                attributes[ERROR.ERROR_TYPE] = (
                    getattr(error, "kind", type(error).__name__) if error else "missing_state"
                )
            code = getattr(error, "code", None)
            if code is not None:
                attributes[RPC.RPC_RESPONSE_STATUS_CODE] = str(code)
            if after is not None:
                attributes["balatro.bot.observation.after"] = after
            self.emit(token["kind"] + ".result", attributes)
        # Recovery reads update the next observation without resolving an uncertain action.
        if after is not None:
            self.current = after
        elif token or error:
            self.current = None


def observe(recorder, operation, *args, intervention=False, **kwargs):
    """An observer failure must not alter a command, its response, or its exception."""
    try:
        return operation(*args, **kwargs)
    except Exception:
        recorder.current = None
        recorder.incomplete = True
        recorder.assistance_uncertain |= intervention
        logging.getLogger(__name__).warning("Bot observation failed", exc_info=True)
        return None


@contextlib.contextmanager
def recording(metadata):
    recorder = Recorder()
    token = _active.set(recorder)
    try:
        observe(recorder, recorder.emit, "episode.start", {"balatro.bot.run.config": copy.deepcopy(metadata)})
        try:
            yield recorder
        except BaseException as error:
            observe(
                recorder,
                recorder.emit,
                "episode.end",
                {"balatro.bot.result.status": "error", ERROR.ERROR_TYPE: type(error).__name__},
            )
            raise
        else:
            observe(recorder, recorder.emit, "episode.end", {"balatro.bot.result.status": "finished"})
    finally:
        _active.reset(token)


def outcome(stop_reason, state):
    recorder = _active.get()
    if recorder:

        def emit():
            recorder.emit(
                "episode.outcome",
                {"balatro.bot.run.outcome": stop_reason, "balatro.bot.observation.final": observation(state)},
            )

        observe(recorder, emit)


@contextlib.contextmanager
def request(method, params, request_id, decision_span_id):
    recorder = _active.get()
    intervention = method in ("set", "add")
    token = (
        observe(recorder, recorder.begin, method, params, request_id, decision_span_id, intervention=intervention)
        if recorder
        else None
    )
    response = {}
    try:
        yield response
    except BaseException as error:
        if recorder:
            observe(recorder, recorder.finish, token, error=error, intervention=intervention)
        raise
    else:
        if recorder:
            observe(recorder, recorder.finish, token, result=response.get("result"), intervention=intervention)
