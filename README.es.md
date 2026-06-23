# mnemo

Servidor MCP local-first y orientado a la privacidad que brinda a cualquier
cliente MCP (Claude Desktop, Code, Cursor, ...) contexto **relacional** y
económico sobre tus archivos. Resume cada archivo exactamente una vez con
modelos locales de Ollama, los almacena en un vector store + grafo de
conocimiento (Kùzu), y al momento de la consulta devuelve un paquete de contexto
reducido para que el modelo de tu cliente sintetice sobre una ventana pequeña
en lugar de explorar el sistema de archivos.

> Los tokens de exploración se trasladan del momento de la consulta (costoso,
> en la nube, repetitivo) al momento de la ingesta (económico, local, único).
> El único gasto en la nube es el cliente sintetizando la respuesta sobre un
> pequeño contexto curado.

## Qué lo hace diferente

- **Resumir-una-vez** por archivo con un modelo local. En caché. Se vuelve a
  resumir solo cuando el hash de contenido del archivo cambia — nunca en
  archivos sin cambios.
- **Relacional, no solo vectorial.** Las entidades, temas y referencias locales
  se almacenan en un grafo Kùzu embebido; la recuperación fusiona similitud
  vectorial con vecinos del grafo para que archivos conectados mediante
  entidades/temas/referencias compartidas aparezcan incluso si no están en los
  primeros N resultados vectoriales.
- **Local-first.** Los resúmenes, embeddings, extracción de entidades y
  consultas al grafo se ejecutan localmente con Ollama + LanceDB + Kùzu. Nada
  sale de tu máquina.
- **Devuelve contexto, no respuestas.** El servidor MCP NO llama a un modelo en
  la nube — devuelve un `ContextBundle` con resúmenes de archivos + los mejores
  fragmentos sin procesar + un esbozo de relaciones para que los sintetice el
  *modelo del cliente*.

## Inicio rápido (60 segundos)

```bash
# 1. Instalar y ejecutar (una línea)
uvx mnemo-mcp
```

Eso es todo para el servidor. Para obtener valor real, indexa una carpeta y
conecta el servidor a tu cliente MCP (siguiente bloque).

Para indexar y buscar en el árbol indexado localmente:

```bash
uvx --from mnemo-cli mnemo index ~/mi-carpeta
uvx --from mnemo-cli mnemo ask "¿de qué trata este proyecto?"
```

## Configuración del cliente MCP (Claude Desktop)

Agrega a la configuración de Claude Desktop
(`~/Library/Application Support/Claude/claude_desktop_config.json` en macOS;
`%APPDATA%\Claude\claude_desktop_config.json` en Windows):

```json
{
  "mcpServers": {
    "mnemo": {
      "command": "uvx",
      "args": ["mnemo-mcp"]
    }
  }
}
```

### Otros clientes

- **Cursor**: Settings → MCP → Add MCP → `uvx mnemo-mcp`.
- **MCP genérico**: cualquier cliente que hable MCP stdio puede ejecutar
  `uvx mnemo-mcp`.

## Herramientas MCP proporcionadas

| Herramienta | Propósito |
|---|---|
| `get_context(query, max_tokens=3000)` | **PRINCIPAL** — paquete de contexto con presupuesto de tokens: resúmenes + fragmentos + esbozo de relaciones. |
| `search_files(query, k=8)` | Archivos clasificados con una línea de "por qué es relevante". |
| `related(file_id, kinds?)` | Vecinos del grafo (entidades/temas/referencias/duplicados compartidos). |
| `get_file_summary(file_id)` | Resumen en caché + metadatos de un archivo. |
| `index_status()` | Conteos por estado, última indexación, total de fragmentos. |
| `index_file_tool(path)` | Activar ingesta de un archivo desde el cliente. |
| `answer(query)` | *(desactivado por defecto)* Síntesis completamente local — nunca sale de la máquina. Activar con `enable_answer_tool=true`. |

## Superficie CLI

```bash
mnemo index ./una-carpeta        # recorrido recursivo con globos include/exclude + progreso Rich
mnemo status                      # conteos + última indexación
mnemo search "aprendizaje automático"  # archivos clasificados
mnemo ask "¿de qué trata este proyecto?"     # imprime el paquete de contexto curado
mnemo ask "¿de qué trata esto?" --local       # también ejecuta síntesis con modelo local
```

## Configuración

Un archivo `mnemo.toml` (o `~/.mnemo/mnemo.toml`) es opcional. Todos los campos
tienen valores predeterminados. Consulta `mnemo.toml.example` para el esquema
completo. Campos clave:

```toml
[mnemo.ollama]
host = "http://localhost:11434"
embed_model = "bge-m3"              # multilingüe ES/EN/ZH
llm_model = "qwen2.5:7b"

[mnemo.ingest]
chunk_token_target = 512
chunk_token_overlap = 64

[mnemo.retrieve]
top_k_chunks = 16
top_k_files = 8
default_context_max_tokens = 3000

[mnemo.mcp]
enable_answer_tool = false          # síntesis local, desactivado por defecto
```

## Arquitectura

```
mnemo/
├── packages/
│   ├── core/mnemo_core/   modelos, configuración, almacenes, clientes, ingesta, recuperación
│   ├── mcp_server/        herramientas FastMCP (este paquete es lo que ejecuta `mnemo-mcp`)
│   └── cli/               CLI `mnemo`: index, status, search, ask
└── tests/
```

Stack local:
- **Embeddings**: Ollama `bge-m3` (local, multilingüe).
- **Vector store**: LanceDB (embebido, en disco).
- **Graph store**: Kùzu (embebido, Cypher).
- **LLM**: Ollama `qwen2.5:7b` (configurable) — resúmenes + extracción JSON restringida.
- **Extracción**: `markitdown` para md/txt/code/pdf/docx → markdown.

## Estado

v0.1 (lanzamiento inicial). La hoja de ruta post-v0.1: vigilancia de archivos
(re-indexación en vivo), resúmenes jerárquicos de carpetas/proyectos,
recuperación multi-salto con agentes mediante LangGraph, reranker local
(`bge-reranker-v2-m3`), explorador visual de grafos.

## Licencia

MIT — consulta [LICENSE](LICENSE).
