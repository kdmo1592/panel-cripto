"""
Genera data.json para el panel semanal de BTC, SOL y HYPE.

Cada fuente se consulta por separado: si una falla, el resto sigue y el
error queda anotado en data.json (el panel lo muestra abajo).

Fuentes (todas gratuitas, sin clave):
- Binance (data-api.binance.vision): velas de BTC y SOL
- Hyperliquid (api.hyperliquid.xyz): velas de HYPE y funding de BTC, SOL y HYPE
- BGeometrics (bitcoin-data.com): coste de compradores recientes (STH),
  precio realizado y MVRV Z-Score. El plan gratuito da los datos con unos
  días de retraso, suficiente para un panel semanal.
- FRED (Reserva Federal de St. Louis): liquidez neta de la Fed y M2
- alternative.me: índice de miedo y codicia
- CoinGecko: dominancia de BTC
- DefiLlama: ingresos del protocolo Hyperliquid
"""
import csv
import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

UA = {"User-Agent": "panel-cripto/1.0 (+https://github.com)"}
HOY = datetime.now(timezone.utc)
ERRORES = []


# ---------------------------------------------------------------- utilidades
def get(url, **kw):
    for intento in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 429:
                time.sleep(15 * (intento + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if intento == 2:
                raise
            time.sleep(5)
    raise RuntimeError(f"Sin respuesta tras 3 intentos: {url}")


def post(url, body):
    for intento in range(3):
        try:
            r = requests.post(url, json=body, headers=UA, timeout=40)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if intento == 2:
                raise
            time.sleep(5)


def fuente(nombre):
    """Decorador: ejecuta la consulta y, si falla, anota el error y devuelve None."""
    def deco(fn):
        def envoltura(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001
                ERRORES.append(f"{nombre}: {type(e).__name__}: {str(e)[:200]}")
                print(f"[ERROR] {nombre}: {e}")
                return None
        return envoltura
    return deco


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def miles(x):
    """Número entero con punto de millar, formato español."""
    return f"{x:,.0f}".replace(",", ".")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


# ---------------------------------------------------------------- precios
@fuente("Binance velas")
def velas_binance(simbolo, intervalo, limite=1000):
    r = get("https://data-api.binance.vision/api/v3/klines",
            params={"symbol": simbolo, "interval": intervalo, "limit": limite})
    return [{"t": iso(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in r.json()]


@fuente("Kraken velas (alternativa)")
def velas_kraken(par, minutos):
    """Alternativa si Binance no responde. Kraken da como máximo 720 velas."""
    r = get("https://api.kraken.com/0/public/OHLC", params={"pair": par, "interval": minutos})
    d = r.json()
    if d.get("error"):
        raise ValueError(d["error"])
    clave = next(k for k in d["result"] if k != "last")
    return [{"t": datetime.fromtimestamp(k[0], tz=timezone.utc).date().isoformat(),
             "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
             "v": float(k[6])}
            for k in d["result"][clave]]


@fuente("Hyperliquid velas")
def velas_hyperliquid(moneda, intervalo="1d", dias=700):
    inicio = int((HOY - timedelta(days=dias)).timestamp() * 1000)
    fin = int(HOY.timestamp() * 1000)
    r = post("https://api.hyperliquid.xyz/info",
             {"type": "candleSnapshot",
              "req": {"coin": moneda, "interval": intervalo, "startTime": inicio, "endTime": fin}})
    return [{"t": iso(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
             "l": float(k["l"]), "c": float(k["c"])} for k in r.json()]


@fuente("Hyperliquid funding")
def funding_hyperliquid():
    r = post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    meta, ctxs = r.json()
    out = {}
    for activo, ctx in zip(meta["universe"], ctxs):
        if activo["name"] in ("BTC", "SOL", "HYPE"):
            f = num(ctx.get("funding"))
            out[activo["name"]] = {
                "funding_hora": f,
                "funding_anual": f * 24 * 365 if f is not None else None,
                "open_interest": num(ctx.get("openInterest")),
                "precio": num(ctx.get("markPx")),
            }
    return out


# ---------------------------------------------------------------- on-chain
CLAVES_FECHA = ("d", "date", "day", "theDay", "fecha")


def _registro_a_par(reg):
    fecha = next((str(reg[k])[:10] for k in CLAVES_FECHA if k in reg), None)
    valor = None
    for k, v in reg.items():
        if k in CLAVES_FECHA or k.lower() in ("unixts", "timestamp", "delayed", "message"):
            continue
        valor = num(v)
        if valor is not None:
            break
    return fecha, valor


@fuente("BGeometrics serie")
def serie_bgeometrics(metrica):
    r = get(f"https://bitcoin-data.com/v1/{metrica}")
    datos = r.json()
    if isinstance(datos, dict):
        datos = datos.get("data") or datos.get("values") or [datos]
    pares = [_registro_a_par(x) for x in datos if isinstance(x, dict)]
    pares = sorted((f, v) for f, v in pares if f and v is not None)
    if not pares:
        raise ValueError(f"respuesta sin datos reconocibles: {str(datos)[:150]}")
    return pares


@fuente("BGeometrics último")
def ultimo_bgeometrics(metrica):
    r = get(f"https://bitcoin-data.com/v1/{metrica}/last")
    d = r.json()
    if isinstance(d, list):
        d = d[-1]
    f, v = _registro_a_par(d)
    if v is None:
        raise ValueError(f"sin valor: {str(d)[:150]}")
    return {"fecha": f, "valor": v, "retrasado": bool(d.get("delayed", False))}


# ---------------------------------------------------------------- macro
@fuente("FRED")
def serie_fred(codigo, desde="2022-01-01"):
    r = get("https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": codigo, "cosd": desde})
    filas = list(csv.reader(io.StringIO(r.text)))
    out = []
    for fila in filas[1:]:
        if len(fila) >= 2 and num(fila[1]) is not None:
            out.append((fila[0], float(fila[1])))
    if not out:
        raise ValueError(f"{codigo}: CSV vacío")
    return out


def valor_en(serie, fecha):
    """Último valor de la serie con fecha <= fecha."""
    v = None
    for f, x in serie:
        if f <= fecha:
            v = x
        else:
            break
    return v


@fuente("Miedo y codicia")
def miedo_codicia():
    r = get("https://api.alternative.me/fng/", params={"limit": 30})
    d = r.json()["data"]
    return {"valor": int(d[0]["value"]), "texto": d[0]["value_classification"],
            "hace_7d": int(d[7]["value"]) if len(d) > 7 else None}


@fuente("CoinGecko dominancia")
def dominancia():
    r = get("https://api.coingecko.com/api/v3/global")
    return r.json()["data"]["market_cap_percentage"]["btc"]


@fuente("DefiLlama ingresos Hyperliquid")
def ingresos_hyperliquid():
    r = get("https://api.llama.fi/summary/fees/hyperliquid",
            params={"dataType": "dailyRevenue"})
    d = r.json()
    serie = [v for _, v in sorted(d.get("totalDataChart", []))]
    if len(serie) < 60:
        raise ValueError("serie demasiado corta")
    ult30, prev30 = sum(serie[-30:]), sum(serie[-60:-30])
    return {"ult30": ult30, "prev30": prev30,
            "variacion": ult30 / prev30 - 1 if prev30 else None}


@fuente("DefiLlama stablecoins")
def stablecoins():
    r = get("https://stablecoins.llama.fi/stablecoincharts/all")
    serie = sorted((int(x["date"]), float(x["totalCirculatingUSD"]["peggedUSD"]))
                   for x in r.json() if (x.get("totalCirculatingUSD") or {}).get("peggedUSD"))
    if len(serie) < 40:
        raise ValueError("serie demasiado corta")
    return {"total": serie[-1][1], "var30": serie[-1][1] / serie[-31][1] - 1}


def peso_num(p):
    p = str(p)
    if "%" in p:
        return float(p.replace("%", "")) / 100
    if "/" in p:
        a, b = p.split("/")
        return float(a) / float(b)
    return 0.0


def media(xs):
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- cálculos
def semaforo(valor, verde, rojo, invertido=False):
    """verde/rojo son umbrales. Sin invertir: valor <= verde -> verde; >= rojo -> rojo."""
    if valor is None:
        return "gris"
    if not invertido:
        return "verde" if valor <= verde else "rojo" if valor >= rojo else "ambar"
    return "verde" if valor >= verde else "rojo" if valor <= rojo else "ambar"


def rsi(cierres, n=14):
    """RSI de Wilder sobre una lista de cierres."""
    if len(cierres) <= n:
        return None
    sub = [max(cierres[i] - cierres[i - 1], 0) for i in range(1, len(cierres))]
    baj = [max(cierres[i - 1] - cierres[i], 0) for i in range(1, len(cierres))]
    ms, mb = sum(sub[:n]) / n, sum(baj[:n]) / n
    for i in range(n, len(sub)):
        ms = (ms * (n - 1) + sub[i]) / n
        mb = (mb * (n - 1) + baj[i]) / n
    return 100.0 if mb == 0 else 100 - 100 / (1 + ms / mb)


def cambio(serie_c, n):
    if not serie_c or len(serie_c) <= n:
        return None
    return serie_c[-1] / serie_c[-1 - n] - 1


def estado_tramo(precio, t):
    if precio is None:
        return {"estado": "sin dato", "distancia": None}
    if precio > t["max"]:
        return {"estado": "por encima", "distancia": t["max"] / precio - 1}
    if precio >= t["min"]:
        return {"estado": "EN ZONA", "distancia": 0}
    return {"estado": "por debajo", "distancia": t["min"] / precio - 1}


def main():
    with open("plan.json", encoding="utf-8") as f:
        plan = json.load(f)
    anterior = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", encoding="utf-8") as f:
                anterior = json.load(f)
        except Exception:  # noqa: BLE001
            anterior = {}

    # --- consultas
    btc_d = velas_binance("BTCUSDT", "1d") or velas_kraken("XBTUSD", 1440)
    btc_w = velas_binance("BTCUSDT", "1w") or velas_kraken("XBTUSD", 10080)
    sol_d = velas_binance("SOLUSDT", "1d") or velas_kraken("SOLUSD", 1440)
    rotacion_d = {
        "ETH": velas_binance("ETHUSDT", "1d") or velas_kraken("ETHUSD", 1440),
        "LINK": velas_binance("LINKUSDT", "1d") or velas_kraken("LINKUSD", 1440),
        "XRP": velas_binance("XRPUSDT", "1d") or velas_kraken("XRPUSD", 1440),
    }
    hype_d = velas_hyperliquid("HYPE")
    fund = funding_hyperliquid() or {}
    sth = serie_bgeometrics("sth-realized-price")
    time.sleep(3)
    rp = serie_bgeometrics("realized-price")
    time.sleep(3)
    mvrvz = ultimo_bgeometrics("mvrv-zscore")
    walcl = serie_fred("WALCL")        # balance de la Fed, millones de $
    tga = serie_fred("WTREGEN")        # cuenta del Tesoro, miles de millones
    rrp = serie_fred("RRPONTSYD")      # repos inversos, miles de millones
    m2 = serie_fred("M2SL", desde="2019-01-01")  # M2, miles de millones
    fng = miedo_codicia()
    dom = dominancia()
    ing_hype = ingresos_hyperliquid()
    stables = stablecoins()
    nasdaq = serie_fred("NASDAQCOM", desde="2025-01-01")

    # --- precios
    p_btc = btc_d[-1]["c"] if btc_d else (fund.get("BTC") or {}).get("precio")
    p_sol = sol_d[-1]["c"] if sol_d else (fund.get("SOL") or {}).get("precio")
    p_hype = hype_d[-1]["c"] if hype_d else (fund.get("HYPE") or {}).get("precio")

    # --- semanas cerradas de BTC y media de 200 semanas
    semanas = btc_w[:-1] if btc_w else []   # la última vela es la semana en curso
    cierres_w = [s["c"] for s in semanas]
    mm200 = sum(cierres_w[-200:]) / 200 if len(cierres_w) >= 200 else None
    mm200_ant = sum(cierres_w[-201:-1]) / 200 if len(cierres_w) >= 201 else None
    cierre_sem = cierres_w[-1] if cierres_w else None
    cierre_sem_ant = cierres_w[-2] if len(cierres_w) >= 2 else None
    fin_semana = (date.fromisoformat(semanas[-1]["t"]) + timedelta(days=6)).isoformat() if semanas else None

    sth_v = sth[-1][1] if sth else None
    sth_f = sth[-1][0] if sth else None
    rp_v = rp[-1][1] if rp else None
    rp_f = rp[-1][0] if rp else None

    # --- liquidez neta de la Fed (miles de millones $)
    def a_miles_de_millones(serie):
        # FRED publica unas series en millones y otras en miles de millones:
        # se normalizan por magnitud (ninguna de estas pasa de 50.000 en miles de millones).
        if not serie:
            return serie
        return [(f, v / 1000) for f, v in serie] if max(v for _, v in serie) > 50000 else serie

    walcl_b, tga_b, rrp_b = (a_miles_de_millones(x) for x in (walcl, tga, rrp))
    liq = []
    if walcl_b and tga_b and rrp_b:
        for f, w in walcl_b:
            t, r_ = valor_en(tga_b, f), valor_en(rrp_b, f)
            if t is not None and r_ is not None:
                liq.append((f, w - t - r_))
    liq_v = liq[-1][1] if liq else None
    liq_13 = liq[-1][1] / liq[-14][1] - 1 if len(liq) >= 14 else None

    m2_yoy = m2[-1][1] / m2[-13][1] - 1 if m2 and len(m2) >= 13 else None

    # --- ratios frente a BTC (30 días)
    def ratio_vs_btc(velas):
        if not velas or not btc_d:
            return None
        btc_map = {v["t"]: v["c"] for v in btc_d}
        serie = [v["c"] / btc_map[v["t"]] for v in velas if v["t"] in btc_map]
        return cambio(serie, 30)

    sol_btc_30 = ratio_vs_btc(sol_d)
    hype_btc_30 = ratio_vs_btc(hype_d)

    senales = []

    # --- métricas añadidas en la revisión del 12 nov 2026
    cierres_d = [v["c"] for v in (btc_d or [])]
    mm200d = media(cierres_d[-200:]) if len(cierres_d) >= 200 else None
    mayer = p_btc / mm200d if p_btc and mm200d else None
    mm111d = media(cierres_d[-111:]) if len(cierres_d) >= 111 else None
    mm350d = media(cierres_d[-350:]) if len(cierres_d) >= 350 else None
    pi_ratio = mm111d / (2 * mm350d) if mm111d and mm350d else None
    ath = max((x["h"] for x in btc_w), default=None) if btc_w else None
    drawdown = p_btc / ath - 1 if p_btc and ath else None
    # STH-MVRV (precio / coste de compradores recientes) frente a su banda de 90 días
    sth_mvrv_hoy = sth_banda = None
    if sth and btc_d:
        serie_sm = [v["c"] / valor_en(sth, v["t"]) for v in btc_d[-91:] if valor_en(sth, v["t"])]
        if len(serie_sm) >= 60:
            previos = serie_sm[:-1]
            m = media(previos)
            desv = (sum((x - m) ** 2 for x in previos) / len(previos)) ** 0.5
            sth_mvrv_hoy, sth_banda = serie_sm[-1], m - desv
    cierres_nq = [v for _, v in (nasdaq or [])]
    nq_mm200 = media(cierres_nq[-200:]) if len(cierres_nq) >= 200 else None
    nq_ratio = cierres_nq[-1] / nq_mm200 if nq_mm200 else None
    nq_dd = cierres_nq[-1] / max(cierres_nq) - 1 if cierres_nq else None
    fng_v = fng["valor"] if fng else None

    # --- señales de suelo: las de Plan BTC calculables gratis + tres añadidas
    rsi_sem = rsi(cierres_w) if len(cierres_w) > 15 else None
    vols_w = [x.get("v") for x in semanas if x.get("v") is not None]
    vol_ratio = (vols_w[-1] / (sum(vols_w[-21:-1]) / 20)) if len(vols_w) >= 21 else None
    rp_ratio = p_btc / rp_v if p_btc and rp_v else None
    mz = mvrvz["valor"] if mvrvz else None
    capitulacion = [
        {"clave": "rsi", "nombre": "RSI semanal de BTC", "valor": rsi_sem, "formato": "n0",
         "cumple": rsi_sem is not None and rsi_sem <= 30, "umbral": "≤ 30 (sobreventa)"},
        {"clave": "vol", "nombre": "Volumen semanal / media de 20 semanas", "valor": vol_ratio, "formato": "x",
         "cumple": vol_ratio is not None and vol_ratio >= 2, "umbral": "≥ 2× (clímax vendedor)"},
        {"clave": "mvrvz_cap", "nombre": "MVRV Z-Score", "valor": mz, "formato": "n2",
         "cumple": mz is not None and mz <= 0, "umbral": "≤ 0 (mercado en pérdidas)"},
        {"clave": "rp_cap", "nombre": "Precio / precio realizado", "valor": rp_ratio, "formato": "x",
         "cumple": rp_ratio is not None and rp_ratio <= 1.05, "umbral": "≤ 1,05× (toca el coste medio de todo el mercado)"},
        {"clave": "sthb_cap", "nombre": "Precio / coste STH frente a su banda de 90 días", "valor": sth_mvrv_hoy, "formato": "x",
         "cumple": sth_mvrv_hoy is not None and sth_mvrv_hoy < sth_banda,
         "umbral": (f"< {sth_banda:.2f}× (media de 90 días menos una desviación)".replace(".", ",") if sth_banda else "por debajo de su banda de 90 días")},
        {"clave": "mayer_cap", "nombre": "Múltiplo de Mayer (precio / media de 200 días)", "valor": mayer, "formato": "x",
         "cumple": mayer is not None and mayer <= 0.8, "umbral": "≤ 0,80×"},
        {"clave": "fng_cap", "nombre": "Miedo y codicia", "valor": fng_v, "formato": "n0",
         "cumple": fng_v is not None and fng_v <= 20, "umbral": "≤ 20 (miedo extremo)"},
    ]
    n_suelo = sum(1 for c in capitulacion if c["cumple"])

    r_mm200_t = p_btc / mm200 if p_btc and mm200 else None
    f_btc_t = (fund.get("BTC") or {}).get("funding_anual")
    techo = [
        {"clave": "mayer_t", "nombre": "Múltiplo de Mayer", "valor": mayer, "formato": "x",
         "cumple": mayer is not None and mayer >= 2.4, "umbral": "≥ 2,40×"},
        {"clave": "pi_t", "nombre": "Pi Cycle (media de 111 días / 2 × media de 350 días)", "valor": pi_ratio, "formato": "x",
         "cumple": pi_ratio is not None and pi_ratio >= 1, "umbral": "≥ 1,00× (cruce)"},
        {"clave": "mvrvz_t", "nombre": "MVRV Z-Score", "valor": mz, "formato": "n2",
         "cumple": mz is not None and mz >= 3, "umbral": "≥ 3"},
        {"clave": "mm200_t", "nombre": "Precio / media de 200 semanas", "valor": r_mm200_t, "formato": "x",
         "cumple": r_mm200_t is not None and r_mm200_t >= 2, "umbral": "≥ 2,00×"},
        {"clave": "fng_t", "nombre": "Miedo y codicia", "valor": fng_v, "formato": "n0",
         "cumple": fng_v is not None and fng_v >= 80, "umbral": "≥ 80 (codicia extrema)"},
        {"clave": "fund_t", "nombre": "Funding de BTC (anualizado)", "valor": f_btc_t, "formato": "pct",
         "cumple": f_btc_t is not None and f_btc_t >= 0.30, "umbral": "≥ 30 %"},
    ]
    n_techo = sum(1 for c in techo if c["cumple"])

    # --- rotación de ETH, LINK y XRP: rebote desde el mínimo marcado desde el inicio del plan
    rotacion = []
    for sim, velas in rotacion_d.items():
        if not velas:
            rotacion.append({"activo": sim, "precio": None})
            continue
        desde = [v for v in velas if v["t"] >= plan["inicio_plan"]] or velas[-1:]
        minimo = min(desde, key=lambda v: v["l"])
        precio = velas[-1]["c"]
        btc_map = {v["t"]: v["c"] for v in (btc_d or [])}
        rat = [v["c"] / btc_map[v["t"]] for v in velas if v["t"] in btc_map]
        rotacion.append({
            "activo": sim, "precio": precio, "cambio_7d": cambio([v["c"] for v in velas], 7),
            "minimo": minimo["l"], "fecha_minimo": minimo["t"],
            "rebote": precio / minimo["l"] - 1,
            "objetivo_rebote": minimo["l"] * 1.5,
            "vs_btc_30d": cambio(rat, 30),
        })
        if precio >= minimo["l"] * 1.5:
            senales.append({"tipo": "venta", "texto": f"{sim} ha rebotado un 50% o más desde su mínimo del {minimo['t']}: según el plan, toca vender un tercio."})

    # --- indicadores
    def ind(clave, nombre, valor, formato, color, detalle, fuente_url, fecha=None):
        return {"clave": clave, "nombre": nombre, "valor": valor, "formato": formato,
                "color": color, "detalle": detalle, "fuente": fuente_url, "fecha": fecha}

    r_mm200 = p_btc / mm200 if p_btc and mm200 else None
    r_sth = p_btc / sth_v if p_btc and sth_v else None
    f_btc = (fund.get("BTC") or {}).get("funding_anual")

    valoracion = [
        ind("mm200", "Precio / media de 200 semanas", r_mm200, "x",
            semaforo(r_mm200, 1.15, 2.0),
            f"Media de 200 semanas: {mm200:,.0f} $".replace(",", ".") if mm200 else "sin dato",
            "https://data-api.binance.vision", fin_semana),
        ind("sth", "Precio / coste de compradores recientes (STH)", r_sth, "x",
            semaforo(r_sth, 0.9, 1.25),
            f"Coste STH: {sth_v:,.0f} $".replace(",", ".") if sth_v else "sin dato",
            "https://charts.bgeometrics.com/sth_realized_price.html", sth_f),
        ind("mvrvz", "MVRV Z-Score", mvrvz["valor"] if mvrvz else None, "n2",
            semaforo(mvrvz["valor"] if mvrvz else None, 0.5, 3.0),
            ("Dato con retraso (plan gratuito)" if mvrvz and mvrvz["retrasado"] else "") ,
            "https://charts.bgeometrics.com/mvrv_zscore.html", mvrvz["fecha"] if mvrvz else None),
        ind("fng", "Miedo y codicia", fng["valor"] if fng else None, "n0",
            semaforo(fng["valor"] if fng else None, 25, 75),
            (f"{fng['texto']} · hace 7 días: {fng['hace_7d']}" if fng else "sin dato"),
            "https://alternative.me/crypto/fear-and-greed-index/"),
        ind("funding", "Funding de BTC (anualizado, Hyperliquid)", f_btc, "pct",
            semaforo(f_btc, 0.05, 0.30),
            "Base neutral en Hyperliquid ≈ 11% anual. Muy por encima: exceso de apalancamiento comprador",
            "https://app.hyperliquid.xyz/fundingComparison"),
        ind("mayer", "Múltiplo de Mayer (precio / media de 200 días)", mayer, "x",
            semaforo(mayer, 0.8, 2.4),
            f"Media de 200 días: {miles(mm200d)} $" if mm200d else "sin dato",
            "https://data-api.binance.vision"),
        ind("dd", "Caída desde el máximo histórico", drawdown, "pct",
            semaforo(drawdown, -0.5, -0.15),
            f"Máximo: {miles(ath)} $. Suelos de mercados bajistas anteriores: entre −77% y −84%" if ath else "sin dato",
            "https://data-api.binance.vision"),
    ]
    fondo = [
        ind("liquidez", "Liquidez neta de la Fed (cambio en 13 semanas)", liq_13, "pct",
            semaforo(liq_13, 0.02, -0.02, invertido=True),
            f"Nivel: {liq_v / 1000:.2f} billones $".replace(".", ",") if liq_v else "sin dato",
            "https://fred.stlouisfed.org/series/WALCL", liq[-1][0] if liq else None),
        ind("m2", "M2 de EE. UU. (crecimiento anual)", m2_yoy, "pct",
            semaforo(m2_yoy, 0.06, 0.02, invertido=True), "Masa monetaria; dato mensual",
            "https://fred.stlouisfed.org/series/M2SL", m2[-1][0] if m2 else None),
        ind("dom", "Dominancia de BTC", dom / 100 if dom else None, "pct1",
            "ambar", "Contexto: si sube, el dinero se refugia en BTC y las altcoins sufren",
            "https://www.coingecko.com/es/global-charts"),
        ind("solbtc", "SOL frente a BTC (30 días)", sol_btc_30, "pct",
            semaforo(sol_btc_30, 0.05, -0.05, invertido=True),
            "Positivo = SOL mejor que BTC; negativo = peor", "https://data-api.binance.vision"),
        ind("hypebtc", "HYPE frente a BTC (30 días)", hype_btc_30, "pct",
            semaforo(hype_btc_30, 0.05, -0.05, invertido=True),
            "Positivo = HYPE mejor que BTC; negativo = peor", "https://app.hyperliquid.xyz"),
        ind("inghype", "Ingresos de Hyperliquid (30 días vs 30 anteriores)",
            ing_hype["variacion"] if ing_hype else None, "pct",
            semaforo(ing_hype["variacion"] if ing_hype else None, 0.10, -0.10, invertido=True),
            (f"Últimos 30 días: {ing_hype['ult30'] / 1e6:.1f} M$".replace(".", ",")
             if ing_hype else "sin dato"),
            "https://defillama.com/protocol/hyperliquid"),
        ind("stables", "Stablecoins en circulación (cambio en 30 días)",
            stables["var30"] if stables else None, "pct",
            semaforo(stables["var30"] if stables else None, 0.02, -0.01, invertido=True),
            (f"Total: {stables['total'] / 1e9:.0f} mil M$" if stables else "sin dato"),
            "https://defillama.com/stablecoins"),
        ind("nasdaq", "Nasdaq / su media de 200 sesiones", nq_ratio, "x",
            semaforo(nq_ratio, 1.02, 0.97, invertido=True),
            (f"A {nq_dd * 100:.1f}% de su máximo desde 2025".replace(".", ",") if nq_dd is not None else "sin dato"),
            "https://fred.stlouisfed.org/series/NASDAQCOM", nasdaq[-1][0] if nasdaq else None),
    ]

    # --- plan: estado de tramos
    plan_estado = {"btc": [], "sol": [], "hype": [], "salida_btc": []}
    for t in plan["btc"]["entrada"]:
        plan_estado["btc"].append({**t, **estado_tramo(p_btc, t)})
    for t in plan["sol"]["entrada"]:
        plan_estado["sol"].append({**t, **estado_tramo(p_sol, t)})
    for t in plan["hype"]["entrada"]:
        plan_estado["hype"].append({**t, **estado_tramo(p_hype, t)})
    for t in plan["btc"]["salida"]:
        d = t["precio"] / p_btc - 1 if p_btc else None
        plan_estado["salida_btc"].append({**t, "distancia": d,
                                          "estado": "ALCANZADO" if d is not None and d <= 0 else "pendiente"})

    # --- posiciones: tramos marcados como ejecutados en plan.json
    posiciones = {}
    for nombre in ("btc", "sol", "hype"):
        tramos = list(plan[nombre]["entrada"]) + ([plan["btc"]["reserva"]] if nombre == "btc" else [])
        ej = [t for t in tramos if t.get("ejecutado")]
        w = sum(peso_num(t["peso"]) for t in ej)
        if ej and w:
            med = sum(peso_num(t["peso"]) * t["ejecutado"]["precio"] for t in ej) / w
            precio = {"btc": p_btc, "sol": p_sol, "hype": p_hype}[nombre]
            posiciones[nombre] = {"desplegado": w, "medio": med, "x2": 2 * med,
                                  "resultado": precio / med - 1 if precio else None}
    for t in plan_estado["btc"]:
        if not t.get("ejecutado") and p_btc and p_btc <= t["max"]:
            senales.append({"tipo": "compra", "texto": f"BTC tramo {t['tramo']} ({miles(t['min'])}–{miles(t['max'])} $): el precio está en la zona o por debajo. Compra según el plan."})
    for nombre in ("sol", "hype"):
        pos = posiciones.get(nombre)
        precio = {"sol": p_sol, "hype": p_hype}[nombre]
        if pos and precio and precio >= pos["x2"]:
            senales.append({"tipo": "venta", "texto": f"{nombre.upper()} ha doblado tu precio medio: vende la mitad y recuperas lo invertido."})

    # --- confluencia de señales de suelo y de techo
    conf_fecha = anterior.get("confluencia_suelo")
    if conf_fecha and conf_fecha < plan["inicio_plan"]:
        conf_fecha = None
    if n_suelo >= 4 and not conf_fecha:
        conf_fecha = HOY.date().isoformat()
    if conf_fecha and not plan["btc"]["reserva"].get("ejecutado"):
        senales.append({"tipo": "compra", "texto": f"Tramo 4 (reserva) ACTIVADO por confluencia: 4 o más señales de suelo se alinearon el {conf_fecha}."})
    if n_techo >= 3:
        senales.append({"tipo": "venta", "texto": f"{n_techo} de {len(techo)} señales de techo alineadas: vende el siguiente tramo pendiente aunque no haya llegado a su precio."})

    # --- señales del plan
    if cierre_sem and cierre_sem_ant and mm200 and mm200_ant:
        if cierre_sem_ant < mm200_ant and cierre_sem > mm200:
            senales.append({"tipo": "compra", "texto": "Tramo 4 (reserva) ACTIVADO: la semana pasada cerró bajo la media de 200 semanas y esta ha cerrado por encima."})
        elif cierre_sem < mm200:
            senales.append({"tipo": "aviso", "texto": "Cierre semanal por debajo de la media de 200 semanas. Si la próxima semana cierra por encima, se activa el tramo 4."})
    if cierre_sem and cierre_sem > plan["btc"]["invalidacion_arriba"]:
        senales.append({"tipo": "aviso", "texto": f"Cierre semanal por encima de {miles(plan['btc']['invalidacion_arriba'])} $: el escenario de caída queda invalidado. Revisa el plan (sección 'Cuándo el plan queda invalidado')."})
    if cierre_sem and rp_v and cierre_sem < rp_v:
        senales.append({"tipo": "aviso", "texto": "Cierre semanal por debajo del precio realizado: el suelo de 55.000–60.000 $ queda invalidado. El tramo 4 espera y la regla de tiempo se alarga 3 meses."})
    max_desde_plan = max((v["h"] for v in btc_d if v["t"] >= plan["inicio_plan"]), default=None) if btc_d else None
    if max_desde_plan and max_desde_plan >= 100000 and cierre_sem and sth_v and cierre_sem < sth_v:
        senales.append({"tipo": "venta", "texto": "Regla de deterioro: BTC superó 100.000 $ y ha cerrado la semana por debajo del coste STH. Vende el siguiente tramo pendiente."})
    # --- regla de altcoins: cada tramo se activa con el tramo SIGUIENTE de BTC
    min_btc_plan = min((v["l"] for v in btc_d if v["t"] >= plan["inicio_plan"]), default=None) if btc_d else None
    t2_btc, t3_btc = plan["btc"]["entrada"][1], plan["btc"]["entrada"][2]
    # confirmación de suelo (tramo 4): alguna semana desde el inicio del plan cerró bajo la MM200 y la siguiente por encima
    suelo_confirmado = False
    for i in range(201, len(cierres_w)):
        if semanas[i]["t"] < plan["inicio_plan"]:
            continue
        mm_i = sum(cierres_w[i - 199:i + 1]) / 200
        mm_a = sum(cierres_w[i - 200:i]) / 200
        if cierres_w[i - 1] < mm_a and cierres_w[i] > mm_i:
            suelo_confirmado = True
    fecha_limite_pasada = HOY.date() > date.fromisoformat(plan["fecha_limite_compras"])
    disparadores = [
        (min_btc_plan is not None and min_btc_plan <= t2_btc["max"], f"su tramo 2 ({miles(t2_btc['min'])}–{miles(t2_btc['max'])} $)"),
        (min_btc_plan is not None and min_btc_plan <= t3_btc["max"], f"su tramo 3 ({miles(t3_btc['min'])}–{miles(t3_btc['max'])} $)"),
        (suelo_confirmado, "su confirmación de suelo (tramo 4)"),
    ]
    precios_alt = {"sol": p_sol, "hype": p_hype}
    for nombre in ("sol", "hype"):
        for i, t in enumerate(plan_estado[nombre]):
            activo, cuando = disparadores[i]
            activo = activo or suelo_confirmado or fecha_limite_pasada
            t["activado"] = activo
            precio = precios_alt[nombre]
            if precio is None or t.get("ejecutado"):
                continue
            if activo:
                if precio <= t["max"]:
                    senales.append({"tipo": "compra", "texto": f"{nombre.upper()} tramo {t['tramo']} ACTIVADO y el precio está en su zona o por debajo: compra a mercado según el plan."})
                else:
                    senales.append({"tipo": "compra", "texto": f"{nombre.upper()} tramo {t['tramo']} ACTIVADO: deja una orden limitada en su zona ({t['min']}–{t['max']} $)."})
            elif precio <= t["max"]:
                senales.append({"tipo": "aviso", "texto": f"{nombre.upper()} está en la zona de su tramo {t['tramo']} o por debajo, pero el tramo no se activa hasta que BTC toque {cuando}. Según el plan, espera."})
    if p_btc and p_btc <= t2_btc["max"]:
        senales.append({"tipo": "compra", "texto": f"BTC está en su tramo 2 o por debajo: según el plan, toca permutar un tercio de ETH, LINK y XRP a BTC (si no lo has hecho ya)."})
    dias_limite = (date.fromisoformat(plan["fecha_limite_compras"]) - HOY.date()).days
    senales.append({"tipo": "info", "texto": f"Regla de tiempo: quedan {dias_limite} días hasta el {plan['fecha_limite_compras']} para ejecutar los tramos de compra pendientes."})

    # --- serie para el gráfico (semanal desde 2022)
    grafico = []
    for i, s in enumerate(semanas):
        if s["t"] < "2022-01-01":
            continue
        ventana = cierres_w[max(0, i - 199): i + 1]
        fin = (date.fromisoformat(s["t"]) + timedelta(days=6)).isoformat()
        grafico.append({
            "t": fin, "c": round(s["c"]),
            "mm200": round(sum(ventana) / 200) if len(ventana) == 200 else None,
            "sth": round(valor_en(sth, fin)) if sth and valor_en(sth, fin) else None,
            "rp": round(valor_en(rp, fin)) if rp and valor_en(rp, fin) else None,
        })

    # --- histórico semanal (una fila por semana cerrada)
    historial = [h for h in anterior.get("historial", []) if h.get("semana") != fin_semana]
    if fin_semana:
        historial.append({
            "semana": fin_semana, "btc": cierre_sem, "sol": p_sol, "hype": p_hype,
            "mm200": mm200, "sth": sth_v, "mvrvz": mvrvz["valor"] if mvrvz else None,
            "fng": fng["valor"] if fng else None, "dom": dom,
            "colores": {i["clave"]: i["color"] for i in valoracion + fondo},
        })
    historial = sorted(historial, key=lambda h: h["semana"])[-104:]

    data = {
        "actualizado": HOY.strftime("%Y-%m-%d %H:%M UTC"),
        "precios": {"BTC": p_btc, "SOL": p_sol, "HYPE": p_hype},
        "cambios_7d": {
            "BTC": cambio([v["c"] for v in btc_d], 7) if btc_d else None,
            "SOL": cambio([v["c"] for v in sol_d], 7) if sol_d else None,
            "HYPE": cambio([v["c"] for v in hype_d], 7) if hype_d else None,
        },
        "niveles": {"mm200": mm200, "sth": sth_v, "sth_fecha": sth_f, "rp": rp_v, "rp_fecha": rp_f,
                    "cierre_semanal": cierre_sem, "fin_semana": fin_semana},
        "valoracion": valoracion,
        "fondo": fondo,
        "plan": plan,
        "plan_estado": plan_estado,
        "senales": senales,
        "grafico": grafico,
        "capitulacion": capitulacion,
        "techo": techo,
        "posiciones": posiciones,
        "confluencia_suelo": conf_fecha,
        "rotacion": rotacion,
        "diario_btc": [[v["t"], round(v["h"]), round(v["l"]), round(v["c"])] for v in (btc_d or [])[-400:]],
        "historial": historial,
        "errores": ERRORES,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"data.json generado. Errores: {len(ERRORES)}")
    for e in ERRORES:
        print("  -", e)


if __name__ == "__main__":
    main()
