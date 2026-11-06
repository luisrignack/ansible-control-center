# Instrucciones para agentes y colaboradores

## Regla de oro: terminar siempre con commit y push

Toda modificación del proyecto se considera **incompleta** hasta que esté
publicada en GitHub. Al terminar cualquier cambio, es obligatorio:

1. Ejecutar la suite de pruebas y corregir las regresiones.
2. Revisar `git status` y `git diff` para confirmar que solo se incluye lo
   previsto.
3. Añadir los archivos con `git add`.
4. Crear un commit con un mensaje descriptivo.
5. Publicarlo con `git push origin master` (o la rama de trabajo
   correspondiente).

No se deben dejar cambios locales sin commit ni commits sin push al finalizar
una tarea. Si el push falla por credenciales, red o protección de rama, hay que
informar del bloqueo y dejar el commit local preparado, indicando exactamente
qué falta para publicarlo.

## Buenas prácticas del proyecto

- No incluir secretos, contraseñas, claves privadas ni archivos de entorno
  reales en el repositorio.
- Mantener las pruebas en verde antes de cada commit.
- Actualizar la documentación cuando cambie el comportamiento.
- Respetar el estilo y la estructura existentes.
