# BTC lab

Base local y reproducible para un laboratorio de investigación de BTC/USDT spot. La fase inicial comprueba la infraestructura con Docker y Freqtrade fijado, sin dinero real, credenciales de exchange ni API pública.

## Objetivo

Separar una ventaja reproducible de un resultado histórico afortunado. Esta entrega prepara el laboratorio; no acredita una estrategia, no es un forward test y no implementa todavía el estudio TRAIN ni la validación económica.

Está implementado un smoke `dry_run` con `NoTradeSmoke`: BTC/USDT spot, `5m`, sin entradas y con estado, hashes, logs, DB SQLite y comprobación de salud locales. Sus cero operaciones son el comportamiento esperado de la prueba técnica, no una métrica de rentabilidad.

Las operaciones reproducibles, el backup seguro y la activación del health timer están en la [guía del laboratorio local](docs/guides/local-lab.md).
