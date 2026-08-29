#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
congreso-wire · cruce con comités
=================================

Marca las operaciones donde el legislador opera en un sector que su propio
comité regula. Corre al final, después de scraper.py y senado.py: lee el
docs/trades.json ya armado, le agrega el campo "cruce" a las operaciones que
corresponda, y lo reescribe.

Un cruce no prueba nada. Dice que la operación merece una segunda mirada:
alguien del Comité de Energía comprando petroleras está en otra posición que
alguien de Veteranos haciendo lo mismo.

Fuentes (dominio público, proyecto unitedstates.io):
  · legislators-current.yaml          quién es quién, con distrito y bioguide
  · committees-current.yaml           los 49 comités y su identificador
  · committee-membership-current.yaml quién integra cada uno

Deliberadamente NO se cruzan fondos indexados, ETF ni notas estructuradas:
son canastas diversificadas, casi siempre manejadas por un asesor, y marcarlas
llenaría el panel de ruido.
"""

import json
import os
import re
import sys
import unicodedata

import requests
import yaml

# ------------------------------------------------------------------ ajustes

FUENTE = ("https://raw.githubusercontent.com/unitedstates/"
          "congress-legislators/main/")

RAIZ   = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(RAIZ, "docs", "trades.json")

# ------------------------------------------------- comités y sus sectores
# Solo los comités con jurisdicción sectorial clara. Los transversales
# (Presupuesto, Reglamento, Ética, Apropiaciones) quedan fuera a propósito:
# alcanzan a todo, así que marcar sus cruces no distingue nada.

SECTORES_COMITE = {
    # Cámara                              sectores                     nombre corto
    "HSAG": (["agro"],                        "Agricultura"),
    "HSAS": (["defensa"],                     "Fuerzas Armadas"),
    "HSBA": (["finanzas", "inmobiliario", "cripto"], "Servicios Financieros"),
    "HSFA": (["defensa"],                     "Relaciones Exteriores"),
    "HSIF": (["energia", "salud", "telecom"], "Energía y Comercio"),
    "HSII": (["energia", "materiales"],       "Recursos Naturales"),
    "HLIG": (["defensa"],                     "Inteligencia"),
    "HSPW": (["transporte", "industria"],     "Transporte e Infraestructura"),
    "HSSY": (["tecnologia", "defensa"],       "Ciencia, Espacio y Tecnología"),
    "HSWM": (["finanzas", "salud"],           "Medios y Arbitrios"),
    "HSZS": (["tecnologia", "defensa"],       "Competencia con China"),
    # Senado
    "SSAF": (["agro", "cripto"],              "Agricultura"),
    "SSAS": (["defensa"],                     "Fuerzas Armadas"),
    "SSBK": (["finanzas", "inmobiliario", "cripto"], "Banca y Vivienda"),
    "SSCM": (["transporte", "telecom", "tecnologia"], "Comercio, Ciencia y Transporte"),
    "SSEG": (["energia", "materiales"],       "Energía y Recursos Naturales"),
    "SSEV": (["energia", "industria"],        "Medio Ambiente y Obras Públicas"),
    "SSFI": (["finanzas", "salud"],           "Finanzas"),
    "SSFR": (["defensa"],                     "Relaciones Exteriores"),
    "SSHR": (["salud"],                       "Salud, Educación y Trabajo"),
    "SLIN": (["defensa"],                     "Inteligencia"),
}

NOMBRE_SECTOR = {
    "agro":         "agro y alimentos",
    "defensa":      "defensa",
    "energia":      "energía",
    "finanzas":     "finanzas",
    "cripto":       "cripto",
    "inmobiliario": "inmobiliario",
    "salud":        "salud y farma",
    "tecnologia":   "tecnología",
    "telecom":      "telecomunicaciones y medios",
    "transporte":   "transporte",
    "materiales":   "minería y materiales",
    "industria":    "industria",
}

# ------------------------------------------------------------ los tickers
# Los más frecuentes en las declaraciones del Congreso. No pretende ser
# exhaustivo: lo que no está acá cae en las palabras clave de más abajo.

TICKERS = {
    # tecnología
    "AAPL": "tecnologia", "MSFT": "tecnologia", "GOOGL": "tecnologia",
    "GOOG": "tecnologia", "AMZN": "tecnologia", "META": "tecnologia",
    "NVDA": "tecnologia", "AMD": "tecnologia", "INTC": "tecnologia",
    "CRM": "tecnologia", "ORCL": "tecnologia", "ADBE": "tecnologia",
    "CSCO": "tecnologia", "IBM": "tecnologia", "QCOM": "tecnologia",
    "TXN": "tecnologia", "AVGO": "tecnologia", "MU": "tecnologia",
    "AMAT": "tecnologia", "LRCX": "tecnologia", "KLAC": "tecnologia",
    "ASML": "tecnologia", "TSM": "tecnologia", "NOW": "tecnologia",
    "PANW": "tecnologia", "CRWD": "tecnologia", "SNOW": "tecnologia",
    "PLTR": "tecnologia", "UBER": "tecnologia", "ABNB": "tecnologia",
    "SHOP": "tecnologia", "SQ": "tecnologia", "PYPL": "tecnologia",
    "DELL": "tecnologia", "HPQ": "tecnologia", "MRVL": "tecnologia",
    "ARM": "tecnologia", "SMCI": "tecnologia", "ANET": "tecnologia",
    "APP": "tecnologia", "DDOG": "tecnologia", "MDB": "tecnologia",
    "ZS": "tecnologia", "FTNT": "tecnologia", "ADSK": "tecnologia",
    "INTU": "tecnologia", "WDAY": "tecnologia", "TEAM": "tecnologia",
    # defensa y aeroespacial
    "LMT": "defensa", "RTX": "defensa", "NOC": "defensa", "GD": "defensa",
    "BA": "defensa", "LHX": "defensa", "HII": "defensa", "TDG": "defensa",
    "LDOS": "defensa", "BAH": "defensa", "CACI": "defensa", "SAIC": "defensa",
    "AXON": "defensa", "KTOS": "defensa", "AVAV": "defensa", "RKLB": "defensa",
    "TXT": "defensa", "HWM": "defensa", "SPR": "defensa",
    # energía
    "XOM": "energia", "CVX": "energia", "COP": "energia", "SLB": "energia",
    "EOG": "energia", "PSX": "energia", "MPC": "energia", "VLO": "energia",
    "OXY": "energia", "HAL": "energia", "BKR": "energia", "DVN": "energia",
    "FANG": "energia", "HES": "energia", "KMI": "energia", "WMB": "energia",
    "OKE": "energia", "ET": "energia", "EPD": "energia", "TRGP": "energia",
    "NEE": "energia", "DUK": "energia", "SO": "energia", "D": "energia",
    "AEP": "energia", "EXC": "energia", "XEL": "energia", "SRE": "energia",
    "ED": "energia", "PCG": "energia", "VST": "energia", "CEG": "energia",
    "FSLR": "energia", "ENPH": "energia", "NRG": "energia", "TLN": "energia",
    # finanzas
    "JPM": "finanzas", "BAC": "finanzas", "WFC": "finanzas", "C": "finanzas",
    "GS": "finanzas", "MS": "finanzas", "SCHW": "finanzas", "BLK": "finanzas",
    "BX": "finanzas", "KKR": "finanzas", "APO": "finanzas", "ARES": "finanzas",
    "AXP": "finanzas", "V": "finanzas", "MA": "finanzas", "COF": "finanzas",
    "USB": "finanzas", "PNC": "finanzas", "TFC": "finanzas", "BK": "finanzas",
    "STT": "finanzas", "SPGI": "finanzas", "MCO": "finanzas", "ICE": "finanzas",
    "CME": "finanzas", "NDAQ": "finanzas", "CB": "finanzas", "AIG": "finanzas",
    "MET": "finanzas", "PRU": "finanzas", "ALL": "finanzas", "PGR": "finanzas",
    "TRV": "finanzas", "AFL": "finanzas", "BRK.B": "finanzas",
    "BRK.A": "finanzas", "HOOD": "finanzas", "SOFI": "finanzas",
    # cripto
    "COIN": "cripto", "MSTR": "cripto", "MARA": "cripto", "RIOT": "cripto",
    "CLSK": "cripto", "GBTC": "cripto", "IBIT": "cripto", "HUT": "cripto",
    "BITO": "cripto", "CIFR": "cripto",
    # salud y farma
    "JNJ": "salud", "PFE": "salud", "MRK": "salud", "ABBV": "salud",
    "LLY": "salud", "BMY": "salud", "AMGN": "salud", "GILD": "salud",
    "BIIB": "salud", "REGN": "salud", "VRTX": "salud", "MRNA": "salud",
    "UNH": "salud", "CVS": "salud", "CI": "salud", "ELV": "salud",
    "HUM": "salud", "HCA": "salud", "ABT": "salud", "TMO": "salud",
    "DHR": "salud", "SYK": "salud", "BSX": "salud", "MDT": "salud",
    "ISRG": "salud", "ZTS": "salud", "MCK": "salud", "IDXX": "salud",
    # telecom y medios
    "T": "telecom", "VZ": "telecom", "TMUS": "telecom", "CMCSA": "telecom",
    "CHTR": "telecom", "DIS": "telecom", "NFLX": "telecom", "WBD": "telecom",
    "PARA": "telecom", "FOX": "telecom", "FOXA": "telecom", "LYV": "telecom",
    "SPOT": "telecom", "TTWO": "telecom", "EA": "telecom", "RBLX": "telecom",
    # transporte
    "UNP": "transporte", "CSX": "transporte", "NSC": "transporte",
    "UPS": "transporte", "FDX": "transporte", "DAL": "transporte",
    "UAL": "transporte", "AAL": "transporte", "LUV": "transporte",
    "ALK": "transporte", "TSLA": "transporte", "GM": "transporte",
    "F": "transporte", "RIVN": "transporte", "LCID": "transporte",
    "ODFL": "transporte", "JBHT": "transporte", "CHRW": "transporte",
    # agro y alimentos
    "ADM": "agro", "BG": "agro", "CTVA": "agro", "MOS": "agro", "CF": "agro",
    "NTR": "agro", "DE": "agro", "TSN": "agro", "HRL": "agro", "K": "agro",
    "GIS": "agro", "KHC": "agro", "CAG": "agro", "CPB": "agro", "SJM": "agro",
    "KO": "agro", "PEP": "agro", "MDLZ": "agro", "STZ": "agro", "MNST": "agro",
    # inmobiliario
    "AMT": "inmobiliario", "PLD": "inmobiliario", "CCI": "inmobiliario",
    "EQIX": "inmobiliario", "SPG": "inmobiliario", "O": "inmobiliario",
    "PSA": "inmobiliario", "AVB": "inmobiliario", "EQR": "inmobiliario",
    "VICI": "inmobiliario", "WELL": "inmobiliario", "DLR": "inmobiliario",
    "DHI": "inmobiliario", "LEN": "inmobiliario", "PHM": "inmobiliario",
    "NVR": "inmobiliario", "Z": "inmobiliario",
    # minería y materiales
    "FCX": "materiales", "NEM": "materiales", "GOLD": "materiales",
    "AA": "materiales", "X": "materiales", "NUE": "materiales",
    "CLF": "materiales", "STLD": "materiales", "MP": "materiales",
    "ALB": "materiales", "LIN": "materiales", "APD": "materiales",
    "SHW": "materiales", "DOW": "materiales", "DD": "materiales",
    "LYB": "materiales", "VMC": "materiales", "MLM": "materiales",
    # industria
    "CAT": "industria", "HON": "industria", "GE": "industria",
    "MMM": "industria", "EMR": "industria", "ETN": "industria",
    "ITW": "industria", "PH": "industria", "CMI": "industria",
    "ROK": "industria", "JCI": "industria", "CARR": "industria",
    "TT": "industria", "PWR": "industria", "URI": "industria",
    "WM": "industria", "RSG": "industria", "FAST": "industria",
}

# palabras en el nombre del activo, para lo que no está en la lista
CLAVES = [
    (r"\bbitcoin|\bethereum|crypto|blockchain|\bcoin\b|digital asset", "cripto"),
    (r"petroleum|\boil\b|\bgas\b|pipeline|drilling|refin|energy|"
     r"electric|utilit|solar|nuclear|power co", "energia"),
    (r"\bbank|bancorp|bancshares|financial|insurance|assurance|"
     r"capital corp|asset manage|securities|credit union|mortgage", "finanzas"),
    (r"pharma|biotech|therapeut|bioscien|health|medical|medicine|"
     r"hospital|diagnost|genom|vaccin|surgical", "salud"),
    (r"defense|defence|aerospace|aviation|munition|missile|"
     r"shipbuild|space system", "defensa"),
    (r"semiconduct|software|technolog|cyber|data system|"
     r"computing|electronics|internet|digital", "tecnologia"),
    (r"telecom|communicat|broadcast|wireless|cable|media|entertain|"
     r"studios|network", "telecom"),
    (r"airline|railroad|railway|freight|logistic|trucking|"
     r"transport|shipping|motors|automotive", "transporte"),
    (r"agricultur|farm|foods|grain|seed|fertiliz|livestock|"
     r"beverage|brands", "agro"),
    (r"realty|real estate|\breit\b|properties|residential|"
     r"homebuild|apartment", "inmobiliario"),
    (r"mining|minerals|metals|steel|copper|\bgold\b|lithium|"
     r"chemical|materials", "materiales"),
    (r"industrial|manufactur|machinery|engineering|construction",
     "industria"),
]

# Lo que NO se cruza: canastas diversificadas. Un ETF del S&P 500 toca todos
# los sectores, así que marcarlo como cruce no significaría nada.
DIVERSIFICADO = re.compile(
    r"\betf\b|index|\bfund\b|mutual|structured note|linked note|"
    r"portfolio|trust\b|\bsector\b|\bs&p\b|russell|nasdaq-100|"
    r"treasury|municipal|money market", re.I)


# Clases que no dicen nada del sector: canastas, deuda pública y cuentas.
# Un bono de una comisión de autopistas menciona "oil" y no es una petrolera.
CLASES_FUERA = {"Fondo mutuo", "ETF", "Bono del Estado", "Bono municipal",
                "Cuenta bancaria", "Cuenta de corretaje", "Inmueble"}


def sector_de(op):
    """Sector de una operación, o None si no se puede decir."""
    if op.get("clase") in CLASES_FUERA:
        return None
    activo = op.get("activo", "")
    if DIVERSIFICADO.search(activo):
        return None
    tk = (op.get("ticker") or "").upper()
    if tk in TICKERS:
        return TICKERS[tk]
    for patron, sector in CLAVES:
        if re.search(patron, activo, re.I):
            return sector
    return None


# ------------------------------------------------------------- legisladores

def _clave(nombre):
    """Nombre normalizado para comparar: sin tildes, cargos ni iniciales."""
    s = unicodedata.normalize("NFKD", nombre or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\b(hon|mr|mrs|ms|dr|rep|sen|senator|jr|sr|ii|iii|iv)\b\.?", " ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    partes = [p for p in s.split() if len(p) > 1]      # cae la inicial del medio
    return f"{partes[0]} {partes[-1]}" if len(partes) >= 2 else " ".join(partes)


def bajar(nombre):
    r = requests.get(FUENTE + nombre, timeout=90)
    r.raise_for_status()
    return yaml.safe_load(r.text)


def cargar_congreso():
    """Devuelve (por_distrito, por_nombre) -> lista de nombres de comité."""
    legisladores = bajar("legislators-current.yaml")
    comites      = bajar("committees-current.yaml")
    membresia    = bajar("committee-membership-current.yaml")

    # los nombres salen de SECTORES_COMITE, ya en castellano

    # bioguide -> [(sector, nombre del comité)]
    sectores = {}
    for tid, gente in membresia.items():
        if tid not in SECTORES_COMITE:
            continue
        for m in gente:
            bio = m.get("bioguide")
            if bio:
                sectores.setdefault(bio, []).append(SECTORES_COMITE[tid])

    por_distrito, por_nombre = {}, {}
    for l in legisladores:
        bio = l.get("id", {}).get("bioguide")
        if bio not in sectores:
            continue
        ultimo = l["terms"][-1]
        entrada = sectores[bio]
        if ultimo["type"] == "rep" and ultimo.get("district") is not None:
            por_distrito[f"{ultimo['state']}{int(ultimo['district']):02d}"] = entrada
        n = l["name"]
        por_nombre[_clave(f"{n['first']} {n['last']}")] = entrada
        if n.get("official_full"):
            por_nombre[_clave(n["official_full"])] = entrada

    print(f"  {len(sectores)} legisladores en comités con sector definido")
    return por_distrito, por_nombre


def comites_de(ficha, por_distrito, por_nombre):
    d = (ficha.get("distrito") or "").strip().upper()
    if d in por_distrito:
        return por_distrito[d]
    # los distritos "at large" se escriben AK00 o AK01 según la fuente
    if re.fullmatch(r"[A-Z]{2}0[01]", d):
        for alt in (d[:2] + "00", d[:2] + "01"):
            if alt in por_distrito:
                return por_distrito[alt]
    return por_nombre.get(_clave(ficha.get("nombre", "")), [])


# ------------------------------------------------------------------ main

def main():
    with open(SALIDA, encoding="utf-8") as f:
        paquete = json.load(f)

    print("Bajando comités y membresías…")
    por_distrito, por_nombre = cargar_congreso()

    cruces, con_sector, sin_ubicar = 0, 0, 0
    for ficha in paquete.get("datos", []):
        propios = comites_de(ficha, por_distrito, por_nombre)
        if not propios:
            sin_ubicar += 1
        for op in ficha.get("operaciones", []):
            op.pop("cruce", None)
            s = sector_de(op)
            if not s:
                continue
            con_sector += 1
            for sectores, nombre in propios:
                if s in sectores:
                    op["cruce"] = {"sector": NOMBRE_SECTOR.get(s, s),
                                   "comite": nombre}
                    cruces += 1
                    break

    paquete["cruces"] = cruces
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(paquete, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n{con_sector} operaciones con sector reconocible")
    print(f"{cruces} cruzan con un comité del propio legislador")
    if sin_ubicar:
        print(f"({sin_ubicar} presentaciones sin comité: bancas nuevas, "
              f"ex legisladores o comités sin jurisdicción sectorial)")


if __name__ == "__main__":
    sys.exit(main())
