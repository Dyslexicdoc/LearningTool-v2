/**
 * ingest.js — Document ingestion UI.
 *
 * Two entry points:
 *   1. "+ Document" button in the sidebar opens a modal with file/URL options
 *   2. Drag-and-drop a PDF anywhere on the page → ingests directly
 *
 * On successful ingest, refresh the session list and load the new session.
 */

const Ingest = (() => {
    function init() {
        installDragDrop();
        // Sidebar button is created in session.js init by injecting next to "+ New"
        const btn = document.getElementById('btn-new-document');
        if (btn) btn.addEventListener('click', openModal);
    }

    function openModal() {
        // Close any existing modal first
        const existing = document.querySelector('.ingest-modal-backdrop');
        if (existing) existing.remove();

        const backdrop = document.createElement('div');
        backdrop.className = 'ingest-modal-backdrop';
        backdrop.innerHTML = `
            <div class="ingest-modal">
                <div class="ingest-modal-header">
                    <h3>Start a session from a document</h3>
                    <button class="btn btn-icon btn-sm ingest-close" title="Close">✕</button>
                </div>
                <div class="ingest-modal-body">
                    <div class="ingest-tabs">
                        <button class="ingest-tab active" data-tab="file">📄 PDF</button>
                        <button class="ingest-tab" data-tab="url">🌐 URL / Article</button>
                        <button class="ingest-tab" data-tab="youtube">▶ YouTube</button>
                    </div>

                    <div class="ingest-tab-panel" data-panel="file">
                        <label class="ingest-drop-zone">
                            <input type="file" accept=".pdf,application/pdf" id="ingest-file-input" hidden>
                            <div class="ingest-drop-inner">
                                <div class="ingest-drop-icon">📄</div>
                                <div class="ingest-drop-text">Click to choose a PDF, or drop one here</div>
                                <div class="ingest-drop-sub">Up to 25 MB. Text-based PDFs only (no scanned images).</div>
                            </div>
                        </label>
                    </div>

                    <div class="ingest-tab-panel hidden" data-panel="url">
                        <input type="text" class="ingest-url-input" id="ingest-url-input"
                               placeholder="https://example.com/article" autocomplete="off">
                        <button class="btn btn-primary" id="ingest-url-submit">Ingest URL</button>
                        <div class="ingest-help">Pulls the main article content. Some sites
                            (paywalled, JS-rendered) may not work.</div>
                    </div>

                    <div class="ingest-tab-panel hidden" data-panel="youtube">
                        <input type="text" class="ingest-url-input" id="ingest-youtube-input"
                               placeholder="https://www.youtube.com/watch?v=..." autocomplete="off">
                        <button class="btn btn-primary" id="ingest-youtube-submit">Ingest Transcript</button>
                        <div class="ingest-help">Fetches the video's transcript with timestamps.
                            Requires captions/CC to be available.</div>
                    </div>

                    <div class="ingest-status hidden" id="ingest-status"></div>
                </div>
            </div>
        `;
        document.body.appendChild(backdrop);

        // Wire close
        backdrop.querySelector('.ingest-close').addEventListener('click', closeModal);
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) closeModal();
        });

        // Tab switching
        backdrop.querySelectorAll('.ingest-tab').forEach(t => {
            t.addEventListener('click', () => {
                backdrop.querySelectorAll('.ingest-tab').forEach(x => x.classList.remove('active'));
                t.classList.add('active');
                backdrop.querySelectorAll('.ingest-tab-panel').forEach(p => p.classList.add('hidden'));
                backdrop.querySelector(`[data-panel="${t.dataset.tab}"]`).classList.remove('hidden');
            });
        });

        // File picker
        const fileInput = document.getElementById('ingest-file-input');
        fileInput.addEventListener('change', async () => {
            if (fileInput.files[0]) {
                await doIngestFile(fileInput.files[0]);
            }
        });

        // URL submit
        document.getElementById('ingest-url-submit').addEventListener('click', async () => {
            const url = document.getElementById('ingest-url-input').value.trim();
            if (url) await doIngestUrl(url);
        });

        document.getElementById('ingest-youtube-submit').addEventListener('click', async () => {
            const url = document.getElementById('ingest-youtube-input').value.trim();
            if (url) await doIngestUrl(url);
        });

        // Enter key submits the visible URL panel
        ['ingest-url-input', 'ingest-youtube-input'].forEach(id => {
            document.getElementById(id).addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const url = e.target.value.trim();
                    if (url) doIngestUrl(url);
                }
            });
        });

        // Esc closes
        document.addEventListener('keydown', onEsc);
    }

    function onEsc(e) {
        if (e.key === 'Escape') closeModal();
    }

    function closeModal() {
        const m = document.querySelector('.ingest-modal-backdrop');
        if (m) m.remove();
        document.removeEventListener('keydown', onEsc);
    }

    function setStatus(msg, kind) {
        const el = document.getElementById('ingest-status');
        if (!el) return;
        el.classList.remove('hidden', 'error', 'success');
        if (kind) el.classList.add(kind);
        el.textContent = msg;
    }

    async function doIngestFile(file) {
        setStatus(`Reading "${file.name}" (${(file.size / 1024).toFixed(0)} KB)…`);
        try {
            const result = await API.ingestFile(file);
            setStatus(`Imported "${result.title}". Loading…`, 'success');
            await onIngestSuccess(result);
        } catch (e) {
            setStatus(`Error: ${e.message || e}`, 'error');
        }
    }

    async function doIngestUrl(url) {
        setStatus(`Fetching ${url}…`);
        try {
            const result = await API.ingestUrl(url);
            setStatus(`Imported "${result.title}". Loading…`, 'success');
            await onIngestSuccess(result);
        } catch (e) {
            setStatus(`Error: ${e.message || e}`, 'error');
        }
    }

    async function onIngestSuccess(result) {
        // Refresh session list, then load the new session
        if (typeof Session !== 'undefined') {
            await Session.refreshList();
            await Session.loadSession(result.session_id);
        }
        setTimeout(closeModal, 600);
    }

    // ---- Drag-and-drop anywhere ----

    function installDragDrop() {
        let dragOverlay = null;
        let counter = 0;  // dragenter/leave fire weirdly without a counter

        document.addEventListener('dragenter', (e) => {
            if (!hasFiles(e)) return;
            counter++;
            if (counter === 1) showOverlay();
        });

        document.addEventListener('dragover', (e) => {
            if (hasFiles(e)) e.preventDefault();
        });

        document.addEventListener('dragleave', (e) => {
            if (!hasFiles(e)) return;
            counter = Math.max(0, counter - 1);
            if (counter === 0) hideOverlay();
        });

        document.addEventListener('drop', async (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            counter = 0;
            hideOverlay();
            const file = e.dataTransfer.files[0];
            if (!file) return;
            if (!file.name.toLowerCase().endsWith('.pdf')) {
                alert('Only PDF files are supported via drag-and-drop.');
                return;
            }
            // Open the modal so the user sees progress
            openModal();
            await doIngestFile(file);
        });

        function hasFiles(e) {
            return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
        }

        function showOverlay() {
            if (dragOverlay) return;
            dragOverlay = document.createElement('div');
            dragOverlay.className = 'ingest-drag-overlay';
            dragOverlay.innerHTML = '<div class="ingest-drag-msg">📄 Drop PDF to start a new session</div>';
            document.body.appendChild(dragOverlay);
        }

        function hideOverlay() {
            if (dragOverlay) {
                dragOverlay.remove();
                dragOverlay = null;
            }
        }
    }

    return { init, openModal };
})();
