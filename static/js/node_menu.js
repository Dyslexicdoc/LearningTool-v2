/**
 * node_menu.js — Right-click context menu on a NODE (header / chrome area).
 * Distinct from context_menu.js which handles right-click on TEXT inside a node.
 *
 * Currently exposes:
 *   📋 Summarize subtree   — generates a summary node covering this node + descendants
 *
 * Designed to be extended: add more entries to the menu HTML and a handler below.
 */

const NodeMenu = (() => {
    let menuEl = null;
    let currentNodeId = null;

    function init() {
        document.addEventListener('contextmenu', onContextMenu, true);
        document.addEventListener('mousedown', (e) => {
            if (menuEl && !menuEl.contains(e.target)) hide();
        }, true);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') hide();
        });
    }

    function onContextMenu(e) {
        const nodeEl = e.target.closest('.lt-node');
        if (!nodeEl) return;

        // If the user has text selected inside response content, defer to the
        // existing text-selection menu in context_menu.js.
        const sel = window.getSelection();
        const inResponse = e.target.closest('.node-response-content');
        if (inResponse && sel && !sel.isCollapsed && sel.toString().trim()) return;

        e.preventDefault();
        e.stopPropagation();
        currentNodeId = nodeEl.id;
        showAt(e.clientX, e.clientY, nodeEl);
    }

    function showAt(x, y, nodeEl) {
        hide();
        const session = Session.getCurrent();
        const node = session ? session.nodes[currentNodeId] : null;
        const isSummary = node && node.prompt_mode === 'summary';
        const childCount = countDescendants(currentNodeId);

        menuEl = document.createElement('div');
        menuEl.className = 'node-menu';
        menuEl.innerHTML = `
            <button class="ctx-btn" data-action="summarize" ${childCount === 0 ? 'disabled' : ''}>
                📋 Summarize subtree
                ${childCount > 0 ? `<span class="ctx-hint">(${childCount} ${childCount === 1 ? 'descendant' : 'descendants'})</span>` : '<span class="ctx-hint">(no descendants)</span>'}
            </button>
            ${isSummary ? `
                <button class="ctx-btn" data-action="toggle-collapsed">
                    👁 Show/hide summarized nodes
                </button>
            ` : ''}
            ${typeof Search !== 'undefined' ? `
                <button class="ctx-btn" data-action="similar">
                    🔗 Find related nodes
                </button>
            ` : ''}
        `;

        // Position with viewport clamping
        document.body.appendChild(menuEl);
        const rect = menuEl.getBoundingClientRect();
        const px = Math.min(x, window.innerWidth - rect.width - 8);
        const py = Math.min(y, window.innerHeight - rect.height - 8);
        menuEl.style.left = `${px}px`;
        menuEl.style.top = `${py}px`;

        menuEl.querySelectorAll('.ctx-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                handleAction(btn.dataset.action);
            });
        });
    }

    function hide() {
        if (menuEl) {
            menuEl.remove();
            menuEl = null;
        }
    }

    function countDescendants(nodeId) {
        const session = Session.getCurrent();
        if (!session) return 0;
        const childrenOf = {};
        for (const [id, n] of Object.entries(session.nodes)) {
            if (n.parent_id) {
                (childrenOf[n.parent_id] = childrenOf[n.parent_id] || []).push(id);
            }
        }
        let count = 0;
        const stack = [...(childrenOf[nodeId] || [])];
        while (stack.length) {
            const id = stack.pop();
            count++;
            stack.push(...(childrenOf[id] || []));
        }
        return count;
    }

    async function handleAction(action) {
        const nodeId = currentNodeId;
        hide();
        if (!nodeId) return;
        const session = Session.getCurrent();
        if (!session) return;

        if (action === 'summarize') {
            await doSummarize(session.id, nodeId);
        } else if (action === 'toggle-collapsed') {
            toggleSummaryCollapsed(nodeId);
        } else if (action === 'similar' && typeof Search !== 'undefined') {
            Search.showSimilar(nodeId);
        }
    }

    async function doSummarize(sessionId, nodeId) {
        // Show a temporary status node-like indicator
        const banner = showBanner('🤔 Generating summary…');
        try {
            const result = await API.summarizeSubtree(sessionId, nodeId);
            // Refresh the session — easiest way to pick up the new node
            await Session.loadSession(sessionId);
            // Hide the summarized subtree by default
            if (result.node && result.node.summarized_nodes) {
                hideNodes(result.node.summarized_nodes, result.node.id);
            }
            // Pan to the new summary node
            const newEl = document.getElementById(result.node_id);
            if (newEl && typeof Canvas !== 'undefined' && Canvas.centerOn) {
                Canvas.centerOn(newEl);
                newEl.classList.add('search-flash');
                setTimeout(() => newEl.classList.remove('search-flash'), 1200);
            }
            banner.update(`✓ Summarized ${result.summarized_count} nodes`, 'success');
        } catch (e) {
            banner.update(`✗ ${e.message || e}`, 'error');
        }
        setTimeout(() => banner.remove(), 2500);
    }

    function hideNodes(nodeIds, excludeId) {
        for (const id of nodeIds) {
            if (id === excludeId) continue;
            const el = document.getElementById(id);
            if (el) el.classList.add('node-hidden-by-summary');
        }
        // Also hide edges touching those nodes
        document.querySelectorAll('.edge-layer line, .edge-layer path').forEach(line => {
            const src = line.dataset.source;
            const tgt = line.dataset.target;
            if (nodeIds.includes(src) || nodeIds.includes(tgt)) {
                line.classList.add('node-hidden-by-summary');
            }
        });
    }

    function showNodes(nodeIds) {
        for (const id of nodeIds) {
            const el = document.getElementById(id);
            if (el) el.classList.remove('node-hidden-by-summary');
        }
        document.querySelectorAll('.edge-layer .node-hidden-by-summary').forEach(line => {
            const src = line.dataset.source;
            const tgt = line.dataset.target;
            if (nodeIds.includes(src) || nodeIds.includes(tgt)) {
                line.classList.remove('node-hidden-by-summary');
            }
        });
    }

    function toggleSummaryCollapsed(summaryNodeId) {
        const session = Session.getCurrent();
        if (!session) return;
        const node = session.nodes[summaryNodeId];
        if (!node || !node.summarized_nodes) return;
        // Check if first summarized node is currently hidden
        const probeEl = document.getElementById(node.summarized_nodes[0]);
        const currentlyHidden = probeEl && probeEl.classList.contains('node-hidden-by-summary');
        if (currentlyHidden) {
            showNodes(node.summarized_nodes);
        } else {
            hideNodes(node.summarized_nodes, summaryNodeId);
        }
    }

    function showBanner(msg) {
        let el = document.getElementById('lt-banner');
        if (!el) {
            el = document.createElement('div');
            el.id = 'lt-banner';
            el.className = 'lt-banner';
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.classList.remove('error', 'success');
        return {
            update(newMsg, kind) {
                el.textContent = newMsg;
                if (kind) el.classList.add(kind);
            },
            remove() {
                if (el && el.parentNode) el.parentNode.removeChild(el);
            },
        };
    }

    /** Called after Session loads — auto-hide summarized subtrees that exist. */
    function applyExistingSummaryCollapses() {
        const session = Session.getCurrent();
        if (!session) return;
        for (const node of Object.values(session.nodes)) {
            if (node.prompt_mode === 'summary' && node.summarized_nodes) {
                hideNodes(node.summarized_nodes, node.id);
            }
        }
    }

    return { init, applyExistingSummaryCollapses };
})();
