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

🚧 **En construcción.** El motor funciona de punta a punta, pero la API pública todavía puede cambiar y no hay interfaz gráfica (existe una CLI mínima para ejercitar el motor).

| Componente | Estado |
|---|---|
| Subtítulos SRT → SRT | ✅ Funcional |
| Libros EPUB → EPUB | ✅ Funcional (incluye tabla de contenidos traducida) |
| Libros PDF | ✅ Funcional (PDFs digitales, sin OCR) |
| Providers: Anthropic, OpenAI-compatible (DeepSeek, OpenRouter…), Ollama (local) | ✅ |
| Glosario + resumen rodante | ✅ |
| Checkpointing y reanudación | ✅ |
| Interfaz gráfica | 🔜 Fase posterior |
| Otros pares de idiomas | 🔜 Hoy: inglés → español neutro |

Fuera de alcance: e-books con DRM (técnica y legalmente) y OCR de PDFs escaneados como camino por defecto.

## Instalación

Requiere Python 3.11+.

```bash
pip install -e .            # núcleo (SRT)
pip install -e ".[epub]"    # + soporte EPUB
pip install -e ".[pdf]"     # + soporte PDF
```

## Arquitectura

Motor primero, UI después. El núcleo es una arquitectura hexagonal: el dominio (chunking, glosario, resumen rodante, orquestación, costos) no conoce ningún proveedor ni formato concreto — todo entra y sale por puertos (`DocumentReader`, `DocumentWriter`, `TranslationProvider`, `Checkpoint`) con adaptadores intercambiables. La API pública es `TranslatorEngine`.

```
borgesica/
├── domain/        # lógica pura: chunking, contexto, glosario, orquestador, costos
├── adapters/
│   ├── readers/   # SRT, EPUB, PDF (pdfplumber / PyMuPDF)
│   ├── writers/   # SRT, EPUB, PDF
│   ├── providers/ # Anthropic, OpenAI-compatible, Ollama
│   └── checkpoints/ # SQLite
└── api.py         # TranslatorEngine — la superficie pública
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
