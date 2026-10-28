--!native
--!optimize 2
-- Decoder for the ScreenStream DCT codec (mirror of sender/codec.py's Decoder; must
-- stay bit-exact, or inter frames drift).
-- Payload after zstd: u8 quant level | low bytes of coefficients | high bytes.
-- Coefficients: plane (Y, Cb, Cr) -> zigzag index k -> block, sign-folded u16.
local EncodingService = game:GetService("EncodingService")

local ZIGZAG = {
	0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
	12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
	35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
	58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
}
local Q_LUMA = {
	16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
	14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
	18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
	49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
}
local Q_CHROMA = {
	17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99,
	24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
	99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
	99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
}
local SCALES16 = {
	1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 5, 6, 6, 7, 9, 10, 12, 13,
	16, 18, 21, 24, 28, 33, 38, 44, 51, 59, 69, 80, 92, 107, 124, 144, 167, 194, 225, 261,
}
-- Integer DCT basis, M[k*8 + n] = round(4096 * dct[k][n]) (row-major, 1-based below).
local M = {
	1448, 1448, 1448, 1448, 1448, 1448, 1448, 1448, 2009, 1703, 1138, 400, -400, -1138, -1703, -2009,
	1892, 784, -784, -1892, -1892, -784, 784, 1892, 1703, -400, -2009, -1138, 1138, 2009, 400, -1703,
	1448, -1448, -1448, 1448, 1448, -1448, -1448, 1448, 1138, -2009, 400, 1703, -1703, -400, 2009, -1138,
	784, -1892, 1892, -784, -784, 1892, -1892, 784, 400, -1138, 1703, -2009, 2009, -1703, 1138, -400,
}
local ROUND = 8388608 -- 2^23
local SCALE = 16777216 -- 2^24

local Decoder = {}
Decoder.__index = Decoder

export type Decoder = typeof(setmetatable({} :: {
	w: number,
	h: number,
	planes: { buffer },
	sizes: { { number } },
	rgba: buffer,
	coefCount: number,
	tables: { [number]: { { number } } },
	ready: boolean,
	yieldCheck: (() -> ())?,
}, Decoder))

function Decoder.new(w: number, h: number): Decoder
	assert(w % 16 == 0 and h % 16 == 0, "size must be a multiple of 16")
	local cw, ch = w // 2, h // 2
	return setmetatable({
		w = w,
		h = h,
		planes = { buffer.create(w * h), buffer.create(cw * ch), buffer.create(cw * ch) },
		sizes = { { w, h }, { cw, ch }, { cw, ch } },
		rgba = buffer.create(w * h * 4),
		coefCount = w * h + 2 * cw * ch,
		tables = {},
		ready = false,
		-- Optional: called regularly during decode/toRGBA so a caller running this in a
		-- coroutine can yield and spread one frame's work over several game frames.
		yieldCheck = nil,
	}, Decoder)
end

-- Dequantization multipliers indexed by zigzag position k (1-based), per plane type.
function Decoder.quantTables(self: Decoder, level: number): { { number } }
	local cached = self.tables[level]
	if cached then
		return cached
	end
	local s = SCALES16[level + 1]
	local luma, chroma = table.create(64), table.create(64)
	for k = 1, 64 do
		local pos = ZIGZAG[k] + 1
		luma[k] = math.max(1, (Q_LUMA[pos] * s + 8) // 16)
		chroma[k] = math.max(1, (Q_CHROMA[pos] * s + 8) // 16)
	end
	cached = { luma, chroma }
	self.tables[level] = cached
	return cached
end

-- Applies one frame. kind 0 = intra, 1 = inter. Returns false if the frame is unusable.
function Decoder.decode(self: Decoder, kind: number, payload: buffer): boolean
	if kind == 1 and not self.ready then
		return false
	end
	local raw = EncodingService:DecompressBuffer(payload, Enum.CompressionAlgorithm.Zstd)
	local n = self.coefCount
	if buffer.len(raw) ~= 1 + 2 * n then
		return false
	end
	local level = buffer.readu8(raw, 0)
	if level >= #SCALES16 then
		return false
	end
	local tables = self:quantTables(level)
	local intra = kind == 0
	local yieldCheck = self.yieldCheck
	self.ready = false -- planes are mid-update until this returns

	local deq = table.create(64, 0)
	local tmp = table.create(64, 0)
	local nzPos = table.create(64, 0) -- positions of non-zero coefficients in this block
	local colUsed = table.create(8, false)
	local base = 0 -- coefficient index where this plane starts
	for p = 1, 3 do
		local plane = self.planes[p]
		local pw, ph = self.sizes[p][1], self.sizes[p][2]
		local bw = pw // 8
		local nb = bw * (ph // 8)
		local q = tables[p == 1 and 1 or 2]
		for b = 0, nb - 1 do
			if yieldCheck and b % 32 == 31 then
				yieldCheck()
			end
			local count = 0
			local idx = 1 + base + b
			for k = 1, 64 do
				local u = buffer.readu8(raw, idx) + buffer.readu8(raw, idx + n) * 256
				if u ~= 0 then
					local v = if u % 2 == 0 then u // 2 else -((u + 1) // 2)
					local pos = ZIGZAG[k]
					deq[pos + 1] = v * q[k]
					count += 1
					nzPos[count] = pos
				end
				idx += nb
			end

			local ox = (b % bw) * 8
			local oy = (b // bw) * 8
			if count == 0 or (count == 1 and nzPos[1] == 0) then
				-- Flat block (no coefficients, or DC only): one value added to all 64 pixels.
				-- Same integer result as the full transform, since every M[0][*] is 1448.
				local r = if count == 0 then 0 else (deq[1] * 2096704 + ROUND) // SCALE
				deq[1] = 0
				if intra then
					local v = r + 128
					v = if v < 0 then 0 elseif v > 255 then 255 else v
					for y = 0, 7 do
						buffer.fill(plane, (oy + y) * pw + ox, v, 8)
					end
				elseif r ~= 0 then
					for y = 0, 7 do
						local row = (oy + y) * pw + ox
						for x = 0, 7 do
							local v = buffer.readu8(plane, row + x) + r
							buffer.writeu8(plane, row + x, if v < 0 then 0 elseif v > 255 then 255 else v)
						end
					end
				end
			else
				-- tmp = M^T * deq, visiting only the non-zero coefficients
				for i = 1, 64 do
					tmp[i] = 0
				end
				for c = 1, count do
					local pos = nzPos[c]
					local k, j = pos // 8, pos % 8
					local d = deq[pos + 1]
					deq[pos + 1] = 0
					colUsed[j + 1] = true
					local mk = k * 8 + 1
					for i = 0, 7 do
						tmp[i * 8 + j + 1] += M[mk + i] * d
					end
				end
				-- out = tmp * M over the used columns, rounded, added to the base picture
				for i = 0, 7 do
					local row = (oy + i) * pw + ox
					local ti = i * 8
					for j = 0, 7 do
						local s = 0
						for k = 0, 7 do
							if colUsed[k + 1] then
								s += tmp[ti + k + 1] * M[k * 8 + j + 1]
							end
						end
						local r = (s + ROUND) // SCALE
						local v = r + (if intra then 128 else buffer.readu8(plane, row + j))
						buffer.writeu8(plane, row + j, if v < 0 then 0 elseif v > 255 then 255 else v)
					end
				end
				for i = 1, 8 do
					colUsed[i] = false
				end
			end
		end
		base += nb * 64
	end
	self.ready = true
	return true
end

-- Converts the current YCbCr planes into self.rgba (little-endian RGBA bytes).
function Decoder.toRGBA(self: Decoder): buffer
	local w, h = self.w, self.h
	local cw = w // 2
	local yp, cbp, crp = self.planes[1], self.planes[2], self.planes[3]
	local out = self.rgba
	local yieldCheck = self.yieldCheck
	for cy = 0, h // 2 - 1 do
		if yieldCheck and cy % 8 == 7 then
			yieldCheck()
		end
		for cx = 0, cw - 1 do
			local cb = buffer.readu8(cbp, cy * cw + cx) - 128
			local cr = buffer.readu8(crp, cy * cw + cx) - 128
			local dr = (91881 * cr + 32768) // 65536
			local dg = (22554 * cb + 46802 * cr + 32768) // 65536
			local db = (116130 * cb + 32768) // 65536
			for sy = 0, 1 do
				for sx = 0, 1 do
					local i = (cy * 2 + sy) * w + cx * 2 + sx
					local y = buffer.readu8(yp, i)
					local r, g, b = y + dr, y - dg, y + db
					r = if r < 0 then 0 elseif r > 255 then 255 else r
					g = if g < 0 then 0 elseif g > 255 then 255 else g
					b = if b < 0 then 0 elseif b > 255 then 255 else b
					buffer.writeu32(out, i * 4, r + g * 256 + b * 65536 + 0xFF000000)
				end
			end
		end
	end
	return out
end

return Decoder
