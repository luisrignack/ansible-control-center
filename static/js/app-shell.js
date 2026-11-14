/*
 * Comportamiento común del shell: modales fuera del stacking context, campos
 * de contraseña maestra, confirmaciones data-confirm, tema, menú de usuario,
 * paleta de comandos, interruptor de simulación y visibilidad de contraseñas.
 *
 * Se carga como fichero estático para que la CSP basada en nonce no dependa
 * de bloques inline (auditoría A6).
 */
(function () {
    // Bootstrap añade el backdrop al <body> con z-index 1050, pero .app-shell
    // crea su propio contexto de apilamiento (z-index: 1). Un modal que viva
    // dentro del shell queda detrás del backdrop: la página se oscurece y no
    // se puede escribir ni pulsar nada. Bootstrap espera los modales en el
    // body, así que los movemos allí antes de que se muestren.
    document.querySelectorAll('.modal').forEach(function (modal) {
        if (modal.parentElement !== document.body) {
            document.body.appendChild(modal);
        }
    });

    // La contraseña maestra del vault nunca debe guardarse ni autocompletarse
    // en el navegador. autocomplete="new-password" evita el autofill y las
    // marcas data-* piden a los gestores de contraseñas que ignoren el campo.
    // Se aplica también a campos que se añadan después (tarjetas nuevas).
    function hardenVaultMasterFields(root) {
        const scope = root && root.querySelectorAll ? root : document;
        const inputs = Array.prototype.slice.call(
            scope.querySelectorAll('input[type="password"]')
        );
        if (root && root.matches && root.matches('input[type="password"]')) {
            inputs.push(root);
        }
        inputs.forEach(function (input) {
            const label = input.labels && input.labels.length
                ? input.labels[0].textContent.toLowerCase()
                : '';
            const isVaultMaster = input.name === 'master_password'
                || /contraseña maestra/.test(label)
                || /master password/i.test(input.placeholder || '');
            if (!isVaultMaster) return;
            // Si el navegador lo rellenó con una contraseña guardada antes de
            // cargar este script, se descarta: el usuario debe escribirla.
            if (input.value) input.value = '';
            input.setAttribute('autocomplete', 'new-password');
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('spellcheck', 'false');
            input.setAttribute('data-lpignore', 'true');
            input.setAttribute('data-1p-ignore', 'true');
            input.setAttribute('data-bwignore', 'true');
            input.setAttribute('data-protonpass-ignore', 'true');
            input.setAttribute('data-form-type', 'other');
        });
    }
    hardenVaultMasterFields(document);
    new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    hardenVaultMasterFields(node);
                }
            });
        });
    }).observe(document.body, { childList: true, subtree: true });

    // Confirmaciones sin manejadores inline: compatibles con la CSP basada en
    // nonce y sin interpolar datos de usuario dentro de JavaScript.
    document.addEventListener('submit', function (event) {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        const submitter = event.submitter;
        const message = (submitter && submitter.dataset.confirm)
            || form.dataset.confirm;
        if (message && !window.confirm(message)) {
            event.preventDefault();
        }
    });

    const root = document.documentElement;
    const themeBtn = document.getElementById('themeBtn');
    const themeIcon = document.getElementById('themeIcon');

    function applyTheme(theme) {
        root.setAttribute('data-theme', theme);
        root.setAttribute('data-bs-theme', theme);
        if (themeIcon) {
            themeIcon.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon-stars';
        }
        if (themeBtn) {
            themeBtn.setAttribute('title', theme === 'dark' ? 'Usar tema claro' : 'Usar tema oscuro');
        }
    }

    function paintThemeChoices() {
        const theme = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        document.querySelectorAll('[data-theme-choice]').forEach(function (button) {
            button.classList.toggle(
                'is-active', button.getAttribute('data-theme-choice') === theme
            );
        });
    }

    function saveTheme(theme) {
        applyTheme(theme);
        try {
            localStorage.setItem('ansible-control-center-theme', theme);
        } catch (_error) {
            /* El tema sigue activo en esta página. */
        }
        paintThemeChoices();
    }

    // API compartida con Preferencias y Ajustes del sistema: evita duplicar
    // la lógica del tema y del icono en cada plantilla.
    window.ACC = window.ACC || {};
    window.ACC.theme = {
        key: 'ansible-control-center-theme',
        current: function () {
            return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        },
        apply: applyTheme,
        save: saveTheme,
        paint: paintThemeChoices
    };

    let savedTheme = null;
    try {
        savedTheme = localStorage.getItem(window.ACC.theme.key);
    } catch (_error) {
        savedTheme = null;
    }
    applyTheme(savedTheme === 'dark' ? 'dark' : 'light');
    paintThemeChoices();

    if (themeBtn) {
        themeBtn.addEventListener('click', function () {
            saveTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
        });
    }

    // Menú del usuario: se abre con el avatar y se cierra al pulsar fuera.
    const userMenu = document.getElementById('userMenu');
    const userMenuBtn = document.getElementById('userMenuBtn');
    const userMenuPanel = document.getElementById('userMenuPanel');

    function setUserMenu(open) {
        if (!userMenu || !userMenuBtn || !userMenuPanel) return;
        userMenuPanel.hidden = !open;
        userMenuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        userMenu.classList.toggle('is-open', open);
    }

    if (userMenu && userMenuBtn && userMenuPanel) {
        userMenuBtn.addEventListener('click', function (event) {
            event.stopPropagation();
            setUserMenu(userMenuPanel.hidden);
        });
        document.addEventListener('click', function (event) {
            if (!userMenu.contains(event.target)) setUserMenu(false);
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                setUserMenu(false);
                userMenuBtn.focus();
            }
        });
    }

    const menuBtn = document.getElementById('menuBtn');
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');

    function setMenu(open) {
        if (!sidebar || !overlay) return;
        sidebar.classList.toggle('is-open', open);
        overlay.classList.toggle('is-visible', open);
        if (menuBtn) menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    if (menuBtn && sidebar && overlay) {
        menuBtn.addEventListener('click', function () {
            setMenu(!sidebar.classList.contains('is-open'));
        });
        overlay.addEventListener('click', function () { setMenu(false); });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') setMenu(false);
        });
        sidebar.querySelectorAll('a').forEach(function (link) {
            link.addEventListener('click', function () { setMenu(false); });
        });
    }

    // Paleta de comandos: ⌘K / Ctrl+K abre, filtra y navega sin ratón.
    const palette = document.getElementById('commandPalette');
    const paletteInput = document.getElementById('paletteInput');
    const paletteList = document.getElementById('paletteList');
    const searchTrigger = document.getElementById('searchTrigger');

    if (palette && paletteInput && paletteList) {
        const items = Array.prototype.slice.call(
            paletteList.querySelectorAll('.palette-item')
        );
        let selected = 0;

        function visibleItems() {
            return items.filter(function (item) { return !item.hidden; });
        }

        function paintSelection() {
            const visible = visibleItems();
            items.forEach(function (item) { item.classList.remove('is-selected'); });
            if (!visible.length) return;
            if (selected < 0) selected = 0;
            if (selected >= visible.length) selected = visible.length - 1;
            visible[selected].classList.add('is-selected');
            if (visible[selected].scrollIntoView) {
                visible[selected].scrollIntoView({ block: 'nearest' });
            }
        }

        function filterPalette() {
            const query = (paletteInput.value || '').trim().toLowerCase();
            items.forEach(function (item) {
                const haystack = item.textContent.toLowerCase();
                item.hidden = query !== '' && haystack.indexOf(query) === -1;
            });
            paletteList.querySelectorAll('.palette-group').forEach(function (group) {
                let next = group.nextElementSibling;
                let hasVisible = false;
                while (next && !next.classList.contains('palette-group')) {
                    if (next.classList.contains('palette-item') && !next.hidden) {
                        hasVisible = true;
                    }
                    next = next.nextElementSibling;
                }
                group.hidden = !hasVisible;
            });
            selected = 0;
            paintSelection();
        }

        function openPalette() {
            palette.hidden = false;
            paletteInput.value = '';
            filterPalette();
            paletteInput.focus();
            document.body.style.overflow = 'hidden';
        }

        function closePalette() {
            if (palette.hidden) return;
            palette.hidden = true;
            document.body.style.overflow = '';
        }

        function runItem(item) {
            if (!item) return;
            const href = item.getAttribute('data-href');
            closePalette();
            if (href) window.location.href = href;
        }

        if (searchTrigger) searchTrigger.addEventListener('click', openPalette);
        paletteInput.addEventListener('input', filterPalette);
        paletteList.addEventListener('mousemove', function (event) {
            const item = event.target.closest('.palette-item');
            if (!item || item.hidden) return;
            selected = visibleItems().indexOf(item);
            paintSelection();
        });
        paletteList.addEventListener('click', function (event) {
            const item = event.target.closest('.palette-item');
            if (item) runItem(item);
        });
        palette.addEventListener('click', function (event) {
            if (event.target === palette) closePalette();
        });
        document.addEventListener('keydown', function (event) {
            const isMac = navigator.platform && /Mac/i.test(navigator.platform);
            const combo = (isMac ? event.metaKey : event.ctrlKey)
                && event.key.toLowerCase() === 'k';
            if (combo) {
                event.preventDefault();
                if (palette.hidden) openPalette(); else closePalette();
                return;
            }
            if (palette.hidden) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                closePalette();
            } else if (event.key === 'ArrowDown') {
                event.preventDefault();
                selected += 1;
                paintSelection();
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                selected -= 1;
                paintSelection();
            } else if (event.key === 'Enter') {
                event.preventDefault();
                runItem(visibleItems()[selected]);
            }
        });
        paintSelection();
    }

    // Interruptor de simulación: solo se envía al servidor al cambiarlo.
    const dryRunForm = document.getElementById('dryRunForm');
    const dryRunToggle = document.getElementById('dryRunToggle');
    const dryRunState = document.getElementById('dryRunState');

    if (dryRunForm && dryRunToggle) {
        let previous = dryRunToggle.checked;

        function setState(enabled) {
            dryRunToggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
            if (dryRunState) {
                dryRunState.textContent = enabled ? 'Activada' : 'Desactivada';
            }
            dryRunForm.classList.toggle('is-live', !enabled);
        }

        dryRunToggle.addEventListener('change', function () {
            if (dryRunToggle.checked === previous) return;
            if (!dryRunToggle.checked && !window.confirm(
                'Vas a desactivar la simulación. Las próximas operaciones se ' +
                'aplicarán realmente sobre los equipos. ¿Continuar?'
            )) {
                dryRunToggle.checked = previous;
                setState(previous);
                return;
            }
            setState(dryRunToggle.checked);
            dryRunForm.requestSubmit();
        });

        dryRunForm.addEventListener('submit', function (event) {
            event.preventDefault();
            const enabled = dryRunToggle.checked;
            dryRunToggle.disabled = true;
            fetch(dryRunForm.action, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': dryRunToggle.dataset.csrf || ''
                },
                body: JSON.stringify({ enabled: enabled })
            }).then(function (response) {
                return response.json().catch(function () { return {}; })
                    .then(function (data) {
                        if (!response.ok) {
                            throw new Error(data.message ||
                                'No se pudo cambiar el modo.');
                        }
                        return data;
                    });
            }).then(function () {
                window.location.reload();
            }).catch(function (error) {
                dryRunToggle.checked = previous;
                dryRunToggle.disabled = false;
                setState(previous);
                window.alert(error.message || 'No se pudo cambiar el modo.');
            });
        });
    }
})();

(function () {
    // Delegación: funciona también con modales que se mueven a <body>.
    document.addEventListener('click', function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const button = target ? target.closest('[data-password-toggle]') : null;
        if (!button) return;

        const wrapper = button.closest('.password-field');
        const input = wrapper ? wrapper.querySelector('input') : null;
        if (!input) return;

        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        button.setAttribute('aria-pressed', show ? 'true' : 'false');
        button.setAttribute('aria-label', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
        button.setAttribute('title', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
        const icon = button.querySelector('i');
        if (icon) {
            icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
        }
    });
})();
