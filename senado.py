#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
congreso-wire · Senado
======================

Lee los PTR del Senate Electronic Financial Disclosure (efd.senate.gov) y los
fusiona dentro de docs/trades.json, el mismo archivo que escribe scraper.py.

Corre DESPUÉS de scraper.py: lee lo que aquél dejó, le agrega los senadores y
reescribe el paquete. Cada presentación queda etiquetada con "camara".

A diferencia de la Cámara, acá no hay PDFs que parsear: el buscador devuelve
JSON y los PTR electrónicos son tablas HTML. Lo único que complica es la
puerta de entrada — hay que aceptar un acuerdo con cookie de sesión y token
CSRF antes de que el servidor conteste nada.

Las presentaciones en papel (escaneadas) se guardan con cero operaciones y el
enlace al original, para que el panel las muestre igual.
"""

import json
import os
import re
import sys
import time
import datetime as dt
from html.parser import HTMLParser

import requests

# ------------------------------------------------------------------ ajustes

CONTACTO = "rodrigo.ingeniero88@gmail.com"
UA = f"congreso-wire/1.0 ({CONTACTO})"
# Ojo: efd.senate.gov es el portal donde los senadores cargan sus
# declaraciones. El buscador público, que es el que sirve acá, vive en
# efdsearch.senate.gov.
BASE = "https://efdsearch.senate.gov"

DIAS_HISTORIA    = 180   # misma ventana que la Cámara
MAX_INFORMES     = 250   # tope de informes nuevos por corrida
PAUSA            = 0.6   # segundos entre descargas
PARSER_V         = 3     # subilo si cambia el parseo: fuerza releer todo

RAIZ   = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(RAIZ, "docs", "trades.json")
ESTADO = os.path.join(RAIZ, "state", "senado.json")

# El eFD tiene un filtro delante que devuelve 403 a cualquier cliente que no
# parezca un navegador. Hay que mandar el juego completo de encabezados.
sesion = requests.Session()
sesion.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
})

# ------------------------------------------------------------------ mapeos
# Se traducen a las mismas etiquetas que usa el scraper de la Cámara, para que
# el panel no tenga que distinguir de dónde vino cada operación.

TIPO_OP = [
    (r"purchase",        ("Compra", "buy")),
    (r"sale.*partial",   ("Venta parcial", "sell")),
    (r"sale",            ("Venta", "sell")),
    (r"exchange",        ("Canje", "")),
]

TIPO_ACTIVO = [
    (r"stock option|option",              "Opción"),
    (r"non-?public stock",                "Acción no cotizante"),
    (r"stock",                            "Acción"),
    (r"exchange.?traded|^etf",            "ETF"),
    (r"mutual fund",                      "Fondo mutuo"),
    (r"corporate bond",                   "Bono corporativo"),
    (r"municipal",                        "Bono municipal"),
    (r"government|treasur",               "Bono del Estado"),
    (r"cryptocurrency|digital asset",     "Cripto"),
    (r"real estate|farm",                 "Inmueble"),
    (r"other securities|other",           "Otro"),
]

DUENOS = [
    (r"spouse",            "Cónyuge"),
    (r"child|dependent",   "Hijo a cargo"),
    (r"joint",             "Conjunta"),
    (r"self",              "Titular"),
]


def _traducir(tabla, texto, defecto=""):
    t = (texto or "").strip().lower()
    for patron, valor in tabla:
        if re.search(patron, t):
            return valor
    return defecto


# ------------------------------------------------------------------ HTML

class _Tablas(HTMLParser):
    """Extrae todas las tablas de un HTML como listas de listas de texto.

    Se usa la librería estándar a propósito: el flujo de GitHub Actions solo
    instala requests y pdfplumber, y no vale la pena sumar otra dependencia
    para leer una tabla de nueve columnas.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tablas, self._t, self._f, self._c = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._t = []
        elif tag == "tr" and self._t is not None:
            self._f = []
        elif tag in ("td", "th") and self._f is not None:
            self._c = []
        elif tag == "br" and self._c is not None:
            self._c.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._c is not None:
            self._f.append(re.sub(r"\s+", " ", "".join(self._c)).strip())
            self._c = None
        elif tag == "tr" and self._f is not None:
            if self._t is not None and self._f:
                self._t.append(self._f)
            self._f = None
        elif tag == "table" and self._t is not None:
            self.tablas.append(self._t)
            self._t = None

    def handle_data(self, d):
        if self._c is not None:
            self._c.append(d)


def tablas_de(html):
    p = _Tablas()
    try:
        p.feed(html)
    except Exception as e:
        print(f"    ! HTML ilegible: {e}")
    return p.tablas


def sin_etiquetas(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# ------------------------------------------------------------------ sesión

def abrir_sesion(intentos=3):
    """Acepta el acuerdo de uso y devuelve el token CSRF vigente."""
    for n in range(1, intentos + 1):
        r = sesion.get(f"{BASE}/search/home/", timeout=45,
                       headers={"Sec-Fetch-Site": "none"})
        if r.status_code == 200:
            break
        print(f"  intento {n}: HTTP {r.status_code}")
        if n == intentos:
            r.raise_for_status()
        time.sleep(3 * n)
    m = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)', r.text)
    if not m:
        raise RuntimeError("no apareció el csrfmiddlewaretoken en el home")
    token = m.group(1)

    r = sesion.post(
        f"{BASE}/search/home/",
        data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
        headers={"Referer": f"{BASE}/search/home/"},
        timeout=45,
    )
    r.raise_for_status()
    # el POST suele rotar la cookie; se usa la nueva si cambió
    return sesion.cookies.get("csrftoken") or token


def buscar(token, desde, inicio=0, tanda=100):
    """Una página de resultados del buscador. Devuelve (filas, total)."""
    payload = {
        "start": str(inicio),
        "length": str(tanda),
        "report_types": "[11]",          # 11 = Periodic Transaction Report
        "filer_types": "[]",
        "submitted_start_date": desde.strftime("%m/%d/%Y 00:00:00"),
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": token,
    }
    r = sesion.post(
        f"{BASE}/search/report/data/",
        data=payload,
        headers={
            "Referer": f"{BASE}/search/",
            "X-CSRFToken": token,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    return j.get("data", []), j.get("recordsTotal", 0)


FECHA = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def entrada(fila):
    """Normaliza una fila del buscador. Devuelve None si no se entiende."""
    celdas = [str(c) for c in fila]
    href = next((m.group(1) for c in celdas
                 if (m := re.search(r'href=["\']([^"\']+)', c))), None)
    if not href:
        return None
    url = href if href.startswith("http") else BASE + href

    doc = next((p for p in url.rstrip("/").split("/")[::-1] if len(p) > 20), url)
    fecha = next((m.group(0) for c in celdas[::-1]
                  if (m := FECHA.search(sin_etiquetas(c)))), "")

    # las dos primeras columnas son nombre y apellido; la tercera, el cargo
    nombre = " ".join(sin_etiquetas(c) for c in celdas[:2] if sin_etiquetas(c))
    cargo = sin_etiquetas(celdas[2]) if len(celdas) > 2 else ""
    est = re.search(r"\(([A-Z]{2})\)|\b([A-Z]{2})\s*$", cargo)
    estado = (est.group(1) or est.group(2)) if est else ""

    return {
        "doc_id":      "S-" + doc,
        "nombre":      nombre or sin_etiquetas(celdas[3]),
        "distrito":    estado,
        "fecha_aviso": fecha,
        "pdf":         url,
        "camara":      "Senado",
        "papel":       "/paper/" in url,
    }


# ------------------------------------------------------------------ informe

ENCABEZADOS = {
    "transaction date": "fecha_op",
    "owner":            "dueno",
    "ticker":           "ticker",
    "asset name":       "activo",
    "asset type":       "clase",
    "type":             "tipo",
    "amount":           "monto",
    "comment":          "comentario",
}


def leer_informe(html):
    """Operaciones de un PTR electrónico."""
    ops = []
    for tabla in tablas_de(html):
        if len(tabla) < 2:
            continue
        cab = [c.strip().lower().rstrip("*") for c in tabla[0]]
        col = {ENCABEZADOS[c]: i for i, c in enumerate(cab) if c in ENCABEZADOS}
        if "fecha_op" not in col or "monto" not in col:
            continue                       # no es la tabla de operaciones

        for fila in tabla[1:]:
            g = lambda k: fila[col[k]].strip() if k in col and col[k] < len(fila) else ""
            f_op = g("fecha_op")
            if not FECHA.search(f_op):
                continue

            etiqueta, direccion = _traducir(TIPO_OP, g("tipo"), ("Otra", ""))
            activo = re.split(
                r"(?:\bCompany\s*:|\bDescription\s*:|\bRate/?Coupon\s*:|"
                r"\bLocation\s*:|\bComments?\s*:|\bMaturity\s*Date\s*:)",
                g("activo"), maxsplit=1)[0]
            ticker = g("ticker")
            if ticker in ("--", "-", "N/A"):
                ticker = ""
            # a veces el nombre repite el ticker entre paréntesis
            if ticker:
                activo = re.sub(r"\s*\(\s*" + re.escape(ticker) + r"\s*\)", "", activo)

            n = re.search(r"([\d,]+(?:\.\d+)?)\s+shares?\b", g("comentario"), re.I)

            ops.append({
                "activo":      re.sub(r"\s+", " ", activo).strip(" .,;:-") or "(sin identificar)",
                "ticker":      ticker,
                "clase":       _traducir(TIPO_ACTIVO, g("clase")),
                "tipo":        etiqueta,
                "direccion":   direccion,
                "dueno":       _traducir(DUENOS, g("dueno"), "Titular"),
                "fecha_op":    FECHA.search(f_op).group(0),
                "fecha_aviso": "",          # el Senado no lo declara por fila
                "monto":       re.sub(r"\s+", " ", g("monto")),
                "acciones":    n.group(1) if n else "",
            })
    return ops


# ------------------------------------------------------------------ fusión

def a_fecha(s):
    for f in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, f).date()
        except (ValueError, TypeError):
            pass
    return None


def fusionar(senado):
    """Mete las presentaciones del Senado en el trades.json de la Cámara."""
    try:
        with open(SALIDA, encoding="utf-8") as f:
            paquete = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        paquete = {"datos": [], "ventana_dias": DIAS_HISTORIA, "pendientes": 0}

    # lo que ya estaba y no es del Senado queda etiquetado como Cámara
    base = []
    for reg in paquete.get("datos", []):
        if reg.get("camara") == "Senado":
            continue
        reg.setdefault("camara", "Cámara")
        base.append(reg)

    todo = base + senado
    todo.sort(key=lambda r: (a_fecha(r.get("fecha_aviso", "")) or dt.date.min,
                             r.get("doc_id", "")), reverse=True)

    paquete["datos"] = todo
    paquete["presentaciones"] = len(todo)
    paquete["operaciones"] = sum(len(r.get("operaciones") or []) for r in todo)
    paquete["senado"] = len(senado)
    paquete["actualizado"] = dt.datetime.now(dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(paquete, f, ensure_ascii=False, separators=(",", ":"))
    return paquete


# ------------------------------------------------------------------ main

def main():
    hoy = dt.date.today()
    corte = hoy - dt.timedelta(days=DIAS_HISTORIA)

    print(f"Abriendo sesión en {BASE}…")
    token = abrir_sesion()

    entradas, inicio = [], 0
    while True:
        filas, total = buscar(token, corte, inicio)
        if not filas:
            break
        for f in filas:
            e = entrada(f)
            if e and (d := a_fecha(e["fecha_aviso"])) and d >= corte:
                entradas.append(e)
        inicio += len(filas)
        print(f"  {inicio} de {total} resultados")
        if inicio >= total:
            break
        time.sleep(PAUSA)

    # sin duplicados, del más nuevo al más viejo
    vistos, unicas = set(), []
    for e in entradas:
        if e["doc_id"] not in vistos:
            vistos.add(e["doc_id"])
            unicas.append(e)
    unicas.sort(key=lambda e: a_fecha(e["fecha_aviso"]) or dt.date.min, reverse=True)
    print(f"{len(unicas)} PTR del Senado dentro de los últimos {DIAS_HISTORIA} días")

    os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
    try:
        with open(ESTADO, encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    pendientes = [e for e in unicas
                  if cache.get(e["doc_id"], {}).get("v") != PARSER_V]
    print(f"{len(pendientes)} sin procesar; se toman hasta {MAX_INFORMES}")

    for i, e in enumerate(pendientes[:MAX_INFORMES], 1):
        print(f"  [{i}/{min(len(pendientes), MAX_INFORMES)}] {e['nombre']}")
        ops = []
        if e["papel"]:
            print("    (presentación en papel: queda con el enlace al original)")
        else:
            try:
                r = sesion.get(e["pdf"], headers={"Referer": f"{BASE}/search/"},
                               timeout=60)
                if r.status_code == 200:
                    ops = leer_informe(r.text)
                    if not ops:
                        print("    ! sin operaciones legibles")
                else:
                    print(f"    ! HTTP {r.status_code}")
            except Exception as ex:
                print(f"    ! error de red: {ex}")
        cache[e["doc_id"]] = {**e, "operaciones": ops, "v": PARSER_V}
        time.sleep(PAUSA)

    salida = [reg for reg in cache.values()
              if (d := a_fecha(reg.get("fecha_aviso", ""))) and d >= corte]

    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))

    paquete = fusionar(salida)
    ops_sen = sum(len(r.get("operaciones") or []) for r in salida)
    print(f"\nListo: {len(salida)} presentaciones del Senado, {ops_sen} operaciones.")
    print(f"Total en el panel: {paquete['presentaciones']} presentaciones, "
          f"{paquete['operaciones']} operaciones.")


if __name__ == "__main__":
    sys.exit(main())
