/**
 * TeraBox Video Downloader — Frontend JavaScript
 * Handles URL input, API calls, results display, and downloads.
 */

// ─── DOM Elements ───────────────────────────────────────────────────────────
const urlInput = document.getElementById('urlInput');
const btnPaste = document.getElementById('btnPaste');
const btnDownload = document.getElementById('btnDownload');
const errorMsg = document.getElementById('errorMsg');
const loadingSection = document.getElementById('loadingSection');
const resultsSection = document.getElementById('resultsSection');
const fileList = document.getElementById('fileList');
const statusIndicator = document.getElementById('statusIndicator');
const loginModal = document.getElementById('loginModal');
const loginForm = document.getElementById('loginForm');
const loginError = document.getElementById('loginError');
const btnLoginCancel = document.getElementById('btnLoginCancel');

// ─── State ──────────────────────────────────────────────────────────────────
let isLoggedIn = false;

// ─── Supported Domains ──────────────────────────────────────────────────────
const SUPPORTED_DOMAINS = [
    'terabox.com', 'terabox.app', 'terabox.fun', 'teraboxapp.com',
    '1024tera.com', '1024terabox.com', 'freeterabox.com', 'mirrobox.com',
    'nephobox.com', '4funbox.co', 'momerybox.com', 'tibibox.com',
    'terasharefile.com',
];

// ─── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    checkStatus();
    setupEventListeners();
});

function setupEventListeners() {
    // Paste button
    btnPaste.addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            urlInput.value = text;
            urlInput.focus();
        } catch (err) {
            showError('Could not read clipboard. Please paste manually (Ctrl+V).');
        }
    });

    // Download button
    btnDownload.addEventListener('click', () => resolveUrl());

    // Enter key in input
    urlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') resolveUrl();
    });

    // Clear error on input
    urlInput.addEventListener('input', () => hideError());

    // Login form
    loginForm.addEventListener('submit', (e) => {
        e.preventDefault();
        submitLogin();
    });

    // Login cancel
    btnLoginCancel.addEventListener('click', () => {
        loginModal.style.display = 'none';
    });
}

// ─── Status Check ───────────────────────────────────────────────────────────
async function checkStatus() {
    const dot = statusIndicator.querySelector('.status-dot');
    const text = statusIndicator.querySelector('.status-text');

    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();

        if (data.logged_in) {
            isLoggedIn = true;
            dot.className = 'status-dot online';
            text.textContent = 'Ready';
        } else {
            isLoggedIn = false;
            dot.className = 'status-dot offline';
            text.textContent = 'Not logged in';
        }
    } catch (err) {
        dot.className = 'status-dot offline';
        text.textContent = 'Error';
    }
}

// ─── URL Validation ─────────────────────────────────────────────────────────
function isValidTeraBoxUrl(url) {
    try {
        const parsed = new URL(url);
        const hostname = parsed.hostname.toLowerCase().replace(/^www\./, '');
        return SUPPORTED_DOMAINS.some(d => hostname === d || hostname === 'www.' + d);
    } catch {
        return false;
    }
}

// ─── Resolve URL ────────────────────────────────────────────────────────────
async function resolveUrl() {
    const url = urlInput.value.trim();

    if (!url) {
        showError('Please enter a TeraBox share link.');
        return;
    }

    if (!isValidTeraBoxUrl(url)) {
        showError('Invalid TeraBox URL. Supported domains: terabox.com, 1024tera.com, freeterabox.com, etc.');
        return;
    }

    // Show loading
    hideError();
    resultsSection.style.display = 'none';
    loadingSection.style.display = 'block';
    btnDownload.disabled = true;

    try {
        const resp = await fetch('/api/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });

        const data = await resp.json();

        if (!resp.ok) {
            // Check if login is needed
            if (resp.status === 400 && data.error && data.error.includes('credentials')) {
                showLoginModal();
                return;
            }
            throw new Error(data.error || 'Failed to resolve URL');
        }

        displayResults(data.files);

    } catch (err) {
        showError(err.message || 'An unexpected error occurred. Please try again.');
    } finally {
        loadingSection.style.display = 'none';
        btnDownload.disabled = false;
    }
}

// ─── Display Results ────────────────────────────────────────────────────────
function displayResults(files) {
    fileList.innerHTML = '';

    if (!files || files.length === 0) {
        showError('No downloadable files found in this link.');
        return;
    }

    files.forEach(file => {
        const card = document.createElement('div');
        card.className = 'file-card';

        // Thumbnail
        let thumbHtml;
        if (file.thumbnail) {
            thumbHtml = `<img class="file-thumb" src="${escapeHtml(file.thumbnail)}" alt="Thumbnail" onerror="this.style.display='none'">`;
        } else {
            thumbHtml = `
                <div class="file-thumb-placeholder">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        ${file.is_dir
                            ? '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
                            : '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'
                        }
                    </svg>
                </div>
            `;
        }

        // Type badge
        const typeIcon = file.is_dir ? '📁' : '📄';

        card.innerHTML = `
            ${thumbHtml}
            <div class="file-info">
                <div class="file-name">${typeIcon} ${escapeHtml(file.filename)}</div>
                <div class="file-size">${escapeHtml(file.size_str)}</div>
            </div>
            <div class="file-actions">
                ${file.is_dir
                    ? '<span style="color: var(--text-muted); font-size: 0.85rem;">Folder</span>'
                    : `<a href="/api/download?token=${encodeURIComponent(file.token)}" class="btn btn-success" download>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                            <polyline points="7 10 12 15 17 10"/>
                            <line x1="12" y1="15" x2="12" y2="3"/>
                        </svg>
                        Download
                    </a>`
                }
            </div>
        `;

        fileList.appendChild(card);
    });

    resultsSection.style.display = 'block';

    // Scroll to results
    resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─── Login ──────────────────────────────────────────────────────────────────
function showLoginModal() {
    loginModal.style.display = 'flex';
    loginError.style.display = 'none';
    document.getElementById('loginEmail').focus();
}

async function submitLogin() {
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
    const btnSubmit = document.getElementById('btnLoginSubmit');

    if (!email || !password) {
        loginError.textContent = 'Please enter both email and password.';
        loginError.style.display = 'block';
        return;
    }

    btnSubmit.disabled = true;
    btnSubmit.textContent = 'Logging in...';
    loginError.style.display = 'none';

    try {
        const resp = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });

        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.message || 'Login failed');
        }

        // Success
        loginModal.style.display = 'none';
        isLoggedIn = true;
        checkStatus();

        // Retry the download
        resolveUrl();

    } catch (err) {
        loginError.textContent = err.message;
        loginError.style.display = 'block';
    } finally {
        btnSubmit.disabled = false;
        btnSubmit.textContent = 'Login';
    }
}

// ─── FAQ ────────────────────────────────────────────────────────────────────
function toggleFaq(button) {
    const item = button.parentElement;
    item.classList.toggle('open');
}

// ─── Utilities ──────────────────────────────────────────────────────────────
function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.style.display = 'block';
}

function hideError() {
    errorMsg.style.display = 'none';
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
