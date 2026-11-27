-- DaVinci Director — bridge between DaVinci Resolve and the DaVinci Director app.
-- Workspace > Scripts > DaVinci Director
--
-- Why Lua and why files (all verified on DaVinci Resolve 21.2 free):
--   * Resolve 21 free lists only Lua scripts in Workspace > Scripts and runs them in
--     a sandbox: no io, os.execute, ffi, require or sockets; bmd.copyfile fails too
--     (it is implemented with io); writing into %APPDATA% is refused.
--   * What the sandbox still allows, inside %TEMP%:
--       - MediaStorage:GetSubFolderList(path)  -> the app sends commands as
--         folder names in  <bridge>\in ;
--       - MediaPool:ExportMetadata(path, {clip}) -> we answer with small files whose
--         NAMES carry the payload in <bridge>\out  (Timeline:Export as fallback);
--       - Timeline:Export(path, EXPORT_OTIO)    -> full timeline state in <bridge>\state .
--   * The script keeps running (bmd.wait between polls), watches the timeline for
--     changes made by the editor in Resolve and reports them, so the transcript in
--     the app always mirrors the real timeline.
--
-- Running the script again replaces the running instance.

local BRIDGE_VERSION = "2.1"
local BASE = [[__BRIDGE_DIR__]]
if BASE:find("__BRIDGE" .. "_DIR__", 1, true) then   -- default: %TEMP%\DaVinciDirector\bridge
  BASE = (os.getenv("TEMP") or os.getenv("TMP") or "C:\\Windows\\Temp") .. "\\DaVinciDirector\\bridge"
end
local IN, OUT, STATE = BASE .. "\\in", BASE .. "\\out", BASE .. "\\state"
local CHUNK = 120            -- hex chars per out file name (keeps paths < 260)
local TOKEN_KEY = "DaVinciDirector.BridgeToken"

local token = string.format("%x%x", os.time(), math.floor(os.clock() * 1000000) % 1048576)
local sid = token:sub(-6)
fusion:SetData(TOKEN_KEY, token)

------------------------------------------------------------------ helpers

local function hex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function unhex(h)
  return (h:gsub("%x%x", function(x) return string.char(tonumber(x, 16)) end))
end

local function jenc(v)
  local t = type(v)
  if t == "nil" then return "null" end
  if t == "boolean" then return tostring(v) end
  if t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
    return string.format("%.6f", v)
  end
  if t == "string" then
    return '"' .. (v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end)) .. '"'
  end
  if t == "table" then
    local out = {}
    if #v > 0 or next(v) == nil then
      for i = 1, #v do out[i] = jenc(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    for k, x in pairs(v) do
      if type(k) == "string" then out[#out + 1] = jenc(k) .. ":" .. jenc(x) end
    end
    return "{" .. table.concat(out, ",") .. "}"
  end
  return jenc(tostring(v))
end

local function parse(s)
  local f, err = loadstring("return " .. s)
  if not f then return nil, err end
  setfenv(f, {})
  local ok, v = pcall(f)
  if not ok then return nil, v end
  return v
end

local function project() return resolve:GetProjectManager():GetCurrentProject() end
local function timeline() local p = project(); return p and p:GetCurrentTimeline() end

-- Any media pool clip: ExportMetadata needs one to write a (tiny) file.
local carrier, carrierProject = nil, nil
local function carrierClip()
  local p = project()
  if not p then return nil end
  if carrier and carrierProject == p:GetName() then return carrier end
  carrier, carrierProject = nil, p:GetName()
  local tl = p:GetCurrentTimeline()
  if tl then
    for _, tt in ipairs({ "video", "audio" }) do
      for ti = 1, tl:GetTrackCount(tt) do
        local l = tl:GetItemListInTrack(tt, ti)
        if type(l) == "table" then
          for _, it in ipairs(l) do
            local mpi = it:GetMediaPoolItem()
            if mpi then carrier = mpi; return mpi end
          end
        end
      end
    end
  end
  local clips = p:GetMediaPool():GetRootFolder():GetClipList()
  if type(clips) == "table" and clips[1] then carrier = clips[1] end
  return carrier
end

local function writeNamed(path)
  local mpi = carrierClip()
  if mpi then
    local ok, res = pcall(function() return project():GetMediaPool():ExportMetadata(path, { mpi }) end)
    if ok and res then return true end
    carrier = nil
  end
  local tl = timeline()
  if tl then
    local ok, res = pcall(function() return tl:Export(path, resolve.EXPORT_EDL, resolve.EXPORT_NONE) end)
    if ok and res then return true end
  end
  return false
end

local outSeq = 0
local function emit(kind, data)
  local h = hex(jenc(data))
  local n = math.max(1, math.ceil(#h / CHUNK))
  outSeq = outSeq + 1
  for k = 1, n do
    local name = string.format("%s-%08d-%s-%dof%d-%s.m", sid, outSeq, kind, k, n, h:sub((k - 1) * CHUNK + 1, k * CHUNK))
    writeNamed(OUT .. "\\" .. name)
  end
end
local function round(x) return math.floor((tonumber(x) or 0) + 0.5) end

local function items(tl, tt, ti)
  local l = tl:GetItemListInTrack(tt, ti)
  if type(l) ~= "table" then return {} end
  local out = {}
  for _, it in ipairs(l) do out[#out + 1] = it end
  return out
end

local function findItem(tl, ref)
  local best, bestD = nil, 3          -- exact start, else the nearest within 2 frames
  for _, it in ipairs(items(tl, ref.t or "video", ref.i or 1)) do
    local d = math.abs(round(it:GetStart()) - ref.s)
    if d == 0 then return it end
    if d < bestD then best, bestD = it, d end
  end
  return best
end

local function findItems(tl, refs)
  local out = {}
  for _, ref in ipairs(refs or {}) do
    local it = findItem(tl, ref)
    if not it then error(string.format("timeline item not found: %s %d @%d", ref.t or "video", ref.i or 1, ref.s or -1)) end
    out[#out + 1] = it
  end
  return out
end

local function withLinked(list)
  local seen, out = {}, {}
  local function add(it)
    local id = it:GetUniqueId()
    if not seen[id] then seen[id] = true; out[#out + 1] = it end
  end
  for _, it in ipairs(list) do
    add(it)
    local linked = it:GetLinkedItems()
    if type(linked) == "table" then for _, l in ipairs(linked) do add(l) end end
  end
  return out
end

local function normPath(p) return (tostring(p or ""):gsub("\\", "/"):lower()) end

local mediaCache = {}
local function scanFolder(folder, want)
  if not folder then return nil end
  local clips = folder:GetClipList()
  if type(clips) == "table" then
    for _, c in ipairs(clips) do
      local ok, path = pcall(function() return c:GetClipProperty("File Path") end)
      if ok and path and path ~= "" then
        mediaCache[normPath(path)] = c
      end
    end
  end
  if mediaCache[want] then return mediaCache[want] end
  local subs = folder:GetSubFolderList()
  if type(subs) == "table" then
    for _, f in ipairs(subs) do
      local found = scanFolder(f, want)
      if found then return found end
    end
  end
  return nil
end

local function mediaFor(tl, spec)
  if spec.via then
    local it = findItem(tl, spec.via)
    local mpi = it and it:GetMediaPoolItem()
    if mpi then return mpi end
  end
  local want = normPath(spec.path)
  if want == "" then error("no media reference") end
  if mediaCache[want] then return mediaCache[want] end
  for _, tt in ipairs({ "video", "audio" }) do
    for ti = 1, tl:GetTrackCount(tt) do
      for _, it in ipairs(items(tl, tt, ti)) do
        local mpi = it:GetMediaPoolItem()
        if mpi then
          local ok, path = pcall(function() return mpi:GetClipProperty("File Path") end)
          if ok and normPath(path) == want then mediaCache[want] = mpi; return mpi end
        end
      end
    end
  end
  local found = scanFolder(project():GetMediaPool():GetRootFolder(), want)
  if not found then error("media not in media pool: " .. tostring(spec.path)) end
  return found
end

------------------------------------------------------------------ state

local stateSeq = 0
local lastFp, lastTc, lastTcEmit, lastInfo = nil, nil, 0, nil

local function fingerprint(tl)
  local parts = { tl:GetUniqueId(), tl:GetName(), tl:GetStartFrame() }
  for _, tt in ipairs({ "video", "audio" }) do
    local n = tl:GetTrackCount(tt)
    parts[#parts + 1] = tt .. n
    for ti = 1, n do
      local acc = {}
      for _, it in ipairs(items(tl, tt, ti)) do
        acc[#acc + 1] = round(it:GetStart()) .. ":" .. round(it:GetEnd()) .. ":" .. round(it:GetLeftOffset())
      end
      parts[#parts + 1] = table.concat(acc, ",")
    end
  end
  return table.concat(parts, "|")
end

local function trackSummary(tl)
  local out = {}
  for _, tt in ipairs({ "video", "audio" }) do
    for ti = 1, tl:GetTrackCount(tt) do
      local l = items(tl, tt, ti)
      local first, last = nil, nil
      if #l > 0 then first = round(l[1]:GetStart()); last = round(l[#l]:GetEnd()) end
      out[#out + 1] = { t = tt, i = ti, n = #l, first = first, last = last }
    end
  end
  return out
end

-- Real frame rate of every media used on the timeline: OTIO gives source times in
-- the timeline rate, but AppendToTimeline needs frames of the clip's own rate.
local function mediaRates(tl)
  local seen, out = {}, {}
  for _, tt in ipairs({ "video", "audio" }) do
    for ti = 1, tl:GetTrackCount(tt) do
      for _, it in ipairs(items(tl, tt, ti)) do
        local mpi = it:GetMediaPoolItem()
        if mpi then
          local id = mpi:GetUniqueId()
          if not seen[id] then
            seen[id] = true
            local ok, props = pcall(function() return mpi:GetClipProperty() end)
            if ok and type(props) == "table" then
              out[#out + 1] = { p = props["File Path"], f = props["FPS"] }
            end
          end
        end
      end
    end
  end
  return out
end

local function exportState(reason)
  local tl = timeline()
  if not tl then
    emit("s", { none = true, reason = reason })
    lastFp = nil
    return nil
  end
  stateSeq = stateSeq + 1
  local file = string.format("%s-%08d.otio", sid, stateSeq)
  local ok = tl:Export(STATE .. "\\" .. file, resolve.EXPORT_OTIO)
  lastFp = fingerprint(tl)
  emit("s", {
    file = ok and file or nil, ok = ok and true or false, reason = reason,
    tl = tl:GetName(), tlid = tl:GetUniqueId(), start = tl:GetStartFrame(),
    fps = tl:GetSetting("timelineFrameRate"), tc = tl:GetCurrentTimecode(),
    tracks = trackSummary(tl), media = mediaRates(tl),
  })
  return file
end

-- Fallback when Timeline:Export is unavailable: per-item numbers of one track.
local function compactState(ref)
  local tl = timeline()
  if not tl then return end
  local media, mediaIdx, rows = {}, {}, {}
  for _, it in ipairs(items(tl, ref.t or "video", ref.i or 1)) do
    local mpi = it:GetMediaPoolItem()
    local m = 0
    if mpi then
      local id = mpi:GetUniqueId()
      if not mediaIdx[id] then
        local props = mpi:GetClipProperty()
        media[#media + 1] = { path = props["File Path"], fps = props["FPS"], frames = props["Frames"], tc = props["Start TC"], name = mpi:GetName() }
        mediaIdx[id] = #media
      end
      m = mediaIdx[id]
    end
    rows[#rows + 1] = { round(it:GetStart()), round(it:GetEnd()), it:GetSourceStartFrame(), it:GetSourceEndFrame(), m, it:GetName() }
  end
  emit("c", { t = ref.t or "video", i = ref.i or 1, tl = tl:GetName(), start = tl:GetStartFrame(),
              fps = tl:GetSetting("timelineFrameRate"), media = media, items = rows })
end

local function emitInfo(force)
  local p = project()
  local tl = p and p:GetCurrentTimeline()
  local info = { v = BRIDGE_VERSION, product = resolve:GetProductName(), version = resolve:GetVersionString(),
                 project = p and p:GetName() or nil, tl = tl and tl:GetName() or nil, tlid = tl and tl:GetUniqueId() or nil }
  local key = jenc(info)
  if force or key ~= lastInfo then
    lastInfo = key
    emit("i", info)
    return true
  end
  return false
end

------------------------------------------------------------------ commands

local OPS = {}

function OPS.trim_end(tl, op)      -- shorten (n > 0) or extend (n < 0) the tail
  local it = findItems(tl, { op.ref })[1]
  return it:SetRightOffset(it:GetRightOffset() + op.n)
end

function OPS.trim_start(tl, op)    -- shorten (n > 0) or extend (n < 0) the head (no ripple)
  local it = findItems(tl, { op.ref })[1]
  return it:SetLeftOffset(it:GetLeftOffset() + op.n)
end

function OPS.append(tl, op)
  local infos = {}
  for _, c in ipairs(op.clips) do
    local info = { mediaPoolItem = mediaFor(tl, c), startFrame = c.s, endFrame = c.e }
    if c.rec then info.recordFrame = c.rec end
    if c.track then info.trackIndex = c.track end
    if c.mt then info.mediaType = c.mt end
    infos[#infos + 1] = info
  end
  local res = project():GetMediaPool():AppendToTimeline(infos)
  return type(res) == "table" and #res > 0
end

function OPS.delete(tl, op)
  local list = findItems(tl, op.refs)
  if #list == 0 then return true end
  return tl:DeleteClips(withLinked(list), false)
end

function OPS.move(tl, op)
  local list = findItems(tl, op.refs)
  if #list == 0 then return true end
  return tl:MoveItems(list, { startFrame = op.to }, true, false)
end

function OPS.go(tl, op) return tl:SetCurrentTimecode(op.tc) end

-- In/Out marks around the part being previewed; returns what Resolve reports back
function OPS.marks(tl, op)
  local ok = tl:SetMarkInOut(op.inn, op.out)
  local got = tl:GetMarkInOut()
  return { ok = ok and true or false, marks = got }
end
function OPS.page(tl, op) return resolve:OpenPage(op.name) end

function OPS.new_timeline(tl, op)
  local p = project()
  local ntl = p:GetMediaPool():CreateEmptyTimeline(op.name)
  if not ntl then return false end
  return p:SetCurrentTimeline(ntl)
end

function OPS.duplicate_timeline(tl, op)
  local ntl = tl:DuplicateTimeline(op.name)
  if not ntl then return false end
  return project():SetCurrentTimeline(ntl)
end

function OPS.set_timeline(tl, op)
  local p = project()
  for i = 1, p:GetTimelineCount() do
    local t = p:GetTimelineByIndex(i)
    if t and (t:GetUniqueId() == op.id or (op.id == nil and t:GetName() == op.name)) then
      return p:SetCurrentTimeline(t)
    end
  end
  return false
end

function OPS.timelines(tl, op)
  local p, out = project(), {}
  for i = 1, p:GetTimelineCount() do
    local t = p:GetTimelineByIndex(i)
    if t then out[#out + 1] = { id = t:GetUniqueId(), name = t:GetName() } end
  end
  emit("t", out)
  return true
end

function OPS.marker(tl, op)
  return tl:AddMarker(op.f, op.color or "Blue", op.name or "", op.note or "", op.dur or 1, op.data or "")
end

function OPS.delete_markers(tl, op)
  if op.data then
    return tl:DeleteMarkerByCustomData(op.data)
  end
  return tl:DeleteMarkersByColor(op.color or "All")
end

function OPS.compact(tl, op) compactState(op.ref or {}); return true end
function OPS.state(tl, op) pcall(emitInfo, true); return true end

local running = true
function OPS.quit(tl, op) running = false; return true end

local function runCommand(seq, ops)
  local done, err, results = 0, nil, {}
  for k, op in ipairs(ops) do
    local tl = timeline()
    local fn = OPS[op.op]
    if not fn then err = "unknown op " .. tostring(op.op); break end
    if not tl and op.op ~= "set_timeline" and op.op ~= "quit" then err = "no current timeline"; break end
    local ok, res = pcall(fn, tl, op)
    if not ok then err = tostring(res); break end
    if type(res) == "table" then results[k] = res else results[k] = res and true or false end
    if res == false and not op.soft then err = op.op .. " failed"; break end
    done = k
  end
  local file = nil
  if running then file = exportState("cmd") end
  emit("r", { id = seq, ok = err == nil, done = done, n = #ops, err = err, res = results, state = file })
end

------------------------------------------------------------------ loop

local ms = resolve:GetMediaStorage()
local handled = {}
local idleSince = os.time()

local function pollInbox()
  local list = ms:GetSubFolderList(IN)
  if type(list) ~= "table" then return false end
  local groups = {}
  for _, full in ipairs(list) do
    if type(full) == "string" then
      local name = full:match("([^\\/]+)$") or ""
      local seq, k, n, h = name:match("^(%d+)%-(%d+)of(%d+)%-(%x*)$")
      if seq and not handled[seq] then
        local g = groups[seq] or { n = tonumber(n), parts = {} }
        g.parts[tonumber(k)] = h
        groups[seq] = g
      end
    end
  end
  local seqs = {}
  for seq, g in pairs(groups) do
    local complete = true
    for k = 1, g.n do if not g.parts[k] then complete = false end end
    if complete then seqs[#seqs + 1] = seq end
  end
  table.sort(seqs, function(a, b) return tonumber(a) < tonumber(b) end)
  for _, seq in ipairs(seqs) do
    handled[seq] = true
    local ops, perr = parse(unhex(table.concat(groups[seq].parts)))
    if type(ops) ~= "table" then
      emit("r", { id = tonumber(seq), ok = false, done = 0, err = "bad command: " .. tostring(perr) })
    else
      runCommand(tonumber(seq), ops)
    end
    if not running then break end
  end
  return true
end

local function watchPlayhead()
  local tl = timeline()
  if not tl then return end
  local tc = tl:GetCurrentTimecode()
  if tc ~= lastTc then
    lastTc = tc
    emit("p", { tc = tc, tlid = tl:GetUniqueId() })
  end
end

local function watchTimeline()
  local tl = timeline()
  if not tl then
    if lastFp ~= nil then exportState("closed") end
    return
  end
  local fp = fingerprint(tl)
  if fp ~= lastFp then exportState("change") end
end

-- Every Resolve API call is marshalled through Resolve's UI event queue; a loop
-- that keeps calling it makes the mouse cursor flicker.  So the idle loop makes
-- NO Resolve calls at all: it only checks two folders with bmd.direxists:
--   <in>\ring       — the app has queued a command (doorbell);
--   <in>\ui-open    — the DaVinci Director window is open: watch playhead/timeline.
-- The app itself is started by its hidden watcher, which opens the window when
-- this script says hello (Resolve's sandbox cannot start programs).
local RING, UI_OPEN, PREVIEW = IN .. "\\ring", IN .. "\\ui-open", IN .. "\\preview"
emitInfo(true)
exportState("start")
local lastBeat, lastWatch, lastPlay, lastTok, watchCost = 0, 0, 0, os.clock(), 0
while running do
  local now = os.clock()
  if bmd.direxists(RING) then
    local ok, err = pcall(pollInbox)
    if not ok then emit("e", { err = tostring(err) }) end
  end
  local ui = bmd.direxists(UI_OPEN)
  local preview = ui and bmd.direxists(PREVIEW)   -- playing a part: follow the playhead closely
  if ui and running then
    if now - lastPlay >= (preview and 0.1 or 0.4) then   -- the panel animates between reports
      lastPlay = now
      local ok, err = pcall(watchPlayhead)
      if not ok then emit("e", { err = tostring(err) }) end
    end
    if now - lastWatch >= math.max(1.0, watchCost * 20) then
      local t0 = os.clock()
      local ok, err = pcall(watchTimeline)
      if not ok then emit("e", { err = tostring(err) }) end
      pcall(emitInfo, false)
      watchCost = os.clock() - t0
      lastWatch = os.clock()
    end
    if now - lastBeat >= 5 then
      lastBeat = now
      emit("h", { t = os.time() })
    end
  end
  if now - lastTok >= 2 then
    lastTok = now
    if fusion:GetData(TOKEN_KEY) ~= token then break end   -- a newer instance took over
  end
  bmd.wait(preview and 0.02 or (ui and 0.1 or 0.5))
end
emit("x", { bye = true })
