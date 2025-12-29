/* #/static/js/room.js */

/* --- Глобальные переменные --- */
let socket;
let hls;
let player; // HTMLVideoElement
let myUserId = null;
let myUsername = 'Me';
let myAvatar = '';
let isOwner = false;
let isPrivate = true;
let isAllowedGuestControl = false;
let currentVideoId = null;
let isDraggingScrubber = false; // Флаг: перетягивает ли пользователь ползунок прямо сейчас

// UI Elements References
const ui = {
    wrapper: null,
    controls: null,
    playBtn: null,
    muteBtn: null,
    volSlider: null,
    timeCurrent: null,
    timeDuration: null,
    timeDelta: null,
    timeline: null,
    scrub: null,
    progress: null,
    buffer: null,
    markers: null,
    fullscreenBtn: null,
    pipBtn: null,
    toastContainer: null
};

// State
let lastSeenMap = {};
let processingMap = {};
let peersState = {}; // Состояние других участников для маркеров
let ignoreSyncEvents = false;
let isUserIdle = false;
let idleTimer = null;
let forcePreload = false; // Состояние галочки предзагрузки
let pollInterval = null;
let isPlayPending = false; // Защита от AbortError

/* --- INITIALIZATION --- */

document.addEventListener('DOMContentLoaded', async () => {
    console.log("🚀 Room JS Initializing...");
    player = document.getElementById('video-player');
    initUIReferences();
    setupCustomPlayer();
    await loadUserInfo();
    await loadRoomData();
    initSocket();

    setInterval(sendHeartbeat, 2000);
    setInterval(cleanupViewers, 5000);
    setInterval(updateTTLCounters, 60000); // Таймеры удаления
    setInterval(() => {
        loadRoomData(); // Периодическое обновление списка
    }, 5 * 60 * 1000);
});

function initUIReferences() {
    ui.wrapper = document.getElementById('player-wrapper');
    ui.controls = document.getElementById('controls-layer');
    ui.playBtn = document.getElementById('btn-play');
    ui.muteBtn = document.getElementById('btn-mute');
    ui.volSlider = document.getElementById('volume-slider');
    ui.timeCurrent = document.getElementById('time-current');
    ui.timeDuration = document.getElementById('time-duration');
    ui.timeDelta = document.getElementById('time-delta');
    ui.timeline = document.getElementById('timeline-area');
    ui.scrub = document.getElementById('timeline-scrub');
    ui.progress = document.getElementById('progress-current');
    ui.buffer = document.getElementById('progress-buffer');
    ui.markers = document.getElementById('markers-layer');
    ui.fullscreenBtn = document.getElementById('btn-fullscreen');
    ui.pipBtn = document.getElementById('btn-pip');
    ui.toastContainer = document.getElementById('player-toasts');

    // Настройка чекбокса предзагрузки
    const chkPreload = document.getElementById('chk-preload');
    if (chkPreload) {
        chkPreload.addEventListener('change', (e) => {
            forcePreload = e.target.checked;
            console.log("Preload setting changed:", forcePreload);
            // Если видео уже загружено, перезагружаем источник чтобы применить настройки
            if (currentVideoId) {
                const activeItem = document.querySelector(`.video-item[data-id="${currentVideoId}"]`);
                if (activeItem) {
                    console.log("Reloading video to apply preload settings...");
                    // Мы не вызываем click(), чтобы не слать сокет всем, а просто перезагружаем локально HLS
                    // Но проще всего подождать смены видео.
                }
            }
        });
    }
}

async function loadUserInfo() {
    try {
        const res = await fetch('/api/me');
        if (res.ok) {
            const user = await res.json();
            myUserId = user.id;
            myUsername = user.username;
            myAvatar = user.avatar_url;
        }
    } catch(e) { console.error(e); }
}

async function loadRoomData() {
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}`);
        if (res.status === 404) {
             document.body.innerHTML = "<h1>Комната не найдена</h1>";
             return;
        }
        const data = await res.json();

        // Проверка доступа
        if (!data.has_access) {
            document.getElementById('access-overlay').style.display = 'flex';
            document.getElementById('room-name').textContent = data.room.name;
            return;
        }

        document.getElementById('access-overlay').style.display = 'none';
        document.getElementById('room-name').textContent = data.room.name;

        isOwner = data.is_owner;
        isPrivate = data.is_private;
        isAllowedGuestControl = data.allow_guest_control;

        console.log(`[Room Data] Loaded. Owner: ${isOwner}, Private: ${isPrivate}`);

        // Показываем элементы управления владельца
        if (isOwner || isAllowedGuestControl) {
            const oc = document.getElementById('owner-controls');
            if(oc) oc.style.display = 'block';
            const bs = document.getElementById('btn-settings');
            if(bs) bs.style.display = 'block';
        }

        renderPlaylist(data.videos);

        // Авто-обновление статусов обработки (polling)
        const hasProcessing = data.videos.some(v => v.status === 'processing' || v.status === 'uploading');
        if (hasProcessing) {
            if (!pollInterval) pollInterval = setInterval(loadRoomData, 3000);
        } else {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        }

        // Если открыта панель настроек, обновляем инпуты (чтобы видеть актуальные данные)
        const settingsArea = document.getElementById('settings-area');
        if (settingsArea && settingsArea.style.display === 'block') {
            loadSettingsInputs(data.room);
        }

    } catch(e) { console.error(e); }
}


/* --- Timeline Scrubbing Logic --- */

function startScrubDrag(e) {
    isDraggingScrubber = true;

    // Сразу обновляем визуально в точку клика
    handleScrubMove(e);

    // Вешаем слушатели на document, чтобы можно было уводить мышь за пределы плеера
    document.addEventListener('mousemove', handleScrubMove);
    document.addEventListener('mouseup', stopScrubDrag);
}

function handleScrubMove(e) {
    if (!isDraggingScrubber) return;

    const rect = ui.timeline.getBoundingClientRect();
    // Вычисляем позицию мыши относительно таймлайна (0..1)
    let pos = (e.clientX - rect.left) / rect.width;

    // Ограничиваем в пределах [0, 1]
    pos = Math.max(0, Math.min(1, pos));

    // Визуальное обновление (без перемотки видео!)
    const pct = pos * 100;
    ui.progress.style.width = `${pct}%`;
    ui.scrub.style.left = `${pct}%`;

    // Обновляем таймер времени, чтобы видеть, куда мотаем
    if (isFinite(player.duration)) {
        const targetTime = pos * player.duration;
        ui.timeCurrent.textContent = formatDuration(targetTime);
    }
}

function stopScrubDrag(e) {
    if (!isDraggingScrubber) return;
    isDraggingScrubber = false;

    // Убираем глобальные слушатели
    document.removeEventListener('mousemove', handleScrubMove);
    document.removeEventListener('mouseup', stopScrubDrag);

    // Финальный расчет времени
    const rect = ui.timeline.getBoundingClientRect();
    let pos = (e.clientX - rect.left) / rect.width;
    pos = Math.max(0, Math.min(1, pos));

    if (isFinite(player.duration)) {
        const targetTime = pos * player.duration;
        player.currentTime = targetTime;

        // Отправляем ивент на сервер только СЕЙЧАС
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', {
                room_uuid: ROOM_UUID,
                action: 'seek',
                timestamp: targetTime
            });
        }
    }
}


/* --- CUSTOM PLAYER LOGIC --- */

function setupCustomPlayer() {
    // 1. Play/Pause
    ui.playBtn.addEventListener('click', togglePlay);
    ui.wrapper.addEventListener('click', (e) => {
        if (e.target === player || e.target === ui.wrapper) togglePlay();
    });

    // 2. Volume
    ui.muteBtn.addEventListener('click', () => {
        player.muted = !player.muted;
        updateVolumeUI();
    });
    ui.volSlider.addEventListener('input', (e) => {
        player.volume = e.target.value;
        player.muted = false;
        updateVolumeUI();
    });

    // 3. Fullscreen
    ui.fullscreenBtn.addEventListener('click', () => {
        if (!document.fullscreenElement) {
            ui.wrapper.requestFullscreen().catch(err => console.log(err));
        } else {
            document.exitFullscreen();
        }
    });

    // 4. PiP
    if (document.pictureInPictureEnabled) {
        ui.pipBtn.addEventListener('click', async () => {
            if (document.pictureInPictureElement) {
                await document.exitPictureInPicture();
            } else {
                await player.requestPictureInPicture();
            }
        });
    } else {
        ui.pipBtn.style.display = 'none';
    }

    // 5. Timeline Drag Logic
    ui.timeline.addEventListener('mousedown', startScrubDrag);

    // 6. Idle Detection
    ui.wrapper.addEventListener('mousemove', resetIdleTimer);
    ui.wrapper.addEventListener('click', resetIdleTimer);

    // 7. Video Events
    player.addEventListener('timeupdate', updateTimelineUI);
    player.addEventListener('play', updatePlayIcon);
    player.addEventListener('pause', updatePlayIcon);
    player.addEventListener('volumechange', updateVolumeUI);
    player.addEventListener('progress', updateBufferUI);
}

// Обертка для Play, чтобы избежать AbortError при частых переключениях
async function safePlay() {
    if (isPlayPending) return;
    isPlayPending = true;
    try {
        await player.play();
    } catch (e) {
        //never mind
    } finally {
        isPlayPending = false;
    }
}

function togglePlay() {
    if (player.paused) {
        safePlay();
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', { room_uuid: ROOM_UUID, action: 'play', timestamp: player.currentTime });
        }
    } else {
        player.pause();
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', { room_uuid: ROOM_UUID, action: 'pause', timestamp: player.currentTime });
        }
    }
}

function updatePlayIcon() {
    const playSvg = ui.playBtn.querySelector('.icon-play');
    const pauseSvg = ui.playBtn.querySelector('.icon-pause');
    if (player.paused) {
        playSvg.style.display = 'block';
        pauseSvg.style.display = 'none';
    } else {
        playSvg.style.display = 'none';
        pauseSvg.style.display = 'block';
        resetIdleTimer(); // Показать контролы при смене статуса
    }
}

function updateVolumeUI() {
    ui.volSlider.value = player.volume;
    ui.muteBtn.style.opacity = player.muted ? '0.5' : '1';
}

function updateTimelineUI() {
    if (!isFinite(player.duration)) return;
    if (isDraggingScrubber) return;
    const pct = (player.currentTime / player.duration) * 100;
    ui.progress.style.width = `${pct}%`;
    if (ui.scrub) {
        ui.scrub.style.left = `${pct}%`;
    }
    ui.timeCurrent.textContent = formatDuration(player.currentTime);
    ui.timeDuration.textContent = formatDuration(player.duration);
}

function updateBufferUI() {
    if (!isFinite(player.duration) || player.buffered.length === 0) return;
    let bufferEnd = 0;
    for (let i = 0; i < player.buffered.length; i++) {
        if (player.buffered.start(i) <= player.currentTime && player.buffered.end(i) >= player.currentTime) {
            bufferEnd = player.buffered.end(i);
            break;
        }
    }
    const pct = (bufferEnd / player.duration) * 100;
    ui.buffer.style.width = `${pct}%`;
}

function resetIdleTimer() {
    ui.wrapper.classList.remove('user-idle');
    clearTimeout(idleTimer);
    if (!player.paused) {
        idleTimer = setTimeout(() => {
            ui.wrapper.classList.add('user-idle');
        }, 3000);
    }
}

/* --- SETTINGS AREA LOGIC --- */

// Делаем функцию глобальной, чтобы она была доступна из HTML
window.toggleSettings = function() {
    const area = document.getElementById('settings-area');
    if (area.style.display === 'none') {
        area.style.display = 'block';
        loadSettingsData(); // Подгружаем актуальные данные
        area.scrollIntoView({ behavior: 'smooth' });
    } else {
        area.style.display = 'none';
    }
}

window.switchSettingsTab = function(tabName) {
    document.querySelectorAll('.settings-col').forEach(el => el.classList.remove('active'));
    document.getElementById(`col-${tabName}`).classList.add('active');
    document.querySelectorAll('.s-tab-btn').forEach(el => el.classList.remove('active'));
    // event мб не передан, если вызов программный, но из HTML он есть
    if (window.event) window.event.target.classList.add('active');
}

function loadSettingsInputs(room) {
    const nameInput = document.getElementById('set-room-name');
    if (nameInput && document.activeElement !== nameInput) nameInput.value = room.name;

    const colorInput = document.getElementById('set-room-color');
    if (colorInput) colorInput.value = room.header_color;

    const privateCheck = document.getElementById('set-is-private');
    if (privateCheck) privateCheck.checked = room.is_private;

    const guestCheck = document.getElementById('set-guest-control');
    if (guestCheck) guestCheck.checked = room.allow_guest_control;
}

async function loadSettingsData() {
    loadBans();
    // Грузим список доступа только если комната приватная
    if (isPrivate) {
        loadAccessList();
        const list = document.getElementById('guest-access-list');
        if(list) list.style.display = 'flex';
        const warning = document.getElementById('public-room-warning');
        if(warning) warning.style.display = 'none';
    } else {
        const list = document.getElementById('guest-access-list');
        if(list) list.style.display = 'none';
        const warning = document.getElementById('public-room-warning');
        if(warning) warning.style.display = 'block';
    }
}

async function openSettingsModal() {
    document.getElementById('settings-modal').style.display = 'flex';

    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}`);
        const data = await res.json();
        const r = data.room;

        document.getElementById('set-room-name').value = r.name;
        document.getElementById('set-room-color').value = r.header_color;
        document.getElementById('set-is-private').checked = r.is_private;
        document.getElementById('set-guest-control').checked = r.allow_guest_control;

        showSettingsTab('general');
    } catch(e) { console.error(e); }
}

window.saveRoomSettings = async function() {
    const data = {
        name: document.getElementById('set-room-name').value,
        header_color: document.getElementById('set-room-color').value,
        is_private: document.getElementById('set-is-private').checked,
        allow_guest_control: document.getElementById('set-guest-control').checked
    };
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (res.ok) {
            showPlayerToast('Настройки сохранены');
            loadRoomData(); // Обновит UI и переменные isPrivate и т.д.
        } else {
            alert('Ошибка сохранения');
        }
    } catch(e) { alert('Ошибка сети'); }
}

async function loadBans() {
    const list = document.getElementById('banned-list');
    if(!list) return;
    list.innerHTML = 'Загрузка...';
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/bans`);
        const data = await res.json();
        if (data.bans.length === 0) {
            list.innerHTML = '<div style="padding:10px;text-align:center;color:#777">Список пуст</div>';
            return;
        }
        list.innerHTML = data.bans.map(u => `
            <div class="video-item">
                <div style="display:flex;align-items:center;gap:10px;flex:1">
                    <img src="${u.avatar_url}" style="width:20px;border-radius:50%"> ${u.username}
                </div>
                <button class="btn-icon-action" style="color:red" onclick="unbanUser(${u.user_id})">✕</button>
            </div>
        `).join('');
    } catch(e) { list.innerHTML = 'Ошибка'; }
}

async function loadAccessList() {
    const list = document.getElementById('guest-access-list');
    if(!list) return;
    list.innerHTML = 'Загрузка...';
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/access`);
        const data = await res.json();
        if (data.users.length === 0) {
            list.innerHTML = '<div style="padding:10px;text-align:center;color:#777">Список пуст</div>';
            return;
        }
        list.innerHTML = data.users.map(u => `
            <div class="video-item">
                <div style="display:flex;align-items:center;gap:10px;flex:1">
                    <img src="${u.avatar_url}" style="width:20px;border-radius:50%"> ${u.username}
                </div>
                <button class="btn-icon-action" style="color:orange" onclick="revokeAccess(${u.id}, '${u.username}')">✕</button>
            </div>
        `).join('');
    } catch(e) { list.innerHTML = 'Ошибка'; }
}

window.unbanUser = async function(userId) {
    await fetch(`/api/rooms/${ROOM_UUID}/bans/${userId}`, { method: 'DELETE' });
    loadBans();
}
window.revokeAccess = async function(userId, username) {
    if(!confirm(`Закрыть доступ для ${username}?`)) return;
    await fetch(`/api/rooms/${ROOM_UUID}/access/${userId}`, { method: 'DELETE' });
    loadAccessList();
}

/* --- SOCKET LOGIC --- */

function initSocket() {
    socket = io();

    socket.on('connect', () => {
        console.log("Connected to socket");
        socket.emit('join', { room_uuid: ROOM_UUID });
    });

    socket.on('new_message', (data) => {
        appendChatMessage(data);
        if (data.is_system) {
            // Если системное сообщение и вкладка плейлиста активна (чат не виден), показываем тост
            const playlistTab = document.querySelector('button[onclick="switchSidebar(\'playlist\')"]');
            if (playlistTab && playlistTab.classList.contains('active')) {
                showPlayerToast(data.text);
            }
        }
    });

    socket.on('sync_event', (data) => {
        ignoreSyncEvents = true;
        // Корректируем время, только если разница существенная
        if (Math.abs(player.currentTime - data.timestamp) > 0.5) {
            player.currentTime = data.timestamp;
        }
        if (data.action === 'play') {
            safePlay();
        } else if (data.action === 'pause') {
            player.pause();
        }
        // Разблокируем отправку событий через полсекунды
        setTimeout(() => { ignoreSyncEvents = false; }, 500);
    });

    socket.on('status_update', (data) => {
        updateViewerList(data);
        if (data.sid !== socket.id) {
             updateTimelineMarkers(data);
             handleSoftSync(data);
        }
        updateDeltaDisplay(data.server_timestamp);
    });

    socket.on('load_video', (data) => {
        console.log("Socket: Load Video", data);
        currentVideoId = data.video_id;
        loadSource(data.url);
        // Подсвечиваем активное видео
        document.querySelectorAll('.video-item').forEach(el => el.classList.remove('active'));
        const activeItem = document.querySelector(`.video-item[data-id="${data.video_id}"]`);
        if (activeItem) activeItem.classList.add('active');
    });

    socket.on('restore_state', (data) => {
        if (data.video) {
            currentVideoId = data.video.id;
            loadSource(data.video.url, data.timestamp, true); // true = start paused
            if (!data.paused) {
                safePlay();
            }
        }
    });

    socket.on('playlist_refresh', () => loadRoomData());
    socket.on('incoming_knock', (data) => showKnockToast(data));
    socket.on('access_granted', () => location.reload());
    socket.on('access_denied', (data) => {
        document.getElementById('access-status').textContent = data.reason;
        const btn = document.querySelector('#access-buttons .btn-primary');
        if(btn) { btn.disabled = false; btn.textContent = "Постучаться"; }
    });

    socket.on('processing_progress', (data) => {
        updateProcessingProgress(data.video_id, data.percent);
    });
}

/* --- MARKERS & SYNC VISUALIZATION --- */

function updateTimelineMarkers(data) {
    peersState[data.sid] = data;
    renderMarkers();
}

function renderMarkers() {
    if (!isFinite(player.duration) || player.duration <= 0) return;
    ui.markers.innerHTML = '';

    const users = Object.values(peersState);
    if (users.length === 0) return;

    // Сортируем по времени для кластеризации
    users.sort((a, b) => a.timestamp - b.timestamp);

    const clusters = [];
    if (users.length > 0) {
        let currentCluster = { users: [users[0]], timestamp: users[0].timestamp };
        for (let i = 1; i < users.length; i++) {
            const u = users[i];
            const diffPercent = Math.abs(u.timestamp - currentCluster.timestamp) / player.duration * 100;
            // Если разница меньше 2%, объединяем в кластер
            if (diffPercent < 2.0) {
                currentCluster.users.push(u);
            } else {
                clusters.push(currentCluster);
                currentCluster = { users: [u], timestamp: u.timestamp };
            }
        }
        clusters.push(currentCluster);
    }

    clusters.forEach(cluster => {
        const leftPct = (cluster.timestamp / player.duration) * 100;
        let statusClass = 'status-green';
        const states = cluster.users.map(u => calculateStatus(u));
        // Выбираем "худший" статус для цвета кластера
        if (states.includes('status-red')) statusClass = 'status-red';
        else if (states.includes('status-yellow')) statusClass = 'status-yellow';
        else if (states.includes('status-gray')) statusClass = 'status-gray';

        const marker = document.createElement('div');
        marker.className = `user-marker ${statusClass}`;
        marker.style.left = `${leftPct}%`;

        const names = cluster.users.map(u => u.username).join(', ');
        const mainUser = cluster.users[0];

        // Расчет дельты
        let deltaHtml = '';
        if (mainUser.server_timestamp) {
            const delta = (mainUser.timestamp - mainUser.server_timestamp).toFixed(1);
            const deltaClass = Math.abs(delta) < 1 ? 'tt-delta good' : 'tt-delta bad';
            deltaHtml = `<span class="${deltaClass}">(${delta > 0 ? '+' : ''}${delta}s)</span>`;
        }

        const tooltip = document.createElement('div');
        tooltip.className = 'marker-tooltip';
        tooltip.innerHTML = `
            <div class="tt-names">${names}</div>
            <div class="tt-info">${formatDuration(mainUser.timestamp)} ${deltaHtml}</div>
        `;
        marker.appendChild(tooltip);
        ui.markers.appendChild(marker);
    });
}

function calculateStatus(data) {
    if (data.state !== 'playing') return 'status-gray';
    if (!data.server_timestamp) return 'status-green';
    const diff = Math.abs(data.timestamp - data.server_timestamp);
    if (diff < 1.5) return 'status-green';
    if (diff < 4.0) return 'status-yellow';
    return 'status-red';
}

function handleSoftSync(data) {
    // Автокоррекция только если мы не лидер, не владелец и не тыкали кнопки только что
    if (!data.is_leader && !isOwner && !isAllowedGuestControl && !ignoreSyncEvents) {
        // Sync Pause
        if (data.server_paused && !player.paused) {
            player.pause();
        } else if (!data.server_paused && player.paused && player.readyState >= 2) {
            safePlay();
        }
        // Sync Time (Drift > 3s)
        const diff = Math.abs(player.currentTime - data.server_timestamp);
        if (diff > 3.0 && !data.server_paused) {
            console.log("Soft Sync: Jumping to server time");
            player.currentTime = data.server_timestamp;
        }
    }
}

function updateDeltaDisplay(serverTs) {
    if (!serverTs) return;
    const diff = serverTs - player.currentTime;
    const absDiff = Math.abs(diff);
    let text = "", cls = "delta-neutral";

    if (absDiff < 10) {
        text = (diff > 0 ? "+" : "") + diff.toFixed(1);
    } else if (absDiff < 99) {
        text = (diff > 0 ? "+" : "") + Math.round(diff);
    } else {
        text = diff > 0 ? "+∞" : "-∞";
    }
    if (absDiff < 1.0) cls = "delta-positive";
    else if (absDiff > 3.0) cls = "delta-negative";

    ui.timeDelta.textContent = text;
    ui.timeDelta.className = "time-delta " + cls;
}

function sendHeartbeat() {
    if (!player) return;
    let bufferedEnd = 0;
    if (player.buffered.length > 0) {
        for (let i = 0; i < player.buffered.length; i++) {
             if (player.buffered.start(i) <= player.currentTime && player.buffered.end(i) >= player.currentTime) {
                 bufferedEnd = player.buffered.end(i);
                 break;
             }
        }
    }
    socket.emit('heartbeat', {
        room_uuid: ROOM_UUID,
        timestamp: player.currentTime,
        state: player.paused ? 'paused' : 'playing',
        buffered: bufferedEnd
    });
}

/* --- HLS LOGIC --- */

function loadSource(url, startTime=0, startPaused=true) {
    console.log(`[Player] Loading URL: ${url}, StartTime: ${startTime}, StartPaused: ${startPaused}`);

    // 1. Показываем оверлей загрузки
    const overlay = document.getElementById('video-overlay');
    if (overlay) overlay.style.display = 'flex';
    document.getElementById('overlay-text').textContent = "Загрузка...";

    // 2. Сброс UI в начальное состояние
    if (ui.progress) ui.progress.style.width = '0%';
    if (ui.buffer) ui.buffer.style.width = '0%';
    if (ui.scrub) ui.scrub.style.left = '0%'; // Возвращаем кружок в начало
    if (ui.timeCurrent) ui.timeCurrent.textContent = '00:00';
    if (ui.timeDuration) ui.timeDuration.textContent = '00:00';
    isDraggingScrubber = false;

    // 3. Очистка предыдущего HLS инстанса
    if (hls) {
        hls.destroy();
        hls = null;
    }

    // 4. Инициализация HLS или нативного воспроизведения
    if (Hls.isSupported()) {
        const config = {
            autoStartLoad: true,
            startPosition: startTime > 0 ? startTime : -1,
            debug: false,
        };

        if (forcePreload) {
            config.maxBufferLength = 600;
            config.maxMaxBufferLength = 3600;
            config.maxBufferSize = 500 * 1000 * 1000;
        }

        hls = new Hls(config);
        hls.loadSource(url);
        hls.attachMedia(player);

        hls.on(Hls.Events.MANIFEST_PARSED, function() {
            console.log("[HLS] Manifest Parsed");
            if (overlay) overlay.style.display = 'none';
            if (startTime > 0) player.currentTime = startTime;

            if (!startPaused) {
                console.log("[Player] Auto-starting playback...");
                safePlay();
            }
        });

        hls.on(Hls.Events.ERROR, (e, data) => {
            if(data.fatal && data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                hls.recoverMediaError();
            } else {
                console.error("[HLS] Error:", data);
            }
        });

    } else if (player.canPlayType('application/vnd.apple.mpegurl')) {
        // Фоллбек для Safari
        player.src = url;
        player.addEventListener('loadedmetadata', function() {
            if (overlay) overlay.style.display = 'none';
            if (startTime > 0) player.currentTime = startTime;
            if (!startPaused) safePlay();
        }, {once: true});
    } else {
        // Фоллбек для обычных MP4
        player.src = url;
        player.addEventListener('loadedmetadata', function() {
            if (overlay) overlay.style.display = 'none';
            if (startTime > 0) player.currentTime = startTime;
            if (!startPaused) safePlay();
        }, {once: true});
    }
}

/* --- HELPERS --- */

function appendChatMessage(data) {
    const container = document.getElementById('chat-messages');
    if(!container) return;
    const div = document.createElement('div');
    if (data.is_system) {
        div.className = 'system-message';
        div.textContent = data.text;
    } else {
        div.className = 'message';
        div.innerHTML = `<span class="msg-author">${data.username}:</span><span class="msg-text">${data.text}</span>`;
    }
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showPlayerToast(text) {
    const div = document.createElement('div');
    div.className = 'player-toast';
    div.textContent = text;
    ui.toastContainer.appendChild(div);
    setTimeout(() => div.remove(), 4000);
}

function updateViewerList(data) {
    lastSeenMap[data.sid] = Date.now();
    const list = document.getElementById('viewers-list');
    if(!list) return;
    let item = document.getElementById(`viewer-${data.sid}`);
    const avatarSrc = data.avatar || `https://ui-avatars.com/api/?name=${data.username}&background=random`;

    if (!item) {
        item = document.createElement('div');
        item.id = `viewer-${data.sid}`;
        item.className = 'viewer-card status-gray';
        item.setAttribute('data-user-id', data.user_id);
        if (isOwner && data.user_id !== myUserId) {
            item.onclick = (e) => showUserPopover(e, data.user_id, data.username);
        }
        item.innerHTML = `<img src="${avatarSrc}" class="viewer-avatar"><span class="viewer-name">${data.username}</span>`;
        list.appendChild(item);
    }
    item.className = `viewer-card ${calculateStatus(data)}`;
}

function updateProcessingProgress(videoId, percent) {
    processingMap[videoId] = percent;
    const item = document.querySelector(`.video-item[data-id="${videoId}"]`);
    if (item) {
        const statusEl = item.querySelector('.video-status');
        if (statusEl) {
            statusEl.innerHTML = `<span style="color:orange; font-weight:bold;">⏳ Обработка: ${percent}%</span>`;
        }
    }
}

function renderPlaylist(videos) {
    const container = document.getElementById('playlist-container');
    if (!container) return;
    if (videos.length === 0) {
        container.innerHTML = '<div class="empty-state">Нет видео</div>';
        return;
    }
    const activeId = currentVideoId;
    container.innerHTML = videos.map(v => {
        const isReady = v.status === 'ready';
        const itemClass = `video-item ${v.id === activeId ? 'active' : ''} ${!isReady ? 'disabled' : ''}`;

        let statusBadge = '';
        if (v.status === 'processing') statusBadge = '<span style="color:orange">⏳ Processing...</span>';
        else if (v.status === 'uploading') statusBadge = '<span style="color:#3498db">⬆️ Uploading...</span>';
        else if (v.status === 'error') statusBadge = '<span style="color:red">❌ Error</span>';

        // Кнопки действий
        const controls = (isOwner || isAllowedGuestControl) ? `
            <div class="item-actions">
                <button class="btn-icon-action btn-edit" onclick="renameVideo(event, ${v.id}, '${v.title.replace(/'/g, "\\'")}')">✏️</button>
                <button class="btn-icon-action btn-delete" onclick="deleteVideo(event, ${v.id})">×</button>
            </div>` : '';

        return `
        <div class="${itemClass}" data-id="${v.id}" onclick="${isReady ? `changeVideo(${v.id})` : ''}">
            <div class="video-item-info">
                <div class="video-title" title="${v.title}">${v.title}</div>
                <div class="video-status">${statusBadge || formatDuration(v.duration)}</div>
            </div>
            ${controls}
        </div>`;
    }).join('');
    updateTTLCounters();
}

function formatDuration(sec) {
    if (!sec || isNaN(sec)) return '00:00';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const mm = m < 10 ? '0'+m : m;
    const ss = s < 10 ? '0'+s : s;
    if (h > 0) return `${h}:${mm}:${ss}`;
    return `${mm}:${ss}`;
}

// Стандартные функции плеера
window.triggerUpload = function() { document.getElementById('file-upload').click(); }
window.changeVideo = function(id) {
    console.log(`[Click] Video ID: ${id}. Permissions -> Owner: ${isOwner}, GuestAllowed: ${isAllowedGuestControl}`);

    if (isOwner || isAllowedGuestControl) {
        if (socket && socket.connected) {
            console.log("Emitting change_video event...");
            socket.emit('change_video', { room_uuid: ROOM_UUID, video_id: id });
        } else {
            console.error("Socket not connected!");
            showPlayerToast("Нет соединения с сервером");
        }
    } else {
        console.warn("Action denied: You are not the owner.");
        showPlayerToast("Только владелец может менять видео");
    }
}

window.uploadVideo = async function() {
    const input = document.getElementById('file-upload');
    const btn = document.getElementById('video-upload-bt');
    if (!input.files.length) return;
    const file = input.files[0];
    if (file.size > MAX_VIDEO_SIZE) { alert("Файл слишком большой"); return; }

    btn.disabled = true;
    const formData = new FormData();
    formData.append('video', file);
    document.getElementById('upload-progress').style.display = 'block';

    const bar = document.getElementById('progress-fill');
    const text = document.getElementById('upload-percent');
    if(bar) bar.style.width = '0%';

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/rooms/${ROOM_UUID}/upload`);
    xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
            const percent = (e.loaded / e.total) * 100;
            if(bar) bar.style.width = percent + '%';
            if(text) text.textContent = Math.round(percent) + '%';
        }
    };
    xhr.onload = () => {
        btn.disabled = false;
        document.getElementById('upload-progress').style.display = 'none';
        input.value = '';
        if(xhr.status === 200) {
            loadRoomData();
        } else {
            alert('Ошибка загрузки');
        }
    };
    xhr.onerror = () => {
        btn.disabled = false;
        alert('Ошибка сети');
    };
    xhr.send(formData);
}

window.deleteVideo = async function(e, id) {
    e.stopPropagation();
    if(confirm('Удалить видео?')) {
        await fetch(`/api/videos/${id}`, {method:'DELETE'});
        loadRoomData();
    }
}

window.renameVideo = async function(e, id, old) {
    e.stopPropagation();
    const n = prompt('Новое название:', old);
    if(n) {
        await fetch(`/api/videos/${id}`, {
            method:'PUT',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({title:n})
        });
        loadRoomData();
    }
}

function updateTTLCounters() {
    const timers = document.querySelectorAll('.ttl-timer');
    const retentionMs = RETENTION_HOURS * 60 * 60 * 1000;
    const now = new Date().getTime();
    timers.forEach(el => {
        const lastPlayedStr = el.getAttribute('data-last-played');
        if (!lastPlayedStr) return;
        const lastPlayedTs = new Date(lastPlayedStr).getTime();
        const diff = (lastPlayedTs + retentionMs) - now;
        if (diff <= 0) {
            el.innerHTML = '<span style="color:red;font-size:10px;">Удаление...</span>';
        } else {
            const h = Math.floor(diff / (1000*60*60));
            const m = Math.floor((diff % (1000*60*60)) / (1000*60));
            el.innerHTML = `<span style="color:#888;font-size:10px;">♻️ ${h}ч ${m}м</span>`;
        }
    });
}

window.copyLink = async function() {
    const url = `${window.location.href}?ref=${encodeURIComponent(myUsername)}`;
    try {
        await navigator.clipboard.writeText(url);
        showPlayerToast('Ссылка скопирована!');
    } catch (err) {
        prompt('Скопируйте ссылку:', url);
    }
}

window.showUserPopover = function(e, uid, name) {
    e.stopPropagation();
    const popover = document.getElementById('user-popover');
    if(!popover) return;
    document.getElementById('pop-username').textContent = name;

    // Глобальные функции для кнопок поповера
    window.confirmBanUser = () => {
        if(confirm(`Забанить ${name}?`)) {
            fetch(`/api/rooms/${ROOM_UUID}/bans`, {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({user_id: uid})
            });
            popover.style.display='none';
        }
    };
    window.confirmKickUser = () => {
        revokeAccess(uid, name);
        popover.style.display='none';
    };

    const rect = e.currentTarget.getBoundingClientRect();
    popover.style.top = (window.scrollY + rect.bottom + 5) + 'px';
    popover.style.left = (rect.left - 20) + 'px';
    popover.style.display = 'block';

    const closeFn = (ev) => {
        if (!popover.contains(ev.target)) {
            popover.style.display = 'none';
            document.removeEventListener('click', closeFn);
        }
    };
    setTimeout(() => document.addEventListener('click', closeFn), 0);
}

function showKnockToast(data) {
    const container = document.getElementById('knock-toast-container');
    const div = document.createElement('div');
    div.className = 'knock-card';
    div.innerHTML = `
        <img src="${data.avatar}" style="width:30px;height:30px;border-radius:50%">
        <div style="font-size:14px"><strong>${data.username}</strong> стучится</div>
        <div style="display:flex;gap:5px;margin-left:auto">
            <button class="btn btn-sm btn-primary" onclick="decideKnock('${data.user_id}','approve',this)">Да</button>
            <button class="btn btn-sm btn-secondary" onclick="decideKnock('${data.user_id}','reject',this)">Нет</button>
        </div>
    `;
    container.appendChild(div);
}

window.decideKnock = (uid, decision, btn) => {
    socket.emit('decide_knock', { room_uuid: ROOM_UUID, target_user_id: uid, decision: decision });
    btn.closest('.knock-card').remove();
};

/* --- Sidebar Logic --- */

window.switchSidebar = function(tab) {
    document.querySelectorAll('.sidebar-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.sidebar-tab').forEach(el => el.classList.remove('active'));

    document.getElementById(`sidebar-${tab}`).classList.add('active');
    // document.querySelector(`button[onclick="switchSidebar('${tab}')"]`).classList.add('active');
    // Используем event.target, если вызвано кликом
    if (window.event && window.event.target) {
        window.event.target.classList.add('active');
    }
}

/* --- Chat --- */

window.sendMessage = function() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    socket.emit('chat_message', {
        room_uuid: ROOM_UUID,
        message: text
    });
    input.value = '';
}

window.handleChatKey = function(e) {
    if (e.key === 'Enter') sendMessage();
}

function addSystemMessage(text) {
    const container = document.getElementById('chat-messages');
    if(!container) return;
    const div = document.createElement('div');
    div.className = 'system-message';
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function cleanupViewers() {
    const now = Date.now();
    const timeout = 10000; // 10 секунд

    // Проходим по всем известным пользователям
    for (const sid in lastSeenMap) {
        // Если прошло больше 10 сек с последнего обновления
        if (now - lastSeenMap[sid] > timeout) {

            // Если это не я сам (на всякий случай, хотя я обновляюсь часто)
            if (sid === socket.id) continue;

            // Удаляем из DOM
            const item = document.getElementById(`viewer-${sid}`);
            if (item) {
                item.remove();
            }

            // Удаляем из памяти
            delete lastSeenMap[sid];
            console.log(`User ${sid} removed due to timeout`);
        }
    }
}