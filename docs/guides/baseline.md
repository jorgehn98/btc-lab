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
`0,026`, y el límite del motor. Si faltan saldo, precio o datos válidos, o el
mínimo del exchange supera el cap, se rechaza la entrada. La fee base es `0,1%`
por lado; la reserva del cap es conservadora y no estima slippage.

## TRAIN y metodología

TRAIN es únicamente BTC/USDT spot de Binance público, `5m`, en el intervalo UTC
semiabierto `[2018-01-01, 2023-01-01)`. No se descargan VALIDATION, TEST ni la
reserva 2026. La conversión a `1h` usa solo grupos completos de 12 velas de `5m`
alineadas; no rellena velas. Los segmentos horarios contiguos se evalúan por
separado, con warmup de 51 velas dentro del segmento y cierres etiquetados en la
frontera. No se seleccionan segmentos ni parámetros por sus retornos ni se suman
sus retornos como si fueran una cartera continua.

La evaluación usa fees equivalentes fijas de `0`, `5`, `10` y `20` puntos básicos
adicionales por lado, con el sizing preregistrado. El informe conserva trades,
retorno, drawdown, costes, exposición y límites del motor, junto con efectivo y
buy-and-hold comparable por segmento. Cero trades o una métrica indefinida es
`INCONCLUSIVE`, no éxito económico. La imagen consulta metadata pública actual del
exchange; esa metadata no es un histórico verificado.

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
download_manifest="$(printf '%s\n' "$download_manifest" | tail -n 1)"
snapshot_manifest="$(docker compose --profile research run --rm research-data snapshot \
  --download "$(basename "$download_manifest")")" || exit $?
snapshot_manifest="$(printf '%s\n' "$snapshot_manifest" | tail -n 1)"
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
`research/sessions/`. No copies datos a Git ni presentes una sesión en curso como
`PASS`.

## Baseline dry-run aislado

El perfil baseline es una excepción cerrada al perfil técnico smoke, no una
selección genérica. Prepara un input nuevo después de revisar los gates:

```sh
export LAB_BASELINE_INPUT="$(python3 -m operations.launch prepare-baseline \
  --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT")" || exit $?
test -f "$LAB_BASELINE_INPUT" || exit 1
docker compose --profile baseline config --quiet
docker compose --profile baseline up -d baseline
```

Usa `SmaCrossBaseline`, una DB `tradesv3.baseline.dryrun.sqlite` y logs propios
en `runtime/baseline`. Mantiene UID/GID `1000:1000`, rootfs de solo lectura,
sin capacidades, API, Telegram, credenciales ni puertos; limita el servicio a
1 CPU y 2 GiB. El smoke debe permanecer detenido mientras baseline sea el perfil
activo.

```sh
python3 -m operations.health --container btc-lab-baseline-1 \
  --logfile "$LAB_STORAGE_ROOT/runtime/baseline/freqtrade.log" \
  --output "$LAB_STORAGE_ROOT/health-baseline.json" \
  --lock "$LAB_STORAGE_ROOT/health-baseline.lock"
```

El timer propio es `operations/systemd/btc-lab-baseline-health.timer`, una
comprobación oneshot cada cinco minutos. Habilítalo solo después de revisar estado,
journal y JSON de salud:

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
docker stop --timeout 30 btc-lab-baseline-1
```

Un rollback conserva DB y manifiestos baseline; reactivar smoke requiere su input
nuevo si cambió cualquiera de sus módulos hasheados. Una parada con posición
simulada no se presenta como una venta ejecutada.
