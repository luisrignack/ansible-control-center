# Propuestas de interfaz — Ansible Control Center

Diez direcciones visuales para la consola de automatización **Ansible Control Center**.
Cada propuesta es un archivo HTML autocontenido: se abre con doble clic, funciona sin
red y no carga CDN, fuentes ni imágenes externas.

Abre [`index.html`](index.html) para ver la galería con vistas previas reales de las diez
propuestas, filtros por tono/densidad y una tabla comparativa.

| # | Propuesta | Tono | Densidad | Ideal para |
|---|---|---|---|---|
| 01 | [Command Center](01-command-center.html) | Oscuro | Alta | Operación continua y telemetría |
| 02 | [Editorial claro](02-editorial-claro.html) | Claro | Media | Lectura pausada y documentación operativa |
| 03 | [Aurora Glass](03-aurora-glass.html) | Oscuro | Media | Producto premium con glassmorphism |
| 04 | [Soft Clay](04-soft-clay.html) | Claro | Media | Equipos mixtos y uso diario amable |
| 05 | [Swiss Brutal](05-swiss-brutal.html) | Claro | Alta | Alto contraste y jerarquía contundente |
| 06 | [Bento Pop](06-bento-pop.html) | Claro | Media | Onboarding y dashboards vistosos |
| 07 | [Phosphor Terminal](07-phosphor-terminal.html) | Oscuro | Alta | Perfiles técnicos y consola primero |
| 08 | [Nordic Minimal](08-nordic-minimal.html) | Claro | Baja | Producto sereno y legible |
| 09 | [SaaS Indigo](09-saas-indigo.html) | Mixto | Media | Presentación comercial + consola |
| 10 | [Ops Data](10-ops-data.html) | Claro | Muy alta | Tablas, filtros y trabajo por teclado |

## Cómo verlas

```bash
# Opción 1: abrir la galería directamente
xdg-open docs/propuestas-ui/index.html

# Opción 2: servirlas en local (mejor para iframes y rutas)
python3 -m http.server 8080 --directory docs/propuestas-ui
# http://localhost:8080/
```

## Criterios comunes

- Marca **Ansible Control Center** y subtítulo **Consola de automatización**.
- Navegación preparada para el crecimiento del producto: Panel, Inventario,
  Playbooks, Ejecuciones, Programaciones, Credenciales y Auditoría.
- Datos de muestra realistas: flota de 254 hosts, lote `#A7F3C9E2`, dry-run,
  zona `Europe/Madrid`, política NTP `restore`.
- Interacción real en JS vanilla: filtros, pestañas, modales, reloj, menú móvil.
- Responsive desde 360 px, foco visible, `aria-*`, skip link y
  `prefers-reduced-motion`.
- Sin dependencias, sin red, sin build. Todo el CSS y los iconos SVG viven en cada archivo.

## Nota sobre la marca

La aplicación ya usa **Ansible Control Center** en `templates/base.html`, `app.py` y
`config.py`. Las propuestas 01 y 02 se actualizaron para eliminar el nombre antiguo;
las propuestas 03–10 nacen directamente con la marca nueva.
