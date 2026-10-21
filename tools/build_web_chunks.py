"""Split Apple Color Emoji (already-converted CBDT TTF) into unicode-range web chunks.

Unlike web/split.py (which rebuilds CBDT from raw PNGs taken from an sbix source),
this script subsets an existing color-bitmap TTF and emits WOFF2 chunks with a
ready-to-use @font-face CSS. It is designed to run on a plain Linux runner using
the released AppleColorEmoji-Linux.ttf.

Two things matter for the emoji sequences to look right in the browser:

* ZWJ sequences (👨‍👩‍👧, ❤️‍🔥, 🧑‍🚀…) are drawn by a GSUB ligature: the font maps the
  whole sequence to a single glyph. For that ligature to survive the subsetting,
  every chunk must keep the ZWJ (U+200D) in its cmap and must keep the components
  of the sequences that start in that chunk, plus the ligature result glyphs.
  Otherwise the browser cannot form the sequence and falls back to the system
  emoji font, and the picker ends up mixing two different emoji styles.
* The variation selector (U+FE0F) and the skin tone modifiers are shared by
  many sequences, so they are added to every chunk.

The script prints a summary of the GSUB ligatures it found so that the build log
shows whether the sequences are really covered.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.subset import Subsetter, Options

# Codepoints que comparten muchas secuencias: se meten en todos los trozos.
SHARED_CPS = [0x200D, 0xFE0F, 0x200C] + list(range(0x1F3FB, 0x1F400))


def unicode_range(cps: list[int]) -> str:
    if not cps:
        return ""
    s = sorted(set(cps))
    parts: list[str] = []
    lo = hi = s[0]
    for cp in s[1:]:
        if cp == hi + 1:
            hi = cp
        else:
            parts.append(f"U+{lo:04X}" if lo == hi else f"U+{lo:04X}-{hi:04X}")
            lo = hi = cp
    parts.append(f"U+{lo:04X}" if lo == hi else f"U+{lo:04X}-{hi:04X}")
    return ", ".join(parts)


def read_ligatures(font: TTFont) -> dict[str, list[tuple[list[str], str]]]:
    """Ligaduras GSUB: glifo inicial -> [(componentes, glifo resultado), ...]."""
    out: dict[str, list[tuple[list[str], str]]] = {}
    if "GSUB" not in font:
        return out
    lookup_list = font["GSUB"].table.LookupList
    if not lookup_list:
        return out
    for lookup in lookup_list.Lookup:
        if lookup.LookupType != 4:  # LigatureSubst
            continue
        for st in lookup.SubTable:
            ligs = getattr(st, "ligatures", None)
            if not ligs:
                continue
            for first, lig_list in ligs.items():
                for lig in lig_list:
                    out.setdefault(first, []).append((list(lig.Component), lig.LigGlyph))
    return out


def main() -> int:
    src = Path(sys.argv[1])
    outdir = Path(sys.argv[2])
    budget_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 1500
    outdir.mkdir(parents=True, exist_ok=True)

    font = TTFont(src, lazy=True)
    cmap = font.getBestCmap() or {}
    ligatures = read_ligatures(font)
    print(f"Tablas: {', '.join(font.keys())}")
    print(f"Codepoints en el cmap: {len(cmap)}")
    print(f"Ligaduras GSUB: {sum(len(v) for v in ligatures.values())} (desde {len(ligatures)} glifos)")
    font.close()

    # Solo emojis: codepoints a partir de U+2000 (símbolos/pictogramas) y banderas.
    emoji_cps = sorted(cp for cp in cmap if cp >= 0x2000)
    if not emoji_cps:
        print("No emoji codepoints found", file=sys.stderr)
        return 1

    # Codepoints que hace falta tener en un trozo: los de la secuencia completa.
    cps_por_glifo: dict[str, set[int]] = {}
    for cp, name in cmap.items():
        cps_por_glifo.setdefault(name, set()).add(cp)

    # Coste estimado por codepoint: tamaño del glifo / número de glifos.
    total = len(emoji_cps)
    avg = max(1, (src.stat().st_size) // total)
    per_cp = avg
    budget = budget_kb * 1024

    groups: list[list[int]] = []
    cur: list[int] = []
    for cp in emoji_cps:
        if cur and (len(cur) + 1) * per_cp > budget:
            groups.append(cur)
            cur = []
        cur.append(cp)
    if cur:
        groups.append(cur)

    chunks: list[tuple[str, str]] = []  # (unicode-range, filename)
    sec_total = 0
    for idx, cps in enumerate(groups, start=1):
        # Componentes de las secuencias que empiezan en este trozo (para que la
        # ligadura se pueda formar dentro de la misma fuente).
        extra: set[int] = set()
        for cp in cps:
            gname = cmap.get(cp)
            if not gname:
                continue
            for components, _result in ligatures.get(gname, []):
                for comp in components:
                    extra |= cps_por_glifo.get(comp, set())
        chunk_cps = sorted(set(cps) | set(SHARED_CPS) | extra)

        f = TTFont(src, lazy=False)
        opts = Options()
        opts.layout_features = ["*"]
        opts.layout_closure = True
        opts.name_IDs = ["*"]
        opts.name_legacy = True
        opts.name_languages = ["*"]
        opts.notdef_outline = True
        opts.recalc_bounds = False
        opts.drop_tables += ["DSIG"]
        opts.passthrough_tables = True
        sub = Subsetter(options=opts)
        sub.populate(unicodes=chunk_cps)
        sub.subset(f)

        buf = io.BytesIO()
        f.flavor = "woff2"
        f.save(buf)
        f.close()
        name = f"AppleColorEmoji[{idx}].woff2"
        (outdir / name).write_bytes(buf.getvalue())
        size = buf.getvalue()
        chunks.append((unicode_range(chunk_cps), name))
        sec = sum(1 for cp in cps if cmap.get(cp) in ligatures)
        sec_total += sec
        print(
            f"Trozo {idx}: {len(cps)} cps (+{len(chunk_cps) - len(cps)} de secuencias) "
            f"-> {name} ({len(size) // 1024} KB, {sec} con ligadura)"
        )

    css = ["/* Apple Color Emoji - generado desde AppleColorEmoji-Linux.ttf */", ""]
    for rstr, fname in chunks:
        css += [
            "@font-face {",
            "  font-family: 'Apple Color Emoji';",
            "  font-style: normal;",
            "  font-weight: 400;",
            "  font-display: swap;",
            f"  src: url('./{fname}') format('woff2');",
            f"  unicode-range: {rstr};",
            "}",
            "",
        ]
    (outdir / "AppleColorEmoji.css").write_text("\n".join(css), encoding="utf-8")
    print(f"{len(chunks)} trozos + AppleColorEmoji.css; {sec_total} secuencias con ligadura")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
