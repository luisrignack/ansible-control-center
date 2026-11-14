/*
 * Edición de programaciones: alterna objetivo manual/grupo y muestra los
 * campos que dependen de la acción seleccionada.
 */
(function () {
    var manual = document.getElementById('editTargetManual');
    var group = document.getElementById('editTargetGroup');
    var ipInput = document.getElementById('editTargetIp');
    var groupSelect = document.getElementById('editTargetGroupSelect');
    function updateTarget() {
        if (!manual || !group) return;
        ipInput.classList.toggle('d-none', !manual.checked);
        groupSelect.classList.toggle('d-none', !group.checked);
        ipInput.required = manual.checked;
        groupSelect.required = group.checked;
    }
    if (manual && group) {
        manual.addEventListener('change', updateTarget);
        group.addEventListener('change', updateTarget);
        updateTarget();
    }

    var action = document.getElementById('editAction');
    var task = document.getElementById('editTask');
    var valueInput = document.getElementById('editValue');
    var valueField = document.getElementById('editValueField');
    var minFreeField = document.getElementById('editMinFreeGbField');
    var ntpPolicyField = document.getElementById('editNtpPolicyField');
    var ntpServersField = document.getElementById('editNtpServersField');
    var timeOptionsField = document.getElementById('editTimeOptionsField');
    var passwordUserField = document.getElementById('editPasswordUserField');
    var passwordSecretField = document.getElementById('editPasswordSecretField');
    var passwordConfirmField = document.getElementById('editPasswordConfirmField');
    var firewallOpField = document.getElementById('editFirewallOpField');
    var firewallIpField = document.getElementById('editFirewallIpField');
    var firewallOp = document.getElementById('editFirewallOp');
    var wallpaperField = document.getElementById('editWallpaperField');

    function updateFirewallFields() {
        var isFirewall = action && action.value === 'FIREWALL';
        var allow = isFirewall && firewallOp && firewallOp.value === 'allow';
        if (firewallOpField) firewallOpField.classList.toggle('d-none', !isFirewall);
        if (firewallIpField) firewallIpField.classList.toggle('d-none', !allow);
        var ipInput = document.getElementById('editFirewallIp');
        if (ipInput) ipInput.required = allow;
    }

    function updateAction() {
        if (!action || !valueInput) return;
        var current = action.value;
        var isTime = current === 'TZ' || current === 'TIME';
        var isApt = current === 'APT';
        var isPassword = current === 'PASSWORD';
        var isWallpaper = current === 'WALLPAPER';
        if (valueField) valueField.classList.toggle('d-none', !isTime);
        valueInput.disabled = !isTime;
        valueInput.required = isTime;
        if (minFreeField) minFreeField.classList.toggle('d-none', !isApt);
        if (ntpPolicyField) ntpPolicyField.classList.toggle('d-none', !isTime);
        if (ntpServersField) ntpServersField.classList.toggle('d-none', !isTime);
        if (timeOptionsField) timeOptionsField.classList.toggle('d-none', current !== 'TIME');
        if (passwordUserField) passwordUserField.classList.toggle('d-none', !isPassword);
        if (passwordSecretField) passwordSecretField.classList.toggle('d-none', !isPassword);
        if (passwordConfirmField) passwordConfirmField.classList.toggle('d-none', !isPassword);
        if (wallpaperField) wallpaperField.classList.toggle('d-none', !isWallpaper);
        var passwordInput = document.getElementById('editNewPassword');
        var passwordConfirm = document.getElementById('editNewPasswordConfirm');
        var hasSecret = passwordInput && passwordInput.dataset.hasSecret === '1';
        if (passwordInput) passwordInput.required = isPassword && !hasSecret;
        if (passwordConfirm) passwordConfirm.required = isPassword && !hasSecret;
        var wallpaperInput = document.getElementById('editWallpaperFile');
        var hasWallpaper = wallpaperInput && wallpaperInput.dataset.hasWallpaper === '1';
        if (wallpaperInput) wallpaperInput.required = isWallpaper && !hasWallpaper;
        updateFirewallFields();
    }
    if (action) {
        action.addEventListener('change', updateAction);
        updateAction();
    }
    if (firewallOp) firewallOp.addEventListener('change', updateFirewallFields);
    if (task && action) {
        task.addEventListener('change', function () {
            var option = task.selectedOptions[0];
            if (!option) return;
            var actions = (option.dataset.actions || '').split(',').filter(Boolean);
            if (actions.length && actions.indexOf(action.value) === -1) {
                action.value = actions[0];
            }
            var fixed = option.dataset.fixed || '';
            if (fixed) valueInput.value = fixed;
            updateAction();
        });
    }
})();
