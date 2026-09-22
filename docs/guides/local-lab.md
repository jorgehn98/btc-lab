# Guía del laboratorio local

Esta guía opera el smoke de la [base BTC lab](../../README.md#objetivo). El histórico y el ledger tienen una [guía separada](history.md), y el baseline una [guía separada](baseline.md), para no mezclar datos, DB, inputs ni timers. El baseline permanece pausado mientras avanza la investigación. Ejecuta los comandos desde una shell de la raíz del proyecto, que es el árbol de trabajo Git, y conserva las rutas absolutas: el espacio de `Crypto Trading Bot` debe ir entre comillas.

## Límites de esta fase

Esta guía cubre el proceso `NoTradeSmoke` sobre BTC/USDT spot en `dry_run`, con `5m`, sin credenciales, API ni Telegram. La estrategia devuelve siempre señales de entrada y salida a cero y bloquea cualquier entrada; no es rentable por diseño, no es un forward test y no produce evidencia económica. No se deben calcular métricas de retorno con sus cero trades. El baseline experimental, actualmente pausado, solo se opera mediante [su guía separada](baseline.md) y una decisión explícita.

El objetivo del proyecto es reunir evidencia reproducible para rechazar estrategias después de costes y riesgo. La campaña actual y sus particiones están en la [guía del histórico](history.md); esta guía no abre `VALIDATION` ni `TEST`. No se publica ningún adjunto privado.

## Variables y Docker

Usa una shell con el grupo efectivo `docker`:

```sh
id -Gn
```

La salida debe incluir `docker`. Si la sesión actual todavía no lo carga, abre una subshell con `sg docker` y vuelve a definir las variables allí; no cambies permisos del socket:

```sh
sg docker
```

Define las raíces absolutas una vez por shell y crea los directorios persistentes antes del primer preflight:

```sh
export LAB_CODE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot"
export LAB_STORAGE_ROOT="/home/jorgextech/dev/personal/Crypto Trading Bot/storage"
mkdir -p "$LAB_STORAGE_ROOT/runs/inputs" "$LAB_STORAGE_ROOT/runtime/smoke" "$LAB_STORAGE_ROOT/backups"
```

El contenedor usa `/lab-storage` para la raíz de storage y ejecuta como UID/GID `1000:1000`. Solo monta `operations/`, `configs/` y `strategies/` como código de solo lectura; no monta la raíz del proyecto, su Git ni el storage completo. El storage persistente permanece en `LAB_STORAGE_ROOT` y se monta únicamente en los subdirectorios operativos declarados por Compose. Los montajes de proyecto llevan la etiqueta SELinux `z`; Fedora permanece en enforcing. Si aparece un aviso cosmético de `sudo chown` del upstream, no desactives el hardening ni `no-new-privileges`: los permisos correctos del UID 1000 son la solución operativa.

La imagen de esta entrega es exactamente:

```text
freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4
```

Comprueba la configuración efectiva sin arrancar servicios:

```sh
docker compose --profile smoke config
```

Los límites de hilos `1` son parte permanente del contrato de Compose. Un contenedor ya creado los recibe al recrearse; un arranque nuevo usa el contrato vigente.

## Preflight, versión y smoke

En una máquina nueva, descarga primero la imagen fijada:

```sh
docker pull "freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
```

`prepare-smoke` exige un árbol de trabajo Git limpio y que la imagen fijada esté disponible localmente. También verifica la configuración, la estrategia y los hashes de `launch.py` y `health.py`:

```sh
python3 -m operations.launch prepare-smoke \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --image "freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
```

Solo `prepare-smoke` acepta `--code-root`/`--storage-root` (o sus variables de host). El subcomando `smoke` no acepta flags ni variables para redirigir esas raíces: dentro del contenedor usa siempre `/opt/btc-lab` y `/lab-storage`.

Comprueba la versión del motor sin mercado ni `up` genérico:

```sh
docker compose --profile tools run --rm engine
```

Para iniciar un smoke nuevo, prepara el input una sola vez y fija la ruta devuelta en la shell. No uses un alias mutable ni vuelvas a ejecutar `prepare-smoke` al reiniciar:

```sh
smoke_input="$(python3 -m operations.launch prepare-smoke \
  --code-root "$LAB_CODE_ROOT" \
  --storage-root "$LAB_STORAGE_ROOT" \
  --image "freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4")" || exit $?
test -f "$smoke_input" || exit 1
export LAB_SMOKE_INPUT="$smoke_input"
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

Cada run comienza como `RUNNING`. Una señal `SIGTERM`/`SIGINT` recibida por el launcher y reenviada al hijo termina como `CANCELLED`; un crash o código de salida no cero termina como `FAILED`. Si el proceso muere con `SIGKILL`, el manifiesto puede quedar `RUNNING` e incompleto: nunca se interpreta como éxito.

Para una pausa normal conserva el estado y detén el contenedor:

```sh
docker stop --timeout 30 btc-lab-smoke-1
```

Para reanudar el mismo contenedor, sin Compose, variables ni regenerar el input:

```sh
docker start btc-lab-smoke-1
```

Si hay que recrear el contenedor, reutiliza el manifiesto congelado que ya está montado. Con las variables de la sección anterior definidas, recupera la ruta y fuerza la recreación; no ejecutes `prepare-smoke` de nuevo mientras config, estrategia y hashes de `launch.py` y `health.py` sigan siendo los mismos:

```sh
export LAB_SMOKE_INPUT="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination \"/lab-storage/active-input.json\"}}{{.Source}}{{end}}{{end}}' btc-lab-smoke-1)"
test -n "$LAB_SMOKE_INPUT" && test -f "$LAB_SMOKE_INPUT"
docker compose --profile smoke up -d --force-recreate smoke
```

Compose declara `restart: "on-failure:3"`: solo limita reintentos cuando el daemon arranca el servicio por un fallo. No es un arranque 24/7 ni arranca el bot en boot. Si necesitas una shell nueva para una operación Compose, recupera primero exactamente el manifiesto montado:

```sh
export LAB_SMOKE_INPUT="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination \"/lab-storage/active-input.json\"}}{{.Source}}{{end}}{{end}}' btc-lab-smoke-1)"
test -n "$LAB_SMOKE_INPUT" && test -f "$LAB_SMOKE_INPUT"
```

## Backup y restauración segura de SQLite

No copies `tradesv3.dryrun.sqlite` mientras el motor pueda escribirla. Primero detén el contenedor y verifica que terminó; el siguiente procedimiento usa la API `sqlite3.Connection.backup` de la biblioteca estándar, escribe en `storage/backups/` y restaura en una ruta nueva sin tocar la DB original.

```sh
docker stop --timeout 30 btc-lab-smoke-1
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

La restauración solo se verifica en `verify-tradesv3.dryrun.sqlite`, una ruta nueva. La comprobación inicial ya se realizó en `storage/backups/smoke-initial.sqlite` y `storage/backups/smoke-restore-check.sqlite`, ambas con `integrity_check=ok`. No borres, reemplaces ni sobrescribas la DB activa. El backup en el mismo disco protege ante errores operativos, no ante pérdida del disco; aún no existe una réplica externa decidida.

## Timer de usuario y límites de disponibilidad

Las units versionadas son `operations/systemd/btc-lab-health.service` y `.timer`: una comprobación oneshot cada cinco minutos, `TimeoutStartSec=30`, journald y sin restart automático. El timer smoke debe permanecer desactivado mientras se mantenga pausado el baseline o exista otro perfil activo; antes de reactivarlo, vuelve a enlazar las units desde `~/.config/systemd/user`. El grupo `docker` efectivo para systemd está verificado y `Linger=no`. No se debe habilitar linger, cambiar suspensión ni convertirlo en un servicio global. Sin linger, el timer solo funciona mientras la sesión de usuario y el PC estén disponibles.

Activa el timer solo como operación explícita y considera la activación confirmada únicamente después de revisar `status`, journal y el JSON de health:

```sh
systemctl --user link "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.service" \
  "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.timer"
systemctl --user daemon-reload
systemctl --user enable --now btc-lab-health.timer
systemctl --user start btc-lab-health.service
systemctl --user status btc-lab-health.timer --no-pager
systemctl --user status btc-lab-health.service --no-pager
journalctl --user -u btc-lab-health.service --since "10 minutes ago" --no-pager
test -f "$LAB_STORAGE_ROOT/health.json" && python3 -m json.tool "$LAB_STORAGE_ROOT/health.json"
```

Para detenerlo de forma reversible antes de activar baseline o cambiar módulos,
desactiva el timer de usuario y conserva los ficheros versionados:

```sh
systemctl --user disable --now btc-lab-health.timer
systemctl --user reset-failed btc-lab-health.service
```

`disable --now` puede retirar los enlaces de las units. Antes de volver a
habilitar el smoke, vuelve a enlazar las plantillas, recarga systemd y ejecuta
`enable --now`:

```sh
systemctl --user link "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.service" \
  "$LAB_CODE_ROOT/operations/systemd/btc-lab-health.timer"
systemctl --user daemon-reload
systemctl --user enable --now btc-lab-health.timer
```

## Estado de Git

El código se publica en el repositorio propio [btc-lab](https://github.com/jorgehn98/btc-lab), no en un fork. El trabajo operativo se hace desde la raíz del proyecto, en la rama `btc-lab-foundation`, y la publicación se integra mediante una PR contra `main`; no se usa ningún worktree enlazado adicional. Los cambios del árbol de trabajo deben ser revisables y `prepare-smoke` rechazará un árbol sucio.

`storage/`, `.engram/`, `work/`, `.env` y cualquier secreto permanecen locales y no se publican. La configuración mantiene API y Telegram desactivados; los placeholders públicos, incluido el JWT de ejemplo, no son credenciales reales. La activación del timer no se considera ejecutada hasta confirmar sus estados, journal y JSON de health.
