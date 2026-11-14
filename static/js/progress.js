/*
 * Página de progreso: stream SSE con polling de respaldo, consola, badges y
 * descarga del registro. La configuración llega en #progress-config.
 */
(function () {
    'use strict';

    const configNode = document.getElementById('progress-config');
    const config = configNode ? JSON.parse(configNode.textContent) : {};
    const taskId = config.task_id;
    const canCancel = Boolean(config.can_cancel);

    const progressBar = document.getElementById('progressBar');
    const progressMessage = document.getElementById('progressMessage');
    const progressLog = document.getElementById('progressLog');
    const progressConsole = document.getElementById('progressConsole');
    const batchLink = document.getElementById('batchLink');
    const batchLinkAnchor = batchLink.querySelector('a');
    const statusBadge = document.getElementById('statusBadge');
    const queueBadge = document.getElementById('queueBadge');
    const dryRunBadge = document.getElementById('dryRunBadge');
    const metaMode = document.getElementById('metaMode');
    const metaOperator = document.getElementById('metaOperator');
    const metaHosts = document.getElementById('metaHosts');
    const metaBatch = document.getElementById('metaBatch');
    const metaTimeMode = document.getElementById('metaTimeMode');
    const metaNtp = document.getElementById('metaNtp');
    const autoScroll = document.getElementById('autoScroll');
    const clearConsole = document.getElementById('clearConsole');
    const downloadLog = document.getElementById('downloadLog');
    const cancelForm = document.getElementById('cancelForm');

    const STATUS_LABELS = {
        pending: ['En cola', 'bg-secondary'],
        running: ['En curso', 'bg-info text-dark'],
        preview: ['Vista previa', 'bg-info text-dark'],
        success: ['Completado', 'bg-success'],
        completed: ['Completado', 'bg-success'],
        partial: ['Parcial', 'bg-warning text-dark'],
        failure: ['Fallido', 'bg-danger'],
        failed: ['Fallido', 'bg-danger'],
        error: ['Error', 'bg-danger'],
        incomplete: ['Incompleto', 'bg-warning text-dark'],
        cancelled: ['Cancelado', 'bg-secondary'],
        not_found: ['No encontrado', 'bg-secondary'],
        unknown: ['Pendiente', 'bg-secondary']
    };

    let lastTask = {};
    let clearedAt = 0;
    let lastConsoleText = '';
    let pollTimer = null;
    let sseErrors = 0;

    function setStatus(task) {
        const key = task.overall_result || task.status || 'pending';
        const [label, css] = STATUS_LABELS[key] || [key, 'bg-secondary'];
        statusBadge.textContent = label;
        statusBadge.className = 'badge ' + css;
    }

    function renderTask(task) {
        lastTask = task;
        const value = Number(task.progress || 0);
        progressBar.value = value;
        progressBar.textContent = value + '%';
        progressMessage.textContent = task.message || '';
        setStatus(task);
        dryRunBadge.classList.toggle('d-none', !task.dry_run);
        const queuePosition = Number(task.queue_position || 0);
        queueBadge.classList.toggle('d-none', queuePosition <= 0);
        if (queuePosition > 0) {
            queueBadge.textContent = 'En cola: ' + queuePosition;
        }

        metaMode.textContent = (task.mode === 'preview' ? 'Vista previa' : 'Ejecución')
            + (task.dry_run ? ' (simulación)' : '');
        metaOperator.textContent = task.operator || '—';
        metaHosts.textContent = task.total_hosts != null ? task.total_hosts : '—';
        metaBatch.textContent = task.batch_id ? task.batch_id.slice(0, 8) + '…' : '—';
        metaTimeMode.textContent = task.time_mode || '—';
        metaNtp.textContent = task.ntp_policy || '—';

        const finished = Boolean(task.completed) || task.status === 'not_found';
        cancelForm.classList.toggle('d-none', !canCancel || finished || task.cancellation_requested);

        if (Array.isArray(task.log)) {
            progressLog.textContent = task.log.join('\n') || 'Sin eventos todavía.';
        }

        const consoleLines = Array.isArray(task.console) ? task.console : [];
        const visible = consoleLines.slice(clearedAt);
        const text = visible.join('\n');
        if (text !== lastConsoleText) {
            lastConsoleText = text;
            progressConsole.textContent = text || 'Esperando salida de Ansible…';
            if (autoScroll.checked) {
                progressConsole.scrollTop = progressConsole.scrollHeight;
            }
        }

        if (task.batch_id) {
            batchLinkAnchor.href = '/batches/' + encodeURIComponent(task.batch_id);
            batchLink.classList.remove('d-none');
        }

        if (finished) {
            stopPolling();
        }
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function startPolling() {
        if (pollTimer) {
            return;
        }
        source.close();
        progressMessage.textContent = 'SSE no disponible; consultando el estado por polling…';
        const tick = async () => {
            try {
                const response = await fetch(
                    '/api/task-status/' + encodeURIComponent(taskId),
                    { cache: 'no-store' }
                );
                if (!response.ok) {
                    return;
                }
                renderTask(await response.json());
            } catch (_error) {
                // Se reintenta en el siguiente ciclo.
            }
        };
        tick();
        pollTimer = setInterval(tick, 2000);
    }

    const source = new EventSource(
        `/progress/${encodeURIComponent(taskId)}/events`
    );

    source.addEventListener('progress', (event) => {
        renderTask(JSON.parse(event.data));
    });

    source.addEventListener('completed', (event) => {
        renderTask(JSON.parse(event.data));
        source.close();
        stopPolling();
    });

    source.addEventListener('timeout', (event) => {
        // El stream SSE está acotado a 90 segundos, pero un lote puede durar
        // hasta una hora. Al cerrarse el stream se sigue consultando el
        // estado persistido en lugar de dejar la página congelada.
        renderTask(JSON.parse(event.data));
        source.close();
        startPolling();
    });

    source.addEventListener('error', () => {
        sseErrors += 1;
        if (sseErrors >= 3) {
            startPolling();
        }
    });

    clearConsole.addEventListener('click', () => {
        clearedAt = (lastTask.console || []).length;
        lastConsoleText = '';
        progressConsole.textContent = 'Consola limpiada. Esperando nueva salida…';
    });

    downloadLog.addEventListener('click', () => {
        const lines = [
            'Ansible Control Center — ejecución ' + taskId,
            'Generado: ' + new Date().toISOString(),
            '',
            '=== RESUMEN ==='
        ];
        lines.push(...(lastTask.log || []));
        lines.push('', '=== SALIDA DE ANSIBLE ===');
        lines.push(...(lastTask.console || []));
        const blob = new Blob([lines.join('\n')], {
            type: 'text/plain;charset=utf-8'
        });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = 'ansible-control-center-' + taskId + '.log';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
    });
})();
