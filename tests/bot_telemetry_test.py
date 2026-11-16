"""Exercise real Python SDK exports and bot RPC behavior against a controlled HTTP server."""

import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import autoplay
import bot_observations as observations
import bot_telemetry as telemetry
import check_bot_traces
import weaver_samples


class Handler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    mode = "ok"
    result: ClassVar[dict] = {"state": "SELECTING_HAND"}

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((request, dict(self.headers)))
        if self.mode == "invalid_json":
            body = b"not json"
        else:
            reply = {"jsonrpc": "2.0", "id": request["id"]}
            if self.mode == "error":
                reply["error"] = {"code": -32602, "message": "Rejected"}
            elif self.mode == "wrong_id":
                reply.update(id=-100, result={})
            else:
                reply["result"] = self.result
            body = json.dumps(reply).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class BotTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spans = InMemorySpanExporter()
        cls.logs = InMemoryLogRecordExporter()
        cls.metrics = InMemoryMetricReader()
        cls.runtime = telemetry.Runtime(span_exporter=cls.spans, metric_reader=cls.metrics, log_exporter=cls.logs)
        telemetry._runtime = cls.runtime
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.runtime.traces.force_flush()
        cls.runtime.logs.force_flush()
        data = cls.metrics.get_metrics_data()
        out = os.getenv("BOT_TEST_OUTPUT")
        if out:
            folder = Path(out)
            folder.mkdir(parents=True, exist_ok=True)
            exports = {
                "bot-client-traces": encode_spans(cls.spans.get_finished_spans()),
                "bot-client-logs": encode_logs(cls.logs.get_finished_logs()),
                "bot-client-metrics": encode_metrics(data),
            }
            for name, payload in exports.items():
                (folder / f"{name}.json").write_text(json.dumps(MessageToDict(payload)))
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.runtime.shutdown()
        telemetry._runtime = None

    def setUp(self):
        Handler.mode = "ok"
        Handler.result = {"state": "SELECTING_HAND"}
        self.runtime.logs.force_flush()
        self.log_start = len(self.logs.get_finished_logs())

    def finished(self):
        self.runtime.traces.force_flush()
        return self.spans.get_finished_spans()

    def test_success_parentage_and_header(self):
        with telemetry.run("BOTTEST", "BLUE", "WHITE") as root:
            with telemetry.decision("SELECTING_HAND", False) as decision:
                result = autoplay.rpc(self.port, "play", cards=[0, 1])
            root.set_attribute("balatro.bot.run.outcome", "game_over")
        self.assertEqual(result["state"], "SELECTING_HAND")
        spans = self.finished()
        call = next(s for s in reversed(spans) if s.kind == SpanKind.CLIENT)
        self.assertEqual(call.parent.span_id, decision.get_span_context().span_id)
        self.assertEqual(call.context.trace_id, root.get_span_context().trace_id)
        request, headers = Handler.requests[-1]
        parent = headers["Traceparent"].split("-")
        self.assertEqual(int(parent[1], 16), call.context.trace_id)
        self.assertEqual(int(parent[2], 16), call.context.span_id)
        self.assertEqual(call.attributes["jsonrpc.request.id"], str(request["id"]))
        self.assertNotIn("cards", call.attributes)
        self.assertEqual(call.attributes["balatro.run.seed"], "BOTTEST")

    def test_rpc_error_and_recovery_log(self):
        Handler.mode = "error"
        with telemetry.decision("SHOP", False):
            with self.assertRaises(autoplay.RPCError) as caught:
                autoplay.rpc(self.port, "buy", card=0)
            self.assertEqual(caught.exception.kind, "jsonrpc_error")
            telemetry.recovery("SHOP")
        call = next(s for s in reversed(self.finished()) if s.kind == SpanKind.CLIENT)
        self.assertEqual(call.status.status_code, StatusCode.ERROR)
        self.assertEqual(call.attributes["rpc.response.status_code"], "-32602")
        self.runtime.logs.force_flush()
        record = self.logs.get_finished_logs()[-1].log_record
        self.assertEqual(record.event_name, "balatro.bot.recovery")
        self.assertEqual(record.trace_id, call.context.trace_id)

    def test_protocol_and_transport_errors(self):
        for mode in ("wrong_id", "invalid_json"):
            Handler.mode = mode
            with self.assertRaises(autoplay.RPCError):
                autoplay.rpc(self.port, "health")
        before = len(Handler.requests)
        with patch("urllib.request.urlopen", side_effect=TimeoutError()), self.assertRaises(autoplay.RPCError):
            autoplay.rpc(self.port, "play", cards=[0])
        self.assertEqual(len(Handler.requests), before, "no transparent replay of an uncertain action")
        calls = [s for s in self.finished() if s.kind == SpanKind.CLIENT]
        self.assertTrue(all(s.status.status_code == StatusCode.ERROR for s in calls[-3:]))

    def test_unknown_method_bounded_metrics(self):
        for method in ("custom_a", "custom_b"):
            autoplay.rpc(self.port, method)
        spans = [s for s in self.finished() if s.attributes.get("rpc.method_original")]
        self.assertEqual([s.name for s in spans[-2:]], ["jsonrpc", "jsonrpc"])
        data = self.metrics.get_metrics_data()
        points = [
            p
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "rpc.client.call.duration"
            for p in metric.data.data_points
        ]
        unknown = [p for p in points if p.attributes["rpc.method"] == "_OTHER"]
        self.assertEqual(len(unknown), 1)
        self.assertGreaterEqual(unknown[0].count, 2)
        for point in points:
            self.assertNotIn("rpc.method_original", point.attributes)
            self.assertNotIn("jsonrpc.request.id", point.attributes)
            self.assertNotIn("balatro.run.seed", point.attributes)

    def test_heuristic_is_explained_and_ids_are_unique(self):
        cards = [{"value": {"rank": rank, "suit": "Hearts"}} for rank in "23456"]
        self.assertEqual(autoplay.best_hand(cards), [4, 3, 2, 1, 0])
        selection = next(s for s in reversed(self.finished()) if s.name == "balatro.bot.choose_hand")
        self.assertEqual(selection.attributes["balatro.bot.strategy"], "flush")
        autoplay.rpc(self.port, "health")
        autoplay.rpc(self.port, "health")
        self.assertNotEqual(Handler.requests[-1][0]["id"], Handler.requests[-2][0]["id"])

    def hand_state(self):
        return {
            "state": "SELECTING_HAND",
            "round_num": 1,
            "ante_num": 1,
            "money": 4,
            "hand": {
                "count": 3,
                "limit": 8,
                "highlighted_limit": 5,
                "cards": [
                    {"id": 10 + i, "key": "H_" + rank, "value": {"rank": rank, "suit": "H"}, "state": {}}
                    for i, rank in enumerate("234")
                ],
            },
            "round": {"hands_left": 4, "discards_left": 3, "chips": 0},
            "cards": {"count": 49, "cards": [{"key": "SECRET_DRAW_ORDER"}]},
            "seed": "SECRET_SEED",
            "future_field": "SECRET_EXTENSION",
        }

    def observation_events(self):
        self.runtime.logs.force_flush()
        return [r.log_record for r in self.logs.get_finished_logs()[self.log_start :]]

    def test_observations_export_as_structured_otlp_with_native_context(self):
        Handler.result = self.hand_state()
        Handler.result["hand"]["cards"][1].update(
            state={"hidden": True}, key="SECRET_FACE_DOWN", value={"rank": "A", "effect": "SECRET_EFFECT"}
        )
        with telemetry.run("BOTTEST", "BLUE", "WHITE") as root, observations.recording({"policy": "test"}):
            autoplay.rpc(self.port, "gamestate")
            with telemetry.decision("SELECTING_HAND", False) as decision:
                autoplay.rpc(self.port, "play", cards=[2, 0])
            observations.outcome("step_limit", Handler.result)
        events = self.observation_events()
        request = next(e for e in events if e.event_name == "balatro.bot.action.requested")
        result = next(e for e in events if e.event_name == "balatro.bot.action.result")
        self.assertEqual(tuple(request.attributes["balatro.bot.command.parameters"]["cards"]), (2, 0))
        self.assertEqual(request.attributes["balatro.bot.observation.before"]["hand"]["cards"][0]["id"], 10)
        self.assertEqual(
            request.attributes["balatro.bot.decision.span_id"], f"{decision.get_span_context().span_id:016x}"
        )
        client = next(s for s in reversed(self.finished()) if s.kind == SpanKind.CLIENT)
        self.assertEqual((request.trace_id, request.span_id), (client.context.trace_id, client.context.span_id))
        self.assertEqual((result.trace_id, result.span_id), (request.trace_id, request.span_id))
        self.assertEqual(events[0].span_id, root.get_span_context().span_id)
        self.assertEqual(
            result.attributes["balatro.bot.request.sequence"], request.attributes["balatro.bot.event.sequence"]
        )
        self.assertEqual(result.attributes["balatro.bot.result.status"], "completed")
        self.assertNotIn("error.type", result.attributes)
        # Test the actual OTLP encoder and Weaver adapter, not a JSON replacement transport.
        exported = self.logs.get_finished_logs()[self.log_start :]
        payload = MessageToDict(encode_logs(exported))
        samples = [s["log"] for s in weaver_samples.samples(payload) if "log" in s]
        sample = next(s for s in samples if s["event_name"] == request.event_name)
        attrs = {a["name"]: a["value"] for a in sample["attributes"]}
        self.assertEqual(attrs["balatro.bot.command.parameters"], {"cards": [2, 0]})
        self.assertEqual(attrs["balatro.bot.observation.before"]["remaining_deck"], {"count": 49})
        self.assertNotIn("SECRET_", json.dumps(attrs["balatro.bot.observation.before"]))
        after = next(s for s in samples if s["event_name"] == result.event_name)
        self.assertNotIn(
            "SECRET_",
            json.dumps(next(a["value"] for a in after["attributes"] if a["name"] == "balatro.bot.observation.after")),
        )
        self.assertTrue(all(e.timestamp and e.observed_timestamp for e in events))
        self.assertEqual([e.attributes["balatro.bot.event.sequence"] for e in events], list(range(1, len(events) + 1)))

    def test_intervention_persists_and_before_state_is_current(self):
        Handler.result = self.hand_state()
        with telemetry.run("BOTTEST", "BLUE", "WHITE"), observations.recording({}):
            autoplay.rpc(self.port, "gamestate")
            Handler.result["round"]["chips"] = 10**9
            autoplay.rpc(self.port, "set", chips=10**9)
            autoplay.rpc(self.port, "discard", cards=[0])
            autoplay.rpc(self.port, "play", cards=[2, 1])
            observations.outcome("max_rounds", Handler.result)
        events = self.observation_events()
        intervention = next(e for e in events if e.event_name == "balatro.bot.intervention.requested")
        self.assertEqual(intervention.attributes["balatro.bot.command.parameters"], {"chips": 10**9})
        self.assertEqual(intervention.attributes["balatro.bot.observation.before"]["round"]["chips"], 0)
        actions = [e for e in events if e.event_name == "balatro.bot.action.requested"]
        self.assertTrue(all(e.attributes["balatro.bot.ever_assisted"] for e in actions))
        self.assertTrue(all(e.attributes["balatro.bot.observation.before"]["round"]["chips"] == 10**9 for e in actions))
        self.assertTrue(events[-1].attributes["balatro.bot.ever_assisted"])

    def test_uncertain_commands_are_not_replayed_or_resolved_by_recovery(self):
        Handler.result = self.hand_state()
        before = len(Handler.requests)
        with telemetry.run("BOTTEST", "BLUE", "WHITE"), observations.recording({}):
            autoplay.rpc(self.port, "gamestate")
            for method, params in (("play", {"cards": [0]}), ("add", {"key": "j_joker"})):
                with patch("urllib.request.urlopen", side_effect=TimeoutError()), self.assertRaises(autoplay.RPCError):
                    autoplay.rpc(self.port, method, **params)
                autoplay.rpc(self.port, "gamestate")
            autoplay.rpc(self.port, "discard", cards=[1])
            observations.outcome("step_limit", Handler.result)
        events = self.observation_events()
        self.assertEqual(len(Handler.requests) - before, 4)
        results = [e for e in events if e.event_name.endswith(".result")]
        self.assertEqual(
            [e.attributes["balatro.bot.result.status"] for e in results], ["unresolved", "unresolved", "completed"]
        )
        self.assertNotIn("balatro.bot.observation.after", results[0].attributes)
        self.assertEqual(results[0].attributes["error.type"], "transport_error")
        self.assertEqual(results[0].severity_number.value, 13)
        self.assertFalse(events[-1].attributes["balatro.bot.ever_assisted"])
        self.assertTrue(events[-1].attributes["balatro.bot.assistance_uncertain"])

    def test_observation_failure_preserves_commands_and_marks_incomplete(self):
        Handler.result = self.hand_state()
        with telemetry.run("BOTTEST", "BLUE", "WHITE"), observations.recording({}):
            autoplay.rpc(self.port, "gamestate")
            with (
                patch.object(observations, "observation", side_effect=ValueError("observer bug")),
                self.assertLogs("bot_observations", "WARNING"),
            ):
                result = autoplay.rpc(self.port, "set", chips=10**9)
            self.assertEqual(result, Handler.result)
            autoplay.rpc(self.port, "gamestate")
            autoplay.rpc(self.port, "play", cards=[0])
        end = self.observation_events()[-1]
        self.assertTrue(end.attributes["balatro.bot.observation.incomplete"])
        self.assertTrue(end.attributes["balatro.bot.assistance_uncertain"])

    def test_observation_rpc_errors_and_episode_isolation(self):
        Handler.result = self.hand_state()
        with telemetry.run("BOTTEST", "BLUE", "WHITE"):
            with self.assertRaises(autoplay.RPCError), observations.recording({}):
                autoplay.rpc(self.port, "gamestate")
                Handler.mode = "error"
                autoplay.rpc(self.port, "discard", cards=[0])
            Handler.mode = "ok"
            with observations.recording({}):
                autoplay.rpc(self.port, "gamestate")
                autoplay.rpc(self.port, "play", cards=[1])
                observations.outcome("game_over", {"state": "GAME_OVER"})
        events = self.observation_events()
        ends = [e for e in events if e.event_name == "balatro.bot.episode.end"]
        self.assertEqual([e.attributes["balatro.bot.result.status"] for e in ends], ["error", "finished"])
        failed = next(e for e in events if e.event_name == "balatro.bot.action.result")
        self.assertEqual(failed.attributes["rpc.response.status_code"], "-32602")
        self.assertEqual(failed.attributes["balatro.bot.result.status"], "unresolved")
        self.assertNotEqual(ends[0].attributes["balatro.bot.episode.id"], ends[1].attributes["balatro.bot.episode.id"])

    def test_autoplay_records_discard_play_and_outcome_before_cleanup(self):
        for force_rounds in (0, 1):
            with self.subTest(force_rounds=force_rounds):
                state = self.hand_state()
                state["round"]["hands_left"] = 1
                methods = []

                def respond(request, state=state, methods=methods, **_):
                    message = json.loads(request.data)
                    method = message["method"]
                    methods.append(method)
                    if method in ("gamestate", "menu"):
                        result = {"state": "MENU"}
                    elif method == "health":
                        result = {"status": "ok"}
                    elif method == "play":
                        result = {**state, "state": "GAME_OVER", "won": False}
                    else:
                        if method == "set":
                            state["round"].update(message["params"])
                        result = state
                    return io.StringIO(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}))

                args = SimpleNamespace(
                    seed="TEST", deck="BLUE", stake="WHITE", port=1, force_rounds=force_rounds, max_rounds=12
                )
                with (
                    telemetry.run("TEST", "BLUE", "WHITE") as root,
                    observations.recording({}),
                    patch("urllib.request.urlopen", side_effect=respond),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    autoplay.play_run(args, root)
                events = self.observation_events()
                episode_id = events[-1].attributes["balatro.bot.episode.id"]
                actions = [
                    e
                    for e in events
                    if e.event_name == "balatro.bot.action.requested"
                    and e.attributes["balatro.bot.episode.id"] == episode_id
                ]
                self.assertEqual([e.attributes["rpc.method"] for e in actions], ["discard", "play"])
                self.assertEqual(actions[0].attributes["balatro.bot.observation.before"]["round"]["chips"], 0)
                self.assertEqual(
                    actions[1].attributes["balatro.bot.observation.before"]["round"]["chips"],
                    10**9 if force_rounds else 0,
                )
                self.assertEqual(events[-1].attributes["balatro.bot.ever_assisted"], bool(force_rounds))
                final = next(e for e in reversed(events) if e.event_name == "balatro.bot.episode.outcome")
                self.assertEqual(final.attributes["balatro.bot.observation.final"]["state"], "GAME_OVER")
                self.assertEqual(final.attributes["balatro.bot.run.outcome"], "game_over")
                self.assertEqual(methods[-1], "menu")

    def test_correlation_gate_rejects_missing_links_and_wrong_parents(self):
        client = {
            "spanId": "client",
            "traceId": "command",
            "name": "play",
            "resource": {"service.name": "balatro-autoplay"},
            "attrs": {"balatro.bot.role": "client"},
        }
        server = {
            "spanId": "server",
            "traceId": "command",
            "parentSpanId": "client",
            "name": "play",
            "resource": {"service.name": "balatrobot"},
            "attrs": {"balatro.bot.role": "server", "rpc.method": "play", "session.id": "session"},
        }
        hand = {
            "spanId": "hand",
            "traceId": "game",
            "name": "balatro.hand",
            "resource": {"service.name": "balatro"},
            "attrs": {"session.id": "session"},
            "links": [{"spanId": "server", "traceId": "command"}],
        }
        with (
            patch.object(check_bot_traces, "traces", return_value={"trace": [client, server, hand]}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(check_bot_traces.check(0, 1), [])
            hand["links"] = []
            self.assertTrue(check_bot_traces.check(0, 1))
            server["parentSpanId"] = "wrong"
            self.assertTrue(check_bot_traces.check(0, 1))


if __name__ == "__main__":
    unittest.main()
