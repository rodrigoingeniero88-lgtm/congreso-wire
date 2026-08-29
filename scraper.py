#!/usr/bin/env python3
"""
Lee los Periodic Transaction Reports (PTR) de la Cámara de Representantes
desde la fuente oficial del Clerk y los deja como JSON estructurado.

Flujo:
  1. Baja <AÑO>FD.zip  -> adentro hay <AÑO>FD.xml con el índice del año
  2. Filtra FilingType == 'P' (PTR)
  3. Por cada DocID nuevo, baja el PDF y le extrae las operaciones
  4. Escribe docs/trades.json y state/seen.json

No requiere claves ni login. Datos de dominio público.
"""

import io
import json
import os
import re
import sys
import time
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET

import requests
import pdfplumber

# ------------------------------------------------------------------ ajustes

# La SEC exige identificarse con un correo de contacto para servir archivos.
# Sin esto, www.sec.gov devuelve 403. Poné tu propio correo acá.
CONTACTO = "rodrigo.ingeniero88@gmail.com"
UA = f"congreso-wire/1.0 ({CONTACTO})"
BASE = "https://disclosures-clerk.house.gov/public_disc"

DIAS_HISTORIA   = 180   # cuánto historial conservar en el JSON
MAX_PDFS_CORRIDA = 400  # tope por ejecución, para no eternizar el job
PAUSA           = 0.6   # segundos entre descargas de PDF
PARSER_V        = 5     # subilo si cambia el parseo: fuerza releer los PDF

RAIZ   = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(RAIZ, "docs", "trades.json")
ESTADO = os.path.join(RAIZ, "state", "seen.json")

sesion = requests.Session()
sesion.headers.update({"User-Agent": UA})

# ------------------------------------------------------------------ parseo

# Códigos de tipo de activo que la Cámara imprime entre corchetes: [ST], [OP]...
# Solo se traducen los confirmados; el resto se muestra tal cual viene.
TIPO_ACTIVO = {
    "ST": "Acción",
    "OP": "Opción",
    "MF": "Fondo mutuo",
    "ET": "ETF",
    "GS": "Bono del Estado",
    "CS": "Bono corporativo",
    "RP": "Inmueble",
    "BA": "Cuenta bancaria",
    "AB": "Cuenta de corretaje",
    "OT": "Otro",
}

TIPO_OP = {
    "P": ("Compra", "buy"),
    "S": ("Venta", "sell"),
    "E": ("Canje", ""),
}

DUENOS = {"SP": "Cónyuge", "DC": "Hijo a cargo", "JT": "Conjunta"}

META = re.compile(
    r"^\s*(?:F\s*S\s*:|S\s*O\s*:|L\s*:|D\s*:|C\s*:|"
    r"FILING\s+STATUS|SUBHOLDING|LOCATION|DESCRIPTION|COMMENTS?)", re.I
)

ANCLAS = ("Owner", "Asset", "Transaction", "Date", "Notification", "Amount", "Cap.")
FECHA = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
ACCIONES = re.compile(r"([\d,]+(?:\.\d+)?)\s+shares?\b", re.I)

# Todo lo que NO puede formar parte del nombre de un activo: el pie de página,
# la certificación final y el texto libre del campo DESCRIPTION.
BASURA = re.compile(
    r"(CERTIFY|please\s+visit|fd\.house\.gov|http|asset\s+type|Digitally\s+Signed|"
    r"INITIAL\s+PUBLIC|Clerk\s+of\s+the|Legislative\s+Resource|Cannon\s+Building|"
    r"Purchased|Sold|Additional\s+investment|strike\s+price|expiration\s+date|"
    r"FILING\s+STATUS|SUBHOLDING|DESCRIPTION|LOCATION|COMMENT)", re.I)

# Un monto continuado siempre arranca con $ o con el guión del rango
FRAGMENTO_MONTO = re.compile(r"^\s*[-–—]?\s*\$[\d,]")


def _renglones(pagina, tol=2.5):
    """Agrupa las palabras de la página en renglones visuales."""
    palabras = sorted(pagina.extract_words(keep_blank_chars=False),
                      key=lambda w: (w["top"], w["x0"]))
    filas, actual, tope = [], [], None
    for w in palabras:
        if tope is None or abs(w["top"] - tope) <= tol:
            actual.append(w)
            tope = w["top"] if tope is None else tope
        else:
            filas.append(sorted(actual, key=lambda x: x["x0"]))
            actual, tope = [w], w["top"]
    if actual:
        filas.append(sorted(actual, key=lambda x: x["x0"]))
    return filas


def _bordes(renglones):
    """Deduce el borde izquierdo de cada columna leyendo el encabezado."""
    for ws in renglones:
        textos = [w["text"].rstrip(":") for w in ws]
        if "Owner" in textos and "Asset" in textos and "Transaction" in textos:
            bordes = {}
            for w in ws:
                t = w["text"].rstrip(":")
                if t in ANCLAS and t not in bordes:
                    bordes[t] = w["x0"]
            if {"Owner", "Asset", "Transaction", "Amount"} <= set(bordes):
                return bordes
    return None


def _por_columna(ws, bordes):
    """Reparte las palabras de un renglón en sus columnas."""
    orden = sorted(bordes.items(), key=lambda kv: kv[1])
    celdas = {k: [] for k, _ in orden}
    for w in ws:
        elegida = None
        for nombre, x in orden:
            if w["x0"] >= x - 4:
                elegida = nombre
            else:
                break
        if elegida:
            celdas[elegida].append(w["text"])
    return {k: " ".join(v).strip() for k, v in celdas.items()}


def leer_pdf(contenido):
    """Devuelve la lista de operaciones que contiene un PTR."""
    try:
        pdf = pdfplumber.open(io.BytesIO(contenido))
    except Exception as e:
        print(f"    ! no se pudo abrir el PDF: {e}")
        return []

    ops, abierta = [], None

    def cerrar():
        if not abierta:
            return
        activo = re.sub(r"\s+", " ", abierta["activo"]).strip()
        # Las etiquetas de metadatos a veces caen en la misma celda que el
        # nombre ("... Ordinary Shares F S: New"). Se corta en la primera.
        activo = re.split(r"(?:\bF\s*S\s*:|\bS\s*O\s*:|\s[LDC]\s*:|"
                          r"FILING\s+STATUS|SUBHOLDING|DESCRIPTION)",
                          activo, maxsplit=1)[0]
        tk = re.search(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", activo)
        cl = re.search(r"\[([A-Z]{2})\]", activo)
        activo = re.sub(r"\s*\[[A-Z]{2}\]\s*", " ", activo)
        if tk:
            activo = activo.replace(tk.group(0), " ")
        etiqueta, direccion = TIPO_OP.get(abierta["tx"], ("Otra", ""))
        ops.append({
            "activo": re.sub(r"\s+", " ", activo).strip(" .,;:-") or "(sin identificar)",
            "ticker": tk.group(1) if tk else "",
            "clase": TIPO_ACTIVO.get(cl.group(1), cl.group(1)) if cl else "",
            "tipo": etiqueta + (" parcial" if abierta["parcial"] else ""),
            "direccion": direccion,
            "dueno": DUENOS.get(abierta["dueno"], "Titular"),
            "fecha_op": abierta["fecha_op"],
            "fecha_aviso": abierta["fecha_aviso"],
            "monto": re.sub(r"\s+", " ", abierta["monto"]).strip(),
            "acciones": abierta["acciones"],
        })

    try:
        with pdf:
            for pagina in pdf.pages:
                renglones = _renglones(pagina)
                bordes = _bordes(renglones)
                if not bordes:
                    continue                      # página sin tabla (portada, firma)

                for ws in renglones:
                    c = _por_columna(ws, bordes)
                    plano = " ".join(w["text"] for w in ws)

                    if "Asset" in plano and "Transaction" in plano:
                        continue                  # el encabezado

                    fechas = [t for t in c.get("Date", "").split() if FECHA.match(t)]
                    aviso = [t for t in c.get("Notification", "").split() if FECHA.match(t)]
                    tipo = re.match(r"^([PSE])\b", c.get("Transaction", "").strip())

                    if fechas and tipo:
                        cerrar()                  # empieza una fila nueva
                        abierta = {
                            "activo": c.get("Asset", ""),
                            "tx": tipo.group(1),
                            "parcial": "partial" in c.get("Transaction", "").lower(),
                            "dueno": (c.get("Owner", "").strip() or "")[:2],
                            "fecha_op": fechas[0],
                            "fecha_aviso": aviso[0] if aviso else fechas[-1],
                            "monto": c.get("Amount", ""),
                            "acciones": "",
                            "cont": 0,
                        }
                        continue

                    if abierta is None:
                        continue

                    extra = c.get("Asset", "")
                    n = ACCIONES.search(plano)
                    if n and not abierta["acciones"]:
                        abierta["acciones"] = n.group(1)

                    # El nombre solo se completa con los renglones inmediatos
                    # siguientes, y nunca con metadatos ni texto legal.
                    if (extra and abierta["cont"] < 2
                            and not META.match(extra) and not BASURA.search(plano)):
                        abierta["activo"] += " " + extra
                        abierta["cont"] += 1

                    # El monto solo se completa si quedó cortado: termina en
                    # guión o le falta el segundo extremo del rango.
                    monto_extra = c.get("Amount", "")
                    incompleto = (abierta["monto"].rstrip().endswith(("-", "–", "—"))
                                  or abierta["monto"].count("$") < 2)
                    if incompleto and FRAGMENTO_MONTO.match(monto_extra):
                        abierta["monto"] += " " + monto_extra.split()[0]

                cerrar()
                abierta = None
    except Exception as e:
        print(f"    ! error leyendo la tabla: {e}")

    return ops


# ------------------------------------------------------------------ índice

def indice_anual(anio):
    """Baja el ZIP del año y devuelve las entradas de tipo PTR."""
    url = f"{BASE}/financial-pdfs/{anio}FD.zip"
    print(f"  bajando {url}")
    r = sesion.get(url, timeout=90)
    if r.status_code != 200:
        print(f"  ! {anio}: HTTP {r.status_code}, se omite")
        return []

    z = zipfile.ZipFile(io.BytesIO(r.content))
    nombre = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
    if not nombre:
        print(f"  ! {anio}: el ZIP no trae XML")
        return []

    raiz = ET.fromstring(z.read(nombre))
    filas = []
    for m in raiz.iter("Member"):
        g = lambda t: (m.findtext(t) or "").strip()
        if g("FilingType") != "P":
            continue
        doc = g("DocID")
        if not doc:
            continue
        filas.append({
            "doc_id":      doc,
            "anio":        anio,
            "nombre":      " ".join(x for x in [g("Prefix"), g("First"),
                                                g("Last"), g("Suffix")] if x),
            "distrito":    g("StateDst"),
            "fecha_aviso": g("FilingDate"),
        })
    print(f"  {anio}: {len(filas)} PTR en el índice")
    return filas


def a_fecha(s):
    for f in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, f).date()
        except (ValueError, TypeError):
            pass
    return None



# ------------------------------------------------- panel (se escribe solo)

PANEL = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#DDE3E6">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Congreso Wire — operaciones declaradas</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --ground:#DDE3E6; --paper:#EFF2F3; --ink:#17222F; --ink-soft:#5C6A77;
  --rule:#BEC8CF; --stamp:#A8322A; --seal:#0F6357; --flag:#B4770E;
  --mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --sans:'IBM Plex Sans',system-ui,sans-serif;
  --disp:'Archivo',system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:640px;margin:0 auto;padding:0 14px 48px}

header{padding:22px 0 12px}
.mast{font-family:var(--disp);font-weight:800;font-size:30px;line-height:.95;
  letter-spacing:-.035em;text-transform:uppercase;margin:0}
.mast span{color:var(--ink-soft)}
.sub{font-family:var(--mono);font-size:11px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-soft);margin-top:7px}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5px;
  background:var(--ink);border:1.5px solid var(--ink);margin-top:16px}
.stat{background:var(--paper);padding:10px 11px}
.stat-n{font-family:var(--disp);font-weight:800;font-size:21px;
  letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.stat-l{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-soft);margin-top:1px}

.tools{margin:14px 0 4px}
input[type=search]{
  width:100%;font-family:var(--mono);font-size:14px;padding:11px 12px;
  background:var(--paper);border:1.5px solid var(--ink);border-radius:0;color:var(--ink);
  -webkit-appearance:none}
input[type=search]::placeholder{color:var(--ink-soft)}
.chips{display:flex;gap:6px;overflow-x:auto;padding:10px 0 8px;
  -webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.cruce{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;
  color:#8a2b12;border-left:2px solid #8a2b12;padding:2px 0 2px 7px;
  margin-top:5px}
.cam{display:inline-block;font-family:var(--mono);font-size:9.5px;font-weight:700;
  letter-spacing:.08em;padding:1px 5px;border-radius:3px;vertical-align:1px;
  border:1px solid currentColor}
.cam.sen{color:#7a4a12}
.cam.rep{color:#1a4d7a}
.vacia{font-size:12.5px;opacity:.6;padding:8px 0 2px}
.chip{font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;white-space:nowrap;background:transparent;
  color:var(--ink-soft);border:1px solid var(--rule);padding:6px 11px;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--paper);border-color:var(--ink)}
:focus-visible{outline:2.5px solid var(--flag);outline-offset:2px}

.filing{border-top:1.5px solid var(--ink);padding:14px 0 4px}
.who{font-family:var(--disp);font-weight:700;font-size:18px;letter-spacing:-.015em}
.when{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-soft);margin-top:2px}

.op{padding:11px 0;border-bottom:1px solid var(--rule);display:grid;
  grid-template-columns:1fr auto;gap:4px 10px;align-items:start}
.op:last-child{border-bottom:none}
.op-name{font-size:14px;font-weight:500;line-height:1.3}
.tk{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.05em;
  padding:1px 5px;border:1px solid var(--ink);margin-right:5px;vertical-align:1px}
.op-amt{font-family:var(--mono);font-size:12px;font-weight:600;
  text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.op-tags{grid-column:1/-1;font-family:var(--mono);font-size:10px;
  letter-spacing:.07em;text-transform:uppercase;color:var(--ink-soft)}
.dir-buy{color:var(--seal)} .dir-sell{color:var(--stamp)}

/* firma: barra de retraso entre la operación y su declaración */
.lag{grid-column:1/-1;display:flex;align-items:center;gap:8px;margin-top:5px}
.lag-track{flex:1;height:3px;background:var(--rule);position:relative}
.lag-fill{position:absolute;left:0;top:0;bottom:0;background:var(--ink-soft)}
.lag-fill.fresca{background:var(--seal)}
.lag-fill.media{background:var(--ink-soft)}
.lag-fill.vieja{background:#9AA7B0}
.lag-n{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-soft);white-space:nowrap}

.pdf{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:var(--ink);
  text-decoration:none;border-bottom:1.5px solid var(--ink);margin:9px 0 4px}
.state{font-family:var(--mono);font-size:12.5px;color:var(--ink-soft);
  padding:30px 0;text-align:center;line-height:1.8}
.err{border:1.5px solid var(--stamp);background:var(--paper);padding:13px;
  font-family:var(--mono);font-size:12px;color:var(--stamp);line-height:1.6;margin-top:12px}
#more{width:100%;margin-top:16px;font-family:var(--mono);font-size:12px;
  font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  background:transparent;color:var(--ink);border:1.5px solid var(--ink);
  padding:11px;cursor:pointer}
footer{margin-top:30px;padding-top:16px;border-top:1.5px solid var(--ink);
  font-family:var(--mono);font-size:11px;color:var(--ink-soft);line-height:1.7}
footer b{color:var(--ink)}

/* pestañas */
.tabs{display:flex;border:1.5px solid var(--ink);margin-top:16px}
.tab{flex:1;font-family:var(--mono);font-size:12px;font-weight:600;
  letter-spacing:.08em;text-transform:uppercase;background:transparent;
  color:var(--ink);border:0;padding:11px 8px;cursor:pointer}
.tab+.tab{border-left:1.5px solid var(--ink)}
.tab[aria-pressed="true"]{background:var(--ink);color:var(--paper)}

/* bloques de análisis */
.blk{margin-top:26px}
.blk-h{font-family:var(--disp);font-weight:700;font-size:17px;
  letter-spacing:-.015em;padding-bottom:6px;border-bottom:1.5px solid var(--ink)}
.blk-s{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;
  color:var(--ink-soft);margin-top:5px;line-height:1.5}

.rank{display:grid;grid-template-columns:auto 1fr auto;gap:3px 9px;
  align-items:baseline;padding:10px 0;border-bottom:1px solid var(--rule)}
.rank-n{font-family:var(--mono);font-size:10px;color:var(--ink-soft);
  font-variant-numeric:tabular-nums}
.rank-t{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.04em}
.rank-v{font-family:var(--mono);font-size:12px;font-weight:600;text-align:right;
  white-space:nowrap;font-variant-numeric:tabular-nums}
.rank-d{grid-column:2/-1;font-size:12px;color:var(--ink-soft);line-height:1.3}
.rank-m{grid-column:2/-1;font-family:var(--mono);font-size:9.5px;
  letter-spacing:.06em;text-transform:uppercase;color:var(--ink-soft);margin-top:2px}

/* barra compra / venta */
.flow{grid-column:1/-1;display:flex;height:5px;margin-top:6px;
  background:var(--rule);overflow:hidden}
.flow-b{background:var(--seal)}
.flow-s{background:var(--stamp)}
.flow-l{grid-column:1/-1;display:flex;justify-content:space-between;
  font-family:var(--mono);font-size:9.5px;letter-spacing:.05em;
  color:var(--ink-soft);margin-top:3px}
.flow-l b{font-weight:600}
.flow-l .b{color:var(--seal)} .flow-l .s{color:var(--stamp)}
.nuevo{display:inline-block;font-family:var(--mono);font-size:9px;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;color:var(--flag);
  border:1px solid var(--flag);padding:1px 5px;margin-left:6px;vertical-align:2px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1 class="mast">Congreso <span>Wire</span></h1>
  <div class="sub">Cámara de Representantes · PTR del Clerk</div>
  <div class="stats">
    <div class="stat"><div class="stat-n" id="s1">—</div><div class="stat-l">Presentaciones</div></div>
    <div class="stat"><div class="stat-n" id="s2">—</div><div class="stat-l">Operaciones</div></div>
    <div class="stat"><div class="stat-n" id="s3">—</div><div class="stat-l">Más fresca</div></div>
  </div>
</header>

<div class="tabs">
  <button class="tab" id="tab-mov" aria-pressed="true">Movimientos</button>
  <button class="tab" id="tab-an" aria-pressed="false">Análisis</button>
</div>

<div class="tools">
  <input type="search" id="q" placeholder="Buscar por nombre o ticker…"
         autocomplete="off" autocorrect="off" spellcheck="false">
  <div class="chips" id="chips">
    <button class="chip" data-f="all" aria-pressed="true">Todo</button>
    <button class="chip" data-f="buy" aria-pressed="false">Compras</button>
    <button class="chip" data-f="sell" aria-pressed="false">Ventas</button>
    <button class="chip" data-f="big" aria-pressed="false">Más de 100k</button>
    <button class
