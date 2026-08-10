# Contribuir a borgésica

Gracias por el interés. Este documento explica cómo levantar el proyecto, qué se
espera de un cambio y qué convenciones sigue el repositorio.

## Preparar el entorno

Requiere **Python 3.11 o superior**. El código usa `datetime.UTC`, que no existe
en 3.10 — con un intérprete más viejo la importación falla directamente.

```bash
python -m venv .venv
```

```bash
pip install -e ".[dev,epub,pdf,serve,openai-compat]"
```

El extra `dev` trae pytest, ruff y las dependencias de fixtures. Los demás son
opcionales según lo que vayas a tocar: `epub` y `pdf` para los lectores de
libros, `serve` para la API HTTP, `openai-compat` para DeepSeek y OpenAI.

> `pdf-fast` queda deliberadamente fuera de esa línea: PyMuPDF es **AGPL-3.0** y
> por eso es opt-in explícito. No lo agregues a las dependencias por defecto.

Para las claves de proveedor, copia `.env.example` a `.env` y completa solo la
del proveedor que vayas a usar. La CLI carga ese archivo al arrancar, buscándolo
desde el directorio en el que corres el comando hacia arriba. Una variable ya
exportada en el entorno le gana al valor del archivo, así que puedes sobrescribir
una clave para una corrida puntual sin editar `.env`.

Para la app de escritorio hacen falta además Node.js y el toolchain de Rust:

```bash
cd desktop && npm install && npm run tauri dev
```

## Correr los tests

**Usa siempre el intérprete del entorno virtual**, no el `python` del sistema:

```bash
./.venv/Scripts/python.exe -m pytest
```

En Linux y macOS, `./.venv/bin/python -m pytest`. Esto no es una formalidad: si
la máquina tiene un Python 3.10 global, `pytest` a secas puede resolver a ese
intérprete y la suite entera falla al importar, con un error que no tiene nada
que ver con tu cambio.

La suite por defecto no hace ninguna llamada de red ni gasta dinero. Hay dos
grupos que se omiten salvo que los pidas explícitamente:

| Marker | Cómo habilitarlo | Qué hace |
|---|---|---|
| `integration` | `INTEGRATION=1` | Llamadas reales al proveedor. **Cuesta dinero.** |
| `golden` | `GOLDEN=1` | Evaluación de calidad con un LLM de juez. **Cuesta dinero.** |

Que aparezcan como *skipped* en una corrida normal es el comportamiento
esperado, no un problema.

## Lint

```bash
./.venv/Scripts/python.exe -m ruff check .
```

El repositorio arrastra un **baseline de 171 errores preexistentes**. Ese número
es la línea de flotación: mientras no suba, tu cambio no agrega deuda. Si tu
rama devuelve más de 171, corrige lo tuyo antes de abrir el PR.

No corras `ruff check --fix` sobre todo el repositorio para "limpiar de paso".
Arreglaría cientos de líneas ajenas a tu cambio, volvería el diff imposible de
revisar y mezclaría dos decisiones distintas en un solo PR. Bajar el baseline es
bienvenido, pero como PR propio y explícito.

## Arquitectura: la regla que no se negocia

El núcleo es una **arquitectura hexagonal**. La regla concreta que la sostiene:

> `borgesica/domain/` no importa nada de `borgesica/adapters/`.

El dominio — chunking, glosario, resumen rodante, orquestación, costos — no sabe
qué proveedor ni qué formato hay del otro lado. Todo entra y sale por puertos
(`DocumentReader`, `DocumentWriter`, `TranslationProvider`, `Checkpoint`). Hoy esa
frontera está limpia; verifícalo antes de commitear:

```bash
git grep -nE "^[[:space:]]*(from|import)[[:space:]]+borgesica\.adapters" -- borgesica/domain
```

Sin salida es lo correcto. Si devuelve algo, el cambio rompe la arquitectura, y
la solución casi siempre es un puerto nuevo, no un import.

El patrón busca líneas de import y no menciones en texto: `domain/ports.py`
nombra `borgesica/adapters/` en su docstring, que es legítimo y no debe contar
como violación.

Los adaptadores son intercambiables por diseño: agregar un proveedor significa
implementar `TranslationProvider` en `borgesica/adapters/providers/`, sin tocar
una línea del dominio.

## TDD estricto

El proyecto se desarrolla con **TDD estricto**: el test primero, y falla antes de
existir la implementación. En la práctica:

- **Dominio** — se testea contra el LLM simulado (`tests/fakes.py`). Rápido y
  determinista. Nunca contra un proveedor real.
- **Adaptadores** — contra fixtures. Un adaptador nuevo necesita sus fixtures.
- **Tests de comportamiento, no de implementación.** Un test que se rompe al
  renombrar un método privado está testeando la cosa equivocada.

Un PR que agrega comportamiento sin tests no se merge, aunque el código esté
bien.

## Idioma

El repositorio mezcla dos idiomas a propósito:

| Qué | Idioma |
|---|---|
| Código, identificadores, comentarios | Inglés |
| Mensajes de commit y PRs | Inglés |
| `README.md`, `CONTRIBUTING.md`, `.env.example` | Español |
| `MODELS.md` y documentación de referencia técnica | Inglés |

Todo texto en español que quede en el repositorio va en **español neutro, sin
voseo**. No es una preferencia estética: el propio motor prohíbe el voseo en las
reglas que le pasa al modelo (`_NEUTRAL_SPANISH`, en
[`borgesica/domain/context.py`](borgesica/domain/context.py)). Un README que
vosea contradice al producto.

## Commits

Se usan **commits convencionales**, en inglés, en imperativo:

```
feat: add OpenRouter provider adapter
fix: preserve SRT tags when a cue spans two chunks
docs: document the ruff baseline
test: cover glossary injection on chapter boundaries
chore: move demo subtitles into samples/
```

**No agregues atribución de IA a los commits.** Nada de `Co-Authored-By` a un
modelo, ni firmas de herramienta en el mensaje.

Mantén el commit como una unidad revisable: el test junto al código que prueba,
no en un commit aparte.

## Qué nunca se commitea

Este repositorio es **público**. Un secreto o un archivo con copyright que entre
acá no se borra con un `git revert` — los forks y la caché de GitHub persisten
después de cualquier limpieza. Las cuatro rutas están en `.gitignore` y nunca
estuvieron en la historia; que siga así:

| Ruta | Por qué |
|---|---|
| `.env` | Claves de API reales |
| `examples/` | Libros y subtítulos con copyright. Tener una copia legítima no da derecho a redistribuirla |
| `openspec/` | Herramienta local de planificación; describe comportamiento que no está implementado |
| `.claude/` | Configuración local del entorno de desarrollo |

Nunca uses `git add -f` sobre ninguna de ellas. Antes de un push, si tienes
dudas:

```bash
git ls-tree -r HEAD --name-only | grep -E "^(examples/|openspec/|\.claude/|\.env$)"
```

Sin salida es lo correcto.

## Abrir un Pull Request

1. Rama desde `main`.
2. Tests en verde con el intérprete del venv.
3. Ruff en 171 o menos.
4. El dominio sigue sin importar adaptadores.
5. Describe **qué problema resuelve**, no solo qué archivos tocaste.

Los PRs enfocados se revisan rápido; los que mezclan un fix, un refactor y un
cambio de formato, no. Si el cambio es grande o toca la arquitectura, abre un
issue antes de escribir el código — es mejor discutir el enfoque que descartar
trabajo ya hecho.

## Licencia

Al contribuir aceptas que tu aporte se publique bajo la licencia
[MIT](LICENSE) del proyecto.
