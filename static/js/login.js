/*
 * Acceso: deja el foco en el campo de usuario al cargar la página.
 */
document.addEventListener('DOMContentLoaded', function () {
    const usernameInput = document.getElementById('username');
    if (usernameInput) usernameInput.focus();
});
