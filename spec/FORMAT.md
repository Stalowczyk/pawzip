# The pawzip file format (`.paw`), version 1

This is the complete, byte-level description of version 1 of the `.paw` format: enough to
write your own encoder or decoder. For the ideas behind it, read
[How it works](../docs/how-it-works.md) first.

**Status: frozen** since 2026-10-14. Every valid version 1 file decodes to the same bytes
forever, and any change a decoder must understand needs a new `version`. Later edits to this
text may only clarify it.

The Python reference implementation (`python/src/paw`) and the Rust library (`rust/pawzip`)
write identical files, and the test vectors in `vectors/` check both (see "Conformance").

## Conventions

- **varint**: unsigned LEB128: 7 bits per byte, lowest bits first, high bit set on every byte except the last. At most 10 bytes, and the value must be below 2⁶⁴. Non-minimal encodings (such as `80 00` for 0) are allowed.
- **u32**: 4 bytes, little-endian.
- Positions are 0-based byte offsets.
- The total size must be below 2⁶⁴ (it is stored as a varint); a single block is limited to 4 GiB − 1.

## Overall layout

A `.paw` file is a header, a sequence of independently compressed **blocks**, and an end
record. Blocks let encoders and decoders work on files of any size with memory bounded by
the block size, and let blocks be compressed in parallel.

```
header      "PAW" (3 bytes) | version (u8 = 1) | block size B (varint, 1 ≤ B < 2³²) | extension length L (u8, 0–16) | extension (L bytes) | extra length X (varint, 0–65536) | extra (X bytes)
block       original size n (varint, 1 ≤ n ≤ B) | mode (u8) | CRC-32 (u32) | body size (varint) | body
block       ...
end         0 (varint) | total original size (varint)
```

- `version`: format version `1`. A decoder must refuse versions it doesn't know.
- `block size`: the largest block in the file. Every block except the last should be
  exactly `B` bytes long (decoders accept any `n ≤ B`). Decoders can use `B` to bound memory
  before decoding anything.
- `extension`: the original file's extension without the dot (`png`), or nothing
  (`L = 0`). Only ASCII letters, digits, `-` and `_` are allowed. A decompressor that names
  its output after the .paw file replaces `.paw` with it: `abc.paw` holding `png` becomes
  `abc.png`, whatever the file was renamed to. Only the extension is stored, never a name
  or path.
- `extra`: optional header data for later versions of the format: information a decoder
  may ignore, such as the original modification time. Version 1 encoders write `X = 0`;
  decoders skip the `X` bytes, and must reject `X` above 65536. Anything a decoder must
  understand needs a new `version` instead.
- Each **block** is compressed on its own: `mode` (below) says how, `CRC-32` is computed
  over the block's original bytes and must be verified, and `body size` is the length of
  the body that follows. The CRC is the one zip and gzip use (CRC-32/ISO-HDLC: reflected
  polynomial `0xEDB88320`, initial value `0xFFFFFFFF`, final XOR `0xFFFFFFFF`; the CRC of
  `123456789` is `0xCBF43926`).
- The **end record** is a block size of `0`, followed by the total original size, which
  must equal the sum of the block sizes. Nothing may follow it.
- An empty input is a header followed directly by the end record (`0`, `0`).

Encoders choose the mode per block, typically by trying several and keeping the smallest.
The reference encoder uses `B` = 16 MiB by default.

Decoding a block takes memory in proportion to `n`, so decoders may refuse blocks above a
configurable limit. The reference decoder refuses blocks holding more than 64 MiB unless
the caller raises its limit; encoders should not make larger blocks by default.

## Modes

Within a block, `n` is the block's original size and "original" means the block's bytes.

| mode | name | body |
|------|------|------|
| 0 | stored | the original bytes, unchanged (`body size` = `n`) |
| 1 | map | a **map body** of the original bytes |
| 2 | bwt-mtf-map | `primary index` (varint), K − 1 `walk rows` (varints), then a **map body** of MTF(BWT(original)) |
| 3 | delta-map | `distance` (varint, ≥ 1) followed by a **map body** of DELTA(original) |
| 4 | delta-bwt-mtf-map | `distance` (varint, ≥ 1), `primary index` (varint), K − 1 `walk rows` (varints), then a **map body** of MTF(BWT(DELTA(original))) |
| 5 | bwt-map | `primary index` (varint), K − 1 `walk rows` (varints), then a **map body** of BWT(original) |
| 6 | delta-bwt-map | `distance` (varint, ≥ 1), `primary index` (varint), K − 1 `walk rows` (varints), then a **map body** of BWT(DELTA(original)) |
| 7 | x86-bwt-map | `primary index` (varint), K − 1 `walk rows` (varints), then a **map body** of BWT(X86(original)) |

### BWT (modes 2 and 4–7)

Burrows–Wheeler transform with an end marker. Append a marker `$` smaller than every byte
to the input and sort its `n + 1` suffixes (a suffix sorts before every longer suffix it is a
prefix of). Row `r`'s entry is the symbol just before suffix `r`: row 0 is `$` alone, so its
entry is the input's last byte, and the row of the whole input (`$` before it) is the
`primary index`, `1 ≤ primary ≤ n`. The stored column `last` is the `n + 1` entries without
that `$`, so it has `n` bytes. Example: `banana` → `last = annbaa`, `primary = 4`.

**Walk rows.** The inverse transform is a walk over the `n + 1` rows. Sorting `last` stably
(ties in order of position) gives the rows' first bytes: row `j + 1` starts with the `j`-th
smallest byte, found at position `p` of `last`, and continues at row `next = p` if
`p < primary`, else `p + 1` (the `$` entry was taken out at `primary`). Row 0 continues at
`primary`. The walk starts at `row = primary` and, for each output position `i` in order,
sets `out[i]` to the first byte of `row`, then moves `row` to its `next`. In a valid file the
walk never comes back to row 0 before the end. A damaged one can: row 0's first byte then
counts as 0, and the block fails its CRC.
With `K = clamp(n // 131072, 1, 16)` and `L = ceil(n / K)`, the `walk rows` are the values of
`row` just before the walk produces positions `L, 2L, …, (K − 1)·L`, which are the rows of
the suffixes starting there (none when `K = 1`, i.e. for blocks under 256 KiB). A decoder can
start `K` independent walks from them, which is much faster than one long chain of dependent
memory reads; it must reject a walk row outside `1..n`, and a file whose walk rows don't
match the walk.

### Delta (modes 3, 4 and 6)

`DELTA(x)[i] = (x[i] − x[i − distance]) mod 256` for `i ≥ distance`; the first `distance`
bytes are copied unchanged. The decoder undoes it with `x[i] = (y[i] + x[i − distance]) mod 256`.
A distance of `n` or more is valid and leaves the block unchanged.
A distance of 3 suits 24-bit images (each colour channel compared with the previous pixel),
2 suits 16-bit audio, and so on.

### x86 calls (mode 7)

`X86(x)` makes the 32-bit relative addresses of x86 `CALL` (byte `E8`) and `JMP` (`E9`)
instructions absolute, so that calls to the same place look alike. Scan `i = 0, 1, …` (the
position in the block) while `i + 5 ≤ n`:

- if `x[i]` is `E8` or `E9`: when `x[i + 4]` is `00` or `FF`, read the little-endian u32
  `v = x[i + 1 .. i + 5]`, compute `w = (v + i + 5) mod 2³²`, set bits 25–31 of `w` to its
  bit 24 (so its top byte is again `00` or `FF`), and store `w` in place of `v`. Either way,
  continue at `i + 5`;
- otherwise continue at `i + 1`.

The decoder runs the same scan over the decoded bytes with `w = (v − (i + 5)) mod 2³²`, again
with bit 24 copied into bits 25–31. It finds the same positions: only the four bytes after an
`E8` or `E9` change, the scan never stops on them, and their top byte stays `00` or `FF`.
This is the classic "E8/E9" preprocessing of executables, as in 7-Zip's BCJ filter.

### MTF (modes 2 and 4)

Move-to-front over a table initialised to `0, 1, …, 255`. Each byte is replaced by its current
index in the table, then moved to the front. Modes 5–7 map the BWT output directly.

## Map body

Describes data `D` of length `n` (the block's original size; BWT, MTF and delta preserve length).

```
background byte      u8
coded stream         see "Range coder"; runs to the end of the block body
```

Everything after the background byte is a sequence of **numbers** (and one kind of
**symbol**) in one coded stream. For each byte value `b` from 0 to 255, skipping
the background:

```
present                          number, model PRESENT            (1 if b occurs in D, else 0)
if present:
    series count                 number, model SERIES_COUNT
    for each series (sorted by start):
        shape                    symbol, model SHAPE              (see "Remembered shapes")
        if the shape is new:
            type                 number, model SERIES_TYPE
            each parameter       number, model PARAM[type][index]
        start − previous start   number, model START               (previous start = 0 for the first)
    leftover count k             number, model LEFTOVER_COUNT
    k gaps                       number each, this symbol's GAP models (geometric prior)
```

The metadata models (`PRESENT` … `LEFTOVER_COUNT`, `SHAPE`, and one `PARAM` model per series
type and parameter index) are shared by all symbols and start with every frequency at 1.
Each symbol has two new `GAP` models, both starting from the geometric prior for `f` and
`k` (see below). The first gap, and every gap after one above 0, uses `GAP₀`; every gap after
a gap of 0 uses `GAP₁`. Inside a run of the byte value the gaps are 0, so `GAP₁` learns how
runs go on and `GAP₀` how far apart they are.

Decoding: fill an `n`-byte buffer with the background byte, then write `b` at every position
covered by its series and its leftovers. A decoder must reject a series that touches a
position already taken by an earlier byte value or an earlier series.

### Series

| type | name | parameters after start | positions |
|------|------|------------------------|-----------|
| 0 | arithmetic | step, count | `start + i·step` for `i < count` |
| 1 | nested | width, stride, rows, inner | `start + r·stride + j·inner` for `r < rows`, `j < width` |

Constraints: `step ≥ 1`, `count ≥ 2`, `width ≥ 2`, `rows ≥ 2`, `inner ≥ 1`, `stride > (width−1)·inner`.
All positions must be `< n` and must not overlap any other series or leftover (any symbol).
The last position (`start + (count−1)·step`, or `start + (rows−1)·stride + (width−1)·inner`)
can need up to 67 bits in a damaged file, since numbers go up to 3·2³¹ − 2 (see "Numbers"): a
decoder computes it without overflow, or treats an overflow as reaching past `n`.

A byte value's series come in order of start without any check: the stored difference can't
be negative, and two series with the same start would overlap.

### Remembered shapes

A series' **shape** is its type and parameters: everything except the start. Each map body
keeps a list of up to 63 shapes, most recently used first. The list starts empty and is
shared by all byte values. The shape symbol of a series says where its shape comes from:

- `0`…`62`: entry `i` of the list, which must exist;
- `63`: a new shape, whose type and parameters follow.

The symbol is coded directly with model `SHAPE`, using all 64 of its symbols (it is not
split into a size class and extra bits). After each series its shape moves to the front of
the list, or is inserted there if it was not in the list, and the list is cut back to 63
entries.

Repeating a shape (the same rectangle somewhere else, in this byte value or another) then
costs a short symbol and a start, instead of a type and all the parameters.

### Leftover gaps (with exclusion)

Positions are counted in **free slots** only. Before a symbol's leftovers are coded, a slot is
*taken* if it belongs to an earlier symbol (series or leftover) or to this symbol's own series;
every other slot is free, including all background slots. Let `f` be the number of free slots.

Each leftover position is replaced by its **rank** among the free slots (0 = the first free
slot), and the ranks `r₀ < r₁ < …` are stored as gaps `gᵢ = rᵢ − rᵢ₋₁ − 1` (with `r₋₁ = −1`).
After the symbol is done, its leftover slots become taken too.

This is the background-byte idea generalised: every byte value already placed shrinks the
space the next one has to describe.

### Numbers

Every number `x ≥ 0` (a flag, count, parameter or gap; not the shape symbol) is split into a **size class** and
**extra bits**:

```
v = x + 1
if v < 4:  class = v − 1, no extra bits
else:      k = floor(log2 v), half = bit (k−1) of v
           class = 2k − 1 + half
           extra = the low (k−1) bits of v
```

Classes range over `0…63` (enough for blocks up to 4 GiB), so every number is at most
3·2³¹ − 2 (class 63 with all 31 extra bits set). The class is coded with the given
adaptive model, then the extra bits, most significant first: the first 4 with **bit models**
(below), any further ones as **raw bits** (probability ½, in the coded stream's raw-bit
part).

#### Bit models

A bit model holds `p`, the probability of a 0 in units of 1/4096, starting at 2048. It is
coded by the range coder (below); afterwards a 0 sets `p += (4096 − p) >> 6` and a 1 sets
`p −= p >> 6`.

Each of the first 4 extra bits gets the bit model for (class, node). The node starts at 1 for the first
extra bit of a number and becomes `2·node + bit` after each predicted bit, so every prefix
of the extra bits has its own model. Bit models are created when first used and belong to a
**context set**: every metadata model has its own set, and all `GAP` models of a map body
(every byte value) share one set.

#### Adaptive model

- 64 frequencies, all 1 at the start, except the `GAP` models, which start from the
  **geometric prior** below (it depends only on the free-slot count `f` and that symbol's
  leftover count `k`, both known to the decoder at that point).
- After coding class `c`: `freq[c] += 64`, `total += 64`.
- If `total > 16384`: every frequency becomes `(f + 1) // 2` and the total is recomputed.

#### Geometric prior

The starting frequencies assume the `k` leftovers are scattered at random over the `f` free
slots. It is computed with integers only (Q31 fixed point, `ONE = 2³¹`) so that every
implementation produces the identical table:

```
r = ((f − k) · 2³¹) // f                      # chance that a free slot is NOT this symbol
pow(b, e):  result = ONE
            while e > 0:
                if e is odd: result = (result · b) >> 31
                b = (b · b) >> 31
                e = e >> 1
            return result
for each class c with (base, nbits) = smallest gap of c and its number of extra bits:
    mass    = pow(r, base) − pow(r, base + 2^nbits)
    freq[c] = 1 + ((1024 · max(mass, 0)) >> 31)
```

`(base, nbits)` per class: classes 0–2 are `(c, 0)`; for `c ≥ 3`, with `k = (c+1) // 2` and
`half = c − (2k − 1)`: `base = 2^k + half·2^(k−1) − 1`, `nbits = k − 1`.
All intermediate products fit in an unsigned 64-bit integer.

#### Range coder

The coded stream is:

```
size          varint, number of range-coded bytes R
range-coded   R bytes: classes, shape symbols and bit-model bits
raw bits      the rest of the map body: raw bits, packed MSB-first
```

Both parts read as zeros past their end, so an encoder drops their trailing zero bytes.

The range coder is 32-bit and byte-wise (as in LZMA). All divisions are integer (floor)
divisions; all values are unsigned. The encoder starts with `low = 0` (it may reach 2³²),
`range = 2³² − 1`, `cache = 0`, `cache_size = 1`. Encoding:

```
class with frequencies [lo, hi) of total:   r = range // total
                                            low += r·lo;  range = r·(hi − lo)
bit model bit (p as above):                 bound = (range >> 12)·p
                                            0: range = bound
                                            1: low += bound;  range −= bound
then, while range < 2²⁴:                    range = range·256;  shift_low

shift_low:
    if low < 0xFF000000 or low ≥ 2³²:
        write (cache + (low >> 32)) mod 256, then cache_size − 1 bytes of
            (0xFF + (low >> 32)) mod 256;  cache_size = 0
        cache = (low >> 24) mod 256
    cache_size += 1;  low = (low mod 2²⁴)·256
```

To finish, call `shift_low` 5 times. The first byte written is always 0 and is not stored;
the rest, without trailing zeros, are the range-coded bytes.

The decoder starts with `range = 2³² − 1` and `code` = the first 4 range-coded bytes (big
endian). Decoding:

```
class:      r = range // total;  count = min(code // r, total − 1)
            the class whose [lo, hi) contains count;  code −= r·lo;  range = r·(hi − lo)
bit model:  bound = (range >> 12)·p
            if code < bound: 0, range = bound
            else:            1, code −= bound;  range −= bound
then, while range < 2²⁴:  range = range·256;  code = (code·256 + next byte) mod 2³²
```

`count` only reaches `total` in a damaged stream; the `min` keeps such a stream decodable
(it fails the CRC later), so every decoder takes the same path through it.

The reference implementation is `python/src/paw/arith.py`.

## Validation

A decoder must reject a file (with an error, never a crash or a hang) when any of these
holds:

- **Container**: the magic is not `PAW`; the version is unknown; a varint is longer than
  10 bytes or not below 2⁶⁴; `B` is 0 or 2³² or more; `L` is above 16 or the extension
  has a character that is not allowed; `X` is above 65536; a block holds more than `B`
  bytes; a block's mode is not 0–7; the data ends inside a header or a body; a block's
  CRC-32 doesn't match; the end record's total differs from the sum of the block sizes;
  anything follows the end record.
- **Bodies**: a stored body isn't `n` bytes long; a delta distance is 0; a primary index is
  0 or more than `n`; a walk row is 0, more than `n`, or doesn't match the walk; a map
  body is empty (no background byte); its range-coded size `R` is more than the bytes left
  in the map body.
- **Map body**: a presence flag is neither 0 nor 1; a present byte value has no series and
  no leftovers; a shape symbol below 63 names a list entry that doesn't exist; a
  series has an unknown type, breaks its parameter constraints, reaches position `n` or
  beyond, or touches a position that is already taken; a leftover count exceeds the number
  of free slots; a leftover's rank reaches past the last free slot.

These are not errors: non-minimal varints; the decoder reading past the end of the
range-coded bytes or the raw bits (it reads zeros); bytes at the end of a body that the decoder never reads. A
decoder may also refuse blocks above a size limit of its own (see "Overall layout").

## Conformance

A decoder conforms when it turns every valid file back into exactly the original bytes and
rejects every file described under "Validation". Encoders only have to write valid files:
two encoders may pick different modes, series or delta distances for the same data, so
their output can differ. The test vectors check this:

- `vectors/`: every mode on a range of inputs. Each `<input>.mode<k>[.blocks<n>].paw` must
  decode to `<input>.bin`, and the reference encoder reproduces it byte for byte.
- `vectors/unusual/`: valid files the reference encoder never writes (a stored extension,
  optional header data, non-minimal varints, a delta distance past the end, padded streams,
  a short block before a full one, a repeated new shape). Each `<input>.<case>.paw` must
  decode to `<input>.bin`.
- `vectors/invalid/`: one file per rule under "Validation", named after it. Each must be
  rejected.

Each folder's `manifest.json` describes its files.
