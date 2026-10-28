-- ScreenStream client: decodes frames from StreamServer and shows them on every part
-- named "StreamScreen" (or tagged "StreamScreen") in Workspace.
-- The picture goes on the part's Front face; set a string attribute "Face" on the part
-- (e.g. "Back") to use another face. Picture size and frame rate come from the server.
-- Each frame's decode is time-sliced across game frames so large sizes don't hitch.
-- Record: seq u32 | kind u8 (0 key, 1 inter) | len u32 | payload (see StreamDecoder)
local AssetService = game:GetService("AssetService")
local CollectionService = game:GetService("CollectionService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")

local Decoder = require(script:WaitForChild("StreamDecoder"))

local SCREEN = "StreamScreen"
local HEADER = 9
local SLICE = 0.006 -- decode time allowed per game frame

local remote = ReplicatedStorage:WaitForChild("StreamFrame")

type Record = { seq: number, kind: number, payload: buffer }

local fps = 15
local image: EditableImage? = nil
local decoder: Decoder.Decoder? = nil
local lastSeq = -1
local queue: { Record } = {}
local worker: thread? = nil
local labels: { ImageLabel } = {}
local frames, bytes = 0, 0
local sliceStart = 0

local function yieldCheck()
	if os.clock() - sliceStart > SLICE then
		coroutine.yield()
	end
end

local function showOn(label: ImageLabel)
	if image then
		label.ImageContent = Content.fromObject(image)
	end
end

local function attach(part: Instance)
	if not part:IsA("BasePart") or not part:IsDescendantOf(workspace) then
		return
	end
	-- Reuse an existing Display label (older setups), otherwise build the GUI.
	local existing = part:FindFirstChild("Display", true)
	local label: ImageLabel
	if existing and existing:IsA("ImageLabel") then
		label = existing
	else
		if part:FindFirstChild("ScreenStreamGui") then
			return
		end
		local gui = Instance.new("SurfaceGui")
		gui.Name = "ScreenStreamGui"
		local face = part:GetAttribute("Face")
		gui.Face = if typeof(face) == "string" and (Enum.NormalId :: any)[face] then (Enum.NormalId :: any)[face] else Enum.NormalId.Front
		gui.CanvasSize = Vector2.new(960, 540)
		gui.LightInfluence = 0
		label = Instance.new("ImageLabel")
		label.Name = "Display"
		label.Size = UDim2.fromScale(1, 1)
		label.BackgroundColor3 = Color3.new(0, 0, 0)
		label.BorderSizePixel = 0
		label.Parent = gui
		gui.Parent = part
	end
	label.ResampleMode = Enum.ResamplerMode.Default
	if not table.find(labels, label) then
		table.insert(labels, label)
	end
	showOn(label)
end

-- (Re)creates the image and decoder for the size the server announces.
local function setup()
	local w, h = remote:GetAttribute("Width"), remote:GetAttribute("Height")
	fps = remote:GetAttribute("Fps") or 15
	if typeof(w) ~= "number" or typeof(h) ~= "number" then
		return
	end
	if image and image.Size == Vector2.new(w, h) then
		return
	end
	if image then
		image:Destroy()
	end
	image = AssetService:CreateEditableImage({ Size = Vector2.new(w, h) })
	if not image then
		warn("[ScreenStream] could not create EditableImage (memory budget, or the API is disabled in Game Settings > Security)")
		return
	end
	local d = Decoder.new(w, h)
	d.yieldCheck = yieldCheck
	decoder = d
	lastSeq = -1
	table.clear(queue)
	worker = nil
	for _, label in labels do
		showOn(label)
	end
	remote:FireServer() -- ask for the latest keyframe
end

local function apply(record: Record): boolean
	local d = decoder :: Decoder.Decoder
	if record.kind == 1 and record.seq ~= lastSeq + 1 then
		d.ready = false -- missed a frame; wait for the next keyframe
		return false
	end
	if not d:decode(record.kind, record.payload) then
		return false
	end
	lastSeq = record.seq
	return true
end

-- Runs inside a coroutine; may yield many times while decoding.
local function playNext()
	local d, img = decoder :: Decoder.Decoder, image :: EditableImage
	local changed = apply(table.remove(queue, 1) :: Record)
	-- Normally one frame per tick; skip ahead when more than a second behind.
	while #queue > fps do
		changed = apply(table.remove(queue, 1) :: Record) or changed
	end
	if changed and decoder == d then
		img:WritePixelsBuffer(Vector2.zero, img.Size, d:toRGBA())
		frames += 1
		script:SetAttribute("FramesDrawn", frames)
	end
end

remote.OnClientEvent:Connect(function(batch: buffer)
	if not decoder then
		return
	end
	bytes += buffer.len(batch)
	script:SetAttribute("BytesReceived", bytes)
	local at, size = 0, buffer.len(batch)
	while at + HEADER <= size do
		local seq = buffer.readu32(batch, at)
		local kind = buffer.readu8(batch, at + 4)
		local len = buffer.readu32(batch, at + 5)
		if seq > lastSeq or kind == 0 then
			local payload = buffer.create(len)
			buffer.copy(payload, 0, batch, at + HEADER, len)
			table.insert(queue, { seq = seq, kind = kind, payload = payload })
		end
		at += HEADER + len
	end
end)

local clock = 0
RunService.Heartbeat:Connect(function(dt)
	if not decoder then
		return
	end
	clock = math.min(clock + dt, 2 / fps)
	sliceStart = os.clock()
	if not worker then
		if clock < 1 / fps or #queue == 0 then
			return
		end
		clock -= 1 / fps
		worker = coroutine.create(playNext)
	end
	local current = worker :: thread
	local ok, err = coroutine.resume(current)
	if not ok then
		warn("[ScreenStream] decode error:", err)
		if decoder then
			decoder.ready = false
		end
	end
	if coroutine.status(current) == "dead" and worker == current then
		worker = nil
	end
end)

-- Width and Height arrive as separate changes; set up once after both.
local setupPending = false
remote.AttributeChanged:Connect(function(name)
	if (name == "Width" or name == "Height" or name == "Fps") and not setupPending then
		setupPending = true
		task.defer(function()
			setupPending = false
			setup()
		end)
	end
end)

for _, part in CollectionService:GetTagged(SCREEN) do
	attach(part)
end
CollectionService:GetInstanceAddedSignal(SCREEN):Connect(attach)
for _, part in workspace:GetDescendants() do
	if part.Name == SCREEN then
		attach(part)
	end
end
workspace.DescendantAdded:Connect(function(part)
	if part.Name == SCREEN then
		attach(part)
	end
end)

setup()
