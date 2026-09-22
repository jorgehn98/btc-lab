# Búsqueda cerrada de estrategias

Esta guía describe la campaña `btc-strategy-search-pr02`. Es una búsqueda
experimental reproducible sobre BTC/USDT spot; no es una promesa de rentabilidad,
un óptimo global ni una autorización para activar un servicio. `smoke` y
`baseline` permanecen detenidos mientras la campaña está pendiente de decisión.

## Alcance congelado

El registry contiene 72 variantes reales: 3 familias, 4 semillas y 2 stops por
cada una de 3 configuraciones de riesgo. La configuración de búsqueda es cerrada:
Binance público, `BTC/USDT`, spot, `dry_run`, `1h`, una posición long como máximo,
`stake_amount=unlimited`, API y Telegram desactivados y sin credenciales.

La campaña usa un presupuesto wall-clock de 12 horas y procesa lotes nativos de
hasta seis estrategias. El coste se evalúa con tasas por lado de `0,1%`, `0,15%`,
`0,2%` y `0,3%`; el stress usa `0,15%` y `0,3%`. El límite histórico es un
drawdown MTM de `15%`, calculado por episodio. Se conserva también el control
fijo `ControlSmaCrossBaseline`; no se suman episodios como una cartera continua
ni se presenta un CAGR global.

El criterio de selección usa ventanas por año y segmento, ponderación por tiempo
de los episodios, al menos 30 operaciones cerradas en la suma de prefijos, cobertura
TRAIN 2019–2022 y al menos 3 de 4 años, drawdown dentro del límite en todo TRAIN,
vecinos fijos, y comprobaciones de coste y sesgo. La salida de una fase es un
artefacto verificable, no un ganador económico por sí mismo.

## Datos y particiones

El snapshot físico TRAIN disponible para esta campaña procede del histórico
`5m` de Binance público, con el intervalo `[2017-08-17T04:00:00Z,
2023-01-01T00:00:00Z)`. Su caché local registra 563.597 velas de `5m` y 46.952
velas derivadas de `1h`. Las cifras son metadatos agregados; los datos, logs,
sesiones y manifiestos permanecen en `storage/` y no se publican.

Las particiones cerradas son:

| Rol | Intervalo UTC | Estado de campaña |
| --- | --- | --- |
| TRAIN | `2017-08-17T04:00:00Z` → `2023-01-01T00:00:00Z` | snapshot congelado |
| VALIDATION | `2023-01-01T00:00:00Z` → `2025-01-01T00:00:00Z` | requiere `prepare-phase` |
| TEST | `2025-01-01T00:00:00Z` → `2026-09-22T00:00:00Z` | requiere reserva y consumo único |

El corte TEST es el límite predeclarado de la campaña y no equivale a afirmar
que exista ya un dataset TEST descargado. El estado de datos se expresa como
snapshot por rol: no se debe describir el intervalo 2017–2023 como si contuviera
también VALIDATION o TEST.

## Bootstrap y manifiesto único

Ejecuta el bootstrap como el usuario que ejecutará Compose, con las rutas reales
del despliegue:

```sh
export LAB_CODE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot"
export LAB_STORAGE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/storage"
mkdir -p "$LAB_STORAGE_ROOT/history/inputs" \
  "$LAB_STORAGE_ROOT/history/downloads" "$LAB_STORAGE_ROOT/history/snapshots" \
  "$LAB_STORAGE_ROOT/history/sessions" "$LAB_STORAGE_ROOT/history/control" \
  "$LAB_STORAGE_ROOT/search/inputs" "$LAB_STORAGE_ROOT/search/generated" \
  "$LAB_STORAGE_ROOT/search/control" "$LAB_STORAGE_ROOT/search/sessions"
```

Prepara TRAIN en el host usando el nombre explícito del snapshot congelado:

```sh
search_input="$(python3 -m operations.search prepare \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --train-snapshot "<MANIFIESTO-TRAIN.json>")" || exit $?
test -f "$search_input" || exit 1
export LAB_SEARCH_INPUT="$search_input"
export LAB_SEARCH_TRAIN_SNAPSHOT="$LAB_STORAGE_ROOT/history/snapshots/<snapshot_dir-de-TRAIN>"
export LAB_SEARCH_TRAIN_MANIFEST="$LAB_STORAGE_ROOT/history/snapshots/<manifiesto-TRAIN.json>"
```

`LAB_SEARCH_INPUT` debe apuntar a ese archivo concreto. No se permite buscar el
input más reciente, editar hashes, usar fechas libres, añadir IDs de fase,
`FREQTRADE__*`, parámetros de riesgo o un campo `force`. `prepare` verifica el
árbol Git, la imagen fijada, el snapshot, el registry de 72 variantes y el source
generado por `research.campaign.render_strategy_module`.

## Fases y reanudación

Los runners reales son subcomandos de `operations.search` dentro de la imagen
fijada:

```sh
docker compose --profile search run --rm search screen
docker compose --profile search run --rm search finalists
docker compose --profile search run --rm search-validation validation
docker compose --profile search run --rm search-test test
docker compose --profile search run --rm search report
docker compose --profile search run --rm search status
docker compose --profile search run --rm search resume --phase screen
```

`screen` evalúa las 72 variantes en TRAIN y congela como máximo nueve. `finalists`
aplica stress y bias nativo y congela como máximo tres. `validation` evalúa como
máximo tres candidatas con las cuatro tasas y prepara la candidata TEST. `test`
evalúa una sola candidata con las cuatro tasas y solo puede escribir el paper
bundle si pasan conjuntamente el gate económico y el técnico. `report` y `status`
son observación reproducible; no autorizan holdouts ni activación. `resume` usa
el estado, los hashes y el grant existentes: no reinicia presupuesto, cambia de
candidata ni convierte una interrupción en éxito.

El estado persistente es `search/control/campaign-btc-strategy-search-pr02.json`.
Los intentos y reportes quedan en `search/sessions/`; un fallo técnico no se
oculta como `INCONCLUSIVE`, y un proceso detenido conserva el presupuesto y la
base de datos local. Si una corrección revisada cambia la definición antes de
congelar TRAIN, `prepare` solo permite continuar cuando todos los intentos fueron
`FAILED` y todavía no existen selecciones, grants ni reportes de fase; archiva el
estado anterior y conserva íntegros intentos, ejecuciones y segundos consumidos.

La verificación actual es de contratos, no de resultados de campaña: 153 tests
se ejecutan en host y en la imagen fijada, y la ruta nativa sintética comprueba
lotes de tres frente a tres ejecuciones individuales y resuelve las 72 clases.
Esto demuestra que el runner puede validar su mecánica; no demuestra que la
búsqueda TRAIN se haya completado ni que exista una candidata.

## Apertura de VALIDATION y TEST

Los helpers puros de autorización no abren un holdout. El coordinator debe usar
`prepare-phase`, primero para crear el grant/input del rol y después para fijar
el snapshot físico por hash:

```sh
validation_prep="$(python3 -m operations.search prepare-phase \
  --phase validation --search-input "$LAB_SEARCH_INPUT" \
  --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT")" || exit $?
```

Con el `history_input` devuelto, se descarga y congela únicamente VALIDATION
usando el runner `history-data`. Después se repite `prepare-phase` con el nombre
simple del manifiesto VALIDATION y el `--grant-id` devuelto para crear el binding
inmutable del snapshot. Antes de ejecutar el runner se exportan manualmente las
rutas reales del manifiesto y del directorio que devuelve ese `--snapshot`:

Los servicios `history-data` y `history` montan únicamente
`search/control/` en `/lab-search-authority/control` como solo lectura. Así
verifican el grant contra el estado y el reporte de fase canónicos; sin esa
autoridad, VALIDATION y TEST fallan cerrados.

```sh
export LAB_HISTORY_INPUT="<history_input-devuelto-por-prepare-phase>"
docker compose --profile history run --rm history-data download
docker compose --profile history run --rm history-data snapshot \
  --download "<manifiesto-download-devuelto>"

export LAB_SEARCH_VALIDATION_SNAPSHOT="$LAB_STORAGE_ROOT/history/snapshots/<snapshot_dir-de-validation>"
export LAB_SEARCH_VALIDATION_MANIFEST="$LAB_STORAGE_ROOT/history/snapshots/<manifiesto-validation.json>"
docker compose --profile search run --rm search-validation validation
```

Para TEST se repite el mismo procedimiento con el `history_input` y el snapshot
TEST devueltos por `prepare-phase --phase test`; se exportan
`LAB_SEARCH_TEST_SNAPSHOT` y `LAB_SEARCH_TEST_MANIFEST` con las rutas host reales
antes de ejecutar `search-test test`:

```sh
export LAB_SEARCH_TEST_SNAPSHOT="$LAB_STORAGE_ROOT/history/snapshots/<snapshot_dir-de-test>"
export LAB_SEARCH_TEST_MANIFEST="$LAB_STORAGE_ROOT/history/snapshots/<manifiesto-test.json>"
docker compose --profile search run --rm search-test test
```

Su primer paso reserva el consumo antes de cualquier descarga o lectura; al
reanudar debe conservar la misma candidata, grant y definición. TRAIN usa del
mismo modo `LAB_SEARCH_TRAIN_SNAPSHOT` y `LAB_SEARCH_TRAIN_MANIFEST`, apuntando
al directorio y manifiesto TRAIN elegidos por `prepare --train-snapshot`.

No se permite abrir TEST con JSON manual, seleccionar el último archivo,
reutilizar una reserva consumida, descargar un rol externo durante TRAIN ni
forzar una fase por un ID libre.

## Estado honesto y publicación

El código permite generar un paper bundle únicamente cuando el resultado TEST
tiene `PASS` económico y técnico. La activación futura sería una decisión manual
del coordinator; no existe un servicio que la active por la existencia del bundle.

Esta documentación no afirma que la búsqueda haya terminado, que exista un
ganador o que no exista uno. Solo debe publicarse un resumen agregado de fases
con estado, cobertura, costes, drawdown y linaje. No se publican CSV, feather,
DB, logs completos, manifiestos con rutas locales ni valores de credenciales.

Referencias: `operations/search.py`, `operations/history.py`,
`market/history.py`, `compose.yaml` y [histórico y ledger](history.md).
