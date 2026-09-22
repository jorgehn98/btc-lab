# BTC lab

Base local y reproducible para un laboratorio de investigación de BTC/USDT spot. El laboratorio usa Docker y Freqtrade fijado, sin dinero real, credenciales de exchange ni API pública.

## Objetivo

Separar una ventaja reproducible de un resultado histórico afortunado. El baseline `SmaCrossBaseline` es una simulación experimental: no acredita una estrategia rentable, no es un forward test y no habilita trading real.

Hay dos perfiles cerrados y separados. El smoke `dry_run` con `NoTradeSmoke` usa BTC/USDT spot en `5m`, sin entradas; sus cero operaciones son el comportamiento esperado de la prueba técnica, no una métrica de rentabilidad. El baseline usa `SmaCrossBaseline` en velas cerradas de `1h`, solo largo, con una DB y un ciclo de salud propios. Ambos perfiles permanecen detenidos salvo una activación explícita; un backtest o la existencia de helpers no desbloquea el runtime.

Las operaciones del smoke, el backup seguro y la recuperación están en la [guía del laboratorio local](docs/guides/local-lab.md). La [guía del histórico y ledger](docs/guides/history.md) es la referencia única para preparar TRAIN, congelar snapshots y reconciliar métricas. La [guía de búsqueda de estrategias](docs/guides/strategy-search.md) recoge la campaña cerrada y su resultado: ninguna variante alcanzó los gates de TRAIN; VALIDATION y TEST siguen sin abrirse. La [guía del baseline experimental](docs/guides/baseline.md) cubre únicamente sus controles y su operación dry-run aislada. Los datos descargados, snapshots y sesiones son locales y no se publican. El código se publica en el repositorio propio [btc-lab](https://github.com/jorgehn98/btc-lab), no en un fork.

El [estudio de reentrada y régimen](docs/guides/regime-study.md) preregistra una segunda cuadrícula de 24 configuraciones SMA50/200; por ahora solo admite TRAIN y no activa ningún perfil.
