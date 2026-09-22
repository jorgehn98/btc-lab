# Estudio cerrado de reentrada y régimen

Esta fue una segunda campaña **independiente**, exclusivamente de investigación
en TRAIN. Su resultado fue `SUCCEEDED / NO_CANDIDATE`.
La búsqueda anterior de 72 variantes terminó sin finalistas y sus datos,
input, estado, presupuesto y código sellado no se reabren. Ningún resultado
de este estudio activa un bot por sí solo.

## Hipótesis registradas antes de ejecutar

Hay 48 configuraciones SMA50/200 spot long: cuatro cambios binarios cruzados
con los perfiles bajo, medio y alto. La celda original sirve de control bajo
el mismo warmup y ventanas que las nuevas:

| Factor | Desactivado | Activado |
|---|---|---|
| Reentrada | Solo cruce alcista SMA50/200 y cierre > SMA200 | Además, nuevo cruce del cierre al alza sobre SMA50 con SMA50>SMA200 y cierre>SMA200 |
| Régimen | Sin pendiente adicional | SMA200 actual > SMA200 de hace 48 velas 1h |
| Salida | Cruce bajista SMA50/200 o cierre<SMA200 | Además, cierre<SMA50 |
| Protección | Stop inicial fijo 2 % | Tras beneficio **neto de fees** 2 %, stop nativo a 1 % del máximo alcanzado |

Las señales usan velas 1h cerradas y un contexto común de 249 horas contiguas.
Para la protección, Freqtrade usa `trailing_stop=True`,
`trailing_stop_positive_offset=0.02`, `trailing_stop_positive=0.01` y
`trailing_only_offset_is_reached=True`. Con fee 0,2 % por lado, el umbral
neto 2 % necesita aproximadamente +2,4 % de subida bruta. Desde 80.000
hasta un máximo de 82.000, un stop del 1 % del máximo quedaría cerca de
81.180, **sin garantizar** una ejecución a ese precio si hay salto o
deslizamiento. La variante control conserva el stop inicial; ambas se
comparan por señales, riesgo y ventana idénticos.
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
Se exige además una meta **exploratoria** de al menos 0,05 % neto por día
observado y por día calendario normalizado en 2019–2022 a fee 0,2 %: para
la segunda tasa se suman log-retornos de episodios independientes y se
dividen entre los 1.461 días de esos cuatro años, tratando los huecos
conocidos como efectivo (retorno 0). Años con datos/fills inválidos no se
rellenan. El informe incluye ambos denominadores y compara cada celda
con su buy-and-hold equiparable. Ni esta tasa calendario sintética ni una
media diaria observada son CAGR real de una cartera enlazada: el objetivo
de ~20 % anual requiere evidencia adicional sobre capital persistente.

El motor Freqtrade 2026.8 a veces exporta `force_exit` fechado antes de
una entrada en la última vela 1h cuando se usa detalle 5m. El ledger rechaza
ese año como inconcluso; **no** se cambian fecha ni precio de los fills.
Se permitían como máximo tres **preseleccionados económicos**, uno por perfil;
no eran
finalistas ni autorizan VALIDATION/TEST. Una entrega condicional posterior
debe completar stress y chequeos técnicos en TRAIN sin reemplazos antes de
conceder cualquier grant. Este PR solo tiene servicio TRAIN. Sin
preseleccionados, se comunica NO_CANDIDATE.

## Resultado de TRAIN

SCREEN completó **540/540 lotes nativos sin fallos**, en 30 ventanas TRAIN,
con 3.213,61 segundos de cómputo nativo y 150,34 segundos adicionales de
agregación. El informe contiene 576 registros anuales: ninguna variante
superó todos los gates y `preliminary_ids` quedó vacío. El código de salida
1 comunica ese veredicto terminal, no un fallo de los backtests.

Las 24 variantes con reentrada tuvieron un año 2017 inconcluso debido a
fills `force_exit` temporalmente invertidos de la imagen oficial. No se les
atribuye una pérdida ni se corrigen sus operaciones. Las otras 24 variantes
con años evaluables tuvieron **menos de 100 operaciones no forzadas** en
2019–2022; ninguna alcanzó 0,05 % diario neto, ni por tiempo observado ni
normalizado a calendario. El mejor valor calendario fue R002, la regla
SMA50/200 original con riesgo alto, stop 2 % y **sin trailing**:
0,01145 % diario sintético sobre los 1.461 días y 0,01325 % en días
observados. Tuvo 83 operaciones, 2/4 años positivos y DD TRAIN 6,50 %;
el buy-and-hold comparable registró 0,02249 % diario calendario.

Con la misma entrada y riesgo, R005 con trailing registró -0,00030 % diario
calendario y DD 3,65 %: limitó caídas pero recortó también beneficio.
De 12 parejas comparables con ambos años evaluables, trailing mejoró la tasa
calendario en tres y redujo el drawdown en las doce. Las 12 parejas restantes
no permiten esa comparación porque sus variantes con reentrada quedaron
inconclusas. No hay finalistas, grants ni acceso a VALIDATION/TEST, y
no se activó ningún bot. No usar el objetivo de 0,05 % para cambiar
retroactivamente el umbral ni proclamar un 20 % anual real.

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

Los tests de contratos se
ejecutan en el host (con skips de pandas/Freqtrade) y la imagen fijada; la
comparación nativa batch/individual, que requiere consultar metadatos
públicos de Binance, se ejecuta aparte en el perfil de investigación.

Como motivación, en la campaña previa 22 de los 89 trades nativos de V020
en 2019–2022 alcanzaron al menos +2,5 % bruto de precio y terminaron con
pérdida neta a coste de 0,2 % por lado. El máximo nativo de cada trade no
determina cuánto se habría vendido usando trailing: hay que volver a
simular la regla completa con velas detalladas, fees y ejecución adversa.
Véanse la [guía oficial de trailing](https://www.freqtrade.io/en/stable/stoploss/#trailing-stop-loss-only-once-the-trade-has-reached-a-certain-offset)
y los [callbacks de stop](https://www.freqtrade.io/en/stable/strategy-callbacks/#custom-stoploss).
