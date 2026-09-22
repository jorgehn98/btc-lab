# Baseline experimental BTC/USDT

Esta guía opera el perfil cerrado `baseline` y su investigación TRAIN. No activa
trading real ni convierte `SmaCrossBaseline` en una estrategia validada. Los datos
descargados, snapshots, sesiones, logs y métricas son artefactos locales: no se
publican ni se usan para afirmar rentabilidad privada.

## Hipótesis y límites

`SmaCrossBaseline` v1 calcula SMA20 y SMA50 con velas cerradas de `1h` UTC. Entra
solo cuando el cruce pasa de `SMA20 <= SMA50` a `SMA20 > SMA50`; sale con el cruce
inverso o con un stop fijo del 2%. Solo permite una posición long, sin short,
apalancamiento, trailing, ROI, DCA ni ajuste de posición. Las órdenes son market
simuladas. Un stop no garantiza una pérdida máxima: un gap o un fallo de ejecución
puede superarla.

El capital inicial ficticio es `10.000 USDT`. El cap de entrada es el mínimo de
`1.000 USDT`, `10%` del capital disponible, `0,25%` del capital dividido por
`0,026`, y el límite del motor. Con el ratio disponible por defecto de `0,99`,
el capital disponible inicial es `9.900 USDT` y el cap teórico es aproximadamente
`951,92 USDT` antes de redondeos; no se debe tratar como un importe garantizado.
Si faltan saldo, precio o datos válidos, o el mínimo del exchange supera el cap,
se rechaza la entrada. La reserva del cap es conservadora y no estima slippage.

## TRAIN y metodología

TRAIN es únicamente BTC/USDT spot de Binance público, `5m`, en el intervalo UTC
semiabierto `[2018-01-01, 2023-01-01)`. No se descargan VALIDATION, TEST ni la
reserva 2026. La conversión a `1h` usa solo grupos completos de 12 velas de `5m`
alineadas; no rellena velas. Los segmentos horarios contiguos se evalúan por
separado, con warmup de 51 velas dentro del segmento y cierres etiquetados en la
frontera. No se seleccionan segmentos ni parámetros por sus retornos ni se suman
sus retornos como si fueran una cartera continua.

La evaluación usa tasas totales fijas por lado de `0,1%`, `0,15%`, `0,2%` y
`0,3%` (fees equivalentes de `0`, `5`, `10` y `20` puntos básicos adicionales),
con el sizing preregistrado. Cada resultado debe conservar la tasa y el coste de
fees en USDT cuando el informe nativo publica los componentes necesarios; si no,
el campo queda `null` con su `null_reason`. La exposición también queda `null`
si el informe nativo no publica ese dato: no se infiere. El benchmark usa el mismo
fee por lado, el `open` de la primera vela efectiva y el `open` de la última vela
efectiva, que coincide con el cierre de la referencia del motor; es aproximado y
usa el detalle `5m`. Cero trades o una métrica indefinida es `INCONCLUSIVE`, no
éxito económico. La imagen consulta metadata pública actual del exchange; esa
metadata no es un histórico verificado.

El análisis de sesgo usa el segmento contiguo más largo, con desempate cronológico.
El gate requiere al menos 5 trades cerrados y cobertura de entrada y salida por
cruce; el gate recursive requiere al menos 1.000 velas y startup `51/100/200/400`.
Muestra insuficiente es `INCONCLUSIVE`; diferencia fuera de tolerancia, señal
distinta o sesgo es `FAIL`. Solo un gate explícitamente `PASS` permite considerar
el siguiente paso, y aun así no demuestra ventaja económica.

## Preparación local

Define las raíces y crea todos los directorios como usuario antes de Compose:

```sh
export LAB_CODE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot"
export LAB_STORAGE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/storage"
mkdir -p "$LAB_STORAGE_ROOT/research/inputs" \
  "$LAB_STORAGE_ROOT/research/downloads" "$LAB_STORAGE_ROOT/research/snapshots" \
  "$LAB_STORAGE_ROOT/research/sessions" "$LAB_STORAGE_ROOT/research/control" \
  "$LAB_STORAGE_ROOT/runs/inputs" "$LAB_STORAGE_ROOT/runs" \
  "$LAB_STORAGE_ROOT/runtime/baseline"
```

Research no monta Git, memoria, la raíz del proyecto ni el estado del bot.
`research-data` escribe solo en `downloads`, `snapshots` y `control`; `research`
lee snapshots en solo lectura y escribe sesiones y el lock compartido. Comprueba
el CLI real de la imagen fijada:

```sh
docker compose --profile research run --rm research-data --help
docker compose --profile research run --rm research --help
```

## Input e investigación reproducible

`prepare` corre en el host, exige el árbol Git limpio y la imagen fijada, y crea
un input único con hashes de configuración, estrategia, launcher, health, market y
research. Tras cualquier cambio en esos módulos hay que preparar un input nuevo;
nunca se editan hashes para reutilizar uno viejo.

```sh
export LAB_RESEARCH_INPUT="$(python3 -m operations.research prepare \
  --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT")" || exit $?
test -f "$LAB_RESEARCH_INPUT" || exit 1

download_manifest="$(docker compose --profile research run --rm research-data download)" || exit $?
test -n "$download_manifest" || exit 1
snapshot_manifest="$(docker compose --profile research run --rm research-data snapshot \
  --download "$(basename "$download_manifest")")" || exit $?
test -n "$snapshot_manifest" || exit 1
```

El snapshot queda `FROZEN` con hashes del conjunto completo y de cada segmento.
Evalúa siempre ese snapshot por nombre explícito:

```sh
docker compose --profile research run --rm research backtest \
  --snapshot "$(basename "$snapshot_manifest")"
docker compose --profile research run --rm research bias \
  --snapshot "$(basename "$snapshot_manifest")"
```

Revisa `session.json`, `gate.json`, logs y resultados nativos bajo
`research/sessions/`. Distingue dos contratos: una sesión de ejecución puede
terminar `SUCCEEDED` aunque la evaluación quede `PARTIAL` o `INCONCLUSIVE`.
`PARTIAL` significa que hubo resultados nativos correctos pero al menos uno fue
no evaluable, por ejemplo cero trades; no es un fallo técnico. El comando puede
devolver exit `1` intencionalmente para no confundir una evaluación incompleta con
un resultado completo. Un fallo técnico nativo queda `FAILED`.

Un gate terminado nunca debe quedar en `RUNNING`: su estado terminal es `PASS`,
`FAIL` o `INCONCLUSIVE`, mapeado en el manifiesto a `SUCCEEDED`, `FAILED` o
`INCONCLUSIVE`, respectivamente. No copies datos a Git ni presentes una sesión en
curso como `PASS`.

### Resumen TRAIN disponible

El snapshot local contiene `524.293` velas de `5m`, `43.679` velas de `1h` y
30 segmentos, de los que 27 son elegibles. Se ejecutaron 108 backtests: 8 no
tuvieron trades y quedan `evaluation_status=INCONCLUSIVE`; los demás no tuvieron
fallo nativo. Estos estados describen la evaluación de cada segmento y no autorizan
a sumar retornos entre segmentos.

En el segmento más largo, el rango efectivo es `2021-10-01 12:00 UTC` a
`2022-12-31 23:00 UTC`, con 133 trades de referencia. Los PnL publicados para las
cuatro tasas anteriores fueron, respectivamente, `-518,94`, `-638,14`, `-755,82`
y `-986,77 USDT` sobre 10.000 USDT. Este resultado corresponde solo a ese segmento,
no a todo TRAIN y no demuestra rentabilidad.

El gate de sesgo terminó `PASS`: la referencia tuvo 133 trades, de los que 132
fueron analizables tras excluir un `force_exit`; se comprobaron 260 eventos con
ventanas `51/100/200/400` sin cambios de SMA ni señales. El `force_exit` se excluye
del conteo de cobertura del análisis, no se presenta como salida por señal.
La evidencia cruda permanece en `storage/research/`, fuera de Git; esta guía no
publica CSV, DB ni logs completos.

## Baseline dry-run aislado

El perfil baseline es una excepción cerrada al perfil técnico smoke, no una
selección genérica. Prepara un input nuevo después de revisar los gates:

```sh
baseline_input="$(python3 -m operations.launch prepare-baseline \
  --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT")" || exit $?
test -f "$baseline_input" || exit 1
export LAB_BASELINE_INPUT="$baseline_input"
systemctl --user disable --now btc-lab-health.timer
docker compose --profile baseline config --quiet
docker compose --profile baseline up -d baseline
```

Usa `SmaCrossBaseline`, una DB `tradesv3.baseline.dryrun.sqlite` y logs propios
en `runtime/baseline`. Mantiene UID/GID `1000:1000`, rootfs de solo lectura,
sin capacidades, API, Telegram, credenciales ni puertos; limita el servicio a
1 CPU y 2 GiB. El baseline está activado como experimento `dry_run`; el smoke y
su timer deben permanecer detenidos mientras baseline sea el perfil activo. El
baseline espera señales del mercado y no fuerza trades para demostrar actividad.

```sh
python3 -m operations.health --container btc-lab-baseline-1 \
  --logfile "$LAB_STORAGE_ROOT/runtime/baseline/freqtrade.log" \
  --output "$LAB_STORAGE_ROOT/health-baseline.json" \
  --lock "$LAB_STORAGE_ROOT/health-baseline.lock"
```

El timer propio es `operations/systemd/btc-lab-baseline-health.timer`, una
comprobación oneshot cada cinco minutos y es el timer activo del baseline.
Habilítalo solo después de revisar estado, journal y JSON de salud:

```sh
systemctl --user link "$LAB_CODE_ROOT/operations/systemd/btc-lab-baseline-health.service" \
  "$LAB_CODE_ROOT/operations/systemd/btc-lab-baseline-health.timer"
systemctl --user daemon-reload
systemctl --user enable --now btc-lab-baseline-health.timer
systemctl --user status btc-lab-baseline-health.timer --no-pager
```

## Pausa, cambios y rollback

Antes de cambiar un módulo hasheado, detén baseline y su timer; si afecta al
smoke, detén también smoke y su timer. Tras cualquier cambio crea un input nuevo.
Si cambias de rama, comprueba los paths montados y ejecuta
`docker compose --profile baseline config --quiet` antes de recrear.

```sh
systemctl --user disable --now btc-lab-baseline-health.timer
systemctl --user disable --now btc-lab-health.timer
docker stop --timeout 30 btc-lab-baseline-1
```

Un rollback conserva DB y manifiestos baseline; reactivar smoke requiere su input
nuevo si cambió cualquiera de sus módulos hasheados. `disable --now` puede retirar
los enlaces de las units de usuario; antes de volver a habilitar un timer hay que
volver a enlazar la plantilla correspondiente, recargar systemd y entonces ejecutar
`enable --now`.

```sh
systemctl --user link "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.service" \
  "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.timer"
systemctl --user daemon-reload
systemctl --user enable --now btc-lab-health.timer
```

Si el rollback mantiene baseline como perfil activo, enlaza sus units y habilita
su timer en lugar del timer smoke:

```sh
systemctl --user link "$LAB_CODE_ROOT/operations/systemd/btc-lab-baseline-health.service" \
  "$LAB_CODE_ROOT/operations/systemd/btc-lab-baseline-health.timer"
systemctl --user daemon-reload
systemctl --user enable --now btc-lab-baseline-health.timer
```

Una parada con posición simulada no se presenta como una venta ejecutada.
