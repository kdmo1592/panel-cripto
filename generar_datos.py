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


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


# ---------------------------------------------------------------- precios
@fuente("Binance velas")
def velas_binance(simbolo, intervalo, limite=1000):
    r = get("https://data-api.binance.vision/api/v3/klines",
            params={"symbol": simbolo, "interval": intervalo, "limit": limite})
    return [{"t": iso(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4])} for k in r.json()]


@fuente("Kraken velas (alternativa)")
def velas_kraken(par, minutos):
    """Alternativa si Binance no responde. Kraken da como máximo 720 velas."""
    r = get("https://api.kraken.com/0/public/OHLC", params={"pair": par, "interval": minutos})
    d = r.json()
    if d.get("error"):
        raise ValueError(d["error"])
    clave = next(k for k in d["result"] if k != "last")
    return [{"t": datetime.fromtimestamp(k[0], tz=timezone.utc).date().isoformat(),
             "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
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


# ---------------------------------------------------------------- cálculos
def semaforo(valor, verde, rojo, invertido=False):
    """verde/rojo son umbrales. Sin invertir: valor <= verde -> verde; >= rojo -> rojo."""
    if valor is None:
        return "gris"
    if not invertido:
        return "verde" if valor <= verde else "rojo" if valor >= rojo else "ambar"
    return "verde" if valor >= verde else "rojo" if valor <= rojo else "ambar"


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
    liq = []
    if walcl and tga and rrp:
        for f, w in walcl:
            t, r_ = valor_en(tga, f), valor_en(rrp, f)
            if t is not None and r_ is not None:
                liq.append((f, w / 1000 - t - r_))
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
            "ambar", "Alta y subiendo: el dinero se refugia en BTC y las altcoins sufren",
            "https://www.coingecko.com/es/global-charts"),
        ind("solbtc", "SOL frente a BTC (30 días)", sol_btc_30, "pct",
            semaforo(sol_btc_30, 0.05, -0.05, invertido=True),
            "Positivo: SOL lo hace mejor que BTC", "https://data-api.binance.vision"),
        ind("hypebtc", "HYPE frente a BTC (30 días)", hype_btc_30, "pct",
            semaforo(hype_btc_30, 0.05, -0.05, invertido=True),
            "Positivo: HYPE lo hace mejor que BTC", "https://app.hyperliquid.xyz"),
        ind("inghype", "Ingresos de Hyperliquid (30 días vs 30 anteriores)",
            ing_hype["variacion"] if ing_hype else None, "pct",
            semaforo(ing_hype["variacion"] if ing_hype else None, 0.10, -0.10, invertido=True),
            (f"Últimos 30 días: {ing_hype['ult30'] / 1e6:.1f} M$".replace(".", ",")
             if ing_hype else "sin dato"),
            "https://defillama.com/protocol/hyperliquid"),
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

    # --- señales del plan
    senales = []
    if cierre_sem and cierre_sem_ant and mm200 and mm200_ant:
        if cierre_sem_ant < mm200_ant and cierre_sem > mm200:
            senales.append({"tipo": "compra", "texto": "Tramo 4 (reserva) ACTIVADO: la semana pasada cerró bajo la media de 200 semanas y esta ha cerrado por encima."})
        elif cierre_sem < mm200:
            senales.append({"tipo": "aviso", "texto": "Cierre semanal por debajo de la media de 200 semanas. Si la próxima semana cierra por encima, se activa el tramo 4."})
    if cierre_sem and cierre_sem > plan["btc"]["invalidacion_arriba"]:
        senales.append({"tipo": "aviso", "texto": f"Cierre semanal por encima de {plan['btc']['invalidacion_arriba']:,} $: el escenario de caída queda invalidado. Revisa el plan (sección 'Cuándo el plan queda invalidado').".replace(",", ".")})
    if cierre_sem and rp_v and cierre_sem < rp_v:
        senales.append({"tipo": "aviso", "texto": "Cierre semanal por debajo del precio realizado: el suelo de 55.000–60.000 $ queda invalidado. El tramo 4 espera y la regla de tiempo se alarga 3 meses."})
    max_desde_plan = max((v["h"] for v in btc_d if v["t"] >= plan["inicio_plan"]), default=None) if btc_d else None
    if max_desde_plan and max_desde_plan >= 100000 and cierre_sem and sth_v and cierre_sem < sth_v:
        senales.append({"tipo": "venta", "texto": "Regla de deterioro: BTC superó 100.000 $ y ha cerrado la semana por debajo del coste STH. Vende el siguiente tramo pendiente."})
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
