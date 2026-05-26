/**
 * settings.js — Settings overlay for managing LLM providers.
 */

const Settings = (() => {
    let overlay = null;
    let providers = [];
    let defaultId = null;
    let fallbackId = null;
    let activeTab = 'providers';   // 'providers' | 'mcp'
    let mcpServers = [];

    function init() {
        overlay = document.getElementById('settings-overlay');
    }

    function show() {
        if (!overlay) return;
        overlay.classList.remove('hidden');
        loadProviders();
    }

    function hide() {
        if (!overlay) return;
        overlay.classList.add('hidden');
    }

    async function loadProviders() {
        try {
            const data = await API.getProviders();
            providers = data.providers;
            defaultId = data.default_provider_id;
            fallbackId = data.fallback_provider_id;
            render();
        } catch (e) {
            console.error('Failed to load providers:', e);
        }
    }

    function render() {
        const panel = overlay.querySelector('.settings-panel');
        if (!panel) return;

        // Clear content using DOM methods
        while (panel.firstChild) panel.removeChild(panel.firstChild);

        // Header
        const header = document.createElement('div');
        header.className = 'settings-header';
        const h2 = document.createElement('h2');
        h2.textContent = 'Settings';
        const closeBtn = document.createElement('button');
        closeBtn.className = 'btn btn-icon settings-close';
        closeBtn.textContent = '\u00D7';
        closeBtn.addEventListener('click', hide);
        header.appendChild(h2);
        header.appendChild(closeBtn);
        panel.appendChild(header);

        // Tab strip
        const tabBar = document.createElement('div');
        tabBar.className = 'settings-tabs';
        for (const [key, label] of [['providers', 'LLM Providers'], ['mcp', 'MCP Servers']]) {
            const btn = document.createElement('button');
            btn.className = 'settings-tab' + (activeTab === key ? ' active' : '');
            btn.textContent = label;
            btn.addEventListener('click', () => {
                activeTab = key;
                if (key === 'mcp') loadMcpAndRender();
                else render();
            });
            tabBar.appendChild(btn);
        }
        panel.appendChild(tabBar);

        if (activeTab === 'mcp') {
            renderMcpTab(panel);
            return;
        }

        // ---- Providers tab body ----
        // Add provider button
        const addBtn = document.createElement('button');
        addBtn.className = 'btn btn-primary settings-add-btn';
        addBtn.textContent = '+ Add Provider';
        addBtn.addEventListener('click', () => showForm(null));
        panel.appendChild(addBtn);

        // Provider cards
        const list = document.createElement('div');
        list.className = 'provider-list';
        for (const p of providers) {
            list.appendChild(createCard(p));
        }
        panel.appendChild(list);

        // Fallback section
        const fbSection = document.createElement('div');
        fbSection.className = 'settings-fallback-section';
        const fbLabel = document.createElement('label');
        fbLabel.textContent = 'Fallback Provider: ';
        const fbSelect = document.createElement('select');
        fbSelect.className = 'provider-select';
        const noneOpt = document.createElement('option');
        noneOpt.value = '';
        noneOpt.textContent = '(None)';
        fbSelect.appendChild(noneOpt);
        for (const p of providers) {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.alias;
            if (p.id === fallbackId) opt.selected = true;
            fbSelect.appendChild(opt);
        }
        fbSelect.addEventListener('change', async () => {
            await API.setFallbackProvider(fbSelect.value || null);
            fallbackId = fbSelect.value || null;
        });
        fbSection.appendChild(fbLabel);
        fbSection.appendChild(fbSelect);
        panel.appendChild(fbSection);
    }

    function createCard(p) {
        const card = document.createElement('div');
        card.className = 'provider-card';
        if (!p.enabled) card.classList.add('provider-disabled');

        // Info row
        const info = document.createElement('div');
        info.className = 'provider-card-info';

        const aliasEl = document.createElement('span');
        aliasEl.className = 'provider-alias';
        aliasEl.textContent = p.alias;
        info.appendChild(aliasEl);

        const typeEl = document.createElement('span');
        typeEl.className = 'provider-type-badge';
        typeEl.textContent = p.type === 'claude-cli' ? 'CLI' : p.type === 'ollama' ? 'Ollama' : 'API';
        info.appendChild(typeEl);

        if (p.id === defaultId) {
            const defBadge = document.createElement('span');
            defBadge.className = 'provider-badge provider-badge-default';
            defBadge.textContent = 'DEFAULT';
            info.appendChild(defBadge);
        }
        if (p.id === fallbackId) {
            const fbBadge = document.createElement('span');
            fbBadge.className = 'provider-badge provider-badge-fallback';
            fbBadge.textContent = 'FALLBACK';
            info.appendChild(fbBadge);
        }

        card.appendChild(info);

        // Details
        const details = document.createElement('div');
        details.className = 'provider-card-details';
        if (p.type !== 'claude-cli' && p.url) {
            const urlEl = document.createElement('div');
            urlEl.className = 'provider-detail';
            urlEl.textContent = p.url;
            details.appendChild(urlEl);
        }
        if (p.model) {
            const modelEl = document.createElement('div');
            modelEl.className = 'provider-detail';
            modelEl.textContent = 'Model: ' + p.model;
            details.appendChild(modelEl);
        }
        card.appendChild(details);

        // Test result area
        const testResult = document.createElement('div');
        testResult.className = 'provider-test-result hidden';
        card.appendChild(testResult);

        // Actions
        const actions = document.createElement('div');
        actions.className = 'provider-card-actions';

        const testBtn = document.createElement('button');
        testBtn.className = 'btn btn-sm';
        testBtn.textContent = 'Test';
        testBtn.addEventListener('click', async () => {
            testBtn.disabled = true;
            testBtn.textContent = 'Testing...';
            testResult.classList.remove('hidden', 'test-success', 'test-error');
            try {
                const result = await API.testProvider(p.id);
                testResult.classList.add(result.success ? 'test-success' : 'test-error');
                testResult.textContent = result.message + (result.response_preview ? ' — "' + result.response_preview.slice(0, 100) + '"' : '');
            } catch (e) {
                testResult.classList.add('test-error');
                testResult.textContent = 'Test failed: ' + e.message;
            }
            testResult.classList.remove('hidden');
            testBtn.disabled = false;
            testBtn.textContent = 'Test';
        });

        const editBtn = document.createElement('button');
        editBtn.className = 'btn btn-sm';
        editBtn.textContent = 'Edit';
        editBtn.addEventListener('click', () => showForm(p));

        const defBtn = document.createElement('button');
        defBtn.className = 'btn btn-sm';
        if (p.id === defaultId) {
            defBtn.textContent = 'Is Default';
            defBtn.disabled = true;
            defBtn.style.opacity = '0.5';
        } else {
            defBtn.textContent = 'Set Default';
        }
        defBtn.addEventListener('click', async () => {
            try {
                await API.setDefaultProvider(p.id);
                defaultId = p.id;
                await loadProviders();
                App.loadProviderDropdown();
            } catch (e) {
                console.error('Set default failed:', e);
                alert('Failed to set default: ' + e.message);
            }
        });

        const delBtn = document.createElement('button');
        delBtn.className = 'btn btn-sm btn-danger';
        delBtn.textContent = 'Delete';
        delBtn.addEventListener('click', async () => {
            if (!confirm('Delete provider "' + p.alias + '"?')) return;
            try {
                await API.deleteProvider(p.id);
                loadProviders();
                App.loadProviderDropdown();
            } catch (e) {
                alert('Cannot delete: ' + e.message);
            }
        });

        actions.appendChild(testBtn);
        actions.appendChild(editBtn);
        actions.appendChild(defBtn);
        actions.appendChild(delBtn);
        card.appendChild(actions);

        return card;
    }

    function showForm(existing) {
        const panel = overlay.querySelector('.settings-panel');
        if (!panel) return;

        // Clear
        while (panel.firstChild) panel.removeChild(panel.firstChild);

        const header = document.createElement('div');
        header.className = 'settings-header';
        const h2 = document.createElement('h2');
        h2.textContent = existing ? 'Edit Provider' : 'Add Provider';
        const backBtn = document.createElement('button');
        backBtn.className = 'btn btn-sm';
        backBtn.textContent = 'Back';
        backBtn.addEventListener('click', () => render());
        header.appendChild(h2);
        header.appendChild(backBtn);
        panel.appendChild(header);

        const form = document.createElement('div');
        form.className = 'provider-form';

        const fields = [
            { key: 'alias', label: 'Alias / Name', type: 'text', value: existing?.alias || '' },
            { key: 'type', label: 'Type', type: 'select', options: [
                { value: 'openai-compatible', label: 'OpenAI-Compatible API' },
                { value: 'claude-cli', label: 'Claude Code (CLI)' },
                { value: 'ollama', label: 'Ollama (Local)' },
            ], value: existing?.type || 'openai-compatible' },
            { key: 'url', label: 'API URL', type: 'text', value: existing?.url || '', placeholder: 'e.g. http://localhost:8080 or http://api.openai.com/v1/chat/completions' },
            { key: 'model', label: 'Model', type: 'text', value: existing?.model || '', placeholder: 'e.g. opus, sonnet, haiku (for CLI) or gpt-4o (for API)' },
            { key: 'api_key', label: 'API Key', type: 'password', value: existing?.api_key || '' },
            { key: 'max_tokens', label: 'Max Tokens', type: 'number', value: existing?.max_tokens ?? 4096 },
            { key: 'temperature', label: 'Temperature', type: 'number', value: existing?.temperature ?? 0.7, step: '0.1' },
            { key: 'timeout', label: 'Timeout (seconds)', type: 'number', value: existing?.timeout ?? 300 },
            { key: 'enabled', label: 'Enabled', type: 'checkbox', value: existing?.enabled ?? true },
        ];

        const inputs = {};
        const fieldRows = {};

        for (const f of fields) {
            const row = document.createElement('div');
            row.className = 'form-row';
            if (f.hideFor) row.dataset.hideFor = f.hideFor;

            const label = document.createElement('label');
            label.textContent = f.label;
            row.appendChild(label);

            let input;
            if (f.type === 'select') {
                input = document.createElement('select');
                input.className = 'form-input';
                for (const opt of f.options) {
                    const o = document.createElement('option');
                    o.value = opt.value;
                    o.textContent = opt.label;
                    if (opt.value === f.value) o.selected = true;
                    input.appendChild(o);
                }
            } else if (f.type === 'checkbox') {
                input = document.createElement('input');
                input.type = 'checkbox';
                input.checked = f.value;
            } else {
                input = document.createElement('input');
                input.className = 'form-input';
                input.type = f.type;
                input.value = f.value;
                if (f.step) input.step = f.step;
                if (f.placeholder) input.placeholder = f.placeholder;
                if (f.type === 'password') input.placeholder = existing ? '(unchanged)' : '';
            }

            inputs[f.key] = input;
            row.appendChild(input);
            form.appendChild(row);
            fieldRows[f.key] = row;
        }

        // Ollama model dropdown row
        const ollamaModelRow = document.createElement('div');
        ollamaModelRow.className = 'form-row';
        const ollamaModelLabel = document.createElement('label');
        ollamaModelLabel.textContent = 'Model';
        ollamaModelRow.appendChild(ollamaModelLabel);

        const ollamaModelWrap = document.createElement('div');
        ollamaModelWrap.style.display = 'flex';
        ollamaModelWrap.style.gap = '8px';
        ollamaModelWrap.style.flex = '1';

        const ollamaModelSelect = document.createElement('select');
        ollamaModelSelect.className = 'form-input';
        ollamaModelSelect.style.flex = '1';
        ollamaModelWrap.appendChild(ollamaModelSelect);

        const refreshBtn = document.createElement('button');
        refreshBtn.className = 'btn btn-sm';
        refreshBtn.textContent = 'Refresh';
        refreshBtn.type = 'button';
        ollamaModelWrap.appendChild(refreshBtn);

        ollamaModelRow.appendChild(ollamaModelWrap);

        // Insert after the model text input row
        const modelRowIndex = Array.from(form.children).indexOf(fieldRows.model);
        if (modelRowIndex >= 0 && modelRowIndex < form.children.length - 1) {
            form.insertBefore(ollamaModelRow, fieldRows.model.nextSibling);
        } else {
            form.appendChild(ollamaModelRow);
        }

        async function fetchOllamaModels() {
            const baseUrl = inputs.url.value || 'http://localhost:11434';
            ollamaModelSelect.disabled = true;
            refreshBtn.disabled = true;
            refreshBtn.textContent = '...';
            while (ollamaModelSelect.firstChild) ollamaModelSelect.removeChild(ollamaModelSelect.firstChild);
            const loadingOpt = document.createElement('option');
            loadingOpt.textContent = 'Loading...';
            ollamaModelSelect.appendChild(loadingOpt);

            try {
                const data = await API.getOllamaModels(baseUrl);
                while (ollamaModelSelect.firstChild) ollamaModelSelect.removeChild(ollamaModelSelect.firstChild);

                if (data.error) {
                    const errOpt = document.createElement('option');
                    errOpt.textContent = 'Cannot reach Ollama — is it running?';
                    ollamaModelSelect.appendChild(errOpt);
                } else if (data.models.length === 0) {
                    const emptyOpt = document.createElement('option');
                    emptyOpt.textContent = 'No models installed';
                    ollamaModelSelect.appendChild(emptyOpt);
                } else {
                    for (const m of data.models) {
                        const opt = document.createElement('option');
                        opt.value = m.name;
                        const sizeMB = m.size ? ` (${(m.size / 1e9).toFixed(1)}GB)` : '';
                        opt.textContent = m.name + sizeMB;
                        if (existing && existing.model === m.name) opt.selected = true;
                        ollamaModelSelect.appendChild(opt);
                    }
                }
            } catch (e) {
                while (ollamaModelSelect.firstChild) ollamaModelSelect.removeChild(ollamaModelSelect.firstChild);
                const errOpt = document.createElement('option');
                errOpt.textContent = 'Error: ' + e.message;
                ollamaModelSelect.appendChild(errOpt);
            }
            ollamaModelSelect.disabled = false;
            refreshBtn.disabled = false;
            refreshBtn.textContent = 'Refresh';
        }

        refreshBtn.addEventListener('click', fetchOllamaModels);

        // Toggle visibility based on type
        function updateVisibility() {
            const type = inputs.type.value;
            // URL: show for openai-compatible and ollama, hide for claude-cli
            fieldRows.url.style.display = type === 'claude-cli' ? 'none' : '';
            // API Key: show only for openai-compatible
            fieldRows.api_key.style.display = type === 'openai-compatible' ? '' : 'none';
            // Model text input: hide for ollama (replaced by dropdown)
            fieldRows.model.style.display = type === 'ollama' ? 'none' : '';
            // Ollama model dropdown: show only for ollama
            ollamaModelRow.style.display = type === 'ollama' ? '' : 'none';

            // Auto-fill URL for ollama
            if (type === 'ollama' && !inputs.url.value) {
                inputs.url.value = 'http://localhost:11434';
            }
            // Fetch models when switching to ollama
            if (type === 'ollama') {
                fetchOllamaModels();
            }
        }
        inputs.type.addEventListener('change', updateVisibility);
        updateVisibility();

        panel.appendChild(form);

        // Save button
        const saveRow = document.createElement('div');
        saveRow.className = 'form-actions';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-primary';
        saveBtn.textContent = existing ? 'Save Changes' : 'Add Provider';
        saveBtn.addEventListener('click', async () => {
            const data = {};
            for (const f of fields) {
                if (f.type === 'checkbox') {
                    data[f.key] = inputs[f.key].checked;
                } else if (f.type === 'number') {
                    data[f.key] = parseFloat(inputs[f.key].value);
                } else {
                    data[f.key] = inputs[f.key].value;
                }
            }
            // For ollama, use the dropdown value instead of text input
            if (data.type === 'ollama' && ollamaModelSelect.value) {
                data.model = ollamaModelSelect.value;
            }
            // Don't send empty password (means "keep existing")
            if (existing && !data.api_key) {
                delete data.api_key;
            }

            try {
                if (existing) {
                    await API.updateProvider(existing.id, data);
                } else {
                    await API.addProvider(data);
                }
                loadProviders();
                App.loadProviderDropdown();
            } catch (e) {
                alert('Error: ' + e.message);
            }
        });
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.addEventListener('click', () => render());
        saveRow.appendChild(saveBtn);
        saveRow.appendChild(cancelBtn);
        panel.appendChild(saveRow);
    }

    // ---------- MCP Servers tab ----------

    async function loadMcpAndRender() {
        try {
            const res = await API.listMcpServers();
            mcpServers = res.servers || [];
        } catch (e) {
            console.warn('Failed to load MCP servers:', e);
            mcpServers = [];
        }
        render();
    }

    function renderMcpTab(panel) {
        // Foundation banner
        const banner = document.createElement('div');
        banner.className = 'mcp-foundation-note';
        banner.innerHTML = `
            <strong>Foundation only.</strong> Configure and verify MCP servers here.
            Wiring MCP tools into the LLM call loop is a follow-up — once landed,
            servers configured here will be available automatically.
        `;
        panel.appendChild(banner);

        // Add button
        const addBtn = document.createElement('button');
        addBtn.className = 'btn btn-primary settings-add-btn';
        addBtn.textContent = '+ Add MCP Server';
        addBtn.addEventListener('click', () => showMcpForm());
        panel.appendChild(addBtn);

        // Server list
        const list = document.createElement('div');
        list.className = 'provider-list';
        if (mcpServers.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'mcp-empty';
            empty.textContent = 'No MCP servers configured yet.';
            list.appendChild(empty);
        } else {
            for (const s of mcpServers) list.appendChild(createMcpCard(s));
        }
        panel.appendChild(list);
    }

    function createMcpCard(s) {
        const card = document.createElement('div');
        card.className = 'provider-card';
        card.innerHTML = `
            <div class="provider-card-header">
                <div>
                    <div class="provider-card-name">${escapeHTML(s.name)}</div>
                    <div class="provider-card-meta">
                        ${escapeHTML(s.transport)} · ${escapeHTML(s.url || s.command || '')}
                    </div>
                </div>
                <div class="provider-card-actions">
                    <button class="btn btn-sm btn-test-mcp">Test</button>
                    <button class="btn btn-sm btn-delete-mcp">Delete</button>
                </div>
            </div>
            <div class="mcp-test-result hidden"></div>
        `;
        const resultEl = card.querySelector('.mcp-test-result');
        card.querySelector('.btn-test-mcp').addEventListener('click', async () => {
            resultEl.classList.remove('hidden', 'success', 'error');
            resultEl.textContent = 'Testing…';
            try {
                const r = await API.testMcpServer(s.id);
                if (r.ok) {
                    resultEl.classList.add('success');
                    const toolsList = (r.tools || []).map(t => `<li><code>${escapeHTML(t.name)}</code> — ${escapeHTML(t.description || '')}</li>`).join('');
                    resultEl.innerHTML = `✓ Connected. <strong>${r.tools.length}</strong> tool${r.tools.length === 1 ? '' : 's'} available.` +
                        (toolsList ? `<ul class="mcp-tools-list">${toolsList}</ul>` : '');
                } else {
                    resultEl.classList.add('error');
                    resultEl.textContent = `✗ ${r.error || 'Unknown error'}`;
                }
            } catch (e) {
                resultEl.classList.add('error');
                resultEl.textContent = `✗ ${e.message || e}`;
            }
        });
        card.querySelector('.btn-delete-mcp').addEventListener('click', async () => {
            if (!confirm(`Delete MCP server "${s.name}"?`)) return;
            await API.deleteMcpServer(s.id);
            await loadMcpAndRender();
        });
        return card;
    }

    function showMcpForm() {
        const panel = overlay.querySelector('.settings-panel');
        while (panel.firstChild) panel.removeChild(panel.firstChild);

        const header = document.createElement('div');
        header.className = 'settings-header';
        const h2 = document.createElement('h2');
        h2.textContent = 'Add MCP Server';
        const closeBtn = document.createElement('button');
        closeBtn.className = 'btn btn-icon settings-close';
        closeBtn.textContent = '\u00D7';
        closeBtn.addEventListener('click', () => loadMcpAndRender());
        header.appendChild(h2);
        header.appendChild(closeBtn);
        panel.appendChild(header);

        const form = document.createElement('div');
        form.className = 'provider-form';
        form.innerHTML = `
            <label>Name<br><input type="text" class="form-input" id="mcp-name" placeholder="e.g. Filesystem MCP"></label>
            <label>Transport<br>
                <select class="form-input" id="mcp-transport">
                    <option value="http">HTTP</option>
                    <option value="sse">SSE</option>
                    <option value="stdio" disabled>stdio (not yet supported)</option>
                </select>
            </label>
            <label>URL<br><input type="text" class="form-input" id="mcp-url" placeholder="http://localhost:3000/mcp"></label>
            <label>Authorization header (optional)<br>
                <input type="text" class="form-input" id="mcp-auth" placeholder="Bearer ...">
            </label>
        `;
        panel.appendChild(form);

        const row = document.createElement('div');
        row.className = 'form-row';
        const save = document.createElement('button');
        save.className = 'btn btn-primary';
        save.textContent = 'Save';
        save.addEventListener('click', async () => {
            const name = document.getElementById('mcp-name').value.trim();
            const transport = document.getElementById('mcp-transport').value;
            const url = document.getElementById('mcp-url').value.trim();
            const auth = document.getElementById('mcp-auth').value.trim();
            if (!name) { alert('Name is required'); return; }
            if (!url) { alert('URL is required'); return; }
            const headers = auth ? { Authorization: auth } : {};
            try {
                await API.addMcpServer({ name, transport, url, headers });
                await loadMcpAndRender();
            } catch (e) {
                alert(`Failed to add: ${e.message || e}`);
            }
        });
        const cancel = document.createElement('button');
        cancel.className = 'btn';
        cancel.textContent = 'Cancel';
        cancel.addEventListener('click', () => loadMcpAndRender());
        row.appendChild(save);
        row.appendChild(cancel);
        panel.appendChild(row);
    }

    function escapeHTML(s) {
        const div = document.createElement('div');
        div.textContent = String(s ?? '');
        return div.innerHTML;
    }

    return { init, show, hide };
})();
