-- Offline check of playlog.lua against a stand-in for PlayLog's log store: each line becomes a
-- balatro.playlog.entry record correlated with the open span and carrying PlayLog's run ID, the
-- line is still written (also when recording fails), and nothing attaches without PlayLog.
-- Writes <dir>/playlog-traces.json and playlog-logs.json for Weaver live-check.
-- Usage (from the repo root): luajit tests/playlog_test.lua <dir>
local smods = os.getenv("SMODS_DIR") or (os.getenv("HOME") .. "/Library/Application Support/Balatro/Mods/smods")
package.path = smods .. "/libs/json/?.lua;" .. "mod/otel-trace/?.lua;" .. package.path
local otel = require "otel"
local playlog = require "playlog"

local function by_key(list)
  local t = {}
  for _, kv in ipairs(list) do t[kv.key] = kv.value end
  return t
end

-- Without PlayLog there is nothing to attach to, and no run ID
G = { GAME = {} }
assert(playlog.install(otel, function() end) == false)
assert(playlog.run_id() == nil)

-- PlayLog's store as far as the bridge uses it
local written = {}
PlayLog = { log_store = {} }
local function to_plain(segments)
  local out = {}
  for i, seg in ipairs(segments) do out[i] = seg.text end
  return table.concat(out)
end
PlayLog.log_store.plain_from_segments = to_plain
function PlayLog.log_store.append_rich_segments(segments)
  written[#written + 1] = to_plain(segments)
  return "appended"
end

local session = otel.uuid()
otel.resource["service.instance.id"] = session
otel.common["session.id"] = session
G.GAME.playlog_run_id = "20261121_164730_502528"
otel.common["balatro.run.seed"] = "TEST1234"
otel.common[playlog.RUN_ID] = playlog.run_id()
local run = otel.start("balatro.run", { ["balatro.deck.name"] = "Plasma Deck", ["balatro.stake"] = 2, ["balatro.run.resumed"] = false })
local current = run
assert(playlog.install(otel, function() return current end) == true)
assert(playlog.install(otel, function() return current end) == true, "installing twice wraps once")

local store = PlayLog.log_store
assert(store.append_rich_segments({ { text = "4:47:30 PM " }, { text = "Started game on Plasma Deck" } }) == "appended")
assert(store.append_rich_segments({}) == "appended", "an empty line is written but not recorded")

-- A failure while recording must not lose the line
local plain = store.plain_from_segments
store.plain_from_segments = function() error("boom") end
assert(store.append_rich_segments({ { text = "kept" } }) == "appended")
store.plain_from_segments = plain
assert(#written == 3 and written[1] == "4:47:30 PM Started game on Plasma Deck" and written[3] == "kept")

local records = otel.logs["PlayLog"]
assert(records and #records == 1, "one record per non-empty line")
local rec = records[1]
assert(rec.eventName == "balatro.playlog.entry")
assert(rec.body.stringValue == "4:47:30 PM Started game on Plasma Deck")
assert(rec.traceId == run.traceId and rec.spanId == run.spanId, "correlated with the open span")
local a = by_key(rec.attributes)
assert(a[playlog.RUN_ID].stringValue == "20261121_164730_502528")
assert(a["balatro.run.seed"].stringValue == "TEST1234" and a["session.id"].stringValue == session)

local warnings = otel.logs["balatro-otel-trace"]
assert(warnings and #warnings == 1 and warnings[1].body.stringValue:find("boom", 1, true), "the failure was logged")
otel.logs["balatro-otel-trace"] = nil -- the deliberate failure is not a fixture

otel.finish(run, { ["balatro.run.outcome"] = "won", ["balatro.ante"] = 9, ["balatro.round"] = 24, ["balatro.dollars"] = 12 })
local span_attrs = by_key(otel.buffer[1].attributes)
assert(span_attrs[playlog.RUN_ID].stringValue == "20261121_164730_502528", "the run ID is on spans")

if arg[1] then
  local function write(name, body)
    local f = assert(io.open(arg[1] .. "/" .. name .. ".json", "w")); f:write(body); f:close()
  end
  write("playlog-traces", otel.encode(otel.buffer))
  write("playlog-logs", otel.encode_logs(otel.logs))
end
io.stderr:write("ok: PlayLog lines as correlated log records, run ID on spans and logs, failures keep the line\n")
