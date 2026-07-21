# borgésica

> Traducción de libros y subtítulos al español, coherente de la primera página a la última.

**borgésica** es un motor de traducción open source que convierte libros (EPUB, PDF) y subtítulos (SRT) del inglés a un **español neutro**, entendible en cualquier región de habla hispana.

## Por qué existe

La enorme mayoría del conocimiento del mundo se publica primero — y muchas veces únicamente — en inglés. Libros técnicos, ensayos, investigación, cursos, documentales: quien no lee inglés llega años tarde, o no llega nunca.

Las opciones actuales son malas en direcciones opuestas: la traducción automática clásica es rápida pero plana e inconsistente; la traducción humana es excelente pero lenta y cara — y nunca va a alcanzar el volumen de lo que se publica.

**borgésica busca democratizar el acceso a la información para el hablante de español**: que cualquier persona pueda tomar un libro o una serie en inglés y obtener una traducción coherente, natural y consistente, con el modelo de lenguaje que elija — desde una API de frontera hasta un modelo local corriendo en su propia máquina, gratis y sin conexión.

## El problema técnico que resuelve

Traducir un texto largo con un LLM de forma ingenua falla por una razón concreta: no es posible procesar un libro de 400 páginas en una sola llamada. Hay que dividirlo en fragmentos — y al dividirlo, la continuidad se rompe. El nombre de un personaje se traduce de tres formas distintas, el registro deriva de formal a coloquial entre capítulos, un término inventado en el capítulo 2 se olvida en el 30.

borgésica lo resuelve con tres mecanismos coordinados:

- **Chunking que respeta unidades naturales** — los subtítulos nunca se parten; la prosa se corta en límites de párrafo y capítulo.
- **Glosario** — traducciones fijas para nombres propios y términos recurrentes, inyectadas en cada chunk. El usuario revisa y aprueba el glosario *antes* de traducir.
- **Resumen rodante** — un registro compacto de tono, registro y trama que acompaña cada chunk, para que la voz no derive a lo largo de la obra.

Además:

- **Reanudable** — checkpointing en SQLite: si el trabajo se corta en la página 300 de 400, se reanuda sin re-traducir (ni re-pagar) lo ya hecho.
- **Estimación de costo previa** — el costo se conoce antes de gastar un centavo, con tope de presupuesto configurable.
- **Round-trip fiel** — un EPUB entra y un EPUB válido sale, con capítulos, formato e imágenes intactos, listo para Send to Kindle. Un SRT conserva tiempos, índices y etiquetas.
- **Agnóstico de modelo y proveedor** — el usuario elige el modelo. Sin dependencia de ningún proveedor.

## Estado del proyecto

El motor funciona de punta a punta y hoy tiene dos formas de usarlo: una **CLI** y una **aplicación de escritorio** con interfaz gráfica.

| Componente | Estado |
|---|---|
| Subtítulos SRT → SRT | ✅ Funcional |
| Libros EPUB → EPUB | ✅ Funcional (incluye tabla de contenidos traducida) |
| Libros PDF | ✅ Funcional (PDFs digitales, sin OCR) |
| Providers: Anthropic, OpenAI-compatible (DeepSeek, OpenRouter…), Ollama (local) | ✅ |
| Glosario + resumen rodante | ✅ |
| Checkpointing y reanudación | ✅ |
| CLI | ✅ Funcional |
| API HTTP local (`serve`, FastAPI, autenticada por token de sesión) | ✅ Funcional |
| Aplicación de escritorio (Tauri + React) | ✅ Funcional — flujo completo de traducción con progreso en vivo |
| Otros pares de idiomas | Hoy: solo inglés → español neutro |

Fuera de alcance: e-books con DRM (técnica y legalmente) y OCR de PDFs escaneados como camino por defecto.

## Aplicación de escritorio

Además de la CLI, borgésica tiene una app de escritorio (Tauri + React) que levanta el motor como un proceso local ("sidecar") y lo consume por una API HTTP autenticada con un token de sesión propio.

El uso es un asistente de un solo flujo:

1. **Elegir proveedor y clave** — Anthropic, DeepSeek u Ollama (local, sin clave). La clave se pide por sesión y nunca se guarda en disco.
2. **Elegir archivo y estimar costo** — antes de traducir una sola palabra.
3. **Revisar y bloquear el glosario** — se edita y se fija antes de correr el trabajo.
4. **Traducir con progreso en vivo** — chunk por chunk, con cancelación cooperativa entre chunks.
5. **Exportar el resultado** — o reanudar un trabajo interrumpido desde su job id.

La app también recupera la conexión si el proceso del motor se cae en medio de una sesión.

## Elegir modelo

borgésica es agnóstica de proveedor, pero el modelo elegido cambia mucho el resultado. Precios verificados en la tabla de precios del proveedor (USD por millón de tokens, input/output); la calidad es una observación de uso real traduciendo con el motor, no un benchmark formal:

| Proveedor | Modelo | Precio (input / output por Mtok) | Calidad observada |
|---|---|---|---|
| DeepSeek | `deepseek-v4-flash` | $0.14 / $0.28 | Muy buena — la mejor relación calidad-precio |
| Anthropic | `claude-haiku-4-5` | $1.00 / $5.00 | Algo más caro que flash y de calidad inferior |
| Anthropic | `claude-sonnet-5` | $3.00 / $15.00 | La mejor calidad del grupo, al precio más alto |
| Ollama (local) | `Tower-Plus-9B-GGUF:Q4_K_M` | Gratis (cómputo local) | Muy baja |
| Ollama (local) | `qwen3:14b` | Gratis (cómputo local) | Muy baja |

Los modelos de Ollama no tienen costo de API — corren en la propia máquina, sin conexión — pero a este tamaño todavía quedan lejos en calidad de traducción de las opciones hospedadas.

## Instalación

Requiere Python 3.11+.

```bash
pip install -e .              # núcleo (SRT)
pip install -e ".[epub]"      # + soporte EPUB
pip install -e ".[pdf]"       # + soporte PDF
pip install -e ".[serve]"     # + API HTTP local (necesaria para la app de escritorio)
```

Para correr la app de escritorio en modo desarrollo hace falta además Node.js y el toolchain de Rust (Tauri):

```bash
cd desktop
npm install
npm run tauri dev
```

## Arquitectura

Motor primero, UI después. El núcleo es una arquitectura hexagonal: el dominio (chunking, glosario, resumen rodante, orquestación, costos) no conoce ningún proveedor ni formato concreto — todo entra y sale por puertos (`DocumentReader`, `DocumentWriter`, `TranslationProvider`, `Checkpoint`) con adaptadores intercambiables. La API pública es `TranslatorEngine`. La CLI, la API HTTP y la app de escritorio son tres formas distintas de llegar al mismo motor.

```
borgesica/
├── domain/        # lógica pura: chunking, contexto, glosario, orquestador, costos
├── adapters/
│   ├── readers/   # SRT, EPUB, PDF (pdfplumber / PyMuPDF)
│   ├── writers/   # SRT, EPUB, PDF
│   ├── providers/ # Anthropic, OpenAI-compatible, Ollama
│   └── checkpoints/ # SQLite
├── serve/         # API HTTP (FastAPI) — consumida por la app de escritorio
└── api.py         # TranslatorEngine — la superficie pública

desktop/           # app de escritorio (Tauri + React), habla con serve/ vía sidecar local
```

## Contribuir

El proyecto es open source y las contribuciones son bienvenidas. Está desarrollado con TDD estricto: la lógica de dominio se testea con el LLM simulado; los adaptadores, contra fixtures.

```bash
pip install -e ".[dev]"
pytest
```

## Licencia

[MIT](LICENSE)

---

*El nombre viene de Borges — traductor, bibliotecario infinito, y autor de la idea de que un libro cambia con cada lector.*
