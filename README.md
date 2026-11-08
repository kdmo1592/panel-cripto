# Panel cripto · BTC, SOL y HYPE

Panel semanal para contrastar el mercado con el plan de inversión propio: estado de los tramos de compra y venta, indicadores de valoración (¿barato o caro?), viento de fondo macro (¿empuja o frena?) e histórico semanal.

## Cómo funciona

- `generar_datos.py` consulta fuentes gratuitas y escribe `data.json`.
- `index.html` lee `data.json` y `plan.json` y dibuja el panel.
- `.github/workflows/actualizar.yml` ejecuta el generador cada día a las 06:17 UTC y guarda `data.json`.

## Cambiar el plan

Edita `plan.json` (zonas de compra, precios de venta, fechas). El repositorio es público: no pongas importes ni cantidades.

## Actualizar a mano

Pestaña **Actions** → **Actualizar panel** → **Run workflow**.

## Fuentes

| Dato | Fuente |
| --- | --- |
| Velas de BTC y SOL | Binance (data-api.binance.vision); si falla, Kraken |
| Velas de HYPE y funding | Hyperliquid (api.hyperliquid.xyz) |
| Coste STH, precio realizado, MVRV Z | BGeometrics (bitcoin-data.com), plan gratuito con unos días de retraso |
| Liquidez neta de la Fed y M2 | FRED (WALCL, WTREGEN, RRPONTSYD, M2SL) |
| Miedo y codicia | alternative.me |
| Dominancia de BTC | CoinGecko |
| Ingresos de Hyperliquid | DefiLlama |

Gráficos con [Chart.js](https://www.chartjs.org/) 4.4.4 (licencia MIT, copia en `assets/`).

Herramienta de seguimiento, no recomendación de inversión.
