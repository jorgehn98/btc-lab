# Guía del laboratorio local

Esta guía opera el smoke de la [base BTC lab](../../README.md#objetivo). Ejecuta los comandos desde una shell del worktree y conserva las rutas absolutas: el espacio de `Crypto Trading Bot` debe ir entre comillas.

## Límites de esta fase

El único proceso operativo es `NoTradeSmoke` sobre BTC/USDT spot en `dry_run`, con `5m`, sin credenciales, API ni Telegram. La estrategia devuelve siempre señales de entrada y salida a cero y bloquea cualquier entrada; no es rentable por diseño, no es un forward test y no produce evidencia económica. No se deben calcular métricas de retorno con sus cero trades.

El objetivo del proyecto es reunir evidencia reproducible para rechazar estrategias después de costes y riesgo. El siguiente estudio usa solo `TRAIN` (2018–2022 en UTC); no adelanta `VALIDATION` ni el `TEST` sellado. No se publica ningún adjunto privado.

## Variables y Docker

Usa una shell con el grupo efectivo `docker`:

```sh
id -Gn
```

La salida debe incluir `docker`. Si la sesión actual todavía no lo carga, abre una subshell con `sg docker` y vuelve a definir las variables allí; no cambies permisos del socket:

```sh
sg docker
```

Define las raíces absolutas una vez por shell:

```sh
export LAB_CODE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/worktrees/btc-lab-foundation"
export LAB_STORAGE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/storage"
```

El contenedor usa `/lab-storage` para la raíz de storage y ejecuta como UID/GID `1000:1000`. Los montajes de proyecto llevan la etiqueta SELinux `z`; Fedora permanece en enforcing. Si aparece un aviso cosmético de `sudo chown` del upstream, no desactives el hardening ni `no-new-privileges`: los permisos correctos del UID 1000 son la solución operativa.

La imagen de esta entrega es exactamente:

```text
freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4
```

Comprueba la configuración efectiva sin arrancar servicios:

```sh
docker compose --profile smoke config
```

El cambio de límites de hilos a `1` ya está en Compose, pero requiere recrear el contenedor para aplicarse al proceso existente. Déjalo para el próximo arranque/recreación; no cambies el runtime activo en esta guía.

## Preflight, versión y smoke

`prepare-smoke` exige un worktree Git limpio y que la imagen fijada esté disponible localmente. También verifica la configuración, la estrategia y los hashes de `launch.py` y `health.py`:

```sh
python3 -m operations.launch prepare-smoke \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --image "freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
```

Comprueba la versión del motor sin mercado ni `up` genérico:

```sh
docker compose --profile tools run --rm engine
```

Para iniciar un smoke nuevo, prepara el input una sola vez y fija la ruta devuelta en la shell. No uses un alias mutable ni vuelvas a ejecutar `prepare-smoke` al reiniciar:

```sh
export LAB_SMOKE_INPUT="$(python3 -m operations.launch prepare-smoke \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --image "freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4")"
docker compose --profile smoke up -d smoke
```

El manifiesto queda en `storage/runs/inputs/` y se monta como el único fichero `/lab-storage/active-input.json`, solo lectura y sin `create_host_path`. `runs/inputs/` también es solo lectura dentro del servicio. Cada arranque genera su propio manifiesto de run bajo `storage/runs/`; el input congelado no se regenera.

## Salud, logs y paradas

La comprobación de salud corre en el host, no reinicia el contenedor y solo informa `healthy` con un heartbeat reciente, estado `running`, sin OOM, sin reinicios y con espacio suficiente:

```sh
python3 -m operations.health \
  --container btc-lab-smoke-1 \
  --logfile "$LAB_STORAGE_ROOT/runtime/smoke/freqtrade.log" \
  --output "$LAB_STORAGE_ROOT/health.json" \
  --lock "$LAB_STORAGE_ROOT/health.lock"
```

Consulta fallos en el JSON de salud, en el log de Freqtrade y en los manifiestos de `storage/runs/`. Un estado no `healthy` tiene salida no cero y debe investigarse; health no hace `restart` automático.

Rutas persistentes principales:

| Ruta | Contenido |
| --- | --- |
| `storage/runtime/smoke/tradesv3.dryrun.sqlite` | DB SQLite del smoke |
| `storage/runtime/smoke/freqtrade.log` | log del motor |
| `storage/runs/inputs/` | manifiestos de input congelados |
| `storage/runs/run-*.json` | resultado de cada arranque |
| `storage/health.json`, `storage/health.lock` | última salud y lock |

Para una pausa normal conserva el estado y detén el contenedor:

```sh
docker stop btc-lab-smoke-1
```

Para reanudar el mismo contenedor, sin Compose, variables ni regenerar el input:

```sh
docker start btc-lab-smoke-1
```

Compose declara `restart: "on-failure:3"`: solo limita reintentos cuando el daemon arranca el servicio por un fallo. No es un arranque 24/7 ni arranca el bot en boot. Si necesitas una shell nueva para una operación Compose, recupera primero exactamente el manifiesto montado:

```sh
export LAB_SMOKE_INPUT="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination \"/lab-storage/active-input.json\"}}{{.Source}}{{end}}{{end}}' btc-lab-smoke-1)"
test -n "$LAB_SMOKE_INPUT" && test -f "$LAB_SMOKE_INPUT"
```

## Backup y restauración segura de SQLite

No copies `tradesv3.dryrun.sqlite` mientras el motor pueda escribirla. Primero detén el contenedor y verifica que terminó; el siguiente procedimiento usa la API `sqlite3.Connection.backup` de la biblioteca estándar, escribe en `storage/backups/` y restaura en una ruta nueva sin tocar la DB original.

```sh
docker stop btc-lab-smoke-1
export DB="$LAB_STORAGE_ROOT/runtime/smoke/tradesv3.dryrun.sqlite"
export BACKUP="$LAB_STORAGE_ROOT/backups/tradesv3.dryrun.sqlite"
export RESTORE="$LAB_STORAGE_ROOT/backups/verify-tradesv3.dryrun.sqlite"
python3 - <<'PY'
import os
import sqlite3
from pathlib import Path

db = Path(os.environ["DB"])
backup = Path(os.environ["BACKUP"])
restore = Path(os.environ["RESTORE"])
backup.parent.mkdir(parents=True, exist_ok=True)
if not db.is_file():
    raise SystemExit(f"DB no encontrada: {db}")
if backup.exists():
    raise SystemExit(f"el backup ya existe, elige otra ruta: {backup}")
if restore.exists():
    raise SystemExit(f"la ruta de verificación ya existe, elige otra: {restore}")
with sqlite3.connect(db) as source, sqlite3.connect(backup) as target:
    source.backup(target)
    target.execute("PRAGMA integrity_check")
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("integrity_check falló en el backup")
with sqlite3.connect(backup) as source, sqlite3.connect(restore) as target:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("integrity_check falló en la restauración")
print(f"backup verificado: {backup}")
print(f"restauración verificada: {restore}")
PY
```

La restauración solo se verifica en `verify-tradesv3.dryrun.sqlite`, una ruta nueva. No borres, reemplaces ni sobrescribas la DB activa. El backup en el mismo disco protege ante errores operativos, no ante pérdida del disco; aún no existe una réplica externa decidida.

## Timer de usuario y límites de disponibilidad

Las units versionadas son `operations/systemd/btc-lab-health.service` y `.timer`: una comprobación oneshot cada cinco minutos, `TimeoutStartSec=30`, journald y sin restart automático. El timer todavía **no está instalado**; T05 hará la instalación tras verificar la sesión y el grupo efectivo. No se debe habilitar linger, cambiar suspensión ni convertirlo en un servicio global. Sin linger, el timer solo funciona mientras la sesión de usuario y el PC estén disponibles.

La instalación futura debe conservar las rutas absolutas con espacios y comprobar primero el grupo `docker`. Para desinstalarla de forma reversible, desactiva el timer de usuario y conserva los ficheros versionados:

```sh
systemctl --user disable --now btc-lab-health.timer
systemctl --user reset-failed btc-lab-health.service
```

## Estado de Git

El laboratorio es local y no tiene remoto configurado en esta entrega. No hagas `push` ni inventes un destino remoto. Los cambios del worktree deben ser revisables y `prepare-smoke` rechazará un árbol sucio. La habilitación del timer y la verificación operativa desde otra sesión quedan pendientes para T05.
