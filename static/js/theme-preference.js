/*
 * Selector de tema de Preferencias y Ajustes del sistema. Reutiliza la API
 * window.ACC.theme que publica app-shell.js para que un cambio de tema se
 * aplique, se guarde y se refleje en el icono del encabezado sin duplicar la
 * lógica en dos plantillas (auditoría A6).
 */
(function () {
    'use strict';

    const api = window.ACC && window.ACC.theme;
    if (!api) return;

    const buttons = Array.prototype.slice.call(
        document.querySelectorAll('[data-theme-choice]')
    );

    buttons.forEach(function (button) {
        button.addEventListener('click', function () {
            const theme = button.getAttribute('data-theme-choice');
            if (theme !== 'dark' && theme !== 'light') return;
            api.save(theme);
        });
    });

    api.paint();
})();
