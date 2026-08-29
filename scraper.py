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

UA = "congreso-wire/1.0 (proyecto personal de seguimiento de divulgaciones)"
BASE = "https://disclosures-clerk.house.gov/public_disc"

DIAS_HISTORIA   = 180   # cuánto historial conservar en el JSON
MAX_PDFS_CORRIDA = 120  # tope por ejecución, para no eternizar el job
PAUSA           = 0.6   # segundos entre descargas de PDF

RAIZ   = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(RAIZ, "docs", "trades.json")
ESTADO = os.path.join(RAIZ, "state", "seen.json")

sesion = requests.Session()
sesion.headers.update({"User-Agent": UA})

# ------------------------------------------------------------------ parseo

# Códigos de tipo de activo que la Cámara imprime entre corchetes: [ST], [OP]...
TIPO_ACTIVO = {
    "ST": "Acción", "OP": "Opción", "CS": "Acción de sociedad cerrada",
    "MF": "Fondo mutuo", "ET": "ETF", "EF": "ETF", "GS": "Bono del Estado",
    "CT": "Bono corporativo", "RP": "Propiedad", "OL": "Petróleo y gas",
    "PS": "Sociedad", "HN": "Vivienda", "BA": "Cuenta bancaria",
    "IH": "Fondo de cobertura", "AB": "Cuenta de corretaje",
    "CO": "Materia prima", "FN": "Préstamo", "OI": "Otro ingreso",
}

TIPO_OP = {
    "P": ("Compra", "buy"),
    "S": ("Venta", "sell"),
    "E": ("Canje", ""),
}

MONTO = r"\$[\d,]+(?:\s*-\s*\$[\d,]+)?"

FILA = re.compile(
    r"(?P<asset>[^\[\]]{3,150}?)"                    # nombre del activo
    r"(?:\((?P<ticker>[A-Z][A-Z0-9.\-]{0,6})\)\s*)?" # ticker opcional
    r"\[(?P<atype>[A-Z]{2})\]\s+"                    # tipo de activo
    r"(?P<tx>[PSE])\s*(?P<partial>\(partial\))?\s+"  # compra / venta / canje
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+"               # fecha de la operación
    r"(?P<ndate>\d{2}/\d{2}/\d{4})\s+"               # fecha de aviso
    r"(?P<amount>" + MONTO + r")"
)

DUENOS = {"SP": "Cónyuge", "DC": "Hijo a cargo", "JT": "Conjunta"}

RUIDO = re.compile(
    r"(Clerk of the House|Legislative Resource Center|Cannon Building|"
    r"asset type abbreviations|I CERTIFY|Digitally Signed|Page \d+|"
    r"Notification\s+Date|Cap\.?\s*Gains|^ID\s+Owner)", re.I
)


def limpiar_activo(texto):
    """
    El bloque previo a cada marcador arrastra el final de la fila anterior.
    Devuelve (dueño, nombre del activo) ya separados y limpios.
    """
    t = re.sub(r"\s+", " ", texto).strip()
    t = RUIDO.sub(" ", t)

    # cortar todo lo que quede del encabezado o de la fila anterior:
    # nos quedamos con lo que hay después del ÚLTIMO marcador de columna
    t = re.sub(r"^.*(?:\$200\?|Amount|Notification|\d{2}/\d{2}/\d{4})", "", t)
    # la columna "Cap. Gains" deja una N o una Y sueltas
    t = re.sub(r"^\s*[NY]\b\s*", " ", t)

    dueno = "Titular"
    m = re.match(r"\s*(SP|DC|JT)\b\s*", t)
    if m:
        dueno = DUENOS[m.group(1)]
        t = t[m.end():]

    t = t.strip(" .,;:|-–—")
    return dueno, (t[:120] if t else "(sin identificar)")


def leer_pdf(contenido):
    """Devuelve la lista de operaciones que contiene un PTR."""
    try:
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            crudo = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        print(f"    ! no se pudo abrir el PDF: {e}")
        return []

    if not crudo.strip():
        return []                     # escaneado a mano: no hay texto que leer

    plano = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", crudo)
    plano = re.sub(r"\s+", " ", plano)

    ops = []
    for m in FILA.finditer(plano):
        dueno, activo = limpiar_activo(m.group("asset"))
        etiqueta, direccion = TIPO_OP.get(m.group("tx"), ("Otra", ""))
        ops.append({
            "activo":   activo,
            "ticker":   m.group("ticker") or "",
            "clase":    TIPO_ACTIVO.get(m.group("atype"), m.group("atype")),
            "tipo":     etiqueta + (" parcial" if m.group("partial") else ""),
            "direccion": direccion,
            "dueno":    dueno,
            "fecha_op": m.group("tdate"),
            "fecha_aviso": m.group("ndate"),
            "monto":    re.sub(r"\s+", " ", m.group("amount")),
        })
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
.lag-fill.late{background:var(--flag)}
.lag-fill.verylate{background:var(--stamp)}
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
    <div class="stat"><div class="stat-n" id="s3">—</div><div class="stat-l">Retraso medio</div></div>
  </div>
</header>

<div class="tools">
  <input type="search" id="q" placeholder="Buscar por nombre o ticker…"
         autocomplete="off" autocorrect="off" spellcheck="false">
  <div class="chips" id="chips">
    <button class="chip" data-f="all" aria-pressed="true">Todo</button>
    <button class="chip" data-f="buy" aria-pressed="false">Compras</button>
    <button class="chip" data-f="sell" aria-pressed="false">Ventas</button>
    <button class="chip" data-f="big" aria-pressed="false">Más de 100k</button>
    <button class="chip" data-f="late" aria-pressed="false">Fuera de plazo</button>
  </div>
</div>

<div id="out"><div class="state">Cargando divulgaciones…</div></div>
<button id="more" hidden>Ver más</button>

<footer>
  <b>De dónde sale:</b> el archivo oficial del Clerk de la Cámara. Un proceso
  automático baja el índice anual, lee cada PTR en PDF y publica el resultado acá.<br><br>
  <b>La barra de retraso</b> mide los días entre la operación y su declaración.
  La ley da 30 días desde que el legislador se entera, con tope de 45. En ámbar,
  lo que pasó los 45; en rojo, lo que pasó los 90.<br><br>
  <b>Falta el Senado:</b> su sistema exige sesión con token, así que va aparte.
  Los montos son rangos, nunca cifras exactas: así los declaran.
</footer>

</div>

<script>
const PAGINA = 25;
let DATOS = [], FILTRO = 'all', BUSCA = '', TOPE = PAGINA;
const $ = s => document.querySelector(s);

const MES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
function fecha(s){
  const m = String(s||'').match(/(\d{2})\/(\d{2})\/(\d{4})/);
  return m ? new Date(+m[3], +m[2]-1, +m[1]) : null;
}
function bonita(s){
  const d = fecha(s);
  return d ? d.getDate()+' '+MES[d.getMonth()]+' '+String(d.getFullYear()).slice(2) : s;
}
function retraso(op){
  const a = fecha(op.fecha_op), b = fecha(op.fecha_aviso);
  return (a && b) ? Math.max(0, Math.round((b-a)/86400000)) : null;
}
function piso(monto){
  const m = String(monto||'').match(/\$([\d,]+)/);
  return m ? +m[1].replace(/,/g,'') : 0;
}

async function cargar(){
  try{
    const r = await fetch('./trades.json?v='+Date.now());
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    DATOS = j.datos || [];

    const todas = DATOS.flatMap(f => f.operaciones || []);
    const lags = todas.map(retraso).filter(x => x !== null);
    const medio = lags.length
      ? Math.round(lags.reduce((a,b) => a+b, 0)/lags.length) : 0;

    $('#s1').textContent = (j.presentaciones ?? DATOS.length).toLocaleString('es');
    $('#s2').textContent = (j.operaciones ?? todas.length).toLocaleString('es');
    $('#s3').textContent = medio + ' d';
    $('.sub').textContent = 'Cámara · actualizado ' +
      new Date(j.actualizado).toLocaleString('es', {day:'numeric', month:'short',
        hour:'2-digit', minute:'2-digit'});
    pintar();
  }catch(e){
    $('#out').innerHTML = '<div class="err"><b>No se pudo leer trades.json.</b><br>'+
      e.message+'<br><br>Si el repo es nuevo, esperá a que el flujo de Actions '+
      'termine su primera corrida y publique el archivo.</div>';
  }
}

function pasa(f){
  const q = BUSCA.trim().toLowerCase();
  let ops = f.operaciones || [];

  if(q && !f.nombre.toLowerCase().includes(q)){
    ops = ops.filter(o => (o.ticker+' '+o.activo).toLowerCase().includes(q));
    if(!ops.length) return null;
  }
  if(FILTRO === 'buy')  ops = ops.filter(o => o.direccion === 'buy');
  if(FILTRO === 'sell') ops = ops.filter(o => o.direccion === 'sell');
  if(FILTRO === 'big')  ops = ops.filter(o => piso(o.monto) >= 100000);
  if(FILTRO === 'late') ops = ops.filter(o => (retraso(o) ?? 0) > 45);

  return ops.length ? {...f, operaciones: ops} : null;
}

function pintar(){
  const vis = DATOS.map(pasa).filter(Boolean);

  if(!vis.length){
    $('#out').innerHTML = '<div class="state">Nada coincide con esta búsqueda.</div>';
    $('#more').hidden = true;
    return;
  }

  const frag = document.createDocumentFragment();
  vis.slice(0, TOPE).forEach(f => {
    const el = document.createElement('div');
    el.className = 'filing';

    const ops = f.operaciones.map(o => {
      const d = retraso(o);
      const pct = d === null ? 0 : Math.min(100, d/120*100);
      const cls = d === null ? '' : (d > 90 ? 'verylate' : d > 45 ? 'late' : '');
      const dir = o.direccion ? ' dir-'+o.direccion : '';
      return '<div class="op">'+
        '<div class="op-name">'+
          (o.ticker ? '<span class="tk">'+o.ticker+'</span>' : '')+
          esc(o.activo)+'</div>'+
        '<div class="op-amt">'+esc(o.monto)+'</div>'+
        '<div class="op-tags"><span class="'+dir.trim()+'">'+esc(o.tipo)+'</span>'+
          ' · '+esc(o.clase)+' · '+esc(o.dueno)+' · op. '+bonita(o.fecha_op)+'</div>'+
        '<div class="lag"><div class="lag-track">'+
          '<div class="lag-fill '+cls+'" style="width:'+pct+'%"></div></div>'+
          '<div class="lag-n">'+(d === null ? 's/d' : d+' días')+'</div></div>'+
      '</div>';
    }).join('');

    el.innerHTML =
      '<div class="who">'+esc(f.nombre)+'</div>'+
      '<div class="when">'+esc(f.distrito || '')+' · declarado '+
        bonita(f.fecha_aviso)+' · '+f.operaciones.length+' op.</div>'+
      '<a class="pdf" href="'+f.pdf+'" target="_blank" rel="noopener">Ver el PTR original</a>'+
      ops;
    frag.appendChild(el);
  });

  $('#out').innerHTML = '';
  $('#out').appendChild(frag);
  $('#more').hidden = vis.length <= TOPE;
}

function esc(s){
  return String(s ?? '').replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

let t;
$('#q').addEventListener('input', e => {
  clearTimeout(t);
  t = setTimeout(() => { BUSCA = e.target.value; TOPE = PAGINA; pintar(); }, 180);
});
$('#chips').addEventListener('click', e => {
  const b = e.target.closest('.chip'); if(!b) return;
  document.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', c === b));
  FILTRO = b.dataset.f; TOPE = PAGINA; pintar();
});
$('#more').addEventListener('click', () => { TOPE += PAGINA; pintar(); });

cargar();
</script>
</body>
</html>
"""


def escribir_panel():
    """Deja docs/index.html si todavia no existe. Si lo editaste, no lo pisa."""
    destino = os.path.join(RAIZ, "docs", "index.html")
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    if os.path.exists(destino):
        return
    with open(destino, "w", encoding="utf-8") as f:
        f.write(PANEL)
    print("Panel creado en docs/index.html")

# ------------------------------------------------------------------ main

def main():
    escribir_panel()

    hoy   = dt.date.today()
    corte = hoy - dt.timedelta(days=DIAS_HISTORIA)

    anios = [hoy.year]
    if hoy.month <= 2:
        anios.append(hoy.year - 1)      # enero y febrero pisan el año anterior

    print("Leyendo el índice del Clerk…")
    entradas = []
    for a in anios:
        entradas += indice_anual(a)

    entradas = [e for e in entradas
                if (d := a_fecha(e["fecha_aviso"])) and d >= corte]
    entradas.sort(key=lambda e: a_fecha(e["fecha_aviso"]), reverse=True)
    print(f"{len(entradas)} PTR dentro de los últimos {DIAS_HISTORIA} días")

    # lo que ya procesamos en corridas anteriores
    os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
    try:
        with open(ESTADO, encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    nuevos = [e for e in entradas if e["doc_id"] not in cache]
    print(f"{len(nuevos)} sin procesar; se toman hasta {MAX_PDFS_CORRIDA}")

    for i, e in enumerate(nuevos[:MAX_PDFS_CORRIDA], 1):
        url = f"{BASE}/ptr-pdfs/{e['anio']}/{e['doc_id']}.pdf"
        print(f"  [{i}/{min(len(nuevos), MAX_PDFS_CORRIDA)}] {e['nombre']}")
        try:
            r = sesion.get(url, timeout=60)
            ops = leer_pdf(r.content) if r.status_code == 200 else []
            if r.status_code != 200:
                print(f"    ! HTTP {r.status_code}")
        except Exception as ex:
            print(f"    ! error de red: {ex}")
            ops = []
        cache[e["doc_id"]] = {**e, "pdf": url, "operaciones": ops}
        time.sleep(PAUSA)

    # armamos la salida solo con lo que sigue dentro de la ventana
    salida = []
    for doc, reg in cache.items():
        d = a_fecha(reg.get("fecha_aviso", ""))
        if d and d >= corte:
            salida.append(reg)
    salida.sort(key=lambda r: (a_fecha(r["fecha_aviso"]), r["doc_id"]),
                reverse=True)

    total_ops = sum(len(r["operaciones"]) for r in salida)
    paquete = {
        "actualizado": dt.datetime.now(dt.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ventana_dias": DIAS_HISTORIA,
        "presentaciones": len(salida),
        "operaciones": total_ops,
        "pendientes": max(0, len(nuevos) - MAX_PDFS_CORRIDA),
        "datos": salida,
    }

    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(paquete, f, ensure_ascii=False, separators=(",", ":"))
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nListo: {len(salida)} presentaciones, {total_ops} operaciones.")
    if paquete["pendientes"]:
        print(f"Quedan {paquete['pendientes']} para la próxima corrida.")


if __name__ == "__main__":
    sys.exit(main())
