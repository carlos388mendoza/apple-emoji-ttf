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
    print(f"  cmap: {len(font.getBestCmap() or {})}")
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
    if "morx" in font:
        print("  morx: si")
    hmtx = font["hmtx"].metrics
    print(f"  hhea: ascent={font['hhea'].ascent} descent={font['hhea'].descent} lineGap={font['hhea'].lineGap}")
    os2 = font["OS/2"]
    print(f"  OS/2: winAscent={os2.usWinAscent} winDescent={os2.usWinDescent} typo={os2.sTypoAscender}/{os2.sTypoDescender} fsType={os2.fsType}")
    for name in ("uni1F600", "uni1F468", "uni263A"):
        if name in hmtx:
            print(f"  avance {name}: {hmtx[name][0]}")
    print(f"  familia: {font['name'].getDebugName(1)} | subfamilia: {font['name'].getDebugName(2)}")
    if "CBDT" in font:
        cbdt = font["CBDT"].strikeData
        print(f"  CBDT strikes: {sorted(cbdt.keys())}")
        for ppem in sorted(cbdt.keys())[:3]:
            strike = cbdt[ppem]
            muestra = []
            for name in ("uni1F600", "uni1F468"):
                g = strike.get(name)
                if g is None:
                    continue
                m = g.metrics
                muestra.append(f"{name}: alto={m.height} ancho={m.width} bearing=({m.BearingX},{m.BearingY}) avance={m.Advance}")
            print(f"    {ppem}px -> {' | '.join(muestra) or 'sin muestra'}")
    font.close()


for arg in sys.argv[1:]:
    describe(arg)
