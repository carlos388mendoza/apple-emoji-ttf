"""Build the web chunks of Apple Color Emoji from the released Windows TTF.

Why the Windows build of the upstream release: it is the only published build that
carries the *GSUB* ligatures which draw the ZWJ sequences (👨‍👩‍👧, ❤️‍🔥, 🧑‍🚀…) as one
single glyph, plus the bitmaps of those composed sequences. The Linux build only has
the AAT `morx` table, which Chrome on Windows never applies, so every ZWJ sequence
fell back to the system emoji font and the icon picker mixed two different styles.

What this script does:

1. reads the PNGs of the wanted strikes (96 px by default, the same single strike the
   published web build uses) straight from the source CBDT/CBLC tables,
2. groups the codepoints into chunks (same idea as ``web/split.py``), but keeping
   together all the codepoints that take part in the same emoji sequence,
3. subsets the source font for every chunk (the GSUB ligatures survive) and rebuilds
   CBDT/CBLC with only the wanted strikes,
4. writes the chunks as WOFF2 plus the matching ``@font-face`` CSS.

Three details matter for the sequences to be drawn as a single glyph:

* the ``unicode-range`` of a chunk only lists *its own* codepoints (plus the shared
  ones), so a codepoint never resolves to two different chunks;
* all the codepoints of a sequence live in the *same* chunk (they are grouped by
  connected components of the sequence graph). Otherwise the browser splits the
  sequence in two text runs, the ligature is never applied and the emoji is drawn
  as several loose glyphs (or with the system emoji font);
* a ligature whose composed glyph has no bitmap is dropped, otherwise the browser
  would draw an empty box.

The skin tone modifiers are left out of the font on purpose: the browser can only
use one font per codepoint and the tone is shared by every chunk, so the sequence
(base + tone) would always be split. Without them the whole sequence is drawn by
the system emoji font, which knows how to compose it.

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

# Codepoints compartidos por muchas secuencias: van en todos los trozos. Son
# invisibles o no cambian de fuente (el navegador no parte la secuencia al pasar
# de un codepoint invisible a otro), asi que no rompen la ligadura.
SHARED_CPS = {0x200D, 0xFE0F, 0x200C}

# Tonos de piel: NO van en la fuente. El navegador solo puede usar un codepoint
# con una fuente a la vez, y como el tono esta en todos los trozos, la secuencia
# (base + tono) se partiria en dos textos y saldria como dos glifos sueltos. Sin
# el tono en la fuente, la secuencia la dibuja entera la fuente del sistema
# (que si sabe componerla), que es como se ve hoy en la web.
TONE_CPS = set(range(0x1F3FB, 0x1F400))

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


def plan_chunks(
    base_cps: list[int],
    cmap: dict[int, str],
    rev_cmap: dict[str, set[int]],
    rules: dict[tuple[str, tuple[str, ...]], str],
    glyph_cost: dict[str, int],
    budget_bytes: int,
) -> list[list[int]]:
    """Agrupa los codepoints en trozos disjuntos.

    Los codepoints de una misma secuencia (👨‍👩‍👧, 🧑‍🚀, 🇲🇽…) tienen que caer en el
    MISMO trozo: el navegador elige una fuente por codepoint, asi que si la
    secuencia cae en dos trozos distintos la parte en dos textos, la ligadura no
    se aplica y el emoji sale como varios glifos sueltos (o con la fuente del
    sistema). Por eso se agrupan por componentes conexas del grafo de secuencias:
    todos los codepoints que aparecen juntos en alguna secuencia se quedan
    juntos.
    """
    avg = max(1, sum(glyph_cost.values()) // max(len(glyph_cost), 1))

    def cost(names: set[str]) -> int:
        return sum(glyph_cost.get(n, avg) for n in names)

    # Componentes conexas sobre los glifos que aparecen en alguna secuencia.
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        parent.setdefault(name, name)
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for first, components in rules:
        find(first)
        for comp in components:
            union(first, comp)

    grupos: dict[str, set[str]] = {}
    for name in list(parent):
        root = find(name)
        grupos.setdefault(root, set()).add(name)

    # Los codepoints de cada grupo y su coste (los glifos y los resultados).
    unidades: list[tuple[set[int], set[str], int]] = []
    usados: set[int] = set()
    for names in grupos.values():
        cps: set[int] = set()
        for name in names:
            cps |= rev_cmap.get(name, set())
        cps = {cp for cp in cps if cp in cmap and cp not in SHARED_CPS}
        if not cps:
            continue
        usados |= cps
        extra = {
            result
            for (first, components), result in rules.items()
            if first in names and all(c in names for c in components)
        }
        unidades.append((cps, names | extra, cost(names | extra)))

    sueltos = [cp for cp in base_cps if cp not in usados]
    for cp in sueltos:
        name = cmap.get(cp)
        nombres = {name} if name else set()
        unidades.append(({cp}, nombres, cost(nombres)))

    # Los grupos grandes (familias, banderas) van solos; los sueltos se rellenan
    # hasta el presupuesto.
    unidades.sort(key=lambda u: -u[2])
    bitmap_budget = max(budget_bytes - OVERHEAD_ESTIMATE, 64 * 1024)

    chunks: list[list[int]] = []
    actual: list[int] = []
    actual_coste = 0
    for cps, _nombres, coste in unidades:
        if actual and (actual_coste + coste) > bitmap_budget:
            chunks.append(actual)
            actual = []
            actual_coste = 0
        actual.extend(cps)
        actual_coste += coste
    if actual:
        chunks.append(actual)

    for chunk in chunks:
        chunk.sort()
    chunks.sort(key=lambda c: c[0])
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


def drop_ligatures_without_bitmap(font: TTFont, con_bitmap: set[str]) -> int:
    """Quita las ligaduras cuyo glifo compuesto no tiene bitmap.

    La fuente de Apple trae la regla (por ejemplo U+2642 + U+FE0F) pero no el
    bitmap del resultado: si la regla se queda, el navegador dibuja un recuadro
    vacio. Sin la regla, esa secuencia la dibuja la fuente del sistema.
    """
    if "GSUB" not in font:
        return 0
    gsub = font["GSUB"].table
    quitadas = 0
    for lookup in gsub.LookupList.Lookup:
        if lookup.LookupType != 4:
            continue
        for subtable in lookup.SubTable:
            ligatures = getattr(subtable, "ligatures", None)
            if not ligatures:
                continue
            for primero in list(ligatures):
                keep = [lig for lig in ligatures[primero] if lig.LigGlyph in con_bitmap]
                quitadas += len(ligatures[primero]) - len(keep)
                if keep:
                    ligatures[primero] = keep
                else:
                    del ligatures[primero]
    return quitadas


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

    con_bitmap = {name for name in active_names if name in pngs}
    drop_ligatures_without_bitmap(font, con_bitmap)

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
    parser.add_argument("--ppem", default="96", help="strikes a incluir (por defecto 96)")
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

    # Una ligadura sin glifo compuesto dibujaria un emoji en blanco: mejor quitarla
    # y que el navegador use la fuente del sistema para esa secuencia.
    sin_bitmap = {key for key, result in rules.items() if result not in pngs}
    if sin_bitmap:
        print(
            f"Ligaduras sin glifo compuesto (se descartan): {len(sin_bitmap)}",
            file=sys.stderr,
        )
        rules = {key: result for key, result in rules.items() if key not in sin_bitmap}

    comp_map: dict[str, set[str]] = {}
    res_map: dict[str, set[str]] = {}
    for (first, components), result in rules.items():
        comp_map.setdefault(first, set()).update([first, *components])
        res_map.setdefault(first, set()).add(result)

    font.close()

    glyph_cost = {
        name: sum(len(png) for png in per_strike.values()) for name, per_strike in pngs.items()
    }

    base_cps = sorted(
        cp for cp in cmap if cp not in SHARED_CPS and cp not in TONE_CPS and cp >= 0x2000
    )
    print(f"Codepoints de emoji: {len(base_cps)}")

    chunks = plan_chunks(base_cps, cmap, rev_cmap, rules, glyph_cost, budget)
    print(f"Trozos: {len(chunks)}")

    css = [
        "/* Apple Color Emoji - generado desde AppleColorEmoji-Windows.ttf (build oficial) */",
        "",
    ]
    total = 0
    for index, chunk in enumerate(chunks, start=1):
        # Rango propio del trozo: sus codepoints y los compartidos. Es lo que ve el
        # navegador. Los componentes de las secuencias se quedan en el cmap del
        # trozo pero FUERA de su unicode-range: asi el navegador elige siempre este
        # trozo como fuente principal de la secuencia y la ligadura se aplica (si el
        # rango se solapara, otro trozo podria ganar y la secuencia se deshacia).
        own_cps: set[int] = set(chunk) | SHARED_CPS
        # Rango ampliado: incluye los componentes que necesitan las ligaduras de este
        # trozo, para que el subconjunto contenga el cmap y los glifos que hacen falta.
        ext_cps: set[int] = set(own_cps)
        for cp in chunk:
            name = cmap.get(cp)
            if name and name in comp_map:
                for comp_name in comp_map[name]:
                    ext_cps |= rev_cmap.get(comp_name, set())

        ext_names = {cmap[cp] for cp in ext_cps if cp in cmap}
        active: set[str] = set(ext_names)
        for cp in set(chunk) | SHARED_CPS:
            name = cmap.get(cp)
            if name:
                active |= comp_map.get(name, set())

        results = 0
        for (first, components), result in rules.items():
            names = [first, *components]
            if all(name in ext_names for name in names):
                active.add(result)
                results += 1

        data = build_chunk(source, ext_cps, active, pngs, ppems, metrics)
        filename = f"AppleColorEmoji[{index}].woff2"
        (outdir / filename).write_bytes(data)
        total += len(data)
        print(
            f"Trozo {index}: {len(chunk)} cps (+{len(own_cps) - len(chunk)} compartidos), "
            f"{results} ligaduras, {len(active)} glifos -> {filename} ({len(data) // 1024} KB)"
        )

        css += [
            "@font-face {",
            f"  font-family: '{FONT_FAMILY}';",
            "  font-style: normal;",
            "  font-weight: 400;",
            "  font-display: swap;",
            f"  src: url('./{filename}') format('woff2');",
            f"  unicode-range: {unicode_range(own_cps)};",
            "}",
            "",
        ]

    (outdir / "AppleColorEmoji.css").write_text("\n".join(css), encoding="utf-8")
    print(f"{len(chunks)} trozos + AppleColorEmoji.css; total {total // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
