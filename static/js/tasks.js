/*
 * Catálogo de tareas: filtro incremental de tarjetas por texto.
 */
(function () {
    const input = document.getElementById('taskSearch');
    const grid = document.getElementById('taskGrid');
    const empty = document.getElementById('taskEmpty');
    if (!input || !grid) return;

    const cards = Array.prototype.slice.call(
        grid.querySelectorAll('.task-card[data-search]')
    );

    function filter() {
        const query = (input.value || '').trim().toLowerCase();
        let visible = 0;
        cards.forEach(function (card) {
            const match = !query || (card.dataset.search || '').indexOf(query) !== -1;
            card.hidden = !match;
            if (match) visible += 1;
        });
        if (empty) empty.hidden = visible !== 0 || !query;
    }

    input.addEventListener('input', filter);
    filter();
})();
