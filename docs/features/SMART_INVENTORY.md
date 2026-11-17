# Inventario inteligente (FASE 6)

Estado: **PARTIALLY_COMPLETED**

Implementado en esta auditoría: exportación/importación CSV, última ejecución y
último resultado por host, y neutralización de fórmulas en CSV. No se añadió
recopilación remota de OS/CPU/memoria/almacenamiento porque exige ejecutar
Ansible y el mandato prohíbe operaciones reales durante la auditoría; queda
diseñada como lectura con `ansible_facts` en una fase posterior.

## 1. Inventario existente (antes)

- Grupos (`inventory_groups`): nombre, descripción, plataforma
  (`linux`/`windows`), conexión (`ssh`/`winrm`), clave SSH opcional,
  criticidad y ventana de mantenimiento, propietario (`0` = compartido).
- Hosts (`inventory_hosts`): IP literal, etiqueta, criticidad propia.
- Validación al añadir: IPv4 + `pin_target` (allowlist del momento).
- Vista web por propietario; el admin puede abrir la vista global con `all=1`.

## 2. Lo añadido

### 2.1 Exportación CSV

`GET /inventario/exportar.csv` (sesión requerida, mismo alcance que la vista):

```
grupo,descripcion,plataforma,conexion,direccion,etiqueta,criticidad,
añadido,ultima_ejecucion,ultimo_resultado
```

- UTF-8 con BOM para que Excel respete los acentos.
- `Cache-Control: no-store`.
- **Neutralización de fórmulas:** un valor que empiece por `=`, `+`, `-`, `@`,
  tabulador o retorno se prefija con `'` para evitar ejecución en hojas de
  cálculo (CWE-1236).
- Solo incluye grupos/hosts visibles para el usuario (`_owner_scope`).

### 2.2 Importación CSV

`POST /inventario/importar` (rol `operator` o `admin`, CSRF, propiedad del
grupo):

- Cabeceras aceptadas: `address`/`direccion`/`dirección`/`ip`/`host`,
  `label`/`etiqueta`/`nombre`, `criticality`/`criticidad`.
- Decodificación `utf-8-sig`; se rechaza otro encoding.
- Tamaño máximo `ACC_MAX_UPLOAD_BYTES` (2 MB por defecto).
- Límite `MAX_HOSTS` (256 por defecto); el resto se ignora e informa.
- **Solo añade**: no borra ni modifica hosts existentes.
- Duplicados (`UNIQUE`) se cuentan como omitidos, no como error.
- Errores por línea acotados a 10 mensajes para no inundar la UI.
- Cada alta pasa por la misma validación IPv4/allowlist que el formulario.

### 2.3 Última ejecución y último resultado

`ExecutionBatch.latest_host_outcomes()` obtiene, en una sola consulta, el
último resultado de `execution_hosts` por dirección. La vista de inventario
muestra fecha y badge de resultado; el CSV los exporta. Es **solo lectura**: no
ejecuta nada contra los equipos.

## 3. Funcionalidad no implementada (y por qué)

| Función | Estado | Motivo / plan |
|---|---|---|
| Importar inventario Ansible (INI/YAML) | NO | Requiere definir mapeo de grupos/variables y validación; el CSV cubre el caso actual |
| Exportación JSON | NO | El CSV y `/ejecuciones/exportar` cubren el uso; añadir es trivial si se pide |
| Etiquetas múltiples (tags) | NO | El modelo tiene `label` y criticidad; migrar a tabla de tags es un cambio de esquema |
| Criticidad por host | SÍ (existente) | `criticality` con herencia del grupo |
| Detección de inaccesibles | PARCIAL | El resultado `failure/error` indica inaccesibilidad en la última ejecución; no hay ping periódico |
| Última conexión | NO | No se registra un «last_seen» separado del resultado de ejecución |
| OS, arquitectura, CPU, memoria, almacenamiento | NO | Requiere `ansible_facts` (solo lectura) en un playbook de inspección; no se ejecutó contra hosts reales |
| Trazabilidad de cambios de inventario | PARCIAL | Los cambios del inventario no se auditan en `auth_events` (solo login/acciones admin); recomendable añadir eventos |

## 4. Plan para completar la fase

1. **Playbook de hechos de solo lectura** (`ansible.builtin.setup` con
   `gather_subset: [min]`) y almacenamiento en `inventory_host_facts` con
   `collected_at`. Nunca usar módulos que modifiquen el host.
2. **Sondeo manual** («Actualizar datos») y/o programado, con límite de
   concurrencia y timeout; resultados visibles en el inventario.
3. **`last_seen`/`last_connection`** derivados del sondeo o del último
   resultado SSH.
4. **Tags**: tabla `inventory_tags` + `inventory_host_tags`, filtros en la UI y
   exportación CSV/JSON.
5. **Auditoría de inventario**: eventos `inventory_group_created`,
   `inventory_host_added`, `inventory_host_removed`, `inventory_import`.
6. Pruebas: importación idempotente, CSV con BOM/CRLF, límites, permisos,
   ausencia de escritura remota.

## 5. Pruebas añadidas

- `test_inventario_exporta_csv`: contenido, MIME y `no-store`.
- `test_inventario_importa_csv_sin_borrar`: altas, inválidos y duplicados.
- `test_viewer_no_puede_importar_csv`: 403 para rol `viewer`.

## 6. Compatibilidad

- No cambia el esquema; el CSV es una nueva ruta.
- El inventario y las ejecuciones existentes siguen funcionando igual.
- La columna «Última ejecución» se rellena con datos ya presentes en
  `execution_hosts`; si no hay historial, muestra «Sin ejecuciones».
