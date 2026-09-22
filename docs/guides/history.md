# Histórico y ledger de la campaña

Esta es la guía única para el histórico ampliado de BTC/USDT y su contabilidad
analítica. PR01 solo permite TRAIN: Binance público, spot, par `BTC/USDT`, datos
`5m`, intervalo UTC semiabierto `[2017-08-17T04:00:00Z,
2023-01-01T00:00:00Z)`. No existe aquí un prefijo Binance de 2016 ni se mezcla
`BTC/USD` de otro proveedor.

## Roles y autorización

Las particiones están fijadas en `market/history.py`:

| Rol | Intervalo UTC | Estado en PR01 |
| --- | --- | --- |
| TRAIN | `2017-08-17 04:00` → `2023-01-01` | abierto |
| VALIDATION | `2023-01-01` → `2025-01-01` | cerrado |
| TEST | `2025-01-01` → `2026-09-22 00:00` | cerrado |

El cutoff de TEST está fijado en `2026-09-22T00:00Z`; no se desplaza durante
esta campaña. `authorize_partition` valida la forma de una selección, pero no
es una autorización persistente. En PR01, `operations.history` rechaza cualquier
rol distinto de TRAIN. VALIDATION requerirá un `finalists.json` verificable por
hash; TEST requerirá un `test-decision.json` verificable, una candidata elegida
solo con el pasado y consumo único. No existen flags `force`, `allow_test`, fechas
libres ni otros bypass.

## Preparación y almacenamiento

Define las raíces y crea los directorios como el usuario que ejecutará Compose:

```sh
export LAB_CODE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot"
export LAB_STORAGE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/storage"
mkdir -p "$LAB_STORAGE_ROOT/history/inputs" \
  "$LAB_STORAGE_ROOT/history/downloads" \
  "$LAB_STORAGE_ROOT/history/snapshots" \
  "$LAB_STORAGE_ROOT/history/sessions" \
  "$LAB_STORAGE_ROOT/history/control"
```

El preflight se ejecuta en el host, exige árbol Git limpio, imagen fijada y
hashes de `history.py`, `research.py`, `equity.py`, `market/history.py`,
`market/train.py`, `launch.py` y `health.py`. Devuelve un único manifiesto; fíjalo
antes de cualquier comando Compose y no elijas el más reciente:

```sh
history_input="$(python3 -m operations.history prepare \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT")" || exit $?
test -f "$history_input" || exit 1
export LAB_HISTORY_INPUT="$history_input"
```

La imagen es la fijada por `compose.yaml`. Comprueba el perfil sin arrancar un
bot:

```sh
docker compose --profile history config --quiet
```

El servicio `history-data` monta únicamente código de solo lectura, el input de
solo lectura y `history/downloads`, `history/snapshots` y `history/control` como
destinos de datos. `history` monta snapshots de solo lectura y escribe solo en
`history/sessions` y `history/control`. Ninguno monta Git, `.engram`, `work`, la
raíz completa de storage ni secretos.

## Descargar y congelar TRAIN

Ambos comandos siguientes corren dentro de la imagen fijada. La descarga usa el
rango exacto TRAIN y genera un manifiesto; el snapshot valida OHLCV, detecta
huecos, deriva `1h` solo de grupos completos de doce velas `5m` y congela hashes.

```sh
download_manifest="$(docker compose --profile history run --rm \
  history-data download)" || exit $?
test -n "$download_manifest" || exit 1

snapshot_manifest="$(docker compose --profile history run --rm \
  history-data snapshot --download "$(basename "$download_manifest")")" || exit $?
test -n "$snapshot_manifest" || exit 1
```

Evalúa siempre un snapshot por nombre explícito. Los segmentos contiguos se
evalúan por separado; el warmup común es de `201` velas de `1h` del pasado del
mismo segmento, identificado como `context_only` y fuera de las métricas. Los
huecos no se rellenan y no se rebasea el tiempo.

## Ledger analítico

Freqtrade sigue siendo el motor de ejecución: el ledger no simula fills. Recibe
los fills nativos y calcula efectivo, cantidad BTC, fees en USDT, valor mark-to-
market y exposición en cada cierre `5m`. Solo acepta spot long, sin margen,
short ni leverage distinto de `1`; una posición abierta no se convierte en un
cierre ficticio por terminar el segmento.

La ventana termina con un evento terminal en el timestamp `end`, usando el fill
de frontera y sin leer una vela futura. El ledger mantiene por separado
`drawdown_usdt` y `drawdown_pct`; el filtro histórico de `15%` usa el drawdown de
equity mark-to-market observado, no solo trades cerrados. Este límite histórico
no garantiza una pérdida máxima futura.

Los trades de entrada del ledger deben estar en el sandbox de `sessions` o en el
snapshot congelado; no se aceptan rutas arbitrarias ni el input del perfil. El
comando no abre roles externos:

```sh
docker compose --profile history run --rm history ledger \
  --snapshot "$(basename "$snapshot_manifest")" \
  --segment 0 \
  --trades trades.json
```

Cuando se entrega `native_profit`, la reconciliación compara el PnL nativo con el
PnL del ledger y exige una diferencia máxima de `0.01 USDT`. La reconciliación no
relanza el backtest. Capital, equity y estadísticas se mantienen independientes
por episodio; nunca se concatenan saldos para presentar una cartera continua.

## Evidencia y límites

Los manifiestos, datos, logs y sesiones bajo `storage/history/` son locales y
están fuera de Git. Un resultado `SUCCEEDED` del job no es un gate económico
`PASS` ni autoriza baseline. En PR01 no se descargan VALIDATION/TEST, no se
selecciona candidata y no se presenta ganador.

Fuentes de implementación: `operations/history.py`, `market/history.py`,
`market/equity.py` y los servicios `history-data`/`history` de `compose.yaml`.
