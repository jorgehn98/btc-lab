# BTC lab — instrucciones del repositorio

## Alcance

Laboratorio propio de BTC/USDT spot con Freqtrade en Docker. La entrega contiene
un smoke `dry_run` (`NoTradeSmoke`) y un baseline experimental cerrado
(`SmaCrossBaseline`). El baseline no es una estrategia económica validada ni
trading real; cualquier activación depende de sus gates y de una decisión explícita.
El repositorio no es un fork de Freqtrade; consume su imagen oficial sin modificarla.

## Stack y estructura

- Python: biblioteca estándar para `operations/` y `unittest` para tests.
  No hay gestor de paquetes propio ni dependencias de host que instalar.
- Freqtrade y sus dependencias viven en la imagen fijada por digest en
  `compose.yaml`; el runtime de aceptación es esa imagen, no el Python del host.
- `operations/launch.py`: preflight, validación cerrada, manifiestos y ejecución.
- `operations/health.py`: estado Docker, heartbeat, disco, lock y salida JSON.
- `operations/systemd/`: plantillas del monitor de usuario.
- `configs/smoke.json`: configuración pública cerrada del smoke.
- `configs/baseline.json`: configuración pública cerrada del baseline `1h`.
- `strategies/smoke/NoTradeSmoke.py`: estrategia técnica sin entradas.
- `strategies/baseline/SmaCrossBaseline.py`: cruce SMA20/50 experimental, solo largo.
- `operations/research.py` y `market/train.py`: descarga, snapshot y evaluación TRAIN.
- `tests/test_operations.py`: contratos de seguridad, identidad, señales y salud.
- `docs/guides/local-lab.md`: guía única de operación y recuperación.
- `docs/guides/baseline.md`: guía operativa de investigación y baseline.
- `storage/`: datos persistentes locales, fuera de Git.
- `work/` y `.engram/`: planificación y memoria locales, fuera de Git.

Mantener la organización por capacidad. Reutilizar stdlib y Freqtrade; no añadir
dependencias, servicios, interfaces ni capas sin una necesidad aprobada.

## Raíz de trabajo y Git

Trabajar desde la raíz del proyecto, sin crear worktrees adicionales: es una
decisión explícita del propietario. Conservar los cambios ajenos y el historial.
Las ramas de trabajo se usan en este mismo checkout; no confundir una rama con
un directorio adicional.

Remoto público: <https://github.com/jorgehn98/btc-lab>. Los cambios de código o
comportamiento se publican mediante rama y PR contra `main`. Mantener la PR en
draft mientras pueda cambiar y no fusionarla sin autorización expresa.
Comprobar status, diff e historial antes de commit o push.

Nunca publicar credenciales reales, claves privadas, tokens ni archivos locales.
`.gitignore` excluye `storage/`, `work/`, `.engram/`, `.env`, `.env.*` y `secrets/`;
`.env.example` es la excepción y solo puede contener valores ficticios.
Las exclusiones no protegen archivos ya versionados: revisar también el historial
antes de publicar contenido que pueda contener secretos. No imprimir sus valores.

## Verificación

Ejecutar desde la raíz, entrecomillando todas las rutas porque pueden contener espacios:

```sh
export LAB_CODE_ROOT="$PWD"
export LAB_STORAGE_ROOT="$PWD/storage"
python3 -m unittest discover -s tests -v
docker compose --profile tools --profile smoke config --quiet
```

Tests en el runtime fijado, sin red (el servicio `engine` declara `network_mode: none`):

```sh
docker compose --profile tools run --rm \
  --volume "$PWD/tests:/opt/btc-lab/tests:ro,z" \
  --entrypoint python engine -m unittest discover -s tests -v
```

Los directorios de storage deben existir previamente; ver el bootstrap de la guía.
No hay lint, typecheck ni CI configurados actualmente. No instalar herramientas
para suplirlos sin aprobación ni afirmar que se ejecutaron checks inexistentes.
Para cambios de comportamiento, elegir el test del contrato afectado; para
montajes, señales o systemd, verificar también el comportamiento real acotado.
No añadir tests de texto que solo copien el contenido de Compose o de la documentación.

## Invariantes del runtime

- Solo Binance público, BTC/USDT spot, `dry_run: true`, sin claves de exchange.
  API y Telegram desactivados y sin puertos publicados. El JWT de configuración
  es un placeholder público requerido por el schema, no una credencial utilizable.
- No habilitar live, short, futuros, leverage, otras estrategias ni overrides
  `FREQTRADE__*`. No cambiar la imagen fijada silenciosamente por `stable/latest`.
- `prepare-smoke` y `prepare-baseline` corren en el host: exigen árbol Git limpio,
  imagen disponible y hashes de los módulos del perfil. Devuelven un archivo único;
  no usar alias mutable ni seleccionar el input más reciente.
- `LAB_SMOKE_INPUT` fija ese archivo antes del primer `up`. Dentro del contenedor
  se monta en `/lab-storage/active-input.json` RO con `create_host_path: false`.
  El subdirectorio `runs/inputs/` también es RO sobre `runs/` RW.
- `smoke` usa raíces internas constantes; no acepta flags ni variables para
  redirigirlas. Cada arranque revalida los hashes y crea un manifiesto nuevo.
- `baseline` es la excepción cerrada a la regla de solo smoke: usa únicamente
  `SmaCrossBaseline`, `configs/baseline.json`, sus módulos TRAIN y su DB propia.
  No abrir una selección genérica de estrategias, configuraciones, raíces o parámetros.
- Reanudar un input congelado no autoriza a modificar los archivos que identifica.
  Una versión nueva requiere nuevo preflight; nunca ajustar hashes para ocultar cambios.
- Docker monta solo `operations/`, `configs/`, `strategies/` y `market/` como código RO.
  Research monta además únicamente los directorios de datos que necesita; nunca la
  raíz con Git/memoria/storage completo ni el socket Docker en el contenedor.
- Conservar UID/GID 1000:1000, rootfs RO, capabilities eliminadas,
  `no-new-privileges`, límites de CPU/RAM/hilos y rotación de logs.

## Operación local

El entorno verificado es Linux/Fedora con SELinux enforcing. Usar montajes `z`
en los directorios concretos del proyecto; no desactivar SELinux ni cambiar los
permisos del socket Docker. Las units incluyen rutas del despliegue local: al
trasladarlas a otro equipo, adaptar esas rutas y verificar con `systemd-analyze`.

El servicio `smoke` es optativo (`--profile smoke`); no usar `up` genérico.
Los servicios `baseline` y `research` son optativos (`--profile baseline` y
`--profile research`); no usar `up` genérico ni arrancarlos como sustituto de los gates.
`restart: on-failure:3` no habilita arranque al encender el equipo. El timer de
usuario comprueba salud cada cinco minutos, sin reiniciar el bot; requiere la
sesión de usuario y el PC disponibles. No cambiar linger, suspensión o servicios
globales sin autorización. Los comandos completos están en la guía operativa.

`healthy` exige heartbeat auténtico RUNNING reciente y posterior al arranque;
un proceso existente o un dato ausente no prueba salud. Mantener lock, timeouts
y códigos de salida de fallo. Propagar SIGTERM/SIGINT al hijo; marcar CANCELLED
solo al observar la señal. Un manifiesto RUNNING tras una caída no es éxito.

El baseline mantiene una posición long simulada como estado persistente. Antes de
cambiar código hasheado se detienen el smoke y su timer; después del cambio se
prepara un input nuevo. Al activar baseline se mantiene smoke detenido y se usa su
timer propio. Un rollback requiere input nuevo tras cambios de código y verificación
de montajes después de cambiar de rama. Una parada no se presenta como una venta.

Conservar DB, logs y manifiestos en `storage/` al recrear contenedores. Usar la
API de backup SQLite y verificar la restauración; no copiar una DB abierta a
ciegas ni borrar estado para resolver errores. No tocar contenedores de otros proyectos.
