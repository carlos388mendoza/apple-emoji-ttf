"""Compara dos TTF de Apple Color Emoji: tablas, GSUB, glifos de secuencia y metricas.

Sirve para decidir con cual se pueden hacer los trozos web: hace falta una fuente con
las ligaduras GSUB de las secuencias con ZWJ (Chrome en Windows no aplica la tabla
AAT `morx`).
"""

from __future__ import annotations

import sys

from fontTools.ttLib import TTFont


def describe(path: str) -> None:
    font = TTFont(path, lazy=True)
    print(f"== {path}")
    print(f"  tablas: {', '.join(sorted(font.keys()))}")
    print(f"  upem: {font['head'].unitsPerEm}")
    cmap = font.getBestCmap() or {}
    print(f"  cmap: {len(cmap)}")
    order = font.getGlyphOrder()
    print(f"  glifos: {len(order)}  (muestra: {', '.join(order[1:6])})")
    seq = [g for g in order if g.startswith("seq.")]
    print(f"  glifos seq.*: {len(seq)}  (muestra: {', '.join(seq[:4])})")
    if "GSUB" in font:
        total = 0
        for lookup in font["GSUB"].table.LookupList.Lookup:
            if lookup.LookupType == 4:
                for st in lookup.SubTable:
                    total += sum(len(v) for v in st.ligatures.values())
        print(f"  ligaduras GSUB: {total}")
    else:
        print("  ligaduras GSUB: sin tabla")
    print(f"  morx: {'si' if 'morx' in font else 'no'}")
    hmtx = font["hmtx"].metrics
    print(f"  hhea: ascent={font['hhea'].ascent} descent={font['hhea'].descent} lineGap={font['hhea'].lineGap}")
    os2 = font["OS/2"]
    print(
        f"  OS/2: winAscent={os2.usWinAscent} winDescent={os2.usWinDescent} "
        f"typo={os2.sTypoAscender}/{os2.sTypoDescender} fsType={os2.fsType}"
    )
    for cp in (0x1F600, 0x1F468, 0x263A, 0x1F3FB):
        name = cmap.get(cp)
        if name and name in hmtx:
            print(f"  U+{cp:04X} ({name}): avance={hmtx[name][0]}")
    print(f"  familia: {font['name'].getDebugName(1)} | subfamilia: {font['name'].getDebugName(2)}")
    try:
        data = font["CBDT"].strikeData
        print(f"  CBDT strikes: {len(data)}")
        for strike in data[:4]:
            size = getattr(strike, "bitmapSizeTable", None)
            ppem = getattr(size, "ppemX", "?")
            glifos = getattr(strike, "bitmapGlyphs", {}) or {}
            muestra = []
            for cp in (0x1F600, 0x1F468):
                name = cmap.get(cp)
                g = glifos.get(name) if name else None
                if g is None:
                    continue
                m = g.metrics
                muestra.append(f"{name}: {m.width}x{m.height} bearing=({m.BearingX},{m.BearingY}) advance={m.Advance}")
            print(f"    {ppem}px -> {' | '.join(muestra) or 'sin muestra'}")
    except Exception as exc:  # pragma: no cover - solo diagnostico
        print(f"  CBDT: no se pudo leer ({exc})")
    font.close()


for arg in sys.argv[1:]:
    describe(arg)
