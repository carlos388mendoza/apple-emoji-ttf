"""Build the web chunks of Apple Color Emoji from the released Windows TTF.

Why the Windows build of the upstream release: it is the only published build that
carries the *GSUB* ligatures which draw the ZWJ sequences (👨‍👩‍👧, ❤️‍🔥, 🧑‍🚀…) as one
single glyph, plus the bitmaps of those composed sequences. The Linux build only has
the AAT `morx` table, which Chrome on Windows never applies, so every ZWJ sequence
fell back to the system emoji font and the icon picker mixed two different styles.

What this script does:

1. reads the PNGs of the wanted strikes (32 and 96 px by default) straight from the
   source CBDT/CBLC tables,
2. groups the codepoints into budget-sized chunks (same idea as ``web/split.py``),
   keeping in every chunk the shared codepoints (ZWJ, VS16, skin tones) and the
   components of the sequences that start there,
3. subsets the source font for every chunk (the GSUB ligatures survive) and rebuilds
   CBDT/CBLC with only the wanted strikes,
4. writes the chunks as WOFF2 plus the matching ``@font-face`` CSS.

The ligatures can be limited to the sequences that the app really uses (the emoji
catalogue) with ``--catalog``; the rest of the sequences then keep using the system
emoji font, exactly as before this change.
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.subset import Options, Subsetter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tables.cbdt_cblc import (  # noqa: E402
    FontMetrics,
    build_cblc_strikes,
    build_cbdt_strikes,
)
from tables.raw import add_raw_table  # noqa: E402

# Codepoints compartidos por muchas secuencias: van en todos los trozos.
SHARED_CPS = {0x200D, 0xFE0F, 0x200C} | set(range(0x1F3FB, 0x1F400))

# Metricas verticales de la build de Linux (las mismas que usa hoy la web), para que
# el emoji no se mueva respecto a la version ya publicada.
UPEM = 2048
HHEA_ASCENT = 2048
HHEA_DESCENT = -640
HHEA_LINE_GAP = 0
TYPO_ASCENT = 1920
TYPO_DESCENT = -640

FONT_FAMILY = "Apple Color Emoji"
OVERHEAD_ESTIMATE = 80 * 1024


# --------------------------------------------------------------------------- #
# Lectura del CBDT / CBLC de la fuente de origen
# --------------------------------------------------------------------------- #
def extract_strike_pngs(cblc: bytes, cbdt: bytes, ppem: int) -> dict[int, bytes]:
    """Devuelve {id de glifo: PNG} del strike pedido."""
    num_sizes = struct.unpack(">I", cblc[4:8])[0]
    for i in range(num_sizes):
        base = 8 + i * 48
        array_offset, _tables_size, n_subtables, _color = struct.unpack(
            ">IIII", cblc[base : base + 16]
        )
        _first, _last, ppem_x, _ppem_y, _depth, _flags = struct.unpack(
            ">HHBBBb", cblc[base + 40 : base + 48]
        )
        if ppem_x != ppem:
            continue
        out: dict[int, bytes] = {}
        for j in range(n_subtables):
            rec = array_offset + j * 8
            first, last, additional = struct.unpack(">HHL", cblc[rec : rec + 8])
            st = array_offset + additional
            index_format, image_format, image_data_offset = struct.unpack(
                ">HHL", cblc[st : st + 8]
            )
            if index_format != 1 or image_format != 17:
                raise RuntimeError(
                    f"Formato de subtabla no soportado: {index_format}/{image_format}"
                )
            count = last - first + 1
            offsets = struct.unpack(
                f">{count + 1}I", cblc[st + 8 : st + 8 + 4 * (count + 1)]
            )
            for k in range(count):
                gid = first + k
                start = image_data_offset + offsets[k]
                size = struct.unpack(">I", cbdt[start + 5 : start + 9])[0]
                png = cbdt[start + 9 : start + 9 + size]
                if png[:8] != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError(f"El bitmap del glifo {gid} no es un PNG valido")
                out[gid] = png
        return out
    return {}


# --------------------------------------------------------------------------- #
# Ligaduras GSUB
# --------------------------------------------------------------------------- #
def read_ligatures(font: TTFont) -> dict[tuple[str, tuple[str, ...]], str]:
    """Reglas de ligadura GSUB: (glifo inicial, componentes) -> glifo resultado."""
    rules: dict[tuple[str, tuple[str, ...]], str] = {}
    if "GSUB" not in font:
        return rules
    lookup_list = font["GSUB"].table.LookupList
    if not lookup_list:
        return rules
    for lookup in lookup_list.Lookup:
        if lookup.LookupType != 4:  # LigatureSubst
            continue
        for st in lookup.SubTable:
            ligs = getattr(st, "ligatures", None)
            if not ligs:
                continue
            for first, lig_list in ligs.items():
                for lig in lig_list:
                    rules[(first, tuple(lig.Component))] = lig.LigGlyph
    return rules


def load_catalog_sequences(path: Path) -> set[tuple[int, ...]]:
    """Secuencias de emoji que usa la app (catalogo de iconos)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("emojis", data) if isinstance(data, dict) else data
    sequences: set[tuple[int, ...]] = set()
    for item in items:
        value = item[0] if isinstance(item, list) else item
        if isinstance(value, str) and value:
            sequences.add(tuple(ord(ch) for ch in value))
    return sequences


def catalog_rule_keys(
    sequences: set[tuple[int, ...]], cmap: dict[int, str]
) -> tuple[set[tuple[str, tuple[str, ...]]], int]:
    """Reglas GSUB que necesita el catalogo, y cuantas secuencias multi-codepoint hay."""
    keys: set[tuple[str, tuple[str, ...]]] = set()
    multi = 0
    for seq in sequences:
        if len(seq) < 2:
            continue
        multi += 1
        names = [cmap.get(cp) for cp in seq]
        if any(name is None for name in names):
            continue
        keys.add((names[0], tuple(names[1:])))
    return keys, multi


# --------------------------------------------------------------------------- #
# Reparto en trozos
# --------------------------------------------------------------------------- #
def unicode_range(cps: set[int]) -> str:
    if not cps:
        return ""
    ordered = sorted(cps)
    parts: list[str] = []
    lo = hi = ordered[0]
    for cp in ordered[1:]:
        if cp == hi + 1:
            hi = cp
        else:
            parts.append(f"U+{lo:04X}" if lo == hi else f"U+{lo:04X}-{hi:04X}")
            lo = hi = cp
    parts.append(f"U+{lo:04X}" if lo == hi else f"U+{lo:04X}-{hi:04X}")
    return ", ".join(parts)


def group_codepoints(
    base_cps: list[int],
    cmap: dict[int, str],
    comp_map: dict[str, set[str]],
    res_map: dict[str, set[str]],
    glyph_cost: dict[str, int],
    budget_bytes: int,
) -> list[list[int]]:
    """Agrupa los codepoints en trozos que no pasan del presupuesto."""
    avg = max(1, sum(glyph_cost.values()) // max(len(glyph_cost), 1))

    def cost(names: set[str]) -> int:
        return sum(glyph_cost.get(n, avg) for n in names)

    shared_active: set[str] = set()
    for cp in SHARED_CPS:
        name = cmap.get(cp)
        if not name:
            continue
        shared_active.add(name)
        shared_active |= comp_map.get(name, set())
        shared_active |= res_map.get(name, set())

    shared_cost = cost(shared_active)
    bitmap_budget = max(budget_bytes - OVERHEAD_ESTIMATE, 64 * 1024)

    chunks: list[list[int]] = []
    current: list[int] = []
    active = set(shared_active)
    current_cost = shared_cost

    for cp in base_cps:
        name = cmap.get(cp)
        new_names: set[str] = set()
        if name:
            new_names = {name} | comp_map.get(name, set()) | res_map.get(name, set())
        new_names -= active
        new_cost = cost(new_names)

        if current and (current_cost + new_cost) > bitmap_budget:
            chunks.append(current)
            current = []
            active = set(shared_active)
            current_cost = shared_cost
            new_names -= active
            new_cost = cost(new_names)

        current.append(cp)
        active |= new_names
        current_cost += new_cost

    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Construccion de un trozo
# --------------------------------------------------------------------------- #
def set_names(font: TTFont) -> None:
    """La build de Windows se llama "Segoe UI Emoji"; aqui es Apple Color Emoji."""
    name = font["name"]
    for record in list(name.names):
        name.removeNames(
            nameID=record.nameID,
            platformID=record.platformID,
            platEncID=record.platEncID,
            langID=record.langID,
        )
    values = {
        0: "Apple Color Emoji (web build generated from the macOS release). Not redistributable.",
        1: FONT_FAMILY,
        2: "Regular",
        3: f"{FONT_FAMILY}:Regular",
        4: FONT_FAMILY,
        5: "Version 1.000",
        6: "AppleColorEmoji-Regular",
    }
    for name_id, value in values.items():
        name.setName(value, name_id, 3, 1, 0x409)


def apply_metrics(font: TTFont) -> None:
    """Mismas metricas verticales que la build de Linux ya publicada."""
    if "hhea" in font:
        font["hhea"].ascent = HHEA_ASCENT
        font["hhea"].descent = HHEA_DESCENT
        font["hhea"].lineGap = HHEA_LINE_GAP
    if "OS/2" in font:
        os2 = font["OS/2"]
        os2.sTypoAscender = TYPO_ASCENT
        os2.sTypoDescender = TYPO_DESCENT
        os2.usWinAscent = 0
        os2.usWinDescent = 0


def build_chunk(
    source: Path,
    range_cps: set[int],
    active_names: set[str],
    pngs: dict[str, dict[int, bytes]],
    ppems: list[int],
    metrics: FontMetrics,
) -> bytes:
    """Subset de la fuente con un solo CBDT/CBLC reconstruido (los strikes pedidos)."""
    font = TTFont(source, lazy=True)
    for tag in ("CBDT", "CBLC"):
        if tag in font:
            del font[tag]

    options = Options()
    options.layout_features = ["*"]
    options.layout_closure = False
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = False
    options.drop_tables += [
        "DSIG",
        "morx",
        "feat",
        "bgcl",
        "trak",
        "meta",
        "vhea",
        "vmtx",
        "GPOS",
    ]
    options.passthrough_tables = False

    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes=sorted(range_cps), glyphs=sorted(active_names))
    subsetter.subset(font)

    set_names(font)
    apply_metrics(font)

    glyph_order = font.getGlyphOrder()
    gid_by_name = {name: gid for gid, name in enumerate(glyph_order)}

    strikes = []
    ordered = sorted((gid_by_name[name], name) for name in active_names)
    for ppem in ppems:
        strike_glyphs = [
            (gid, name, pngs[name][ppem])
            for gid, name in ordered
            if ppem in pngs.get(name, {})
        ]
        if strike_glyphs:
            strikes.append((ppem, strike_glyphs))

    if not strikes:
        raise RuntimeError("El trozo no tiene ningun bitmap")

    cbdt, strike_data = build_cbdt_strikes(strikes, metrics, y_bearing="five_sixths_height")
    cblc = build_cblc_strikes(strike_data, metrics)
    add_raw_table(font, "CBDT", cbdt)
    add_raw_table(font, "CBLC", cblc)

    buffer = io.BytesIO()
    font.flavor = "woff2"
    font.save(buffer)
    font.close()
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="TTF de origen (build de Windows del release)")
    parser.add_argument("outdir", help="carpeta de salida")
    parser.add_argument("--ppem", default="32,96", help="strikes a incluir (por defecto 32,96)")
    parser.add_argument("--budget-kb", type=int, default=700, help="tamano objetivo por trozo")
    parser.add_argument("--catalog", default="", help="catalogo de emojis para podar ligaduras")
    args = parser.parse_args()

    source = Path(args.source)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ppems = [int(p) for p in args.ppem.split(",") if p.strip()]
    budget = args.budget_kb * 1024
    metrics = FontMetrics(upem=UPEM, ascent=HHEA_ASCENT, descent=HHEA_DESCENT)

    font = TTFont(source, lazy=True)
    print("Tablas:", ", ".join(sorted(font.keys())))
    for tag in sorted(font.keys()):
        try:
            print(f"  {tag}: {font.reader.tables[tag].length} bytes")
        except Exception:  # pragma: no cover - solo diagnostico
            pass

    cmap = font.getBestCmap() or {}
    glyph_order = font.getGlyphOrder()
    rules = read_ligatures(font)
    print(f"Codepoints en el cmap: {len(cmap)}")
    print(f"Ligaduras GSUB: {len(rules)}")

    rev_cmap: dict[str, set[int]] = {}
    for cp, name in cmap.items():
        rev_cmap.setdefault(name, set()).add(cp)

    if args.catalog:
        sequences = load_catalog_sequences(Path(args.catalog))
        wanted, multi = catalog_rule_keys(sequences, cmap)
        all_rules = rules
        rules = {key: result for key, result in all_rules.items() if key in wanted}
        covered = sum(1 for key in wanted if key in all_rules)
        print(
            f"Ligaduras del catalogo: {len(rules)} de {len(wanted)} "
            f"({covered}/{multi} secuencias del catalogo cubiertas)"
        )
        if not rules:
            print("Aviso: ninguna ligadura del catalogo; se usan todas", file=sys.stderr)
            rules = all_rules

    comp_map: dict[str, set[str]] = {}
    res_map: dict[str, set[str]] = {}
    for (first, components), result in rules.items():
        comp_map.setdefault(first, set()).update([first, *components])
        res_map.setdefault(first, set()).add(result)

    # PNGs de los strikes que nos interesan.
    cblc_data = font.reader["CBLC"]
    cbdt_data = font.reader["CBDT"]
    pngs: dict[str, dict[int, bytes]] = {}
    for ppem in ppems:
        strike = extract_strike_pngs(cblc_data, cbdt_data, ppem)
        for gid, png in strike.items():
            if gid >= len(glyph_order):
                continue
            pngs.setdefault(glyph_order[gid], {})[ppem] = png
        print(f"Strike {ppem}px: {len(strike)} glifos")
    font.close()

    glyph_cost = {
        name: sum(len(png) for png in per_strike.values()) for name, per_strike in pngs.items()
    }

    base_cps = sorted(cp for cp in cmap if cp not in SHARED_CPS and cp >= 0x2000)
    print(f"Codepoints de emoji: {len(base_cps)}")

    chunks = group_codepoints(base_cps, cmap, comp_map, res_map, glyph_cost, budget)
    print(f"Trozos: {len(chunks)}")

    css = [
        "/* Apple Color Emoji - generado desde AppleColorEmoji-Windows.ttf (build oficial) */",
        "",
    ]
    total = 0
    for index, chunk in enumerate(chunks, start=1):
        range_cps: set[int] = set(chunk) | SHARED_CPS
        for cp in chunk:
            name = cmap.get(cp)
            if name and name in comp_map:
                for comp_name in comp_map[name]:
                    range_cps |= rev_cmap.get(comp_name, set())

        range_names = {cmap[cp] for cp in range_cps if cp in cmap}
        active: set[str] = set(range_names)
        for cp in set(chunk) | SHARED_CPS:
            name = cmap.get(cp)
            if name:
                active |= comp_map.get(name, set())

        results = 0
        for (first, components), result in rules.items():
            names = [first, *components]
            if all(name in range_names for name in names):
                active.add(result)
                results += 1

        data = build_chunk(source, range_cps, active, pngs, ppems, metrics)
        filename = f"AppleColorEmoji[{index}].woff2"
        (outdir / filename).write_bytes(data)
        total += len(data)
        print(
            f"Trozo {index}: {len(chunk)} cps (+{len(range_cps) - len(chunk)} compartidos), "
            f"{results} ligaduras, {len(active)} glifos -> {filename} ({len(data) // 1024} KB)"
        )

        css += [
            "@font-face {",
            f"  font-family: '{FONT_FAMILY}';",
            "  font-style: normal;",
            "  font-weight: 400;",
            "  font-display: swap;",
            f"  src: url('./{filename}') format('woff2');",
            f"  unicode-range: {unicode_range(range_cps)};",
            "}",
            "",
        ]

    (outdir / "AppleColorEmoji.css").write_text("\n".join(css), encoding="utf-8")
    print(f"{len(chunks)} trozos + AppleColorEmoji.css; total {total // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
