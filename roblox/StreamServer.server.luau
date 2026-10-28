-- ScreenStream server: polls the sender (sender/stream_server.py) and relays frames
-- to every player. Frames are forwarded still compressed; each client decodes its own.
-- Record: seq u32 | kind u8 (0 key, 1 inter) | len u32 | payload

-- Settings ------------------------------------------------------------------------
-- localhost only works while testing in Studio. For a published game, run a tunnel
-- (e.g. `cloudflared tunnel --url http://localhost:8080`) and put its https URL here.
local URL = "http://localhost:8080"
-- Must match the --token you start the sender with.
local TOKEN = "change-me"
-- 5 requests/s = 300/min, under Roblox's 500 HTTP requests/min per server.
local POLL_INTERVAL = 0.2
------------------------------------------------------------------------------------

local HttpService = game:GetService("HttpService")
local EncodingService = game:GetService("EncodingService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local HEADER = 9

local remote = Instance.new("RemoteEvent")
remote.Name = "StreamFrame"
remote.Parent = ReplicatedStorage

local lastSeq = 0
local sinceKey: { buffer } = {} -- latest keyframe and the frames after it, for late joiners
local lastHello: { [Player]: number } = {}

local function concat(list: { buffer }): buffer
	local total = 0
	for _, b in list do
		total += buffer.len(b)
	end
	local out = buffer.create(total)
	local at = 0
	for _, b in list do
		buffer.copy(out, at, b)
		at += buffer.len(b)
	end
	return out
end

-- Picture size and frame rate come from the sender, published as attributes for clients.
local function applyFormat(headers: { [string]: string })
	local lower = {}
	for k, v in headers do
		lower[string.lower(k)] = v
	end
	local w = tonumber(lower["x-stream-width"])
	local h = tonumber(lower["x-stream-height"])
	local fps = tonumber(lower["x-stream-fps"])
	if not (w and h and fps) then
		return
	end
	if w ~= remote:GetAttribute("Width") or h ~= remote:GetAttribute("Height") then
		table.clear(sinceKey) -- old frames don't fit the new size
		lastSeq = 0
	end
	remote:SetAttribute("Width", w)
	remote:SetAttribute("Height", h)
	remote:SetAttribute("Fps", fps)
end

-- A client asks for the current picture once its script is ready.
remote.OnServerEvent:Connect(function(player)
	local now = os.clock()
	if lastHello[player] and now - lastHello[player] < 2 then
		return
	end
	lastHello[player] = now
	if #sinceKey > 0 then
		remote:FireClient(player, concat(sinceKey))
	end
end)

Players.PlayerRemoving:Connect(function(player)
	lastHello[player] = nil
end)

while true do
	local ok, res = pcall(HttpService.RequestAsync, HttpService, {
		Url = `{URL}/frames?t={HttpService:UrlEncode(TOKEN)}&since={lastSeq}`,
		Method = "GET",
	})
	if not ok then
		warn("[ScreenStream] request failed (is HTTP enabled and the sender running?):", res)
		task.wait(2)
	elseif not res.Success then
		warn("[ScreenStream] sender replied", res.StatusCode, res.StatusMessage, "(check TOKEN)")
		task.wait(2)
	else
		applyFormat(res.Headers)
		if #res.Body > 0 then
			local batch = EncodingService:Base64Decode(buffer.fromstring(res.Body))
			local at, size = 0, buffer.len(batch)
			while at + HEADER <= size do
				local seq = buffer.readu32(batch, at)
				local kind = buffer.readu8(batch, at + 4)
				local len = HEADER + buffer.readu32(batch, at + 5)
				local record = buffer.create(len)
				buffer.copy(record, 0, batch, at, len)
				if kind == 0 then
					table.clear(sinceKey)
				end
				table.insert(sinceKey, record)
				lastSeq = seq
				at += len
			end
			remote:FireAllClients(batch)
		end
	end
	task.wait(POLL_INTERVAL)
end
