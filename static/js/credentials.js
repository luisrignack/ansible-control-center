/*
 * Credenciales: alta cifrada en el vault y borrado con la contraseña maestra.
 * La configuración (token CSRF y URL) llega en #credentials-config.
 */
(function () {
    'use strict';

    const configNode = document.getElementById('credentials-config');
    const config = configNode ? JSON.parse(configNode.textContent) : {};
    const CSRF_TOKEN = config.csrf_token || '';
    const CREDENTIALS_URL = config.url || '';

function deletePassword(id) {
    if (!confirm('¿Estás seguro de eliminar esta contraseña del vault?')) {
        return;
    }
    const masterPassword = prompt('Contraseña maestra del Vault:');
    if (masterPassword === null) {
        return;
    }
    fetch(CREDENTIALS_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': CSRF_TOKEN
            },
            body: 'action=delete&password_id=' + encodeURIComponent(id) +
                  '&master_password=' + encodeURIComponent(masterPassword)
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(() => alert('Error de conexión'));
}

function showModalStatus(message, isError) {
    const status = document.getElementById('passwordModalStatus');
    if (!status) return;
    status.textContent = message;
    status.classList.toggle('alert-danger', Boolean(isError));
    status.classList.toggle('alert-success', !isError);
    status.classList.toggle('d-none', !message);
}

function savePasswords(btn) {
    const passwordsText = document.getElementById('passwordsTextarea').value;
    const masterPassword = document.getElementById('masterPasswordModal').value;

    if (!passwordsText.trim()) {
        showModalStatus('Por favor, ingresa al menos una contraseña.', true);
        return;
    }

    if (!masterPassword) {
        showModalStatus('Por favor, ingresa la contraseña maestra del Vault.', true);
        return;
    }

    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Cifrando...';
    showModalStatus('Cifrando y guardando en el vault…', false);

    fetch(CREDENTIALS_URL, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': CSRF_TOKEN
        },
        body: 'action=encrypt&passwords_textarea=' + encodeURIComponent(passwordsText) +
              '&master_password=' + encodeURIComponent(masterPassword)
    })
    .then(response => response.json().catch(() => ({})).then(data => {
        if (!response.ok || !data.success) {
            throw new Error(data.message || 'No se pudo guardar el vault.');
        }
        return data;
    }))
    .then(data => {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        showModalStatus(data.message || 'Contraseñas guardadas.', false);
        const modal = bootstrap.Modal.getOrCreateInstance(
            document.getElementById('addPasswordModal')
        );
        modal.hide();
        window.setTimeout(() => location.reload(), 600);
    })
    .catch(error => {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        showModalStatus(error.message || 'Error de conexión', true);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    // El modal está dentro de .app-shell, que crea un stacking context con
    // z-index 1. Bootstrap añade el backdrop al <body> con z-index 1050, por
    // lo que el backdrop tapaba el modal y la página parecía congelada.
    // Moverlo al body (igual que en el panel) lo saca de ese contexto.
    const addModal = document.getElementById('addPasswordModal');
    if (addModal && addModal.parentElement !== document.body) {
        document.body.appendChild(addModal);
    }

    document.querySelectorAll('.delete-password-btn').forEach((button) => {
        button.addEventListener('click', () => {
            deletePassword(button.dataset.passwordId);
        });
    });

    const saveButton = document.getElementById('savePasswordsBtn');
    if (saveButton) {
        saveButton.addEventListener('click', () => savePasswords(saveButton));
    }
});
})();
