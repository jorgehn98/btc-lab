# Estudio cerrado de reentrada y régimen

Esta es una campaña **nueva**, exclusivamente de investigación en TRAIN.
La búsqueda anterior de 72 variantes terminó sin finalistas y sus datos,
input, estado, presupuesto y código sellado no se reabren. Ningún resultado
de este estudio activa un bot por sí solo.

## Hipótesis registradas antes de ejecutar

Hay 24 configuraciones SMA50/200 spot long: tres cambios binarios cruzados
con los perfiles bajo, medio y alto. La celda original sirve de control bajo
el mismo warmup y ventanas que las nuevas:

| Factor | Desactivado | Activado |
|---|---|---|
| Reentrada | Solo cruce alcista SMA50/200 y cierre > SMA200 | Además, nuevo cruce del cierre al alza sobre SMA50 con SMA50>SMA200 y cierre>SMA200 |
| Régimen | Sin pendiente adicional | SMA200 actual > SMA200 de hace 48 velas 1h |
| Salida | Cruce bajista SMA50/200 o cierre<SMA200 | Además, cierre<SMA50 |

Las señales usan velas 1h cerradas y un contexto común de 249 horas contiguas.
Todos usan stop fijo 2 %, una posición long, `dry_run`, sin API, Telegram,
claves, cortos, futuros ni apalancamiento. El tamaño de posición sigue los
tres perfiles registrados (riesgo 0,125/0,25/0,50 %; exposición máxima
10/20/40 %). Solo se evalúa Binance público BTC/USDT spot.

TRAIN usa el snapshot inmutable `[2017-08-17T04:00:00Z,
2023-01-01T00:00:00Z)`, costes 0,1 % y 0,2 % por lado, y un presupuesto
nativo máximo de 12 horas con fallos incluidos. Selección 2019–2022 a 0,2 %:
al menos 100 operaciones no forzadas, tres años con media diaria neta
positiva, exceso mediano anual positivo frente a buy-and-hold comparable,
g sin contribuciones positivas de cierres forzados ≥0, dos variantes
vecinas de un solo factor positivas y drawdown MTM 5m ≤15 % en todo TRAIN.
Una media diaria observada no es CAGR ni rentabilidad acumulada de una
cartera ficticia enlazada entre episodios.

El motor Freqtrade 2026.8 a veces exporta `force_exit` fechado antes de
una entrada en la última vela 1h cuando se usa detalle 5m. El ledger rechaza
ese año como inconcluso; **no** se cambian fecha ni precio de los fills.
Hay como máximo tres finalistas, uno por perfil, sin reemplazar al que falle
gates posteriores. VALIDATION y TEST continúan cerrados: este PR solo tiene
servicio TRAIN. Si no hay finalistas, se comunica NO_CANDIDATE.

## Ejecución local

Desde la raíz del checkout limpio de la nueva rama, crear directorios como
el mismo usuario UID/GID 1000 que ejecutará Docker:

```sh
export LAB_CODE_ROOT="$PWD"
export LAB_STORAGE_ROOT="$PWD/storage"
mkdir -p "$LAB_STORAGE_ROOT/regime/control" \
  "$LAB_STORAGE_ROOT/regime/inputs" \
  "$LAB_STORAGE_ROOT/regime/generated" \
  "$LAB_STORAGE_ROOT/regime/sessions"

regime_input="$(python3 -m operations.regime prepare \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --train-snapshot 'train-snap-2026-09-22T114307.436903Z-b5914210-b6ae.json')" || exit $?
test -f "$regime_input" || exit 1
export LAB_REGIME_INPUT="$regime_input"
export LAB_REGIME_TRAIN_SNAPSHOT="$LAB_STORAGE_ROOT/history/snapshots/train-snap-b6ae65f3"
export LAB_REGIME_TRAIN_MANIFEST="$LAB_STORAGE_ROOT/history/snapshots/train-snap-2026-09-22T114307.436903Z-b5914210-b6ae.json"

docker compose --profile regime run --rm regime-screen status
docker compose --profile regime run --rm regime-screen screen
```

`prepare` devuelve un input **único**; conservar exactamente su ruta. No
seleccionar el más reciente ni editar un hash, y no llamar `prepare` otra vez
para reiniciar presupuesto. El servicio monta solo TRAIN como datos RO y
source generado RO; no hay comandos/montajes de VALIDATION/TEST para el
estudio. El estado y los reportes quedan en `storage/regime/` fuera de Git.
Un proceso detenido no implica éxito: comprobar el estado terminal en el
reporte de la sesión y `status` antes de concluir o reanudar.

La campaña aún no tiene resultado económico. Los 163 tests de contratos se
ejecutan en el host (con skips de pandas/Freqtrade) y la imagen fijada; la
comparación nativa batch/individual, que requiere consultar metadatos
públicos de Binance, se ejecuta aparte en el perfil de investigación.
