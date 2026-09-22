# BTC lab

Base local y reproducible para un laboratorio de investigación de BTC/USDT spot. El laboratorio usa Docker y Freqtrade fijado, sin dinero real, credenciales de exchange ni API pública.

## Objetivo

Separar una ventaja reproducible de un resultado histórico afortunado. El baseline `SmaCrossBaseline` es una simulación experimental: no acredita una estrategia rentable, no es un forward test y no habilita trading real.

Hay dos perfiles cerrados y separados. El smoke `dry_run` con `NoTradeSmoke` usa BTC/USDT spot en `5m`, sin entradas; sus cero operaciones son el comportamiento esperado de la prueba técnica, no una métrica de rentabilidad. El baseline usa `SmaCrossBaseline` en velas cerradas de `1h`, solo largo, con una DB y un ciclo de salud propios.

Las operaciones del smoke, el backup seguro y la recuperación están en la [guía del laboratorio local](docs/guides/local-lab.md). La [guía del baseline experimental](docs/guides/baseline.md) cubre TRAIN, snapshots, informes y el perfil dry-run aislado. Los datos descargados, snapshots y sesiones son locales y no se publican. El código se publica en el repositorio propio [btc-lab](https://github.com/jorgehn98/btc-lab), no en un fork.
