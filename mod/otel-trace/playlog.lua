-- PlayLog (https://github.com/nh6574/PlayLog) keeps a human-readable log of every action in a run.
-- When it is installed, each line it writes also becomes a log record (event balatro.playlog.entry,
-- body = the line as plain text) correlated with the span that was open, and PlayLog's run ID
-- (its playlog_runs/<id>.txt file name) goes on every span and log record during the run, so a
-- line in a log backend leads to its trace and back.
local M = {}

M.RUN_ID = "balatro.playlog.run_id"

-- PlayLog's run ID for the current run, or nil without PlayLog or outside a run.
function M.run_id()
  local id = G and G.GAME and G.GAME.playlog_run_id
  return id and tostring(id)
end

-- Wraps PlayLog's line writer, which every line passes through (PlayLog's UI looks the function
-- up on its log_store table at each call). Returns true once attached; false while PlayLog is
-- absent, so the caller can try again later. The line is still written if recording it fails.
function M.install(otel, current_span)
  local store = rawget(_G, "PlayLog") and PlayLog.log_store
  if not (store and store.append_rich_segments and store.plain_from_segments) then return false end
  if store.otel_wrapped then return true end
  local append = store.append_rich_segments
  store.append_rich_segments = function(segments, ...)
    local ok, err = pcall(function()
      local line = store.plain_from_segments(segments)
      if line == "" then return end
      otel.log("INFO", line, { [M.RUN_ID] = M.run_id() }, current_span(), "PlayLog", "balatro.playlog.entry")
    end)
    if not ok then pcall(otel.log, "WARN", "otel hook failed: " .. tostring(err), nil, current_span()) end
    return append(segments, ...)
  end
  store.otel_wrapped = true
  return true
end

return M
