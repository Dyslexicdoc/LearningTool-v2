/**
 * search.js — Semantic search panel.
 *
 * Wires the top-bar search input to /api/search, shows results in a side
 * panel, and lets the user jump to a node in another session.
 */

const Search = (() => {
    const DEBOUNCE_MS = 300;
    let debounceTimer = null;
    let available = null;  // tri-state: null=unknown, true/false after first check
    let lastMode = null;   // 'search' | 'similar'

    function init() {
        const input = document.getElementById('global-search');
        const closeBtn = document.getElementById('btn-close-search');
        if (!input) return;

        // Lazy availability check on first focus
        input.addEventListener('focus', checkAvailability, { once: true });

        input.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            const q = input.value.trim();
            if (!q) {
                hidePanel();
                return;
            }
            debounceTimer = setTimeout(() => runSearch(q), DEBOUNCE_MS);
        });

        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                clearTimeout(debounceTimer);
                const q = input.value.trim();
                if (q) runSearch(q);
            } else if (e.key === 'Escape') {
                input.value = '';
                hidePanel();
                input.blur();
            }
        });

        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                input.value = '';
                hidePanel();
            });
        }

        // Global shortcut: Ctrl/Cmd+K focuses search
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                input.focus();
                input.select();
            }
        });
    }

    async function checkAvailability() {
        try {
            const status = await API.embeddingsStatus();
            available = !!status.available;
            const input = document.getElementById('global-search');
            if (!available) {
                input.placeholder = '🔎 Search disabled — see Settings';
                input.title = status.reason || 'Semantic search is not enabled';
                input.disabled = true;
                input.style.opacity = '0.6';
            } else {
                const count = status.indexed_node_count || 0;
                input.title = `Semantic search across ${count} indexed nodes`;
            }
        } catch (e) {
            available = false;
            console.warn('Embeddings status check failed:', e);
        }
    }

    async function runSearch(query) {
        if (available === false) return;
        showPanel();
        lastMode = 'search';
        const resultsEl = document.getElementById('search-results');
        resultsEl.innerHTML = '<div class="search-loading">Searching…</div>';
        try {
            const data = await API.searchNodes(query, { k: 15 });
            renderResults(data.results, { emptyMsg: 'No matching nodes found.' });
        } catch (e) {
            resultsEl.innerHTML = `<div class="search-error">Search failed: ${escapeHTML(String(e))}</div>`;
        }
    }

    /** Show the panel with nodes similar to a given node. Public API. */
    async function showSimilar(nodeId) {
        if (available === false) return;
        if (available === null) await checkAvailability();
        if (available === false) return;
        showPanel();
        lastMode = 'similar';
        const resultsEl = document.getElementById('search-results');
        document.querySelector('.search-panel-title').textContent = 'Related nodes';
        resultsEl.innerHTML = '<div class="search-loading">Finding related nodes…</div>';
        try {
            const data = await API.similarNodes(nodeId, { k: 8 });
            renderResults(data.results, { emptyMsg: 'No related nodes found yet.' });
        } catch (e) {
            resultsEl.innerHTML = `<div class="search-error">Lookup failed: ${escapeHTML(String(e))}</div>`;
        }
    }

    function renderResults(results, opts = {}) {
        const resultsEl = document.getElementById('search-results');
        if (!results || results.length === 0) {
            resultsEl.innerHTML = `<div class="search-empty">${escapeHTML(opts.emptyMsg || 'No results.')}</div>`;
            return;
        }
        // Group by session
        const bySession = new Map();
        for (const r of results) {
            if (!bySession.has(r.session_id)) {
                bySession.set(r.session_id, { name: r.session_name, items: [] });
            }
            bySession.get(r.session_id).items.push(r);
        }

        resultsEl.innerHTML = '';
        const currentSessionId = Session.getCurrent()?.id;
        for (const [sid, group] of bySession.entries()) {
            const groupEl = document.createElement('div');
            groupEl.className = 'search-group';
            const isCurrent = sid === currentSessionId;
            groupEl.innerHTML = `
                <div class="search-group-header">
                    ${escapeHTML(group.name || '(untitled session)')}
                    ${isCurrent ? '<span class="search-current-tag">current</span>' : ''}
                </div>
            `;
            for (const r of group.items) {
                const item = document.createElement('div');
                item.className = 'search-result';
                const promptLine = r.prompt_text || '(no prompt)';
                const snippet = r.snippet || '';
                const modeTag = r.prompt_mode && r.prompt_mode !== 'initial'
                    ? `<span class="search-mode-tag">${escapeHTML(r.prompt_mode)}</span>` : '';
                item.innerHTML = `
                    <div class="search-result-prompt">${modeTag}${escapeHTML(promptLine)}</div>
                    <div class="search-result-snippet">${escapeHTML(snippet)}</div>
                `;
                item.addEventListener('click', () => {
                    if (sid === currentSessionId) {
                        // Same session — just pan/highlight
                        focusNode(r.node_id);
                    } else {
                        // Different session — load it, then focus the node
                        Session.loadSession(sid).then(() => {
                            // Small delay so DOM settles after load
                            setTimeout(() => focusNode(r.node_id), 150);
                        });
                    }
                });
                groupEl.appendChild(item);
            }
            resultsEl.appendChild(groupEl);
        }
    }

    function focusNode(nodeId) {
        const el = document.getElementById(nodeId);
        if (!el) return;
        // Pan canvas to center on this node, then flash it
        if (typeof Canvas !== 'undefined' && Canvas.centerOn) {
            Canvas.centerOn(el);
        } else {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        el.classList.add('search-flash');
        setTimeout(() => el.classList.remove('search-flash'), 1200);
    }

    function showPanel() {
        const panel = document.getElementById('search-panel');
        if (panel) panel.classList.remove('hidden');
        document.querySelector('.search-panel-title').textContent = 'Search results';
    }

    function hidePanel() {
        const panel = document.getElementById('search-panel');
        if (panel) panel.classList.add('hidden');
    }

    function escapeHTML(s) {
        const div = document.createElement('div');
        div.textContent = String(s ?? '');
        return div.innerHTML;
    }

    return { init, showSimilar, hidePanel };
})();
