const SERVER = "http://localhost:8000";

// --- UTILS ---
const showStatus = (msg, type = "info") => {
    const el = document.getElementById('status-msg');
    el.innerHTML = msg;
    el.style.display = 'block';
    el.style.background = type === 'error' ? '#fce8e6' : '#e6f4ea';
    el.style.color = type === 'error' ? '#d93025' : '#137333';
    setTimeout(() => el.style.display = 'none', 3000);
};

const formatMarkdown = (text) => {
    // Very basic formatter for bold and lists
    let html = text
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n\s*-\s/g, '<br>• ');
    return html;
};

// --- TABS LOGIC (Manifest V3 Safe) ---
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        const tabName = tab.getAttribute('data-tab');

        // Remove active class from all
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));

        // Add active to current
        tab.classList.add('active');
        document.getElementById(tabName).classList.add('active');

        // Specific actions
        if (tabName === 'sources') loadSources();
    });
});

// --- 1. INGEST ---
document.getElementById('ingest-btn').addEventListener('click', async () => {
    const btn = document.getElementById('ingest-btn');
    const originalText = btn.innerText;

    // Get URL
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url.startsWith('http')) {
        return showStatus("Cannot add this page (must be http/https)", "error");
    }

    btn.innerText = "Adding...";
    btn.disabled = true;

    try {
        const res = await fetch(`${SERVER}/ingest`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: tab.url })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");

        showStatus(`Success! Added ${data.count} chunks.`);

        // Auto-switch to Sources tab
        document.querySelector('.tab[data-tab="sources"]').click();

        // Clear empty state if present
        const es = document.querySelector('.empty-state');
        if (es) es.style.display = 'none';

    } catch (e) {
        showStatus(`Error: ${e.message}`, "error");
    } finally {
        btn.innerText = originalText;
        btn.disabled = false;
    }
});

// --- 2. CHAT ---
const sendQuestion = async () => {
    const input = document.getElementById('question');
    const btn = document.getElementById('ask-btn');
    const box = document.getElementById('chat-box');
    const q = input.value.trim();
    // DEBUG: Remove this after verifying
    // alert("Sending: " + q); 

    if (!q) return;

    // Remove empty state
    const es = document.querySelector('.empty-state');
    if (es) es.remove();

    // User Message
    box.innerHTML += `<div class='msg user'>${q}</div>`;
    input.value = "";
    box.scrollTop = box.scrollHeight;

    // Loading State
    const loadingId = 'loading-' + Date.now();
    box.innerHTML += `<div id="${loadingId}" class='msg bot' style='color:#666'>Thinking...</div>`;
    box.scrollTop = box.scrollHeight;

    try {
        const res = await fetch(`${SERVER}/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: q })
        });
        const data = await res.json();

        // Remove loading
        document.getElementById(loadingId).remove();

        if (!res.ok) throw new Error(data.detail || "Server Error");

        // Parse Answer
        let html = `<div class='msg bot'>${formatMarkdown(data.answer)}`;
        if (data.citations && data.citations.length > 0) {
            html += `<div style="margin-top:8px; border-top:1px solid #eee; padding-top:4px;">`;
            data.citations.forEach(url => {
                html += `<a class='citation' href='${url}' target='_blank' title='${url}'>Source</a>`;
            });
            html += `</div>`;
        }
        html += `</div>`;
        box.innerHTML += html;
    } catch (e) {
        document.getElementById(loadingId).remove();
        box.innerHTML += `<div class='msg bot' style='color:#d93025'>Error: ${e.message}</div>`;
    }
    box.scrollTop = box.scrollHeight;
};

document.getElementById('ask-btn').addEventListener('click', sendQuestion);
document.getElementById('question').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendQuestion();
});

// --- 3. SOURCES LIST ---
async function loadSources() {
    const list = document.getElementById('sources-list');
    list.innerHTML = `<div style="text-align:center; color:#666; margin-top:20px;">Fetching sources...</div>`;

    try {
        const res = await fetch(`${SERVER}/sources`);
        const data = await res.json();

        if (!data.sources || data.sources.length === 0) {
            list.innerHTML = `<div class="empty-state">No sources saved yet.</div>`;
            return;
        }

        list.innerHTML = "";
        data.sources.forEach(url => {
            const div = document.createElement('div');
            div.className = 'source-item';

            // Delete button setup
            const delBtn = document.createElement('button');
            delBtn.className = 'danger';
            delBtn.innerText = '×';
            delBtn.style.padding = '2px 8px';
            delBtn.title = 'Remove Source';
            delBtn.onclick = async () => {
                if (confirm('Remove this source and its memory?')) {
                    await fetch(`${SERVER}/delete_source`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ source_url: url })
                    });
                    loadSources(); // Refresh
                }
            };

            div.innerHTML = `<div class="source-url" title="${url}">${url}</div>`;
            div.appendChild(delBtn);
            list.appendChild(div);
        });
    } catch (e) {
        list.innerText = "Connection failed. Is server running?";
        list.style.color = "#d93025";
        list.style.textAlign = "center";
        list.style.marginTop = "20px";
    }
}
document.getElementById('refresh-sources-btn').addEventListener('click', loadSources);

// --- 4. BRIEFING ---
document.getElementById('gen-briefing-btn').addEventListener('click', async () => {
    const btn = document.getElementById('gen-briefing-btn');
    const div = document.getElementById('briefing-content');

    btn.disabled = true;
    btn.innerText = "Generating...";
    div.innerHTML = "<div style='text-align:center; color:#666'>Reading your notebook... this may take a moment.</div>";

    try {
        // We pass current tab URL just in case, but usually Briefing is global context
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        const res = await fetch(`${SERVER}/briefing`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: tab ? tab.url : "global" })
        });

        if (!res.ok) throw new Error("Failed");
        const data = await res.json();

        div.innerHTML = formatMarkdown(data.content);
    } catch (e) {
        div.innerHTML = `<div style="color:#d93025">Failed to generate briefing. Ensure you have sources added.</div>`;
    } finally {
        btn.disabled = false;
        btn.innerText = "✨ Generate Briefing Doc";
    }
});

// --- 5. PODCAST ---
document.getElementById('podcast-btn').addEventListener('click', async () => {
    const btn = document.getElementById('podcast-btn');
    const player = document.getElementById('player');

    btn.disabled = true;
    btn.innerText = "Creating Script & Audio...";

    try {
        player.style.display = 'block';
        player.src = `${SERVER}/podcast?t=${Date.now()}`;
        player.play();
        showStatus("Playing Audio!");
    } catch (e) {
        showStatus("Audio failed", "error");
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.innerText = "🎧 Generate Audio Overview";
        }, 5000);
    }
});

// --- 6. CLEAR ---
document.getElementById('clear-btn').addEventListener('click', async () => {
    if (!confirm("⚠️ Are you sure? This will wipe ALL memory and cannot be undone.")) return;
    try {
        await fetch(`${SERVER}/clear`, { method: "POST" });
        showStatus("Memory Wiped.");
        document.getElementById('chat-box').innerHTML = "<div class='empty-state'>Memory cleared.</div>";
        loadSources();
    } catch (e) { showStatus("Failed to clear", "error"); }
});
