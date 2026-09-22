# BTC lab — notas operativas

Imagen fijada: `freqtradeorg/freqtrade:2026.8@sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4`.
Roots por entorno (absolutas; entrecomillar por los espacios):
`LAB_CODE_ROOT="<raíz del proyecto>"`,
`LAB_STORAGE_ROOT="<...>/storage"`, y en contenedor `LAB_STORAGE=/lab-storage`.

- Tests: `python -m unittest discover -s tests -v` (raíz del proyecto).
- Compose: `docker compose --profile smoke config` (solo inspección; sin `up` genérico).
- Motor: `python -m operations.launch version` (en contenedor, sin mercado).
- Preflight host: `python -m operations.launch prepare-smoke --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT" --image <digest>`.
- Smoke: `export LAB_SMOKE_INPUT="$(python -m operations.launch prepare-smoke --code-root "$LAB_CODE_ROOT" --storage-root "$LAB_STORAGE_ROOT")"` UNA VEZ y luego `up`; el daemon monta esa ruta única en `/lab-storage/active-input.json` (RO, sin crear si falta) y la conserva en restarts.
- Salud host: `python -m operations.health --container btc-lab-smoke-1 --logfile <...>/freqtrade.log --output <...>/health.json --lock <...>/health.lock`.
- Timer usuario: `operations/systemd/btc-lab-health.{service,timer}` (instalado, oneshot cada 5 min).

El checkout operativo está en la raíz del proyecto, sin worktree adicional,
por petición del usuario. Docker monta únicamente `operations/`, `configs/`
y `strategies/` como código RO; no monta la raíz con Git, memoria y storage.

Notas: el smoke usa `restart: "on-failure:3"` optativo (reintentos solo si el
daemon lo arranca; no implica arranque en boot). `runs/inputs/` va montado RO
anidado en ambos servicios y el manifiesto explícito además como fichero RO
en smoke: el runtime no puede alterar el input por ninguna ruta. Sin alias
mutable: cada `prepare-smoke` devuelve un manifiesto único y `LAB_SMOKE_INPUT`
apunta a uno solo. Sin API/Telegram ni credenciales; el smoke nunca abre
operaciones.
