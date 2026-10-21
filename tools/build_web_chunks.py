"""Split Apple Color Emoji (already-converted CBDT TTF) into unicode-range web chunks.

Unlike web/split.py (which rebuilds CBDT from raw PNGs taken from an sbix source),
this script subsets an existing color-bitmap TTF and emits WOFF2 + WOFF chunks with
a ready-to-use @font-face CSS. It is designed to run on a plain Linux runner using
the released AppleColorEmoji-Linux.ttf.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.subset import Subsetter, Options


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


def main() -> int:
    src = Path(sys.argv[1])
    outdir = Path(sys.argv[2])
    budget_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 480
    outdir.mkdir(parents=True, exist_ok=True)

    font = TTFont(src, lazy=True)
    cmap = font.getBestCmap() or {}
    # Solo emojis: codepoints a partir de U+2000 (símbolos/pictogramas) y banderas.
    emoji_cps = sorted(cp for cp in cmap if cp >= 0x2000)
    font.close()
    if not emoji_cps:
        print("No emoji codepoints found", file=sys.stderr)
        return 1

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
    for idx, cps in enumerate(groups, start=1):
        f = TTFont(src, lazy=False)
        opts = Options()
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]
        opts.name_legacy = True
        opts.name_languages = ["*"]
        opts.notdef_outline = True
        opts.recalc_bounds = False
        opts.drop_tables += ["DSIG"]
        opts.passthrough_tables = True
        sub = Subsetter(options=opts)
        sub.populate(unicodes=cps)
        sub.subset(f)

        buf = io.BytesIO()
        f.flavor = "woff2"
        f.save(buf)
        f.close()
        name = f"AppleColorEmoji[{idx}].woff2"
        (outdir / name).write_bytes(buf.getvalue())
        size = buf.getvalue()
        chunks.append((unicode_range(cps), name))
        print(f"Chunk {idx}: {len(cps)} cps -> {name} ({len(size)//1024} KB)")

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
    print(f"Wrote {len(chunks)} chunks + AppleColorEmoji.css")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
