/*
 * Panel de operaciones: resumen en vivo, validación de objetivo, campos
 * dependientes de la acción, vista previa del tapiz y confirmación de
 * ejecución. La configuración llega en el bloque JSON #panel-config.
 */
(function () {
    'use strict';

    const configNode = document.getElementById('panel-config');
    const config = configNode ? JSON.parse(configNode.textContent) : {};
    const prefill = config.prefill || {};
    const defaultNtpServers = config.default_ntp_servers || '';
    const dryRunEnabled = Boolean(config.dry_run);
    const maxHosts = config.max_hosts || 1;
    const maxUploadBytes = config.max_upload_bytes || 0;
    const osPasswordMinLength = config.os_password_min_length || 8;

// Modo «Ejecutar ahora» / «Programar»: oculta los botones de ejecución y
// muestra los campos de programación sin duplicar el formulario.
(function () {
    const intentSchedule = document.getElementById('intentSchedule');
    const intentNow = document.getElementById('intentNow');
    const scheduleFields = document.getElementById('scheduleFields');
    const nowActions = document.getElementById('nowActions');
    const scheduleActions = document.getElementById('scheduleActions');
    const nowHelp = document.getElementById('nowHelp');
    if (!scheduleFields || !nowActions || !scheduleActions) return;

    function syncIntent() {
        const scheduling = Boolean(intentSchedule && intentSchedule.checked);
        scheduleFields.classList.toggle('d-none', !scheduling);
        nowActions.classList.toggle('d-none', scheduling);
        scheduleActions.classList.toggle('d-none', !scheduling);
        if (nowHelp) nowHelp.classList.toggle('d-none', scheduling);
        ['scheduleName', 'scheduleRunAt'].forEach(function (id) {
            const field = document.getElementById(id);
            if (field) field.required = scheduling;
        });
    }

    if (intentNow) intentNow.addEventListener('change', syncIntent);
    if (intentSchedule) intentSchedule.addEventListener('change', syncIntent);

    const planificarBtn = document.getElementById('planificarBtn');
    if (planificarBtn && intentSchedule) {
        planificarBtn.addEventListener('click', function () {
            intentSchedule.checked = true;
            syncIntent();
            scheduleFields.scrollIntoView({ behavior: 'smooth', block: 'center' });
            const nameField = document.getElementById('scheduleName');
            if (nameField) nameField.focus();
        });
    }
    // «Nueva programación» llega con mode=schedule: deja el formulario listo
    // para guardar la operación sin ejecutarla.
    if (prefill && prefill.schedule_intent === 'schedule' && intentSchedule) {
        intentSchedule.checked = true;
        if (intentNow) intentNow.checked = false;
    }
    syncIntent();
})();

function splitTargetList(value) {
    return (value || '').split(',').map((piece) => piece.trim()).filter(Boolean);
}

function describeTarget(value) {
    if (value.includes(',')) {
        return value + ' (' + splitTargetList(value).length + ' entradas)';
    }
    if (value.includes('/')) return value + ' (CIDR)';
    if (value.includes('-')) return value + ' (rango)';
    return value;
}

function selectedWallpaperName() {
    const input = document.getElementById('wallpaperFile');
    if (!input || !input.files || !input.files.length) return '';
    return input.files[0].name || '';
}

function aptThresholdText() {
    const input = document.getElementById('minFreeGb');
    if (!input || !input.value) return 'umbral por defecto';
    return 'umbral mínimo ' + input.value + ' GB';
}

function actionDisplayValue() {
    const action = document.getElementById('action');
    if (action && action.value === 'WALLPAPER') {
        return selectedWallpaperName() || '—';
    }
    if (action && action.value === 'APT') {
        return 'Actualización segura (' + aptThresholdText() + ')';
    }
    if (action && action.value === 'PASSWORD') {
        const user = document.getElementById('targetUser');
        const userText = (user && user.value.trim()) || '—';
        return 'Cambio de contraseña de ' + userText;
    }
    if (action && action.value === 'FIREWALL') {
        return firewallSummaryText();
    }
    const valueInput = document.getElementById('actionValue');
    return (valueInput && valueInput.value) || '—';
}

function firewallSummaryText() {
    const op = document.getElementById('firewallOp');
    const ip = document.getElementById('firewallIp');
    if (!op) return '—';
    if (op.value === 'enable') return 'Activar el firewall';
    if (op.value === 'disable') return 'Desactivar el firewall';
    const source = (ip && ip.value.trim()) || '—';
    return 'Permitir ' + source + ' (recarga el firewall)';
}

function validateWallpaperFile(event) {
    const action = document.getElementById('action');
    if (!action || action.value !== 'WALLPAPER') return true;
    const input = document.getElementById('wallpaperFile');
    if (!input || !input.files || !input.files.length) {
        event.preventDefault();
        if (input) {
            input.classList.add('is-invalid');
            input.focus();
        }
        return false;
    }
    if (input.files[0].size > maxUploadBytes) {
        event.preventDefault();
        input.classList.add('is-invalid');
        input.focus();
        return false;
    }
    input.classList.remove('is-invalid');
    return true;
}

function validatePasswordFields(event) {
    const action = document.getElementById('action');
    if (!action || action.value !== 'PASSWORD') return true;
    const user = document.getElementById('targetUser');
    const password = document.getElementById('newPassword');
    const confirm = document.getElementById('newPasswordConfirm');
    const execute = !!(event.submitter
        && (event.submitter.value === 'execute'
            || event.submitter.value === 'schedule'));
    for (const field of [user, password, confirm]) {
        if (field) field.classList.remove('is-invalid');
    }
    // La vista previa solo comprueba que el usuario exista: no pide secreto.
    if (!execute) return true;

    let firstInvalid = null;
    if (!user || !user.value.trim()) {
        if (user) user.classList.add('is-invalid');
        firstInvalid = firstInvalid || user;
    }
    const secret = password ? password.value : '';
    if (secret.length < osPasswordMinLength) {
        if (password) password.classList.add('is-invalid');
        firstInvalid = firstInvalid || password;
    }
    if (!confirm || confirm.value !== secret) {
        if (confirm) confirm.classList.add('is-invalid');
        firstInvalid = firstInvalid || confirm;
    }
    if (firstInvalid) {
        event.preventDefault();
        firstInvalid.focus();
        return false;
    }
    return true;
}

const WALLPAPER_PREVIEW_TYPES = {
    'image/png': true,
    'image/jpeg': true,
    'image/webp': true,
    'image/gif': true,
    'image/bmp': true,
    'image/x-ms-bmp': true
};

function clearWallpaperPreview() {
    const img = document.getElementById('wallpaperPreviewImg');
    const meta = document.getElementById('wallpaperPreviewMeta');
    const empty = document.getElementById('wallpaperPreviewEmpty');
    if (!img) return;
    img.removeAttribute('src');
    img.alt = '';
    img.classList.add('d-none');
    if (meta) meta.classList.add('d-none');
    if (empty) empty.classList.remove('d-none');
}

function updateWallpaperPreview() {
    const field = document.getElementById('wallpaperPreviewField');
    if (!field) return;
    const action = document.getElementById('action');
    const input = document.getElementById('wallpaperFile');
    const show = !!(action && action.value === 'WALLPAPER');
    field.classList.toggle('d-none', !show);
    if (!show) {
        clearWallpaperPreview();
        return;
    }

    const file = input && input.files && input.files[0];
    const mime = file ? (file.type || '') : '';
    if (!file || file.size > maxUploadBytes
            || (mime && !WALLPAPER_PREVIEW_TYPES[mime])) {
        clearWallpaperPreview();
        return;
    }

    const img = document.getElementById('wallpaperPreviewImg');
    const meta = document.getElementById('wallpaperPreviewMeta');
    const empty = document.getElementById('wallpaperPreviewEmpty');
    const name = document.getElementById('wallpaperPreviewName');
    if (!img || !meta || !empty || !name) return;

    // La lectura es local: la imagen no se sube hasta enviar el formulario.
    const reader = new FileReader();
    reader.onload = () => {
        const actionElement = document.getElementById('action');
        if (!actionElement || actionElement.value !== 'WALLPAPER'
                || !input.files || input.files[0] !== file) return;
        img.src = reader.result;
        img.alt = 'Vista previa de ' + file.name;
        img.classList.remove('d-none');
        name.textContent = file.name;
        name.title = file.name;
        meta.classList.remove('d-none');
        empty.classList.add('d-none');
    };
    reader.onerror = () => {
        const actionElement = document.getElementById('action');
        if (!actionElement || actionElement.value !== 'WALLPAPER'
                || !input.files || input.files[0] !== file) return;
        clearWallpaperPreview();
    };
    reader.readAsDataURL(file);
}

function ipv4ToInt(ip) {
    const parts = ip.split('.');
    if (parts.length !== 4) return null;
    let result = 0;
    for (const part of parts) {
        if (!/^\d{1,3}$/.test(part)) return null;
        const value = Number(part);
        if (value > 255) return null;
        result = result * 256 + value;
    }
    return result;
}

function isValidIpv4(ip) {
    return ipv4ToInt(ip) !== null;
}

function normalizeIpv4(ip) {
    return ip.split('.').map((part) => String(Number(part))).join('.');
}

function normalizeTarget(value) {
    const raw = (value || '').trim();
    if (raw.includes(',')) {
        return splitTargetList(raw).map((piece) => normalizeTarget(piece)).join(',');
    }
    let target = raw;
    if (!/^\d/.test(target) || target.includes(':')) return target;
    if (target.includes('-')) {
        const parts = target.split('-');
        if (parts.length === 2 && isValidIpv4(parts[0].trim()) && isValidIpv4(parts[1].trim())) {
            return normalizeIpv4(parts[0].trim()) + '-' + normalizeIpv4(parts[1].trim());
        }
    }
    if (target.includes('/')) {
        const parts = target.split('/');
        if (parts.length === 2 && isValidIpv4(parts[0].trim())) {
            return normalizeIpv4(parts[0].trim()) + '/' + parts[1].trim();
        }
    }
    if (isValidIpv4(target)) return normalizeIpv4(target);
    return target;
}

function targetHostCount(value) {
    const target = (value || '').trim();
    if (target.includes('-')) {
        const parts = target.split('-');
        if (parts.length === 2 && isValidIpv4(parts[0].trim()) && isValidIpv4(parts[1].trim())) {
            const count = ipv4ToInt(parts[1].trim()) - ipv4ToInt(parts[0].trim()) + 1;
            return count > 0 ? count : 1;
        }
        return 1;
    }
    if (target.includes('/')) {
        const parts = target.split('/');
        const prefix = Number(parts[1]);
        if (parts.length === 2 && isValidIpv4(parts[0].trim())
                && Number.isInteger(prefix) && prefix >= 0 && prefix <= 32) {
            return Math.max(Math.pow(2, 32 - prefix) - 2, 1);
        }
    }
    return 1;
}

function validateTarget(value) {
    const target = (value || '').trim();
    if (!target) return 'La IP o rango es obligatorio.';
    if (target.includes(',')) {
        const pieces = splitTargetList(target);
        if (!pieces.length) return 'La IP o rango es obligatorio.';
        let total = 0;
        for (const piece of pieces) {
            const error = validateTarget(piece);
            if (error) return piece + ': ' + error;
            total += targetHostCount(piece);
        }
        if (total > maxHosts) {
            return 'Las entradas suman ' + total + ' hosts; el máximo es ' + maxHosts + '.';
        }
        return '';
    }
    // IPv6 se valida en el servidor; aquí solo se comprueba el formato básico.
    if (target.includes(':')) return '';
    if (target.includes('-')) {
        const parts = target.split('-');
        if (parts.length !== 2) return 'Rango inválido: usa inicio-fin.';
        const startText = parts[0].trim();
        const endText = parts[1].trim();
        if (!isValidIpv4(startText) || !isValidIpv4(endText)) {
            return 'Rango inválido: usa inicio-fin, por ejemplo 172.17.128.118-172.17.128.235.';
        }
        const start = ipv4ToInt(startText);
        const end = ipv4ToInt(endText);
        if (start > end) {
            return 'El inicio del rango debe ser menor o igual que el fin.';
        }
        const count = end - start + 1;
        if (count > maxHosts) {
            return 'El rango incluye ' + count + ' hosts; el máximo es ' + maxHosts + '.';
        }
        return '';
    }
    if (target.includes('/')) {
        const parts = target.split('/');
        if (parts.length !== 2) return 'CIDR inválido: usa IP/prefijo.';
        const ip = parts[0].trim();
        const prefix = parts[1].trim();
        if (!isValidIpv4(ip) || !/^\d{1,2}$/.test(prefix)) {
            return 'CIDR inválido: usa por ejemplo 192.168.1.0/24.';
        }
        const prefixNumber = Number(prefix);
        if (prefixNumber < 0 || prefixNumber > 32) {
            return 'El prefijo CIDR debe estar entre 0 y 32.';
        }
        return '';
    }
    if (isValidIpv4(target)) return '';
    // Hostname: el servidor lo resuelve y valida contra la allowlist.
    if (/^[A-Za-z][A-Za-z0-9.-]*$/.test(target)) return '';
    return 'Formato inválido. Usa IP, CIDR o rango inicio-fin.';
}

function targetMaskAllows(value) {
    const trimmed = value.replace(/^\s+/, '');
    if (!/^\d/.test(trimmed)) return true; // hostname o IPv6
    if (/[^0-9.\-\/, ]/.test(value)) return false;
    return value.split(',').every((piece) => singleTargetMaskAllows(piece));
}

function singleTargetMaskAllows(value) {
    const trimmed = value.replace(/^\s+/, '');
    if (!/^\d/.test(trimmed)) return true; // hostname o IPv6

    if ((value.match(/-/g) || []).length > 1) return false;
    if ((value.match(/\//g) || []).length > 1) return false;
    if (value.includes('-') && value.includes('/')) return false;

    function octetsOk(part) {
        part = part.trim();
        const octets = part.split('.');
        if (octets.length > 4) return false;
        for (const octet of octets) {
            if (octet.length > 3) return false;
            if (octet !== '' && Number(octet) > 255) return false;
        }
        return true;
    }

    const firstIp = value.split(/[-\/]/)[0];
    if (!octetsOk(firstIp)) return false;
    if (value.includes('-') && !octetsOk(value.split('-')[1])) return false;
    if (value.includes('/')) {
        const prefix = value.split('/')[1].trim();
        if (prefix.length > 2) return false;
        if (prefix !== '' && Number(prefix) > 32) return false;
    }
    return true;
}

const targetInput = document.getElementById('targetIp');
const targetError = document.getElementById('targetError');
let lastAllowedTarget = targetInput ? targetInput.value : '';

function setTargetError(message, native) {
    if (!targetInput) return;
    targetInput.classList.toggle('is-invalid', Boolean(message));
    if (targetError) targetError.textContent = message || '';
    if (native) targetInput.setCustomValidity(message || '');
}

if (targetInput) {
    targetInput.addEventListener('input', () => {
        if (!targetMaskAllows(targetInput.value)) {
            targetInput.value = lastAllowedTarget;
            return;
        }
        lastAllowedTarget = targetInput.value;
        setTargetError(validateTarget(targetInput.value), false);
        updateSummary();
    });
    targetInput.addEventListener('blur', () => {
        const normalized = normalizeTarget(targetInput.value);
        if (normalized !== targetInput.value) {
            targetInput.value = normalized;
            lastAllowedTarget = normalized;
        }
        setTargetError(validateTarget(targetInput.value), true);
        updateSummary();
    });
}

const targetModeManual = document.getElementById('targetModeManual');
const targetModeGroup = document.getElementById('targetModeGroup');
const targetManualField = document.getElementById('targetManualField');
const targetGroupField = document.getElementById('targetGroupField');
const targetGroupSelect = document.getElementById('targetGroup');

function currentTargetMode() {
    if (targetModeGroup && targetModeGroup.checked) return 'group';
    return 'manual';
}

function updateTargetModeFields() {
    const mode = currentTargetMode();
    targetManualField.classList.toggle('d-none', mode !== 'manual');
    targetGroupField.classList.toggle('d-none', mode !== 'group');
    if (targetInput) targetInput.required = mode === 'manual';
    if (targetGroupSelect) targetGroupSelect.required = mode === 'group';
    updateSafetyPanel();
    updateSummary();
}

function updateSafetyPanel() {
    const panel = document.getElementById('safetyPanel');
    if (!panel) return;
    const overrideBox = document.getElementById('safetyOverrideBox');
    const reasonBox = document.getElementById('safetyReasonBox');
    const overrideCheck = document.getElementById('safetyOverride');
    let data = null;
    if (currentTargetMode() === 'group' && targetGroupSelect) {
        const option = targetGroupSelect.options[targetGroupSelect.selectedIndex];
        if (option && option.value) data = option.dataset;
    }
    if (!data) {
        panel.classList.add('d-none');
        if (overrideBox) overrideBox.classList.add('d-none');
        if (reasonBox) reasonBox.classList.add('d-none');
        if (overrideCheck) overrideCheck.checked = false;
        return;
    }
    const blocked = data.blocked === '1';
    const title = document.getElementById('safetyTitle');
    const text = document.getElementById('safetyText');
    panel.classList.remove('d-none', 'banner-warn', 'banner-info', 'banner-danger');
    panel.classList.add(blocked ? 'banner-danger' : 'banner-info');
    if (title) {
        title.textContent = blocked
            ? 'Ejecución bloqueada por ventana de mantenimiento'
            : 'Política del grupo';
    }
    if (text) {
        const parts = [];
        if (data.criticality) parts.push('Criticidad: ' + data.criticality + '.');
        if (data.window) parts.push('Ventana: ' + data.window + '.');
        parts.push(data.summary || '');
        if (blocked && data.nextOpen) parts.push('Próxima apertura: ' + data.nextOpen + '.');
        text.textContent = parts.join(' ');
    }
    if (overrideBox) {
        overrideBox.classList.toggle('d-none', !blocked);
        if (!blocked && overrideCheck) overrideCheck.checked = false;
        if (reasonBox) {
            reasonBox.classList.toggle('d-none', !blocked || !overrideCheck || !overrideCheck.checked);
        }
    }
}

if (targetGroupSelect) {
    targetGroupSelect.addEventListener('change', () => {
        updateSafetyPanel();
        updateSummary();
    });
    const overrideCheck = document.getElementById('safetyOverride');
    if (overrideCheck) {
        overrideCheck.addEventListener('change', () => {
            const reasonBox = document.getElementById('safetyReasonBox');
            if (reasonBox) reasonBox.classList.toggle('d-none', !overrideCheck.checked);
        });
    }
}

if (targetModeManual && targetModeGroup) {
    for (const radio of [targetModeManual, targetModeGroup]) {
        radio.addEventListener('change', updateTargetModeFields);
    }
}

function updateSummary() {
    const target = document.getElementById('targetIp');
    const targetValue = target ? target.value.trim() : '';
    const targetEl = document.getElementById('sumTarget');
    if (targetEl) {
        if (currentTargetMode() === 'group') {
            const option = targetGroupSelect && targetGroupSelect.selectedIndex > 0
                ? targetGroupSelect.options[targetGroupSelect.selectedIndex] : null;
            targetEl.textContent = option ? 'Grupo: ' + option.textContent : '—';
        } else {
            targetEl.textContent = targetValue ? describeTarget(targetValue) : '—';
        }
    }

    const action = document.getElementById('action');
    const actionEl = document.getElementById('sumAction');
    if (action && actionEl) {
        actionEl.textContent = action.options[action.selectedIndex].text;
    }

    const valueInput = document.getElementById('actionValue');
    let value = valueInput ? valueInput.value : '';
    if (action && action.value === 'WALLPAPER') {
        value = selectedWallpaperName();
    } else if (action && action.value === 'APT') {
        value = 'Actualización segura · ' + aptThresholdText();
    } else if (action && action.value === 'PASSWORD') {
        const user = document.getElementById('targetUser');
        value = 'Cambio de contraseña de ' + ((user && user.value.trim()) || '—');
    } else if (action && action.value === 'FIREWALL') {
        value = firewallSummaryText();
    }
    const timeMode = document.getElementById('timeMode');
    const timeZone = document.getElementById('timeZone');
    if (action && action.value === 'TIME' && timeMode && timeMode.value === 'local'
            && timeZone && timeZone.value.trim()) {
        value = (value || '—') + ' · ' + timeZone.value.trim();
    }
    const valueEl = document.getElementById('sumValue');
    if (valueEl) valueEl.textContent = value || '—';

    const ntp = document.getElementById('ntpPolicy');
    const ntpEl = document.getElementById('sumNtp');
    if (ntpEl) {
        ntpEl.textContent = ntp ? ntp.options[ntp.selectedIndex].text : 'sin cambios (keep)';
    }

    const origin = document.getElementById('passwordOrigin');
    const credsEl = document.getElementById('sumCreds');
    if (origin && credsEl) {
        credsEl.textContent = origin.options[origin.selectedIndex].text;
    }

    const modeEl = document.getElementById('sumMode');
    if (modeEl) {
        modeEl.textContent = dryRunEnabled ? 'Simulación (DRY RUN)' : 'Ejecución real';
    }

    const riskEl = document.getElementById('sumRisk');
    if (riskEl) {
        riskEl.textContent = dryRunEnabled ? 'Simulación' : 'Real';
        riskEl.className = 'badge ' + (dryRunEnabled ? 'badge-accent' : 'bg-warning');
    }
}

function updatePasswordFields(origin) {
    const stored = origin === 'stored';
    const masterField = document.getElementById('masterPasswordField');
    const masterInput = masterField.querySelector('input');
    const fileField = document.getElementById('temporalFileField');
    const fileInput = document.getElementById('passwordFile');

    masterField.classList.toggle('d-none', !stored);
    if (masterInput) masterInput.required = stored;

    fileField.classList.toggle('d-none', stored);
    if (fileInput) fileInput.required = !stored;
    updateSummary();
}

function updateNtpFields(policy) {
    const field = document.getElementById('ntpServersField');
    const input = document.getElementById('ntpServers');
    const configure = policy === 'configure';
    if (field) field.classList.toggle('d-none', !configure);
    if (input) {
        input.required = configure;
        if (configure && !input.value) input.value = defaultNtpServers;
    }
    updateSummary();
}

function updateTimeModeField(action) {
    const field = document.getElementById('timeModeField');
    if (!field) return; // Tareas de solo TZ no publican el campo hora local
    field.classList.toggle('d-none', action !== 'TIME');
    updateLocalFields();
}

function updateLocalFields() {
    const timeModeElement = document.getElementById('timeMode');
    const timeZoneElement = document.getElementById('timeZone');
    const timeZoneField = document.getElementById('timeZoneField');
    const timeFoldField = document.getElementById('timeFoldField');
    if (!timeModeElement || !timeModeFieldExists()) return;
    const action = document.getElementById('action').value;
    const mode = timeModeElement.value;
    const show = action === 'TIME' && mode === 'local';
    timeZoneField.classList.toggle('d-none', !show);
    timeFoldField.classList.toggle('d-none', !show);
    if (timeZoneElement) timeZoneElement.required = show;
    updateSummary();
}

function timeModeFieldExists() {
    return !!document.getElementById('timeZoneField');
}

function updateValueField(action, currentValue) {
    const valueField = document.getElementById('valueField');
    const aptField = document.getElementById('aptField');
    const passwordUserField = document.getElementById('passwordUserField');
    const passwordSecretField = document.getElementById('passwordSecretField');
    const existing = document.getElementById('actionValue');
    if (aptField) aptField.classList.toggle('d-none', action !== 'APT');
    if (passwordUserField) passwordUserField.classList.toggle('d-none', action !== 'PASSWORD');
    if (passwordSecretField) passwordSecretField.classList.toggle('d-none', action !== 'PASSWORD');
    if (valueField) {
        valueField.classList.toggle(
            'd-none',
            action === 'APT' || action === 'PASSWORD' || action === 'FIREWALL'
        );
    }
    updateFirewallFields();
    // Las tareas con valor fijo (p. ej. America/Caracas) no cambian el campo.
    if (existing && existing.dataset.fixed) {
        updateSummary();
        return;
    }
    valueField.innerHTML = '';

    if (action === 'TZ') {
        valueField.innerHTML = '<label for="actionValue" class="form-label">Zona Horaria</label>' +
            '<input list="timezoneList" type="text" class="form-control" id="actionValue" name="value" ' +
            'placeholder="Ej: America/Argentina/Buenos_Aires" required autocomplete="off">';
        if (currentValue) {
            document.getElementById('actionValue').value = currentValue;
        }
    } else if (action === 'TIME') {
        valueField.innerHTML = '<label for="actionValue" class="form-label">Hora Exacta</label>' +
            '<input type="datetime-local" class="form-control" id="actionValue" name="value" ' +
            'required>';
        if (currentValue) {
            document.getElementById('actionValue').value = currentValue;
        }
    }
    updateSummary();
}

function updateFirewallFields() {
    const action = document.getElementById('action');
    const op = document.getElementById('firewallOp');
    const opField = document.getElementById('firewallOpField');
    const ipField = document.getElementById('firewallIpField');
    const ip = document.getElementById('firewallIp');
    if (!op || !opField || !ipField) return;
    const show = !!(action && action.value === 'FIREWALL');
    const allow = show && op.value === 'allow';
    opField.classList.toggle('d-none', !show);
    if (ipField) ipField.classList.toggle('d-none', !allow);
    if (ip) ip.required = allow;
    updateSummary();
}

// Confirmación explícita antes de ejecutar (no para vista previa).
let confirmedSubmit = false;
let pendingSubmitter = null;

const execForm = document.getElementById('execForm');
const confirmModalElement = document.getElementById('confirmModal');
// El modal está dentro de .app-shell, que crea un stacking context
// (z-index: 1). Bootstrap añade el backdrop directamente al body con
// z-index 1050, por lo que el backdrop quedaría por encima del modal y
// bloquearía sus botones. Moverlo al body lo saca de ese contexto.
document.body.appendChild(confirmModalElement);
const confirmModal = new bootstrap.Modal(confirmModalElement);

execForm.addEventListener('submit', (event) => {
    if (!validateWallpaperFile(event)) return;
    if (!validatePasswordFields(event)) return;
    if (currentTargetMode() === 'group') {
        if (targetGroupSelect && !targetGroupSelect.value) {
            event.preventDefault();
            targetGroupSelect.classList.add('is-invalid');
            targetGroupSelect.focus();
            return;
        }
        targetGroupSelect.classList.remove('is-invalid');
        if (confirmedSubmit) return;
        if (!event.submitter || event.submitter.value !== 'execute') return;

        event.preventDefault();
        pendingSubmitter = event.submitter;
        const option = targetGroupSelect.options[targetGroupSelect.selectedIndex];
        document.getElementById('confirmHosts').textContent =
            'todos los hosts del grupo ' + option.textContent;
        document.getElementById('confirmTarget').textContent = 'inventario:' + targetGroupSelect.value;
        document.getElementById('confirmAction').textContent =
            document.getElementById('action').options[document.getElementById('action').selectedIndex].text;
        document.getElementById('confirmValue').textContent =
            actionDisplayValue();
        const ntp = document.getElementById('ntpPolicy');
        document.getElementById('confirmNtp').textContent =
            ntp ? ntp.options[ntp.selectedIndex].text : 'sin cambios (keep)';
        document.getElementById('confirmMode').textContent = dryRunEnabled
            ? 'DRY RUN (simulación, sin cambios reales)'
            : 'Ejecución real';
        confirmModal.show();
        return;
    }

    const targetElement = document.getElementById('targetIp');
    const normalizedTarget = normalizeTarget(targetElement.value);
    if (normalizedTarget !== targetElement.value) {
        targetElement.value = normalizedTarget;
        lastAllowedTarget = normalizedTarget;
    }
    const targetValue = normalizedTarget.trim();
    const targetValidation = validateTarget(targetValue);
    if (targetValidation) {
        event.preventDefault();
        setTargetError(targetValidation, true);
        targetElement.focus();
        return;
    }
    setTargetError('', true);

    if (confirmedSubmit) return;
    if (!event.submitter || event.submitter.value !== 'execute') return;

    event.preventDefault();
    pendingSubmitter = event.submitter;

    const target = document.getElementById('targetIp').value.trim();
    const action = document.getElementById('action');
    const ntp = document.getElementById('ntpPolicy');
    const ntpText = ntp
        ? ntp.options[ntp.selectedIndex].text
        : 'sin cambios (keep)';

    let confirmHosts;
    if (target.includes(',')) {
        confirmHosts = 'todos los hosts de las ' + splitTargetList(target).length + ' entradas';
    } else if (target.includes('/')) {
        confirmHosts = 'todos los hosts del CIDR ' + target;
    } else if (target.includes('-')) {
        confirmHosts = 'todos los hosts del rango ' + target;
    } else {
        confirmHosts = 'el host ' + target;
    }
    document.getElementById('confirmHosts').textContent = confirmHosts;
    document.getElementById('confirmTarget').textContent = target || '—';
    document.getElementById('confirmAction').textContent =
        action.options[action.selectedIndex].text;
    document.getElementById('confirmValue').textContent =
        actionDisplayValue();
    document.getElementById('confirmNtp').textContent = ntpText;
    document.getElementById('confirmMode').textContent = dryRunEnabled
        ? 'DRY RUN (simulación, sin cambios reales)'
        : 'Ejecución real';

    confirmModal.show();
});

document.getElementById('confirmExecute').addEventListener('click', () => {
    confirmedSubmit = true;
    confirmModal.hide();
    execForm.requestSubmit(pendingSubmitter || undefined);
});

// Inicializar campos al cargar
document.addEventListener('DOMContentLoaded', () => {
    const actionSelect = document.getElementById('action');
    const originSelect = document.getElementById('passwordOrigin');
    const ntpSelect = document.getElementById('ntpPolicy');

    updateValueField(actionSelect.value, prefill.value || '');
    updatePasswordFields(originSelect.value);
    updateNtpFields(ntpSelect ? ntpSelect.value : 'keep');
    updateTimeModeField(actionSelect.value);
    updateTargetModeFields();
    updateWallpaperPreview();
    updateFirewallFields();

    originSelect.addEventListener('change', (event) => {
        updatePasswordFields(event.target.value);
    });

    actionSelect.addEventListener('change', (event) => {
        updateValueField(event.target.value, '');
        updateTimeModeField(event.target.value);
        updateWallpaperPreview();
    });

    const wallpaperInput = document.getElementById('wallpaperFile');
    if (wallpaperInput) {
        wallpaperInput.addEventListener('change', updateWallpaperPreview);
    }

    const firewallOpSelect = document.getElementById('firewallOp');
    if (firewallOpSelect) {
        firewallOpSelect.addEventListener('change', updateFirewallFields);
    }
    const firewallIpInput = document.getElementById('firewallIp');
    if (firewallIpInput) {
        firewallIpInput.addEventListener('input', updateSummary);
    }

    if (ntpSelect) {
        ntpSelect.addEventListener('change', (event) => {
            updateNtpFields(event.target.value);
        });
    }

    const timeModeSelect = document.getElementById('timeMode');
    if (timeModeSelect) {
        timeModeSelect.addEventListener('change', updateLocalFields);
    }

    const summaryForm = document.getElementById('execForm');
    if (summaryForm) {
        summaryForm.addEventListener('input', updateSummary);
        summaryForm.addEventListener('change', updateSummary);
    }
    updateSummary();
});
})();
