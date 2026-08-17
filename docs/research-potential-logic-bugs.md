# Investigación: fallas potenciales de lógica

## Alcance y reproducción

Ejecutados los tres tests reportados el 2026-08-17: los tres fallan. Se inspeccionó el código fuente y las pruebas; no se modificó código de producción ni pruebas.

## 1. Fuga de secretos — defecto confirmado

`redact_secrets` sustituye valores sólo si cumplen patrones con mínimo de 16 caracteres (`packages/core/katsi_core/workspace/verifier_execution.py:50-61`; API key en `:27`). Por ello un valor etiquetado como `API_KEY=abc123def456` (12 caracteres) no coincide y se expone. Las pruebas existentes sólo cubren valores de 16+ caracteres (`tests/test_verification_and_recovery.py:284-299`), por lo que no detectan el caso informado. También los patrones de token restringen caracteres a `[A-Za-z0-9_.-]`, omitiendo valores comunes con `/`, `+` o `=` (`verifier_execution.py:27-31`).

Siguiente paso: agregar pruebas parametrizadas de secretos etiquetados cortos y de charset de token; ampliar el valor a no-blancos con un mínimo explícito y conservador (p. ej. 8), preservando el prefijo. Un valor sin etiqueta sigue fuera de alcance salvo que se incorpore un detector específico.

## 2. `workspace_brief` — fixture desactualizado, no evidencia de autorización demasiado estricta

El setup registra identidad pero no inserta `CapabilityGrant` (`tests/test_workspace_brief.py:48-64`); el test publica su primer claim en `:126`. La publicación exige explícitamente una capacidad `CLAIM` activa (`packages/core/katsi_core/workspace/claims.py:269-310`), de modo que el `AuthorizationDeniedError` es el resultado esperado de este fixture. Las pruebas de `ClaimService` sí crean el grant antes de publicar (`tests/test_claim_service.py:26-54, 57-89`).

Siguiente paso: centralizar un helper/fixture de grant `CLAIM` y usarlo en `_build`; mantener un test separado que demuestre la denegación sin grant.

Hallazgo adicional: `_get_active_capability_grant` recibe `operation_class` pero su consulta selecciona únicamente el grant activo más reciente, sin filtrar esa clase (`packages/core/katsi_core/workspace/authorization.py:259-297`). Un grant reciente sin `CLAIM` puede ocultar otro grant `CLAIM` activo y provocar una denegación falsa. Al corregirlo, seleccionar el grant activo que realmente contenga la clase requerida y añadir cobertura con grants superpuestos.

## 3. Rebuild de grafo — expectativas de prueba incompatibles con el contrato de `neighbors`

El rebuild sí crea los nodos `Entity`/`Topic` y sus aristas directas `MENTIONS`/`ABOUT` (`packages/core/katsi_core/store/graph.py:424-440`). Pero `neighbors` no enumera esas aristas: devuelve **otros archivos** conectados por una entidad o tema compartido y exige `o.id <> $id` (`graph.py:179-258`, particularmente `:213-241`). Por ello un único archivo con enriquecimiento caché correctamente reconstruido retorna `[]`, exactamente como falla en `tests/test_projection_rebuild.py:93-112`; la prueba parcial tiene el mismo problema en `:114-136`.

La API ya ofrece `get_direct_relationships` para inspeccionar entidades y temas del propio archivo (`graph.py:279-315`); `count_nodes` también verifica que los nodos se reconstruyeron (`:370-378`). Pruebas coherentes existentes requieren dos archivos compartiendo enriquecimiento antes de afirmar vecinos (`tests/test_rebuild_projections.py:376-410`).

Siguiente paso: cambiar esas dos pruebas para validar `get_direct_relationships`/`count_nodes`, o suministrar un segundo archivo con la misma entidad/tema si la intención es verificar `neighbors`. No hay evidencia de que el rebuild esté perdiendo caché en estos escenarios.
