function initDashbot(options) {

const DASHBOT_BASE_URL = options.backendUrl.replace(/\/chat\/?$/, '');
const root = options.element ? document.querySelector(options.element) : document.body;

let sessionId = null;
let sessionToken = null;
let isChatOpen = false;
let userLocation = null;   // {lat, lon} or null
let locationEnabled = false;
// Reported to the backend so the agent knows WHY there are no coordinates and
// can ask the user to enable location: 'on'|'off'|'denied'|'unavailable'|'timeout'|'unsupported'
let locationStatus = 'off';

// ---- Voice state (module functions live below, before the Send section) ----
const DB_LANG = (options && options.language === 'de') ? 'de' : 'en';
const DB_STT_LANG = DB_LANG === 'de' ? 'de-DE' : 'en-US';
const SPEAK_ALL_REPLIES = !!(options && options.speakAllReplies); // speak typed turns too
let voiceAvailable = false;     // backend /voice/* proxy configured (learned at /session/start)
let voiceRepliesOn = true;      // master switch (header speaker button, persisted)
let pendingVoiceInput = false;  // the next sendMessage() was initiated by the mic
try { voiceRepliesOn = localStorage.getItem('dashbot-voice') !== 'off'; } catch (e) {}

//  HTML 
const html = `
<!-- Avatar Button -->
<div class="dashbot-avatar" id="dashbotAvatar">
    <div class="avatar-inner"></div>
    <div class="avatar-status-dot"></div>
    <div class="avatar-tooltip">Ask the Smart City</div>
</div>

<!-- Chat Side Panel -->
<div class="dashbot-panel" id="dashbotPanel">
    <div class="dashbot-panel-header">
        <div class="dashbot-header-left">
            <div class="dashbot-header-avatar"></div>
            <div class="dashbot-header-info">
                <h2>Dashbot</h2>
                <span class="dashbot-header-status"><span class="dot"></span> Online</span>
            </div>
        </div>
        <div class="dashbot-header-actions">
            <button class="dashbot-voicemode-btn" id="dashbotVoiceModeBtn" title="Speaking mode" type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M2 10v3"/><path d="M6 6v11"/><path d="M10 3v18"/><path d="M14 8v7"/><path d="M18 5v13"/><path d="M22 10v3"/>
                </svg>
            </button>
            <button class="dashbot-speaker-btn" id="dashbotSpeakerBtn" title="Voice replies"></button>
            <button class="dashbot-theme-btn" id="dashbotThemeBtn" title="Toggle dark mode"></button>
            <button class="dashbot-reset-btn" id="dashbotResetBtn" title="New chat">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21.5 2v6h-6"/><path d="M21.34 15.57a10 10 0 1 1-.57-8.38L21.5 8"/>
                </svg>
            </button>
            <button class="dashbot-close-btn" id="dashbotCloseBtn">&times;</button>
        </div>
    </div>

    <div class="dashbot-messages" id="dashbotMessages">
        <div class="dashbot-welcome" id="dashbotWelcome">
            <div class="dashbot-welcome-icon"></div>
            <h3>Hey there!</h3>
            <p>I'm your smart city assistant. Ask me about parking, weather, traffic, campus buildings, routes, and more.</p>
            <div class="dashbot-quick-actions">
                <button class="dashbot-quick-btn" data-q="What's the current temperature?">
                    <span class="q-icon">&#127777;</span> Current weather
                </button>
                <button class="dashbot-quick-btn" data-q="Available parking spaces?">
                    <span class="q-icon">&#127359;</span> Parking status
                </button>
                <button class="dashbot-quick-btn" data-q="Where is the library?">
                    <span class="q-icon">&#127963;</span> Find a building
                </button>
                <button class="dashbot-quick-btn" data-q="How do I get to Uni Mensa?">
                    <span class="q-icon">&#128587;</span> Get directions
                </button>
                <button class="dashbot-quick-btn" data-q="What events are happening in Magdeburg today?">
                    <span class="q-icon">&#127917;</span> Events today
                </button>
                <button class="dashbot-quick-btn dashbot-loc-quick" id="dashbotLocQuick" type="button">
                    <span class="q-icon">&#128205;</span> Share my location
                </button>
            </div>
        </div>
    </div>

    <div class="dashbot-typing" id="dashbotTyping">
        <div class="db-typing-wave"><span></span><span></span><span></span><span></span><span></span></div>
        <span class="dashbot-typing-text">Dashbot is thinking...</span>
    </div>

    <div class="dashbot-input-area">
        <div class="dashbot-context-bar">
            <button class="dashbot-location-btn location-off" id="dashbotLocationBtn" type="button" title="Tap to share your location">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>
                    <circle cx="12" cy="10" r="3"/>
                </svg>
                <span class="db-loc-label">Share location</span>
            </button>
        </div>
        <div class="dashbot-input-wrap">
            <input type="text" class="dashbot-input" id="dashbotInput" placeholder="Ask about city data..." autocomplete="off">
            <button class="dashbot-mic-btn" id="dashbotMicBtn" title="Talk to Dashbot" type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                    <line x1="12" y1="19" x2="12" y2="23"/>
                </svg>
            </button>
            <button class="dashbot-send-btn" id="dashbotSendBtn">
                <svg id="dashbotSendIcon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
                </svg>
            </button>
        </div>
    </div>

</div>

<!-- Speaking mode: hands-free ("just talking"). NO window, NO box — the page
     (map) stays fully visible and only the avatar sits in the screen corner
     with its state ring, plus a small Keyboard pill to exit. The mic re-arms
     itself after every answer, so the conversation flows like a call.
     Transcript still accumulates in the hidden chat DOM. -->
<div class="dashbot-speakmode" id="dashbotSpeakMode">
    <button class="dashbot-speak-exit" id="dashbotSpeakExit" title="Back to chat" type="button">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="6" width="20" height="12" rx="2"/>
            <path d="M6 10h0M10 10h0M14 10h0M18 10h0M6 14h0M18 14h0"/><path d="M9 14h6"/>
        </svg>
        <span>Keyboard</span>
    </button>
    <div class="dashbot-speak-stage" id="dashbotSpeakStage" role="button" tabindex="0" title="Tap to interrupt and talk">
        <div class="dashbot-voice-ring"></div>
        <div class="dashbot-voice-avatar"></div>
    </div>
</div>
`;

root.insertAdjacentHTML('beforeend', html);

// ---- DOM refs ----
const avatar      = document.getElementById('dashbotAvatar');
const panel       = document.getElementById('dashbotPanel');
const closeBtn    = document.getElementById('dashbotCloseBtn');
const messages    = document.getElementById('dashbotMessages');
const input       = document.getElementById('dashbotInput');
const sendBtn     = document.getElementById('dashbotSendBtn');
const typing      = document.getElementById('dashbotTyping');
const resetBtn    = document.getElementById('dashbotResetBtn');
const locationBtn = document.getElementById('dashbotLocationBtn');
const themeBtn    = document.getElementById('dashbotThemeBtn');
const micBtn      = document.getElementById('dashbotMicBtn');
const speakerBtn  = document.getElementById('dashbotSpeakerBtn');
const voiceModeBtn = document.getElementById('dashbotVoiceModeBtn');
const speakModeEl = document.getElementById('dashbotSpeakMode');
const speakStage  = document.getElementById('dashbotSpeakStage');
const speakExit   = document.getElementById('dashbotSpeakExit');

// The widget usually mounts INSIDE the dashboard's Leaflet map container, so
// wheel/drag events over the chat bubble up to Leaflet — the MAP zooms and the
// chat never scrolls. Cut the bubbling at the widget boundary (Leaflet's own
// helpers when the map is present, plain stopPropagation otherwise).
[panel, avatar, speakModeEl].forEach(function (el) {
    if (!el) return;
    if (window.L && L.DomEvent) {
        try {
            L.DomEvent.disableScrollPropagation(el);
            L.DomEvent.disableClickPropagation(el);
        } catch (e) {}
    }
    ['wheel', 'touchmove', 'dblclick', 'mousedown'].forEach(function (t) {
        el.addEventListener(t, function (e) { e.stopPropagation(); });
    });
});

// Quick-action buttons (the data-q ones send a canned question; the location
// one shares position). Re-bound after a chat reset, which restores this HTML.
function bindWelcomeButtons() {
    document.querySelectorAll('.dashbot-quick-btn[data-q]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            const q = btn.getAttribute('data-q');
            if (q) { input.value = q; sendMessage(); }
        });
    });
    const locQuick = document.getElementById('dashbotLocQuick');
    if (locQuick) locQuick.addEventListener('click', toggleLocation);
}
bindWelcomeButtons();

// ---- Session ----
// Mint a fresh session token. Returns true on success. On failure we leave the
// existing session vars untouched (don't poison them with a fake id) and let the
// caller surface a connection error.
async function startSession() {
    try {
        const res = await fetch(DASHBOT_BASE_URL + '/session/start', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        sessionId = data.session_id;
        sessionToken = data.session_token;
        // Server says whether the ElevenLabs proxy is configured; without it
        // spoken replies are silent and server-side STT is hidden.
        voiceAvailable = !!data.voice;
        updateMicVisibility();
        // Per-tab persistence: survives a RELOAD, dies with the tab. Without
        // this, every refresh minted a fresh session and wiped the
        // conversation history ("it forgets stuff" after each reload).
        try {
            sessionStorage.setItem('dashbot-session', JSON.stringify(
                { id: sessionId, token: sessionToken, voice: voiceAvailable }));
        } catch (e) {}
        return true;
    } catch (e) {
        console.warn('Dashbot: could not start session', e);
        return false;
    }
}

// Restore the tab's session across reloads. If it expired server-side in the
// meantime (30 min idle) or the server restarted, the 401/404 recovery in
// sendMessage transparently re-mints one. No unload teardown: the server's
// own idle TTL is the cleanup, so a reload keeps the conversation going.
try {
    const saved = JSON.parse(sessionStorage.getItem('dashbot-session') || 'null');
    if (saved && saved.id && saved.token) {
        sessionId = saved.id;
        sessionToken = saved.token;
        voiceAvailable = !!saved.voice;
        updateMicVisibility();
    }
} catch (e) {}
if (!sessionId) startSession();

// ---- Open / Close ----
function openChat() {
    isChatOpen = true;
    panel.classList.add('open');
    avatar.classList.add('hidden');
    if (!voiceMode) input.focus();   // the input is hidden in voice mode
}

function closeChat() {
    isChatOpen = false;
    panel.classList.remove('open');
    avatar.classList.remove('hidden');
    // Closing the panel always returns to a predictable state: next open
    // starts in the chat view, and nothing keeps talking off-screen.
    if (voiceMode) exitSpeakMode();
}

// Save welcome HTML so we can restore it on reset
const welcomeHTML = document.getElementById('dashbotWelcome').outerHTML;

async function resetChat() {
    // Clear backend history
    if (sessionId) {
        try {
            await fetch(DASHBOT_BASE_URL + '/chat/reset', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...(sessionToken ? { 'X-Session-Token': sessionToken } : {})
                },
                body: JSON.stringify({ session_id: sessionId })
            });
        } catch (_) {}
    }
    // Clear UI messages and restore welcome
    messages.innerHTML = welcomeHTML;
    // Re-bind welcome quick-actions (incl. the Share-my-location button)
    bindWelcomeButtons();
    hideTyping();
    setLoading(false);
    speech.stopAll();
}

avatar.addEventListener('click', openChat);
closeBtn.addEventListener('click', closeChat);
resetBtn.addEventListener('click', resetChat);

// ---- Theme (light default; opt-in dark, remembered per browser) ----
const DB_MOON_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const DB_SUN_SVG  = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';

function applyTheme(theme) {
    const dark = theme === 'dark';
    document.documentElement.classList.toggle('dashbot-dark', dark);
    if (themeBtn) {
        themeBtn.innerHTML = dark ? DB_SUN_SVG : DB_MOON_SVG;
        themeBtn.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    }
}

function initTheme() {
    let t = null;
    try { t = localStorage.getItem('dashbot-theme'); } catch (e) {}
    if (t !== 'dark' && t !== 'light') {
        t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    applyTheme(t);
}

function toggleTheme() {
    const next = document.documentElement.classList.contains('dashbot-dark') ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('dashbot-theme', next); } catch (e) {}
}

initTheme();
if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

// ---- Avatar "listening" glow while the user is typing ----
input.addEventListener('focus', function () { panel.classList.add('listening'); });
input.addEventListener('blur',  function () { panel.classList.remove('listening'); });

// ---- Location ----
function updateLocationButton(state) {
    locationBtn.classList.remove('location-off', 'location-loading', 'location-on', 'location-error');
    locationBtn.classList.add('location-' + state);
    // The label IS the affordance: say what tapping does (off) or what the
    // current state is (on/loading), not just tint an icon.
    var labels = { off: 'Share location', loading: 'Locating…', on: 'Location on', error: 'No access' };
    var titles = {
        off: 'Tap to share your location',
        loading: 'Getting your position…',
        on: 'Location is shared — tap to turn off',
        error: 'Location unavailable — tap to retry'
    };
    var label = locationBtn.querySelector('.db-loc-label');
    if (label) label.textContent = labels[state] || labels.off;
    locationBtn.title = titles[state] || titles.off;
}

function showLocationToast(msg) {
    var toast = document.createElement('div');
    toast.className = 'dashbot-location-toast';
    toast.textContent = msg;
    messages.appendChild(toast);
    scrollToBottom();
    setTimeout(function() {
        toast.style.opacity = '0';
        setTimeout(function() { toast.remove(); }, 300);
    }, 2000);
}

function toggleLocation() {
    if (locationEnabled) {
        userLocation = null;
        locationEnabled = false;
        locationStatus = 'off';
        updateLocationButton('off');
        showLocationToast('Location disabled');
        return;
    }
    if (!navigator.geolocation) {
        locationStatus = 'unsupported';
        updateLocationButton('error');
        showLocationToast('Geolocation not supported');
        setTimeout(function() { updateLocationButton('off'); }, 3000);
        return;
    }
    updateLocationButton('loading');
    navigator.geolocation.getCurrentPosition(
        function(pos) {
            userLocation = { lat: pos.coords.latitude, lon: pos.coords.longitude };
            locationEnabled = true;
            locationStatus = 'on';
            updateLocationButton('on');
            showLocationToast('Location enabled');
        },
        function(err) {
            var msg = 'Location error';
            locationStatus = 'unavailable';
            if (err.code === 1) { msg = 'Location permission denied'; locationStatus = 'denied'; }
            else if (err.code === 2) { msg = 'Location unavailable'; locationStatus = 'unavailable'; }
            else if (err.code === 3) { msg = 'Location request timed out'; locationStatus = 'timeout'; }
            updateLocationButton('error');
            showLocationToast(msg);
            setTimeout(function() { updateLocationButton('off'); }, 3000);
        },
        { enableHighAccuracy: true, timeout: 10000 }
    );
}

locationBtn.addEventListener('click', toggleLocation);

// ---- Helpers ----
function scrollToBottom() { messages.scrollTop = messages.scrollHeight; }

var thinkingPhrases = [
    '\uD83C\uDF10 Scanning the city...',
    '\uD83D\uDEE3\uFE0F Checking road conditions...',
    '\uD83D\uDCCF Calculating distances...',
    '\uD83D\uDCE1 Reading sensor data...',
    '\uD83C\uDFDB\uFE0F Looking over campus buildings...',
    '\uD83D\uDE97 Analyzing traffic flow...',
    '\uD83C\uDD7F\uFE0F Checking parking availability...',
    '\u26C5 Fetching weather updates...',
    '\uD83D\uDE8B Mapping transit routes...',
    '\u2699\uFE0F Querying smart city systems...',
    '\uD83E\uDDE0 Processing your request...',
    '\uD83D\uDCF6 Connecting to FIWARE sensors...',
    '\uD83D\uDE8A Checking tram schedules...',
    '\u2693 Surveying the Science Harbor...',
    '\uD83D\uDDFA\uFE0F Exploring the map...',
    '\uD83C\uDF21\uFE0F Reading temperatures...',
];
var typingTextEl = typing.querySelector('.dashbot-typing-text');
var typingInterval = null;

function showTyping() {
    typing.classList.add('show');
    panel.classList.add('thinking');
    var idx = Math.floor(Math.random() * thinkingPhrases.length);
    typingTextEl.textContent = thinkingPhrases[idx];
    typingInterval = setInterval(function() {
        idx = (idx + 1) % thinkingPhrases.length;
        typingTextEl.style.opacity = '0';
        setTimeout(function() {
            typingTextEl.textContent = thinkingPhrases[idx];
            typingTextEl.style.opacity = '1';
        }, 200);
    }, 2000);
    scrollToBottom();
}

function hideTyping() {
    typing.classList.remove('show');
    panel.classList.remove('thinking');
    if (typingInterval) { clearInterval(typingInterval); typingInterval = null; }
}

// Pin the typing indicator to one phrase (stops the rotating placeholders).
// Used by voice mode so the SPOKEN acknowledgment and the on-screen status
// say the same thing while the agent works.
function pinTypingText(text) {
    if (typingInterval) { clearInterval(typingInterval); typingInterval = null; }
    typingTextEl.style.opacity = '1';
    typingTextEl.textContent = '🎙️ ' + stripAudioTags(text);
}

function setLoading(on) {
    // Busy state just dims the (disabled) send button — no spinner. The
    // "Checking…" typing indicator already shows the assistant is working.
    sendBtn.disabled = on;
    // Speaking mode: an answer that produced no speech (error, empty, muted
    // proxy AND no browser voice) must still hand the turn back — return the
    // ring to idle and re-arm the mic. When audio IS playing, the queue's
    // own 'idle' event does this later instead.
    if (!on && voiceMode && !speech.isActive()) {
        setSpeakState('idle');
        scheduleHandsFreeResume(600);
    }
}

// ElevenLabs v3 audio tags ([sighs], [pause]) the voice-mode agent emits.
// They are for the EARS only — strip from anything rendered. `(?!\()`
// spares markdown links [text](url).
var DB_AUDIO_TAG_RE = /\[[a-zA-Z][a-zA-Z ]{0,28}\](?!\()/g;
function stripAudioTags(text) {
    if (!text || text.indexOf('[') === -1) return text;
    return text.replace(DB_AUDIO_TAG_RE, '').replace(/  +/g, ' ');
}

// Light formatter for streaming — only safe inline transforms, no structure detection
function formatStreaming(content) {
    if (!content) return content;
    var f = stripAudioTags(content);
    // URLs
    f = f.replace(/(https:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" class="map-link">View Location</a>');
    // Bold markdown
    f = f.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Newlines → <br>
    f = f.replace(/\n/g, '<br>');
    return f;
}

function formatBotMessage(content) {
    if (!content) return content;

    // --- Inline transformations (URLs, bold, italic) ---
    var f = stripAudioTags(content);
    f = f.replace(/(https:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener" class="map-link">$1</a>');
    f = f.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // italic: match single * but not ** (already handled)
    f = f.replace(/(^|[\s(])\*(?!\*)([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');

    // --- Block parsing: paragraphs, bullet lists, numbered lists ---
    var rawLines = f.split('\n');
    var blocks = [];
    var current = null;

    function flush() {
        if (current && current.lines.length) blocks.push(current);
        current = null;
    }

    for (var i = 0; i < rawLines.length; i++) {
        var line = rawLines[i].trim();
        if (!line) { flush(); continue; }

        var mBullet = /^[-•]\s+(.*)$/.exec(line);
        var mNum = /^\d+\.\s+(.*)$/.exec(line);

        if (mBullet) {
            if (!current || current.type !== 'ul') { flush(); current = { type: 'ul', lines: [] }; }
            current.lines.push(mBullet[1]);
        } else if (mNum) {
            if (!current || current.type !== 'ol') { flush(); current = { type: 'ol', lines: [] }; }
            current.lines.push(mNum[1]);
        } else {
            if (!current || current.type !== 'p') { flush(); current = { type: 'p', lines: [] }; }
            current.lines.push(line);
        }
    }
    flush();

    // --- Inline highlights for times, distances, temperatures ---
    function highlight(text) {
        text = text.replace(/\b(\d+\.?\d*)\s*(minutes?|min|hours?|hrs?)\b/gi, '<span class="db-pill db-pill-time">$1 $2</span>');
        text = text.replace(/\b(\d+\.?\d*)\s*(meters?|m|km|kilometres?|kilometers?)\b(?!\w)/gi, '<span class="db-pill db-pill-dist">$1 $2</span>');
        text = text.replace(/\b(-?\d+\.?\d*)\s*°\s*(C|F)\b/gi, '<span class="db-pill db-pill-temp">$1°$2</span>');
        return text;
    }

    // --- Render ---
    var html = '';
    for (var b = 0; b < blocks.length; b++) {
        var blk = blocks[b];
        if (blk.type === 'ul') {
            html += '<ul class="db-list">';
            for (var j = 0; j < blk.lines.length; j++) html += '<li>' + highlight(blk.lines[j]) + '</li>';
            html += '</ul>';
        } else if (blk.type === 'ol') {
            html += '<ol class="db-list">';
            for (var j = 0; j < blk.lines.length; j++) html += '<li>' + highlight(blk.lines[j]) + '</li>';
            html += '</ol>';
        } else {
            html += '<p class="db-para">' + blk.lines.map(highlight).join(' ') + '</p>';
        }
    }

    return html || content;
}

function now() {
    return new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
}

// ---- Messages ----
function addMessage(content, isUser) {
    const div = document.createElement('div');
    div.className = 'dashbot-msg ' + (isUser ? 'user' : 'bot with-avatar');

    const bubble = document.createElement('div');
    bubble.className = 'dashbot-bubble';
    bubble.innerHTML = isUser ? content : formatBotMessage(content);

    const time = document.createElement('div');
    time.className = 'dashbot-msg-time';
    time.textContent = now();

    bubble.appendChild(time);

    if (!isUser) {
        var botAvatar = document.createElement('div');
        botAvatar.className = 'db-bot-avatar';
        div.appendChild(botAvatar);
    }
    div.appendChild(bubble);
    messages.appendChild(div);

    // remove welcome
    const w = document.getElementById('dashbotWelcome');
    if (w) w.remove();

    scrollToBottom();
}

function addStreamingMessage() {
    const div = document.createElement('div');
    div.className = 'dashbot-msg bot with-avatar';

    const botAvatar = document.createElement('div');
    botAvatar.className = 'db-bot-avatar';

    const bubble = document.createElement('div');
    bubble.className = 'dashbot-bubble';

    const textContainer = document.createElement('div');
    textContainer.className = 'db-bubble-text';

    const cardsContainer = document.createElement('div');
    cardsContainer.className = 'db-cards';

    // Text first, cards below — cards are appended after response finishes.
    bubble.appendChild(textContainer);
    bubble.appendChild(cardsContainer);

    const time = document.createElement('div');
    time.className = 'dashbot-msg-time';
    time.textContent = now();

    div.appendChild(botAvatar);
    div.appendChild(bubble);
    messages.appendChild(div);

    const w = document.getElementById('dashbotWelcome');
    if (w) w.remove();

    // Return textContainer as `bubble` so existing innerHTML assignments target the
    // text area only — the cards container above them is preserved.
    // `outerBubble` exposes the outer .dashbot-bubble for class toggles (e.g. is-streaming).
    // `msg` is the whole row (for appending suggestion chips); `botAvatar` is the orb
    // (toggled to "speaking" while tokens stream).
    return { bubble: textContainer, time, cardsContainer, outerBubble: bubble, msg: div, botAvatar };
}

// ---- Info card rendering ----
function dbEscape(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function dbFmtDist(m) {
    if (m == null) return '';
    if (m < 1000) return Math.round(m) + ' m';
    return (m / 1000).toFixed(1) + ' km';
}

function dbFmtDur(s) {
    if (s == null) return '';
    if (s < 60) return Math.round(s) + ' s';
    const min = Math.round(s / 60);
    if (min < 60) return min + ' min';
    const h = Math.floor(min / 60);
    const mm = min % 60;
    return h + 'h ' + (mm ? mm + 'm' : '');
}

function renderTransitCard(card) {
    const segments = card.segments || [];
    const transfers = card.total_transfers || 0;

    // Metric strip
    const metrics = [];
    metrics.push('<span class="db-metric"><span class="db-metric-val">' + (card.total_stops || 0) + '</span><span class="db-metric-unit">stops</span></span>');
    if (transfers > 0) {
        metrics.push('<span class="db-metric db-metric-warn"><span class="db-metric-val">' + transfers + '</span><span class="db-metric-unit">transfer' + (transfers > 1 ? 's' : '') + '</span></span>');
    } else {
        metrics.push('<span class="db-metric db-metric-ok"><span class="db-metric-val">Direct</span></span>');
    }
    if (card.origin_walk_m) {
        metrics.push('<span class="db-metric"><span class="db-metric-val">' + dbFmtDist(card.origin_walk_m) + '</span><span class="db-metric-unit">walk start</span></span>');
    }
    if (card.destination_walk_m) {
        metrics.push('<span class="db-metric"><span class="db-metric-val">' + dbFmtDist(card.destination_walk_m) + '</span><span class="db-metric-unit">walk end</span></span>');
    }

    // Timeline
    const rows = [];
    // Origin
    const originName = (segments[0] && segments[0].from) || card.origin_stop || card.origin || '';
    rows.push(
        '<div class="db-tl-item db-tl-start">' +
            '<div class="db-tl-marker"><div class="db-tl-dot"></div></div>' +
            '<div class="db-tl-body">' +
                '<div class="db-tl-name">' + dbEscape(originName) + '</div>' +
                '<div class="db-tl-sub">Start</div>' +
            '</div>' +
        '</div>'
    );

    segments.forEach(function(s, i) {
        const isBus = /bus/i.test(s.line || '');
        const icon = isBus ? '\uD83D\uDE8C' : '\uD83D\uDE8B';
        const badgeClass = isBus ? 'db-tl-badge db-line-bus' : 'db-tl-badge';
        const intermediate = (s.stops || []).slice(1, -1); // exclude from/to
        const stopsList = intermediate.length
            ? '<details class="db-tl-stops">' +
                '<summary>' + intermediate.length + ' stop' + (intermediate.length > 1 ? 's' : '') + ' along the way</summary>' +
                '<ol>' + intermediate.map(function(st) { return '<li>' + dbEscape(st) + '</li>'; }).join('') + '</ol>' +
              '</details>'
            : '';
        rows.push(
            '<div class="db-tl-item db-tl-segment">' +
                '<div class="db-tl-marker"><div class="db-tl-connector"></div></div>' +
                '<div class="db-tl-body">' +
                    '<span class="' + badgeClass + '">' + icon + ' ' + dbEscape(s.line || '') + '</span>' +
                    (s.direction ? '<div class="db-tl-dir"><span class="db-tl-dir-arrow">\u2192</span>' + dbEscape(s.direction) + (s.num_stops ? ' \u00B7 ride ' + s.num_stops + ' stops' : '') + '</div>' : '') +
                    stopsList +
                '</div>' +
            '</div>'
        );

        // Transfer marker between segments (intermediate stop is also the next from)
        if (i < segments.length - 1) {
            rows.push(
                '<div class="db-tl-item db-tl-transfer">' +
                    '<div class="db-tl-marker"><div class="db-tl-dot"></div></div>' +
                    '<div class="db-tl-body">' +
                        '<div class="db-tl-name">' + dbEscape(s.to || '') + '</div>' +
                        '<div class="db-tl-sub">Transfer</div>' +
                    '</div>' +
                '</div>'
            );
        }
    });

    // Destination
    const destName = (segments.length && segments[segments.length - 1].to) || card.destination_stop || card.destination || '';
    rows.push(
        '<div class="db-tl-item db-tl-end">' +
            '<div class="db-tl-marker"><div class="db-tl-dot db-tl-dot-end"></div></div>' +
            '<div class="db-tl-body">' +
                '<div class="db-tl-name">' + dbEscape(destName) + '</div>' +
                '<div class="db-tl-sub">Arrive</div>' +
            '</div>' +
        '</div>'
    );

    return '<div class="db-card db-card-transit">' +
        '<div class="db-card-head">' +
            '<div class="db-card-title">' +
                '<span class="db-card-icon">\uD83D\uDE8F</span>' +
                '<span>' + dbEscape(card.origin || '') + ' \u2192 ' + dbEscape(card.destination || '') + '</span>' +
            '</div>' +
        '</div>' +
        '<div class="db-metrics">' + metrics.join('') + '</div>' +
        '<div class="db-timeline">' + rows.join('') + '</div>' +
    '</div>';
}

function renderRouteCard(card) {
    const icons = { walking: '\uD83D\uDEB6', cycling: '\uD83D\uDEB4', driving: '\uD83D\uDE97' };
    const modeLabel = (card.mode || '').charAt(0).toUpperCase() + (card.mode || '').slice(1);
    const dirs = (card.directions || []).slice(0, 8).map(function(d) {
        const text = typeof d === 'string' ? d : (d.instruction || d.text || d.message || '');
        return text ? '<li>' + dbEscape(text) + '</li>' : '';
    }).filter(Boolean).join('');

    const metrics = [];
    if (card.distance_m != null) {
        metrics.push('<span class="db-metric"><span class="db-metric-val">' + dbFmtDist(card.distance_m) + '</span></span>');
    }
    if (card.duration_s != null) {
        metrics.push('<span class="db-metric"><span class="db-metric-val">' + dbFmtDur(card.duration_s) + '</span></span>');
    }
    if (card.traffic_delay_s) {
        metrics.push('<span class="db-metric db-metric-warn"><span class="db-metric-val">+' + dbFmtDur(card.traffic_delay_s) + '</span><span class="db-metric-unit">traffic</span></span>');
    }

    // ---- Live real-time chips: driving \u2192 traffic + parking;
    //      walking/cycling \u2192 air quality + weather ----
    var live = [];

    if (card.congestion) {
        var cMap = { clear: ['Clear', 'db-live-ok'], moderate: ['Slow', 'db-live-warn'], heavy: ['Heavy', 'db-live-bad'] };
        var c = cMap[card.congestion] || [card.congestion, ''];
        live.push('<span class="db-live-chip ' + c[1] + '">\uD83D\uDEA6 ' + c[0] + ' traffic</span>');
    }
    if (card.parking && card.parking.free != null) {
        var pTxt = '\uD83C\uDD7F\uFE0F ' + card.parking.free + ' free';
        if (card.parking.distance_m != null) pTxt += ' \u00B7 ' + dbFmtDist(card.parking.distance_m);
        live.push('<span class="db-live-chip">' + dbEscape(pTxt) + '</span>');
    }
    if (card.air && card.air.level) {
        var aMap = { good: ['Good', 'db-live-ok'], moderate: ['Moderate', 'db-live-warn'], poor: ['Poor', 'db-live-bad'] };
        var a = aMap[card.air.level] || [card.air.level, ''];
        live.push('<span class="db-live-chip ' + a[1] + '"><span class="db-dot"></span>' + a[0] + ' air</span>');
    }
    if (card.weather && (card.weather.condition || card.weather.temp_c != null)) {
        var wMap = { rain: '\uD83C\uDF27\uFE0F', windy: '\uD83D\uDCA8', clear: '\u2600\uFE0F' };
        var wTxt = (wMap[card.weather.condition] || '\u26C5');
        if (card.weather.temp_c != null) wTxt += ' ' + Math.round(card.weather.temp_c) + '\u00B0C';
        live.push('<span class="db-live-chip">' + wTxt + '</span>');
    }

    var liveRow = live.length ? '<div class="db-live-row">' + live.join('') + '</div>' : '';
    var liveBadge = live.length ? '<span class="db-live-badge">LIVE</span>' : '';

    return '<div class="db-card db-card-route db-route-' + dbEscape(card.mode || '') + '">' +
        '<div class="db-card-head">' +
            '<div class="db-card-title">' +
                '<span class="db-card-icon">' + (icons[card.mode] || '\uD83D\uDDFA\uFE0F') + '</span>' +
                '<span>' + modeLabel + ' route</span>' +
            '</div>' +
            liveBadge +
        '</div>' +
        (metrics.length ? '<div class="db-metrics">' + metrics.join('') + '</div>' : '') +
        liveRow +
        (dirs ? '<details class="db-route-directions"><summary>Turn-by-turn directions</summary><ol>' + dirs + '</ol></details>' : '') +
    '</div>';
}

// ---- Map overlay ----
// Draws on the HOST page's Leaflet map (the IMIQ dashboard's `map`). No-ops
// gracefully when the page has no Leaflet map (e.g. Dashbot's standalone chat
// page), so the widget stays self-contained and infra-free.
let dashbotMapOverlay = null;   // place markers
let routePolyline = null;       // the single route line currently on the map
let defaultRouteDrawn = false;  // has the default (walking) route been auto-shown this answer?
let dashbotPlaceMarkers = {};   // "lat,lon" -> marker, so a place card can refocus its pin

function getLeafletMap() {
    // The dashboard declares a page-global `const map` (classic script) + global `L`.
    try { if (typeof map !== 'undefined' && window.L && map instanceof L.Map) return map; } catch (e) {}
    try { if (window.map && window.L && window.map instanceof L.Map) return window.map; } catch (e) {}
    return null;
}

function getMapOverlay(m) {
    if (!dashbotMapOverlay) { dashbotMapOverlay = L.layerGroup().addTo(m); }
    return dashbotMapOverlay;
}

function clearMapOverlay() {
    try { if (dashbotMapOverlay) dashbotMapOverlay.clearLayers(); } catch (e) {}
    const m = getLeafletMap();
    try { if (m && routePolyline) m.removeLayer(routePolyline); } catch (e) {}
    routePolyline = null;
    defaultRouteDrawn = false;
    dashbotPlaceMarkers = {};
}

const DB_ROUTE_COLORS = { walking: '#2e7d32', cycling: '#1565c0', driving: '#7b1fa2' };

// Category → pin/card emoji. `kind` is detected server-side (api.py _pin_kind)
// from the record's type fields; "place" is the neutral fallback.
const DB_PIN_EMOJI = {
    event: '🎉', restaurant: '🍽️', cafe: '☕', bar: '🍸', supermarket: '🛒',
    parking: '🅿️', stop: '🚏', hotel: '🛏️', culture: '🎭', building: '🏛️',
    place: '📍'
};

function kindEmoji(kind) {
    return DB_PIN_EMOJI[kind] || DB_PIN_EMOJI.place;
}

// Emoji chip pin (glassy circle + pointer tail) instead of Leaflet's stock
// blue marker — the category is readable at a glance on the map.
function pinIcon(kind) {
    return L.divIcon({
        className: 'db-pin',
        html: '<div class="db-pin-chip">' + kindEmoji(kind) + '</div>',
        iconSize: [34, 42],
        iconAnchor: [17, 40],
        popupAnchor: [0, -38]
    });
}

function drawOnMap(card) {
    // Place pins only — routes are handled by drawRoute/selectRoute so the map
    // never shows more than ONE route line at a time.
    const m = getLeafletMap();
    if (!m || !window.L || !card) return;
    if (card.type !== 'place' || card.lat == null || card.lon == null) return;
    try {
        const overlay = getMapOverlay(m);
        const marker = L.marker([card.lat, card.lon], { icon: pinIcon(card.kind) });
        if (card.name) marker.bindPopup(String(card.name));
        overlay.addLayer(marker);
        dashbotPlaceMarkers[card.lat.toFixed(5) + ',' + card.lon.toFixed(5)] = marker;
        // Single pin → center + open popup; multiple → frame them all.
        if (overlay.getLayers().length <= 1) {
            m.setView([card.lat, card.lon], Math.max(m.getZoom(), 16));
            marker.openPopup();
        } else {
            try { m.fitBounds(overlay.getBounds(), { padding: [40, 40], maxZoom: 17 }); } catch (e) {}
        }
    } catch (e) {
        console.warn('Dashbot: map draw failed', e);
    }
}

// Always exactly ONE route line. Drawing a mode replaces the previous line.
// A straight-line connector (router gave no path geometry) is drawn dashed so
// it reads as "from here to there", not as the actual path.
function drawRoute(mode, coords, straightLine) {
    const m = getLeafletMap();
    if (!m || !window.L || !Array.isArray(coords) || coords.length < 2) return;
    try { if (routePolyline) m.removeLayer(routePolyline); } catch (e) {}
    routePolyline = L.polyline(coords, {
        color: DB_ROUTE_COLORS[mode] || '#7a003f', weight: 5, opacity: 0.9,
        dashArray: straightLine ? '8 10' : null,
    }).addTo(m);
    try {
        const layers = [routePolyline].concat(dashbotMapOverlay ? dashbotMapOverlay.getLayers() : []);
        m.fitBounds(L.featureGroup(layers).getBounds(), { padding: [40, 40], maxZoom: 17 });
    } catch (e) {}
}

// Draw the chosen mode's line and highlight its card among its siblings.
function selectRoute(el, mode, coords, straightLine) {
    drawRoute(mode, coords, straightLine);
    try {
        const group = el.closest('.db-cards') || el.parentElement;
        if (group) {
            group.querySelectorAll('.db-card-route').forEach(function (c) {
                if (c === el) {
                    c.style.outline = '2px solid ' + (DB_ROUTE_COLORS[mode] || '#7a003f');
                    c.style.outlineOffset = '1px';
                } else {
                    c.style.outline = 'none';
                }
            });
        }
    } catch (e) {}
}

// Pan/zoom the host map to a pinned place and open its popup (place-card click).
function selectPlace(lat, lon) {
    const m = getLeafletMap();
    if (!m || !window.L) return;
    try {
        m.setView([lat, lon], Math.max(m.getZoom(), 17));
        const marker = dashbotPlaceMarkers[lat.toFixed(5) + ',' + lon.toFixed(5)];
        if (marker && marker.openPopup) marker.openPopup();
    } catch (e) {}
}

function renderPlaceCard(card) {
    return '<div class="db-card db-card-place">' +
        '<div class="db-card-head">' +
            '<div class="db-card-title">' +
                '<span class="db-card-icon">' + kindEmoji(card.kind) + '</span>' +
                '<span>' + dbEscape(card.name || 'Location') + '</span>' +
            '</div>' +
        '</div>' +
        '<div class="db-tl-sub">Shown on the map</div>' +
    '</div>';
}

function renderCard(container, card) {
    if (!container || !card || !card.type) return;
    // Draw the geo overlay on the host Leaflet map (dashboard) when present.
    drawOnMap(card);
    let html = '';
    switch (card.type) {
        case 'transit_route': html = renderTransitCard(card); break;
        case 'route': html = renderRouteCard(card); break;
        case 'place': html = renderPlaceCard(card); break;
        default: return;
    }
    const wrap = document.createElement('div');
    wrap.innerHTML = html;
    const el = wrap.firstElementChild;
    if (el) {
        el.classList.add('db-card-enter');
        const isRoute = (card.type === 'route' && Array.isArray(card.geometry) && card.geometry.length > 1);
        if (isRoute) {
            el.setAttribute('data-mode', card.mode);
            el.style.cursor = 'pointer';
            el.title = 'Show this route on the map';
            el.addEventListener('click', function () { selectRoute(el, card.mode, card.geometry, card.straight_line); });
        } else if (card.type === 'place' && card.lat != null && card.lon != null && getLeafletMap()) {
            el.style.cursor = 'pointer';
            el.title = 'Show on the map';
            el.addEventListener('click', function () { selectPlace(card.lat, card.lon); });
        }
        container.appendChild(el);
        // Show ONE route by default (walking — route cards arrive walking-first).
        if (isRoute && !defaultRouteDrawn) {
            selectRoute(el, card.mode, card.geometry, card.straight_line);
            defaultRouteDrawn = true;
        }
        requestAnimationFrame(function() { el.classList.add('db-card-shown'); });
    }
}

// ---- Follow-up suggestion chips (rendered below a finished answer) ----
function buildSuggestions(cards) {
    var types = {};
    (cards || []).forEach(function (c) { if (c && c.type) types[c.type] = true; });
    var s = [];
    if (types.place) {
        s.push({ icon: '🧭', label: 'Directions there', q: 'How do I get there?' });
        s.push({ icon: '📍', label: "What's nearby?", q: "What's around there?" });
    }
    // Route answers already show every mode (walk/bike/tram/drive) with live
    // conditions, so they get no follow-up chips — not even the generic fallback.
    var hadCards = !!(types.place || types.route || types.transit_route);
    if (!s.length && !hadCards) {
        s.push({ icon: '🅿️', label: 'Parking nearby', q: 'Available parking near me?' });
        s.push({ icon: '⛅', label: 'Weather now', q: "What's the weather right now?" });
    }
    // de-dupe by label, cap at 3
    var seen = {}, out = [];
    for (var i = 0; i < s.length && out.length < 3; i++) {
        if (!seen[s[i].label]) { seen[s[i].label] = true; out.push(s[i]); }
    }
    return out;
}

function renderSuggestions(msgDiv, suggestions) {
    if (!msgDiv || !suggestions || !suggestions.length) return;
    var row = document.createElement('div');
    row.className = 'db-suggestions';
    suggestions.forEach(function (sg) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'db-suggest-chip';
        b.textContent = sg.icon + ' ' + sg.label;
        b.addEventListener('click', function () {
            if (sendBtn.disabled) return;   // ignore taps while a request is in flight
            input.value = sg.q;
            sendMessage();
        });
        row.appendChild(b);
    });
    msgDiv.appendChild(row);
    scrollToBottom();
}

// ============================================================================
// Voice — STT (mic button) + TTS (spoken replies) via the backend /voice/*
// proxy (the ElevenLabs key never reaches the browser).
//
// Latency design: an agent turn takes ~6-7s (tool calls), and silence that
// long sounds robotic. So:
//   1. The instant a voice question is sent, a short RULE-BASED
//      acknowledgment plays ("Hmm, okay. Let me check the city."). The
//      phrases are fixed strings, so the backend disk-caches their audio —
//      after first use they play with zero synthesis latency. (An
//      LLM-generated filler can't do this job: the model spends those
//      seconds calling tools BEFORE emitting its first token.)
//   2. The real answer is synthesized SENTENCE BY SENTENCE as tokens
//      stream in, so speech starts when the first sentence exists — not
//      when the whole reply is done. Fetches run in parallel; a
//      sequential audio queue preserves order; `previous_text` keeps the
//      prosody continuous across chunks.
//
// STT prefers the browser's Web Speech API (free, near-instant); browsers
// without it (Firefox, some Safari) record via MediaRecorder and transcribe
// through the backend's ElevenLabs Scribe proxy.
// ============================================================================

// ---- Acknowledgment phrases (rule-based, keyword-routed) ----
// SHORT, calm acks (~1.5-2.5s of audio). The queue plays strictly in order,
// so a long ack would BLOCK the answer even when the agent is already done —
// short ack now, and the adaptive fillers below cover any longer wait. No
// tags, no theatrics: the ack sets the emotional baseline for the answer.
const ACK_PHRASES = {
    en: {
        route:   ['Okay, let me plan that route…', 'One sec, checking the way there…', 'Alright, let me look at the routes…'],
        live:    ['Okay, checking the live data…', 'One sec, reading the sensors…', 'Let me get the latest readings…'],
        place:   ['Okay, let me look that up…', 'Hmm, let me find that…', 'One sec…'],
        // Content-free on purpose: generic fires for anything unrecognized,
        // so it must never assume the question was about the city.
        generic: ['Hmm, okay… one sec…', 'Alright, let me think…', 'Mmm, one moment…']
    },
    de: {
        route:   ['Okay, ich plane kurz die Route…', 'Einen Moment, ich schaue nach dem Weg…'],
        live:    ['Okay, ich schaue auf die Live-Daten…', 'Einen Moment, ich lese die Sensoren…'],
        place:   ['Okay, ich suche das kurz raus…', 'Hmm, mal sehen…'],
        generic: ['Hmm, okay… einen Moment…', 'Alles klar, Sekunde…', 'Mmm, Moment…']
    }
};
// Adaptive fillers: repeat every few seconds ONLY while the answer still
// hasn't reached the audio queue — total wait coverage grows with the actual
// latency instead of one long fixed clip delaying a fast answer.
const FILLER_PHRASES = {
    en: ['Mmm…', 'Hmm… almost there…', 'Just a moment…', 'One sec…'],
    de: ['Mmm…', 'Hmm… gleich…', 'Einen Augenblick…', 'Moment…']
};
let lastAckPhrase = '';

function classifyAck(msg) {
    const m = (msg || '').toLowerCase().trim();
    // Small talk gets NO ack ('none'): greetings answer in a beat (no tool
    // calls), and "let me check the city" over a "hello" is exactly wrong.
    if (/^(hi|hello|hey|yo|hallo|moin|servus|na)\b/.test(m) ||
        /^(thanks|thank you|thx|danke|cheers|ok|okay|cool|nice|great|bye|goodbye|tschüss|ciao)\b/.test(m) ||
        /^(good (morning|afternoon|evening|night)|guten (morgen|tag|abend)|gute nacht)\b/.test(m) ||
        /(how are you|wie geht)/.test(m)) return 'none';
    if (/(route|get to|go to|come to|reach|directions|way to|nearest|closest|how far|tram|bus|drive|walk|cycle|verbindung|weg nach|wie komme)/.test(m)) return 'route';
    if (/(weather|temperature|rain|wind|parking|traffic|air quality|pollution|menu|mensa|water level|wetter|temperatur|regen|parkplatz|verkehr|luft|speiseplan)/.test(m)) return 'live';
    if (/(where is|where's|what is|what's|find|show me|open now|opening|wo ist|was ist|zeig|geöffnet)/.test(m)) return 'place';
    return 'generic';
}

function pickAckPhrase(kind) {
    const table = ACK_PHRASES[DB_LANG] || ACK_PHRASES.en;
    const list = table[kind] || table.generic;
    // Avoid saying the exact same thing twice in a row.
    let pick = list[Math.floor(Math.random() * list.length)];
    if (list.length > 1 && pick === lastAckPhrase) {
        pick = list[(list.indexOf(pick) + 1) % list.length];
    }
    lastAckPhrase = pick;
    return pick;
}

function pickFillerPhrase() {
    const list = FILLER_PHRASES[DB_LANG] || FILLER_PHRASES.en;
    return list[Math.floor(Math.random() * list.length)];
}

// ---- Text cleanup: chat markdown → speakable prose ----
function speechClean(text) {
    if (!text) return '';
    let t = text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');   // md links → their label
    t = t.replace(/https?:\/\/\S+/g, '');                    // bare URLs
    t = t.replace(/^\s*(?:[-•]|\d+\.)\s+/gm, '');            // bullet/number markers
    t = t.replace(/[*_`#|]+/g, '');                          // md emphasis marks
    t = t.replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}\u{1F1E6}-\u{1F1FF}]/gu, '');
    return t.replace(/\s+/g, ' ').trim();
}

// ---- Sequential audio player. Fetches resolve in parallel; playback is
// strictly in enqueue order. stopAll() bumps the generation so stale items
// (and stale in-flight fetches) are dropped instead of played late.
// There is deliberately NO browser-voice backup: a failed ElevenLabs chunk
// is SILENCE (the speechSynthesis fallback sounded like a different robot
// and read audio tags literally — removed at the user's request). ----
const speech = (function () {
    const queue = [];
    let playing = false;
    let generation = 0;
    let currentAudio = null;

    function playBlob(blob) {
        return new Promise(function (resolve) {
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            currentAudio = audio;
            function finish() {
                URL.revokeObjectURL(url);
                if (currentAudio === audio) currentAudio = null;
                resolve();
            }
            audio.onended = finish;
            audio.onerror = finish;
            audio.play().catch(finish);   // autoplay blocked etc. — never wedge the queue
        });
    }

    let onActivity = null;   // voice mode listens: ('speak', text) | ('idle')

    async function drain() {
        if (playing) return;
        playing = true;
        try {
            while (queue.length) {
                const item = queue.shift();
                if (item.gen !== generation) continue;
                let blob = null;
                try { blob = await item.blobPromise; } catch (e) {}
                if (item.gen !== generation) continue;   // stopped while fetching
                if (!blob) continue;   // TTS failed/unconfigured → this chunk is silence
                if (onActivity) onActivity('speak', item.text);
                await playBlob(blob);
            }
        } finally {
            playing = false;
            if (onActivity) onActivity('idle');
        }
    }

    return {
        // blobPromise resolves to an MP3 Blob, or null on any failure — a
        // null chunk is simply skipped (silence). `text` feeds the activity
        // listener (speaking-mode state), not any fallback voice.
        enqueue: function (blobPromise, text) {
            queue.push({ gen: generation, blobPromise: blobPromise, text: text });
            drain();
        },
        stopAll: function () {
            generation++;
            queue.length = 0;
            try { if (currentAudio) { currentAudio.pause(); currentAudio = null; } } catch (e) {}
        },
        isActive: function () { return playing; },
        // Single observer is enough (the voice-mode view); not a full emitter.
        setActivityListener: function (fn) { onActivity = fn; }
    };
})();

// ---- TTS fetch (backend proxy). Resolves null on any failure — the audio
// queue then skips that chunk entirely (silence, no backup voice). ----
function fetchTTS(text, previousText) {
    if (!voiceAvailable || !sessionId) return Promise.resolve(null);
    return fetch(DASHBOT_BASE_URL + '/voice/tts', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(sessionToken ? { 'X-Session-Token': sessionToken } : {})
        },
        body: JSON.stringify({
            text: text,
            session_id: sessionId,
            language: DB_LANG,
            previous_text: previousText || undefined
        })
    }).then(function (r) { return r.ok ? r.blob() : null; }).catch(function () { return null; });
}

// ---- Sentence chunker: feed it streaming tokens, it emits TTS-sized chunks.
// The FIRST chunk fires on the first sentence boundary (speech starts ASAP).
// Rest-chunk sizing balances two failure modes on eleven_v3 (no previous_text
// stitching): chunks too SMALL → a tone seam at every sentence; too BIG →
// synthesis of the huge tail can't keep up with playback and the voice goes
// silent mid-answer. ~2-3 sentences per generation keeps the pipeline
// gapless with at most one or two seams. ----
function makeSpeechChunker() {
    let buf = '';
    let prev = '';        // previous spoken chunk (prosody continuity on v2 models)
    let emitted = false;
    const MIN_FIRST = 12, MIN_REST = 280, MAX = 600;
    const BOUNDARY = /[.!?…]+["')\]]*\s+/g;

    function emit(raw) {
        const clean = speechClean(raw);
        if (!clean || clean.length < 2) return;
        // Cap previous_text to the server's 600-char validation limit —
        // with big chunks the full prior chunk can exceed it (422 → the
        // whole request would silently fall back to the browser voice).
        speech.enqueue(fetchTTS(clean, prev.slice(-500)), clean);
        prev = clean;
        emitted = true;
    }

    function drain(final) {
        while (true) {
            const min = emitted ? MIN_REST : MIN_FIRST;
            BOUNDARY.lastIndex = 0;
            let cut = -1, m;
            while ((m = BOUNDARY.exec(buf)) !== null) {
                const end = m.index + m[0].length;
                if (end >= min) { cut = end; break; }
            }
            if (cut === -1) {
                if (buf.length > MAX) {   // runaway sentence — cut at a space
                    const sp = buf.lastIndexOf(' ', MAX);
                    cut = sp > min ? sp + 1 : MAX;
                } else break;
            }
            emit(buf.slice(0, cut));
            buf = buf.slice(cut);
        }
        if (final && buf.trim()) { emit(buf); buf = ''; }
    }

    return {
        push: function (token) { buf += token; drain(false); },
        flush: function () { drain(true); },
        // Has any speech chunk reached the audio queue yet? The delayed ack
        // uses this to skip itself once the real answer is already talking.
        hasEmitted: function () { return emitted; }
    };
}

// ---- STT: mic button ----
const DB_SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let recording = false;
let mediaRecorder = null;
let mediaChunks = [];

function setMicUI(state) {
    if (micBtn) {
        micBtn.classList.remove('mic-recording', 'mic-busy');
        if (state === 'recording') micBtn.classList.add('mic-recording');
        else if (state === 'busy') micBtn.classList.add('mic-busy');
    }
    // Mirror the recorder state onto the speaking-mode ring. Recorder 'idle'
    // with nothing else in flight means an empty/silent recording — in
    // hands-free mode that just re-arms the mic (the "always running" loop);
    // a real transcript flows into sendMessage → 'thinking' instead.
    if (voiceMode) {
        if (state === 'recording') setSpeakState('listening');
        else if (state === 'busy') setSpeakState('thinking');
        else if (state === 'idle' && !sendBtn.disabled && !speech.isActive()) {
            setSpeakState('idle');
            scheduleHandsFreeResume(400);
        }
    }
}

function updateMicVisibility() {
    // Mic works via browser STT, or via the backend Scribe proxy — hide the
    // mic and the speaking-mode toggle only when NEITHER is available.
    const canTalk = (DB_SR || voiceAvailable) ? '' : 'none';
    if (micBtn) micBtn.style.display = canTalk;
    if (voiceModeBtn) voiceModeBtn.style.display = canTalk;
}

function sendTranscript(text) {
    const t = (text || '').trim();
    if (!t) return;
    input.value = t;
    pendingVoiceInput = true;
    sendMessage();
}

function startBrowserSTT() {
    recognition = new DB_SR();
    recognition.lang = DB_STT_LANG;
    recognition.interimResults = true;   // live transcript in the input box
    recognition.continuous = false;      // stop when the user stops speaking
    let transcript = '';
    let hadError = false;
    recognition.onresult = function (e) {
        let t = '';
        for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
        transcript = t;
        input.value = t;
    };
    recognition.onerror = function (e) {
        // no-speech is NOT an error for the hands-free loop — silence just
        // means nobody talked yet; onend re-arms the mic and keeps waiting.
        if (e.error === 'no-speech') return;
        hadError = true;
        if (e.error === 'not-allowed') {
            voiceLoopBlocked = true;   // never auto-retry against a denied mic
            showLocationToast('Microphone permission denied');
        } else if (e.error !== 'aborted') {
            showLocationToast('Voice input failed');
        }
    };
    recognition.onend = function () {
        recording = false;
        setMicUI('idle');
        if (!hadError) sendTranscript(transcript);
    };
    recording = true;
    setMicUI('recording');
    try { recognition.start(); } catch (e) { recording = false; setMicUI('idle'); }
}

async function startServerSTT() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
        showLocationToast('Voice input not supported in this browser');
        return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { showLocationToast('Microphone permission denied'); return; }
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
        .find(function (m) { return MediaRecorder.isTypeSupported(m); }) || '';
    mediaChunks = [];
    mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    mediaRecorder.ondataavailable = function (e) { if (e.data && e.data.size) mediaChunks.push(e.data); };
    mediaRecorder.onstop = async function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        recording = false;
        const blob = new Blob(mediaChunks, { type: mime || 'audio/webm' });
        mediaChunks = [];
        if (!blob.size) { setMicUI('idle'); return; }
        setMicUI('busy');
        try {
            if (!sessionId || !sessionToken) await startSession();
            const form = new FormData();
            form.append('audio', blob, mime.indexOf('mp4') !== -1 ? 'audio.mp4' : 'audio.webm');
            form.append('session_id', sessionId);
            // no `language` field — Scribe auto-detects (German and English both fine)
            const res = await fetch(DASHBOT_BASE_URL + '/voice/stt', {
                method: 'POST',
                headers: sessionToken ? { 'X-Session-Token': sessionToken } : {},
                body: form
            });
            setMicUI('idle');
            if (!res.ok) { showLocationToast('Transcription failed'); return; }
            const data = await res.json();
            sendTranscript(data.text);
        } catch (e) {
            setMicUI('idle');
            showLocationToast('Transcription failed');
        }
    };
    recording = true;
    setMicUI('recording');
    mediaRecorder.start();
}

function toggleMic() {
    if (recording) {
        try { if (recognition) recognition.stop(); } catch (e) {}
        try { if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop(); } catch (e) {}
        return;
    }
    speech.stopAll();   // barge-in: stop talking when the user starts
    if (DB_SR) startBrowserSTT();
    else startServerSTT();
}

// ---- Speaking mode: hands-free, ChatGPT-style ("just talking") ----
// The chat panel slides away and ONLY the avatar remains, in the screen
// corner with its state ring — no window, no box; the page/map stays fully
// visible. No subtitles. The mic runs CONTINUOUSLY: browser STT
// auto-endpoints on silence → the transcript is sent → the answer is
// spoken → the mic re-arms itself. The map keeps receiving pins/routes,
// and the hidden chat DOM keeps the transcript.
let voiceMode = false;          // speaking mode active
let voiceLoopBlocked = false;   // mic permission denied — stop auto-retrying
let handsFreeTimer = null;

function setSpeakState(state) {
    if (!speakModeEl) return;
    speakModeEl.classList.remove('state-idle', 'state-listening', 'state-thinking', 'state-speaking');
    speakModeEl.classList.add('state-' + state);
}

// Re-arm the mic shortly — the heart of hands-free. The delay keeps the mic
// from catching the tail of the bot's own audio, and collapses the many
// resume triggers (recorder ended silent, queue drained, answer finished)
// into one pending restart.
function scheduleHandsFreeResume(delayMs) {
    if (!voiceMode) return;
    if (handsFreeTimer) clearTimeout(handsFreeTimer);
    handsFreeTimer = setTimeout(function () {
        handsFreeTimer = null;
        handsFreeListen();
    }, delayMs || 500);
}

function handsFreeListen() {
    if (!voiceMode || recording || sendBtn.disabled || speech.isActive()) return;
    if (voiceLoopBlocked) { setSpeakState('idle'); return; }   // tap to retry
    if (DB_SR) startBrowserSTT();
    else setSpeakState('idle');   // no Web Speech API: tap-to-talk via server STT
}

function enterSpeakMode() {
    if (voiceMode) return;
    voiceMode = true;
    voiceLoopBlocked = false;
    if (!voiceRepliesOn) toggleSpeaker();     // speaking mode implies hearing replies
    isChatOpen = true;
    avatar.classList.add('hidden');
    panel.classList.add('open', 'speak-hidden');   // slide the chat away, keep it warm
    speakModeEl.classList.add('open');
    setSpeakState('idle');
    if (DB_SR) handsFreeListen();             // user gesture → permission prompt → loop
    else toggleMic();                         // one-shot server STT fallback
}

function exitSpeakMode() {
    if (handsFreeTimer) { clearTimeout(handsFreeTimer); handsFreeTimer = null; }
    if (recording) toggleMic();               // stop an in-flight recording
    speech.stopAll();
    voiceMode = false;
    speakModeEl.classList.remove('open');
    panel.classList.remove('speak-hidden');   // chat slides back in, transcript intact
    setSpeakState('idle');
    scrollToBottom();
    input.focus();
}

// Tap the avatar: interrupt the answer and talk (or stop the current
// recording). While the agent is still THINKING there is nothing to
// interrupt yet — ignore taps instead of double-sending.
function speakStageTap() {
    voiceLoopBlocked = false;                 // a tap is a fresh user gesture
    if (recording) { toggleMic(); return; }
    if (sendBtn.disabled && !speech.isActive()) return;
    speech.stopAll();
    if (DB_SR) handsFreeListen();
    else toggleMic();
}

// Speaking / idle transitions come from the audio queue itself: 'speak'
// fires as each sentence starts, 'idle' when the queue drains — back to
// thinking if the answer is still streaming (sendBtn stays disabled until
// setLoading(false)), else re-arm the mic and keep the conversation going.
speech.setActivityListener(function (kind) {
    if (!voiceMode) return;
    if (kind === 'speak') setSpeakState('speaking');
    else if (kind === 'idle') {
        if (sendBtn.disabled) setSpeakState('thinking');
        else { setSpeakState('idle'); scheduleHandsFreeResume(600); }
    }
});

if (micBtn) micBtn.addEventListener('click', enterSpeakMode);
if (voiceModeBtn) voiceModeBtn.addEventListener('click', enterSpeakMode);
if (speakExit) speakExit.addEventListener('click', exitSpeakMode);
if (speakStage) {
    speakStage.addEventListener('click', speakStageTap);
    speakStage.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); speakStageTap(); }
    });
}
updateMicVisibility();

// ---- Speaker (voice replies) toggle ----
const DB_SPK_ON_SVG  = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';
const DB_SPK_OFF_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';

function updateSpeakerUI() {
    if (!speakerBtn) return;
    speakerBtn.innerHTML = voiceRepliesOn ? DB_SPK_ON_SVG : DB_SPK_OFF_SVG;
    speakerBtn.classList.toggle('voice-muted', !voiceRepliesOn);
    speakerBtn.title = voiceRepliesOn ? 'Voice replies: on' : 'Voice replies: off';
}

function toggleSpeaker() {
    voiceRepliesOn = !voiceRepliesOn;
    if (!voiceRepliesOn) speech.stopAll();
    try { localStorage.setItem('dashbot-voice', voiceRepliesOn ? 'on' : 'off'); } catch (e) {}
    updateSpeakerUI();
}

if (speakerBtn) speakerBtn.addEventListener('click', toggleSpeaker);
updateSpeakerUI();

// ---- Send ----
input.addEventListener('keypress', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
sendBtn.addEventListener('click', sendMessage);

async function sendMessage() {
    const msg = input.value.trim();
    if (!msg) return;

    if (!isChatOpen) openChat();

    // Voice: speak this reply if the question came in by voice (or the host
    // page opted into speaking everything) and the speaker isn't muted.
    const wasVoice = pendingVoiceInput;
    pendingVoiceInput = false;
    const speakReply = voiceRepliesOn && (wasVoice || SPEAK_ALL_REPLIES || voiceMode);
    const speechChunker = speakReply ? makeSpeechChunker() : null;
    const ACK_GRACE_MS = 1200;
    const FILLER_INTERVAL_MS = 4000;
    const MAX_FILLERS = 2;
    let ackTimer = null;      // whichever stage is pending: ack or next filler
    let fillerCount = 0;
    function cancelAck() {
        if (ackTimer) { clearTimeout(ackTimer); ackTimer = null; }
    }
    function scheduleFiller() {
        ackTimer = setTimeout(function () {
            ackTimer = null;
            if (speechChunker && speechChunker.hasEmitted()) return;
            if (fillerCount >= MAX_FILLERS) return;
            fillerCount++;
            const filler = pickFillerPhrase();
            speech.enqueue(fetchTTS(filler, ''), filler);
            scheduleFiller();   // keep humming until the answer shows up
        }, FILLER_INTERVAL_MS);
    }
    speech.stopAll();   // a new question always interrupts the previous answer

    addMessage(msg, true);
    clearMapOverlay();   // wipe the previous answer's pins/routes from the map
    input.value = '';
    setLoading(true);
    showTyping();
    // Speaking mode: the question was heard — now we're working on it.
    if (voiceMode) setSpeakState('thinking');

    try {
        // Make sure we have a session before sending; mint one if the initial
        // startSession() hasn't landed yet (or previously failed).
        if (!sessionId || !sessionToken) {
            await startSession();
        }

        // Rule-based acknowledgment, ARMED on a short grace timer instead of
        // played instantly. Its only job is masking TOOL-CALL latency: if the
        // first answer sentence reaches the audio queue within the grace
        // window (greetings, small talk, quick follow-ups — no tools), the
        // ack silently skips and the real answer just speaks. Tool-bound
        // questions still get the ack, masking the remaining seconds. Small
        // talk ('none') never gets one at all.
        if (speakReply) {
            const ackKind = classifyAck(msg);
            if (ackKind !== 'none') {
                const ack = pickAckPhrase(ackKind);
                ackTimer = setTimeout(function () {
                    ackTimer = null;
                    if (speechChunker && speechChunker.hasEmitted()) return;
                    pinTypingText(ack);
                    speech.enqueue(fetchTTS(ack, ''), ack);
                    scheduleFiller();   // adaptive hums while the agent works
                }, ACK_GRACE_MS);
            }
        }

        function postChat() {
            return fetch(DASHBOT_BASE_URL + '/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...(sessionToken ? { 'X-Session-Token': sessionToken } : {})
                },
                body: JSON.stringify({
                    message: msg,
                    session_id: sessionId || undefined,
                    stream: true,
                    conversational: true,
                    user_location: userLocation,
                    location_status: locationStatus,
                    // Hands-free speaking mode: the server injects the
                    // spoken-conversation style (short, natural, v3 audio tags).
                    voice_mode: voiceMode
                })
            });
        }

        let res = await postChat();

        // The server expires idle sessions (and loses all sessions when it
        // restarts). When that happens our stored session_id/token is stale and
        // the server replies 404 (unknown session) or 401 (token rejected) —
        // INSTANTLY, before any work. Transparently mint a fresh session and
        // retry once, so the user never sees an error or has to reload the page.
        if (res.status === 404 || res.status === 401) {
            const ok = await startSession();
            if (ok) res = await postChat();
        }

        if (!res.ok) throw new Error('HTTP ' + res.status);

        // JSON (non-streaming) fast path for the LangGraph backend
        const ctype = res.headers.get('content-type') || '';
        if (ctype.includes('application/json')) {
            const data = await res.json();
            cancelAck();
            hideTyping();
            const s = addStreamingMessage();
            s.outerBubble.classList.add('is-streaming');
            const fullText = data.text || 'Sorry, I could not generate a response.';
            s.bubble.innerHTML = formatBotMessage(fullText);
            s.bubble.appendChild(s.time);
            s.outerBubble.classList.remove('is-streaming');
            renderSuggestions(s.msg, buildSuggestions([]));
            if (speechChunker && data.text) {
                speechChunker.push(data.text);
                speechChunker.flush();
            }
            scrollToBottom();
            setLoading(false);
            return;
        }

        var bubble = null;
        var outerBubble = null;
        var time = null;
        var cardsContainer = null;
        var msgDiv = null;
        var botAvatar = null;
        let fullText = '';
        var firstToken = true;
        var pendingCards = [];

        function ensureBubble() {
            if (!firstToken) return;
            hideTyping();
            var s = addStreamingMessage();
            bubble = s.bubble;
            outerBubble = s.outerBubble;
            time = s.time;
            cardsContainer = s.cardsContainer;
            msgDiv = s.msg;
            botAvatar = s.botAvatar;
            outerBubble.classList.add('is-streaming');
            if (botAvatar) botAvatar.classList.add('db-speaking');
            firstToken = false;
        }

        function flushCards() {
            for (var i = 0; i < pendingCards.length; i++) {
                renderCard(cardsContainer, pendingCards[i]);
            }
            pendingCards.length = 0;
        }

        // ---- Typewriter pacing ----
        // gpt tokens arrive faster than the eye can follow (a whole answer in
        // ~0.2 s), which reads as the message "popping in". Reveal the text at
        // reading speed instead, accelerating with backlog so long answers
        // still catch up, and finish the bubble only when the reveal lands.
        var displayedLen = 0;
        var typerActive = false;
        var streamDone = false;
        var finalized = false;
        var suggList = null;

        function finalizeBubble() {
            if (finalized) return;
            finalized = true;
            cancelAck();
            if (speechChunker) speechChunker.flush();
            if (bubble) {
                bubble.innerHTML = formatBotMessage(fullText);
                bubble.appendChild(time);
            }
            if (suggList === null) suggList = buildSuggestions(pendingCards);
            flushCards();
            if (outerBubble) outerBubble.classList.remove('is-streaming');
            if (botAvatar) botAvatar.classList.remove('db-speaking');
            if (msgDiv) renderSuggestions(msgDiv, suggList);
            scrollToBottom();
        }

        function runTyper() {
            if (typerActive) return;
            typerActive = true;
            // setTimeout, NOT requestAnimationFrame: rAF freezes in hidden/
            // background tabs, which would stall the reveal AND the finalize
            // step (cards, suggestions) until the tab is refocused.
            function step() {
                var target = fullText.length;
                // Hidden tab → nothing to animate for anyway; land it all.
                if (document.hidden) displayedLen = target;
                if (displayedLen < target) {
                    var backlog = target - displayedLen;
                    // ~2 chars/tick at a calm pace; proportionally faster the
                    // further behind we are (and much faster once the stream
                    // has ended — no reason to dawdle on a finished answer).
                    var speed = streamDone ? Math.max(8, backlog / 6)
                                           : Math.max(2, backlog / 24);
                    displayedLen = Math.min(target, displayedLen + Math.ceil(speed));
                    if (bubble) {
                        bubble.innerHTML = formatStreaming(fullText.slice(0, displayedLen));
                        scrollToBottom();
                    }
                    setTimeout(step, 28);
                } else if (streamDone) {
                    typerActive = false;
                    finalizeBubble();
                } else {
                    typerActive = false; // idle — the next token restarts us
                }
            }
            setTimeout(step, 0);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const json = line.slice(6).trim();
                if (!json) continue;
                try {
                    const ev = JSON.parse(json);
                    if (ev.type === 'card') {
                        // Buffer until text finishes — cards render below the message
                        pendingCards.push(ev.card);
                    } else if (ev.type === 'token') {
                        ensureBubble();
                        fullText += ev.content;
                        // Voice gets the raw feed immediately — only the
                        // VISUAL reveal is paced.
                        if (speechChunker) speechChunker.push(ev.content);
                        runTyper();
                    } else if (ev.type === 'done') {
                        ensureBubble();
                        cancelAck();   // answer landed — a late ack would be noise
                        // Build follow-ups from the cards BEFORE flushCards clears them
                        suggList = buildSuggestions(pendingCards);
                        streamDone = true;
                        runTyper();    // finalizes once the reveal catches up
                    } else if (ev.type === 'error') {
                        ensureBubble();
                        fullText += ' [Error: ' + ev.content + ']';
                        streamDone = true;
                        displayedLen = fullText.length;  // no typewriter on errors
                        finalizeBubble();
                    }
                } catch (_) {}
            }
        }

        hideTyping();
        if (!fullText) {
            cancelAck();
            if (speechChunker) speechChunker.flush();
            if (botAvatar) botAvatar.classList.remove('db-speaking');
            if (!bubble) {
                var sf = addStreamingMessage();
                bubble = sf.bubble; time = sf.time;
                cardsContainer = sf.cardsContainer;
                outerBubble = sf.outerBubble;
                msgDiv = sf.msg;
            }
            bubble.textContent = 'Sorry, I could not generate a response.';
            bubble.appendChild(time);
            if (outerBubble) outerBubble.classList.remove('is-streaming');
            if (pendingCards.length && cardsContainer) flushCards();
        } else {
            // Stream ended (with or without an explicit `done`): let the
            // typewriter finish the reveal — finalizeBubble ties off the
            // text, cards, suggestions, ack and avatar state.
            streamDone = true;
            runTyper();
        }

        setLoading(false);

    } catch (err) {
        console.error('Dashbot Error:', err);
        cancelAck();
        hideTyping();
        setLoading(false);
        addMessage(
            err.message.includes('Failed to fetch')
                ? 'Cannot connect to Dashbot. Is the backend running?'
                : 'Sorry, something went wrong. Please try again.',
            false
        );
    }

    if (!voiceMode) input.focus();
}

console.log('Dashbot widget initialized — base URL:', DASHBOT_BASE_URL);

}

