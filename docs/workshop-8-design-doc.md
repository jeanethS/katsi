# Katsi Sentinel — Design Doc para Workshop 8

**Autora:** Jeaneth Hernández  
**LinkedIn:** <https://www.linkedin.com/in/jeaneth-s-hdz-rios/>  
**GitHub:** <https://github.com/jeanethS>  
**Repositorio:** <https://github.com/jeanethS/katsi>

## 1. Resumen ejecutivo

Katsi Sentinel extiende `katsi`, un servidor MCP local-first que entiende relaciones entre archivos, para convertirlo en un agente de confiabilidad y auditoría de sistemas de archivos y buckets S3.

El agente detecta cambios peligrosos o inesperados, reconstruye qué ocurrió, propone la reparación mínima, solicita aprobación según el nivel de riesgo, ejecuta únicamente acciones permitidas, verifica el resultado y genera evidencia auditable.

La promesa no es “dejar que un LLM modifique producción”. La promesa es:

> **Detectar → explicar → proponer → aprobar → reparar → verificar → auditar.**

## 2. Problema

Durante un incidente de almacenamiento, los equipos pierden tiempo respondiendo preguntas dispersas:

- ¿Qué archivo u objeto cambió, desapareció o dejó de replicarse?
- ¿Qué servicio, usuario o proceso realizó el cambio?
- ¿Qué otros archivos, configuraciones o workloads dependen de ese objeto?
- ¿Existe una versión sana que pueda recuperarse?
- ¿Cuál es la reparación más pequeña y segura?
- ¿Cómo demostramos después qué se cambió y por qué?

Las herramientas tradicionales entregan logs, métricas o inventarios por separado. Katsi ya tiene una base útil para correlacionarlos: hashes por contenido, resúmenes cacheados, búsqueda vectorial y un grafo de relaciones.

## 3. Usuario objetivo

**Usuario primario:** SRE, platform engineer o cloud operations engineer responsable de datos y configuraciones críticas.

**Usuarios secundarios:** security operations, compliance, incident commander y application owners.

## 4. Caso de uso principal

Un objeto crítico de configuración es eliminado o sobrescrito en un bucket versionado.

Katsi Sentinel:

1. recibe o detecta el cambio;
2. compara el estado actual contra su baseline por hash y metadatos;
3. correlaciona el objeto con eventos, identidad, dependencias y versiones anteriores;
4. explica el impacto en lenguaje claro;
5. genera un plan estructurado de recuperación;
6. aplica una política determinista de riesgo;
7. ejecuta la restauración sólo si está permitida o aprobada;
8. verifica hash, disponibilidad y políticas posteriores;
9. produce un reporte de incidente inmutable.

## 5. Alcance del MVP del hackathon

### Incluido

- Monitoreo de un directorio local con baseline de hashes.
- Detección de creación, modificación y eliminación.
- Snapshots locales versionados para recuperar archivos de demostración.
- Grafo de impacto: archivo afectado, referencias, entidades y temas relacionados.
- `IncidentPlan` en JSON validado: evidencia, hipótesis, acciones, riesgo y verificación.
- Modo `dry-run` por defecto.
- Aprobación explícita antes de escribir o restaurar.
- Ejecución de una lista cerrada de acciones seguras.
- Verificación posterior y reporte Markdown/JSON.
- Demo opcional de auditoría read-only de un bucket S3 versionado.

### Fuera del MVP

- Reparaciones autónomas irrestrictas en producción.
- Cambios de IAM, bucket policies, KMS o networking.
- Recuperación cross-account o cross-region.
- Protección contra ransomware a escala empresarial.
- Soporte para todos los proveedores de object storage.

## 6. Experiencia de demostración

1. Indexar una carpeta de ejemplo con una configuración y archivos relacionados.
2. Mostrar el estado saludable y el grafo de dependencias.
3. Sobrescribir o eliminar el archivo crítico.
4. Ejecutar `katsi incident inspect`.
5. Mostrar una explicación: qué cambió, evidencia, impacto y confianza.
6. Mostrar el plan de reparación en `dry-run`.
7. Aprobar el incidente.
8. Restaurar la última versión sana.
9. Verificar el hash y generar `incident-report.md`.

**Momento “wow”:** el agente no sólo encuentra el archivo; explica qué depende de él, recupera una versión sana y deja una cadena de evidencia reproducible.

## 7. Arquitectura propuesta

```text
Filesystem watcher / S3 events / inventory
                    │
                    ▼
          Normalized ChangeEvent
                    │
                    ▼
     Evidence collector + Katsi ingest
       hashes · versions · metadata · logs
                    │
                    ▼
       Relational evidence graph + vectors
                    │
                    ▼
       Local incident analysis and planning
                    │
                    ▼
       Deterministic policy/risk gate
          │                       │
          ├── deny / dry-run      └── approval required
          │                       │
          └───────────┬───────────┘
                      ▼
             Allowlisted executor
                      │
                      ▼
          Verifier + signed audit report
```

### Componentes

- **Connectors:** filesystem para el MVP; S3 como adaptador posterior.
- **Evidence collector:** contenido, hash, tamaño, propietario, timestamps, versiones y eventos disponibles.
- **Katsi graph:** relaciona objetos con archivos dependientes, entidades, temas y referencias.
- **Incident planner:** produce un contrato JSON estricto; no ejecuta comandos libres.
- **Policy engine:** reglas deterministas por tipo de recurso, ambiente, confianza e impacto.
- **Executor:** acciones predefinidas como `restore_snapshot` o `copy_object_version`.
- **Verifier:** vuelve a calcular hashes y repite checks de salud.
- **Audit writer:** conserva evidencia, decisión humana, acciones y resultados.

## 8. Contratos principales

```json
{
  "event_id": "evt_123",
  "source": "filesystem",
  "resource": "/demo/config/payment.yaml",
  "change": "deleted",
  "observed_at": "2026-08-05T21:00:00Z",
  "before_hash": "...",
  "after_hash": null,
  "actor": "demo-user"
}
```

```json
{
  "incident_id": "inc_123",
  "summary": "A critical payment configuration was deleted.",
  "evidence": ["hash mismatch", "delete event", "3 dependent files"],
  "confidence": 0.96,
  "risk": "medium",
  "requires_approval": true,
  "actions": [
    {
      "type": "restore_snapshot",
      "resource": "/demo/config/payment.yaml",
      "snapshot_id": "snap_456"
    }
  ],
  "verification": ["hash_equals_baseline", "dependent_files_resolve"]
}
```

## 9. Seguridad y límites de autonomía

- Read-only y `dry-run` son los defaults.
- El modelo nunca genera ni ejecuta shell arbitrario.
- Toda acción pertenece a un catálogo tipado y validado.
- Las credenciales usan mínimo privilegio y se separan por entorno.
- Los cambios destructivos, de identidad o de cifrado siempre se niegan en el MVP.
- Toda escritura requiere idempotency key, backup previo y verificación posterior.
- Si la evidencia es insuficiente o contradictoria, el agente escala a una persona.
- El contenido y el análisis frecuente permanecen locales; sólo se comparte un contexto mínimo si se habilita un modelo cloud.

## 10. Adaptación a Amazon S3

La versión S3 puede combinar:

- Event Notifications para cambios casi en tiempo real;
- versiones y delete markers para seleccionar un estado recuperable;
- CloudTrail data events para atribución de operaciones a nivel de objeto;
- S3 Inventory o metadata inventory tables para auditoría masiva de cifrado, tags, versiones y replicación;
- `HeadObject` y checks de aplicación para verificar la recuperación.

Las notificaciones deben procesarse de forma idempotente porque pueden entregarse más de una vez. Un reporte de inventario es excelente para reconciliación, pero no debe ser el detector inmediato del MVP.

## 11. Métricas de éxito

- **MTTD:** cambio peligroso detectado en menos de 5 segundos en la demo local.
- **MTTR:** archivo restaurado y verificado en menos de 60 segundos después de aprobación.
- **Precisión:** cero reparaciones sobre archivos no afectados en el escenario de prueba.
- **Auditabilidad:** 100% de las acciones incluyen evidencia, aprobación y resultado de verificación.
- **Costo/contexto:** los archivos sin cambios no se vuelven a resumir.

## 12. Plan de construcción

### Fase 1 — demo confiable

- `ChangeEvent`, `IncidentPlan` y `VerificationResult` con Pydantic.
- Baseline y snapshots versionados de un directorio de demo.
- Detector de drift por hash.
- Comando `incident inspect` con grafo de impacto.
- Plan en JSON y `dry-run`.
- Aprobación, restauración y verificación.
- Reporte final reproducible.

### Fase 2 — S3 read-only

- Adaptador para listar versiones y metadatos.
- Ingesta de eventos normalizados.
- Detección de delete markers, cifrado faltante y replicación fallida.
- Reporte de incidente sin escritura.

### Fase 3 — reparación S3 controlada

- `copy_object_version` sobre un bucket de sandbox.
- Políticas por bucket/prefix.
- Aprobación humana y rollback.
- Verificación de versión, hash/checksum y metadata.

## 13. Riesgos

| Riesgo | Mitigación |
|---|---|
| El agente repara la causa equivocada | Evidencia obligatoria, umbral de confianza y aprobación |
| Prompt injection dentro de archivos | Tratar contenido como datos; acciones cerradas y policy engine externo al modelo |
| Eventos duplicados o fuera de orden | Idempotency keys y secuencia/version ID |
| Restauración incompleta | Backup previo y verificación posterior obligatoria |
| Demo demasiado grande | Terminar primero el flujo local end-to-end; S3 queda como adaptador |

## 14. Pitch de 30 segundos

> Katsi Sentinel es un agente local-first para incidentes de archivos y buckets. Cuando un objeto crítico cambia o desaparece, correlaciona hashes, versiones, eventos y dependencias para explicar el impacto. Luego propone la reparación mínima, pide aprobación según el riesgo, restaura una versión sana, verifica el resultado y genera un reporte auditable. A diferencia de un chatbot con acceso a producción, Katsi separa la inteligencia generativa de la autorización y la ejecución.

## 15. Decisión recomendada

Para el hackathon, construir primero el flujo completo sobre filesystem local. Es la demo más confiable y aprovecha directamente lo que `katsi` ya tiene. Presentar S3 como el segundo adaptador empresarial y, si el tiempo alcanza, agregar un scan read-only de un bucket de prueba.
