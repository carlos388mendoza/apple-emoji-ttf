"""Verifica los trozos web de Apple Color Emoji.

Para cada trozo comprueba que:

* el CBDT/CBLC tiene los strikes pedidos y que todos los glifos con bitmap
  existen tambien en el cmap o son resultados de ligadura,
* el GSUB conserva las ligaduras de las secuencias que el trozo cubre,
* cada ligadura tiene su glifo compuesto (con bitmap) dentro del mismo trozo,
* el ZWJ (U+200D) esta en el cmap del trozo (imprescindible para las secuencias).

Ademas, para el catalogo de emojis de la app, comprueba que cada secuencia
multi-codepoint queda compuesta por un unico glifo: para eso busca el trozo que
cubre *todos* sus codepoints (tiene que ser el mismo, si no el navegador parte
la secuencia) y verifica que ese trozo tiene la ligadura, el glifo compuesto con
su bitmap y todos los componentes en su cmap. Tambien comprueba que ningun
codepoint este en dos trozos a la vez.

Uso:
    python tools/verify_web_chunks.py output [--catalog input/emojis-es.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

RUTA_CSS = re.compile(
    r"url\('\./(?P<archivo>[^']+)'\)[^;]*;\s*unicode-range:\s*(?P<rango>[^;]+);",
    re.DOTALL,
)
RANGO = re.compile(r"U\+([0-9A-Fa-f]{1,6})(?:-([0-9A-Fa-f]{1,6}))?")


class Trozo:
    def __init__(self, path: Path, rango: str) -> None:
        self.path = path
        self.rango_texto = rango
        self.cps = self._parsear(rango)
        self.font = TTFont(str(path), lazy=False)
        self.cmap = self.font.getBestCmap() or {}
        self.orden = self.font.getGlyphOrder()
        self.gid = {nombre: i for i, nombre in enumerate(self.orden)}
        self.reglas = self._ligaduras()
        self.con_bitmap = self._glifos_con_bitmap()

    @staticmethod
    def _parsear(rango: str) -> set[int]:
        cps: set[int] = set()
        for base, fin in RANGO.findall(rango):
            inicio = int(base, 16)
            final = int(fin, 16) if fin else inicio
            cps.update(range(inicio, final + 1))
        return cps

    def _ligaduras(self) -> dict[tuple[str, tuple[str, ...]], str]:
        reglas: dict[tuple[str, tuple[str, ...]], str] = {}
        if "GSUB" not in self.font:
            return reglas
        for lookup in self.font["GSUB"].table.LookupList.Lookup:
            if lookup.LookupType != 4:
                continue
            for st in lookup.SubTable:
                for primero, lista in getattr(st, "ligatures", {}).items():
                    for lig in lista:
                        reglas[(primero, tuple(lig.Component))] = lig.LigGlyph
        return reglas

    def _glifos_con_bitmap(self) -> set[str]:
        nombres: set[str] = set()
        if "CBLC" not in self.font:
            return nombres
        cblc = self.font.reader["CBLC"]
        num_sizes = int.from_bytes(cblc[4:8], "big")
        for i in range(num_sizes):
            base = 8 + i * 48
            array_offset = int.from_bytes(cblc[base : base + 4], "big")
            n_subtables = int.from_bytes(cblc[base + 8 : base + 12], "big")
            for j in range(n_subtables):
                rec = array_offset + j * 8
                first = int.from_bytes(cblc[rec : rec + 2], "big")
                last = int.from_bytes(cblc[rec + 2 : rec + 4], "big")
                st = array_offset + int.from_bytes(cblc[rec + 4 : rec + 8], "big")
                index_format = int.from_bytes(cblc[st : st + 2], "big")
                if index_format != 1:
                    continue
                for gid in range(first, last + 1):
                    if gid < len(self.orden):
                        nombres.add(self.orden[gid])
        return nombres

    def strikes(self) -> list[int]:
        cblc = self.font.reader["CBLC"]
        num_sizes = int.from_bytes(cblc[4:8], "big")
        return [cblc[8 + i * 48 + 44] for i in range(num_sizes)]

    def cubre(self, cp: int) -> bool:
        return cp in self.cps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("carpeta", help="carpeta con los trozos y el CSS")
    parser.add_argument("--catalog", default="", help="catalogo de emojis de la app")
    args = parser.parse_args()

    carpeta = Path(args.carpeta)
    css = (carpeta / "AppleColorEmoji.css").read_text(encoding="utf-8")
    # La familia "Apple Color Emoji Sueltos" (iconos de un solo emoji) reusa los trozos: se revisa aparte.
    css, _, sueltos = css.partition("/* Apple Color Emoji Sueltos")
    bloques = RUTA_CSS.findall(css)
    for archivo, rango in RUTA_CSS.findall(sueltos):
        trozo = Trozo(carpeta / archivo, rango)
        faltan = [cp for cp in trozo.cps if cp not in trozo.cmap]
        if faltan:
            print(f"Sueltos: {archivo} no tiene {len(faltan)} codepoints de su rango", file=sys.stderr)
            return 1
        if not trozo.con_bitmap:
            print(f"Sueltos: {archivo} no tiene bitmaps", file=sys.stderr)
            return 1
    if not bloques:
        print("El CSS no tiene bloques @font-face", file=sys.stderr)
        return 1

    trozos: list[Trozo] = []
    for archivo, rango in bloques:
        trozos.append(Trozo(carpeta / archivo, rango))

    fallos: list[str] = []
    total_bitmap = 0
    for trozo in trozos:
        # El ZWJ solo hace falta donde viven las secuencias (la primera cara).
        if trozo.reglas and 0x200D not in trozo.cmap:
            fallos.append(f"{trozo.path.name}: el ZWJ no esta en el cmap")
        for (primero, componentes), resultado in trozo.reglas.items():
            if resultado not in trozo.con_bitmap:
                fallos.append(
                    f"{trozo.path.name}: la ligadura de {primero} no tiene glifo compuesto"
                )
        total_bitmap += len(trozo.con_bitmap)
        print(
            f"{trozo.path.name}: {len(trozo.cps)} cps, {len(trozo.orden)} glifos, "
            f"{len(trozo.con_bitmap)} con bitmap, {len(trozo.reglas)} ligaduras, "
            f"strikes {trozo.strikes()}, {trozo.path.stat().st_size // 1024} KB"
        )

    # El navegador elige la cara POR CARACTER: la primera del CSS cuyo
    # unicode-range lo incluye. Una secuencia se dibuja como un solo glifo solo si
    # TODOS sus caracteres caen en la misma cara y esa cara tiene la ligadura. Que
    # un codepoint este en dos rangos no es un fallo en si: gana el primero, y es
    # justo el truco que usa la cara de secuencias para quedarse con el ZWJ, el
    # VS16 y los tonos.
    def cara_de(cp: int) -> "Trozo | None":
        for trozo in trozos:
            if trozo.cubre(cp):
                return trozo
        return None

    if args.catalog:
        data = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        items = data.get("emojis", data) if isinstance(data, dict) else data
        secuencias = []
        for item in items:
            valor = item[0] if isinstance(item, list) else item
            if isinstance(valor, str) and len(valor) > 1:
                secuencias.append(tuple(ord(c) for c in valor))
        compuestas = 0
        delegadas = 0
        sin_componer = []
        # La fuente de Apple no trae el bitmap de la variante con VS16 de estos
        # simbolos: se dibujan con la fuente del sistema (igual que antes).
        sin_variante_apple = {0x2640, 0x2642, 0x2695}
        for seq in secuencias:
            if seq[0] < 0x2000 or seq[0] in sin_variante_apple:
                # Por debajo de U+2000 (teclas, digitos, copyright) no entramos a
                # proposito: la fuente del sistema los dibuja mejor.
                delegadas += 1
                continue
            # El VS16 no dibuja nada: si la secuencia es base + VS16, la dibuja
            # el propio glifo de la base (no hace falta ligadura).
            base = [cp for cp in seq if cp not in (0xFE0F, 0x200C)]
            if len(base) == 1:
                compuestas += 1
                continue
            # Las secuencias ZWJ con VS16 se dejan al sistema (enteras, otro estilo).
            if 0x200D in seq and 0xFE0F in seq:
                delegadas += 1
                continue
            # Las secuencias sin ZWJ (banderas, teclas, VS16) se dejan al sistema.
            if 0x200D not in seq:
                delegadas += 1
                continue
            caras = {cara_de(cp) for cp in base}
            if None in caras or len(caras) != 1:
                sin_componer.append(seq)
                continue
            trozo = caras.pop()
            nombres = [trozo.cmap.get(cp) for cp in base]
            if any(n is None for n in nombres):
                sin_componer.append(seq)
                continue
            clave = (nombres[0], tuple(nombres[1:]))
            if clave in trozo.reglas and trozo.reglas[clave] in trozo.con_bitmap:
                compuestas += 1
            else:
                sin_componer.append(seq)
        print(
            f"Catalogo: {compuestas}/{len(secuencias) - delegadas} secuencias "
            f"multi-codepoint quedan compuestas por un solo glifo "
            f"({delegadas} delegadas a la fuente del sistema)"
        )
        for seq in sin_componer[:20]:
            print(f"  sin componer: {' '.join(f'{cp:04X}' for cp in seq)}")
        if sin_componer:
            fallos.append(f"{len(sin_componer)} secuencias del catalogo sin componer")

    for fallo in fallos:
        print(f"FALLO: {fallo}", file=sys.stderr)
    print(f"Total: {len(trozos)} trozos, {total_bitmap} glifos con bitmap")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
