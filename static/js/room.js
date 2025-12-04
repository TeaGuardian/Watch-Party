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
let lastSeenMap = {};

// Флаг, чтобы отличать наше нажатие на паузу от серверного события
let ignoreSyncEvents = false;

document.addEventListener('DOMContentLoaded', async () => {
    player = document.getElementById('video-player');

    // 1. Получаем инфу о юзере (id) для идентификации себя
    await loadUserInfo();

    // 2. Загружаем данные комнаты (название, видео)
    await loadRoomData();

    // 3. Подключаем сокеты
    initSocket();

    // 4. Настраиваем слушатели плеера (Play/Pause/Seek)
    setupPlayerListeners();

    // 5. Запускаем Heartbeat (отправка статуса каждые 2 сек)
    setInterval(sendHeartbeat, 2000);
    setInterval(cleanupViewers, 5000);
});

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

let pollInterval = null;

async function loadRoomData() {
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}`);

        if (res.status === 404) {
             document.body.innerHTML = "<h1>Комната не найдена</h1>";
             return;
        }

        const data = await res.json();

        // --- ПРОВЕРКА ДОСТУПА ---
        if (!data.has_access) {
            document.getElementById('access-overlay').style.display = 'flex';
            document.getElementById('room-name').textContent = data.room.name; // Хоть название покажем
            return;
        }
        // ------------------------

        document.getElementById('access-overlay').style.display = 'none';
        document.getElementById('room-name').textContent = data.room.name;

        isOwner = data.is_owner;
        isPrivate = data.is_private;
        isAllowedGuestControl = data.allow_guest_control;
        if (isOwner) {
            document.getElementById('owner-controls').style.display = 'block';
            document.getElementById('btn-settings').style.display = 'block';
        }

        renderPlaylist(data.videos);

        // --- ЛОГИКА АВТО-ОБНОВЛЕНИЯ СТАТУСОВ ---
        // Проверяем, есть ли видео в обработке
        const hasProcessing = data.videos.some(v => v.status === 'processing' || v.status === 'uploading');

        if (hasProcessing) {
            // Если таймер еще не запущен, запускаем
            if (!pollInterval) {
                console.log("Start polling for video status...");
                pollInterval = setInterval(loadRoomData, 3000); // Каждые 3 сек
            }
        } else {
            // Если всё готово, останавливаем таймер
            if (pollInterval) {
                console.log("All videos ready. Stop polling.");
                clearInterval(pollInterval);
                pollInterval = null;
            }
        }
        // ---------------------------------------

    } catch(e) { console.error(e); }
}

/* --- SETTINGS LOGIC --- */

async function openSettingsModal() {
    document.getElementById('settings-modal').style.display = 'flex';

    // Загружаем текущие данные (можно взять из DOM, но лучше из API или room object если сохранили)
    // Для простоты запросим свежие данные
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

function showSettingsTab(tab) {
    document.getElementById('set-tab-general').style.display = tab === 'general' ? 'block' : 'none';
    document.getElementById('set-tab-bans').style.display = tab === 'bans' ? 'block' : 'none';

    if (tab === 'bans') loadBans();
}

async function saveRoomSettings() {
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
            showSuccess('Настройки сохранены');
            document.getElementById('settings-modal').style.display = 'none';
            // Обновляем локально цвет и название
            document.getElementById('room-name').textContent = data.name;
            // Цвет обновится при перезагрузке или через сокет, если реализуем
        } else {
            showError('Ошибка сохранения');
        }
    } catch(e) { showError('Ошибка сети'); }
}

/* --- BAN LOGIC --- */

async function loadBans() {
    const list = document.getElementById('banned-list');
    list.innerHTML = 'Загрузка...';

    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/bans`);
        const data = await res.json();

        if (data.bans.length === 0) {
            list.innerHTML = '<div style="padding:10px; text-align:center;">Список пуст</div>';
            return;
        }

        list.innerHTML = data.bans.map(u => `
            <div class="video-item">
                <div class="video-item-info" style="display:flex; align-items:center; gap:10px;">
                    <img src="${u.avatar_url}" style="width:24px; border-radius:50%">
                    <span>${u.username}</span>
                </div>
                <button class="btn-delete" onclick="unbanUser(${u.user_id})">Разбанить</button>
            </div>
        `).join('');
    } catch(e) { list.innerHTML = 'Ошибка'; }
}

async function banUser(userId, username) {
    if (!confirm(`Забанить пользователя ${username}? Он будет исключен из комнаты.`)) return;

    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/bans`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ user_id: userId })
        });
        if (res.ok) {
            showSuccess(`${username} забанен`);
            // Удаляем визуально (сокет кикнет его реально)
            const el = document.getElementById(`viewer-${userId}`);
            if(el) el.remove();
        }
    } catch(e) { showError('Ошибка'); }
}

async function unbanUser(userId) {
    try {
        await fetch(`/api/rooms/${ROOM_UUID}/bans/${userId}`, { method: 'DELETE' });
        loadBans(); // Обновляем список
    } catch(e) { showError('Ошибка'); }
}

/* --- LOGIC: Knock-Knock --- */

function sendKnock() {
    const btn = document.querySelector('#access-buttons .btn-primary');
    btn.disabled = true;
    btn.textContent = "Стучимся...";

    document.getElementById('access-status').textContent = "Владельцу отправлен запрос...";

    // Инициализируем сокет даже если доступа нет, чтобы отправить knock
    if (!socket) socket = io();

    socket.emit('knock_knock', { room_uuid: ROOM_UUID });
}

function showKnockToast(data) {
    const container = document.getElementById('knock-toast-container');
    const div = document.createElement('div');
    div.className = 'knock-card';
    div.innerHTML = `
        <img src="${data.avatar}" style="width:30px;height:30px;border-radius:50%">
        <div class="knock-info">
            <strong>${data.username}</strong> хочет войти
        </div>
        <div class="knock-actions">
            <button class="btn btn-sm btn-primary" onclick="decideKnock('${data.user_id}', 'approve', this)">Да</button>
            <button class="btn btn-sm btn-secondary" onclick="decideKnock('${data.user_id}', 'reject', this)">Нет</button>
        </div>
    `;
    container.appendChild(div);
}

function decideKnock(targetId, decision, btnElement) {
    socket.emit('decide_knock', {
        room_uuid: ROOM_UUID,
        target_user_id: targetId,
        decision: decision
    });

    // Удаляем тост
    const toast = btnElement.closest('.knock-card');
    toast.remove();
}


/* --- SocketIO --- */

function initSocket() {
    socket = io();

    socket.on('connect', () => {
        console.log("Connected to socket");
        socket.emit('join', { room_uuid: ROOM_UUID });
    });

    socket.on('error_limit', (data) => {
        alert('⛔ Превышен лимит подключений!\n' + data.msg);
        if (data.redirect) {
            window.location.href = '/';
        }
    });

    socket.on('user_left', (data) => {
        const item = document.getElementById(`viewer-${data.sid}`);
        if (item) {
            item.remove();
        }
        delete lastSeenMap[data.sid];
    });

    socket.on('you_are_banned', () => {
        alert('Вы были забанены владельцем комнаты.');
        window.location.href = '/';
    });

    socket.on('room_updated', (data) => {
        if (data.name) document.getElementById('room-name').textContent = data.name;
        // Если нужно, можно обновлять и цвет хедера через JS, но это сложнее достать из DOM
    });

    // Вход нового юзера
    socket.on('user_joined', (data) => {
        if (data.sid !== socket.id) {
            addSystemMessage(`${data.username} присоединился`);
        }
    });

    socket.on('access_granted', () => {
        document.getElementById('access-status').textContent = "✅ Доступ разрешен! Входим...";
        setTimeout(() => {
            window.location.reload(); // Перезагружаем страницу, чтобы получить has_access=True от API
        }, 1000);
    });

    socket.on('access_denied', (data) => {
        document.getElementById('access-status').textContent = `⛔ Отказано: ${data.reason}`;
        const btn = document.querySelector('#access-buttons .btn-primary');
        btn.disabled = false;
        btn.textContent = "Постучаться снова";
    });

    // Уведомление для ВЛАДЕЛЬЦА
    socket.on('incoming_knock', (data) => {
        showKnockToast(data);
    });

    // Чат
    socket.on('new_message', (data) => {
        const container = document.getElementById('chat-messages');
        const div = document.createElement('div');
        div.className = 'message';
        div.innerHTML = `<span class="msg-author">${data.username}:</span><span class="msg-text">${data.text}</span>`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    });

    // --- СИНХРОНИЗАЦИЯ (Принимаем команды) ---
    socket.on('sync_event', (data) => {
        console.log("Sync Event:", data);
        ignoreSyncEvents = true; // Блокируем отправку нашего события в ответ

        // data: { action: 'play'|'pause'|'seek', timestamp: 10.5 }

        // Коррекция времени, если рассинхрон > 0.5 сек
        if (Math.abs(player.currentTime - data.timestamp) > 0.5) {
            player.currentTime = data.timestamp;
        }

        if (data.action === 'play') {
            player.play().catch(e => console.log("Auto-play blocked:", e));
        } else if (data.action === 'pause') {
            player.pause();
        }

        // Снимаем блокировку через небольшой таймаут
        setTimeout(() => { ignoreSyncEvents = false; }, 500);
    });

    // --- СМЕНА ВИДЕО ---
    socket.on('load_video', (data) => {
        // data: { url: '...', title: '...', video_id: 1 }
        currentVideoId = data.video_id;
        loadSource(data.url);
        addSystemMessage(`Включено видео: ${data.title}`);

        // Обновляем UI плейлиста
        document.querySelectorAll('.video-item').forEach(el => el.classList.remove('active'));
        const activeItem = document.querySelector(`.video-item[data-id="${data.video_id}"]`);
        if (activeItem) activeItem.classList.add('active');
    });

    // --- СТАТУСЫ УЧАСТНИКОВ ---
    socket.on('status_update', (data) => {
        // data: { user_id, state, timestamp, avatar }
        if (data.sid === socket.id) return;
        updateViewerStatus(data);
    });

    socket.on('restore_state', (data) => {
        console.log("Restoring state:", data);

        // 1. Загружаем видео, если оно выбрано
        if (data.video) {
            currentVideoId = data.video.id;
            loadSource(data.video.url);

            // Подсветка в плейлисте
            setTimeout(() => {
                document.querySelectorAll('.video-item').forEach(el => el.classList.remove('active'));
                const activeItem = document.querySelector(`.video-item[data-id="${currentVideoId}"]`);
                if (activeItem) activeItem.classList.add('active');
            }, 500); // Небольшая задержка, чтобы плейлист успел отрисоваться
        }

        // 2. Выставляем время и паузу
        // HLS.js требует, чтобы манифест загрузился, поэтому используем hook
        const onManifestParsed = () => {
             if (player && data.timestamp > 0) {
                 player.currentTime = data.timestamp;
             }
             if (!data.paused) {
                 player.play().catch(e => console.log("Autoplay blocked", e));
             }
        };

        // Если HLS уже готов, применяем сразу, иначе ждем
        if (hls && hls.url) {
             onManifestParsed();
        } else {
             // Мы надеемся, что loadSource выше сработает и hls создастся
             // Можно добавить слушатель, но для MVP достаточно таймера или надежды на loadSource
             setTimeout(() => {
                 if (data.timestamp > 0) player.currentTime = data.timestamp;
                 if (!data.paused) player.play().catch(()=>{});
             }, 1000);
        }
    });

    socket.on('playlist_refresh', () => {
        console.log("Playlist refresh requested");
        loadRoomData(); // Эта функция сама запустит поллинг, если увидит статус 'processing'
    });
}

/* --- HLS Player Logic --- */

function loadSource(url) {
    console.log("Player loading URL:", url);
    const overlay = document.getElementById('video-overlay');

    if (overlay) overlay.style.display = 'flex';
    document.getElementById('overlay-text').textContent = "Загрузка...";

    // Сбрасываем предыдущий инстанс
    if (hls) {
        hls.destroy();
        hls = null;
    }

    if (Hls.isSupported()) {
        hls = new Hls({
            autoStartLoad: true, // Начинаем грузить данные сразу
            startPosition: -1,   // С начала
            debug: false
        });

        hls.loadSource(url);
        hls.attachMedia(player);

        hls.on(Hls.Events.MANIFEST_PARSED, function() {
            console.log("HLS Manifest Parsed. Ready.");
            if (overlay) overlay.style.display = 'none';
            // Мы НЕ вызываем player.play(), видео встанет на паузу на первом кадре.
            // Ждем действий пользователя или события 'sync_action'
        });

        hls.on(Hls.Events.ERROR, function(event, data) {
            if (data.fatal) {
                console.error("HLS Fatal Error:", data);
                switch(data.type) {
                case Hls.ErrorTypes.NETWORK_ERROR:
                    // Пытаемся восстановить при ошибке сети
                    console.log("Network error, trying to recover...");
                    hls.startLoad();
                    break;
                case Hls.ErrorTypes.MEDIA_ERROR:
                    console.log("Media error, trying to recover...");
                    hls.recoverMediaError();
                    break;
                default:
                    hls.destroy();
                    break;
                }
            }
        });
    }
    else if (player.canPlayType('application/vnd.apple.mpegurl')) {
        // Для Safari (Native HLS)
        player.src = url;
        player.addEventListener('loadedmetadata', function() {
            if (overlay) overlay.style.display = 'none';
        });
    }
}

function setupPlayerListeners() {
    // Если я нажал Play
    player.addEventListener('play', () => {
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', {
                room_uuid: ROOM_UUID,
                action: 'play',
                timestamp: player.currentTime
            });
        }
    });

    // Если я нажал Pause
    player.addEventListener('pause', () => {
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', {
                room_uuid: ROOM_UUID,
                action: 'pause',
                timestamp: player.currentTime
            });
        }
    });

    // Если я перемотал (Seek)
    player.addEventListener('seeked', () => {
        if (!ignoreSyncEvents) {
            socket.emit('sync_action', {
                room_uuid: ROOM_UUID,
                action: 'seek',
                timestamp: player.currentTime
            });
        }
    });
}

function sendHeartbeat() {
    if (!player) return;

    const currentState = player.paused ? 'paused' : 'playing';
    const currentTime = player.currentTime;

    socket.emit('heartbeat', {
        room_uuid: ROOM_UUID,
        timestamp: currentTime,
        state: currentState
    });

    updateViewerStatus({
        user_id: myUserId,
        username: myUsername,
        avatar: myAvatar,
        state: currentState,
        timestamp: currentTime
    });
}

/* --- Viewers Logic (Светофор) --- */

function updateViewerStatus(data) {
    lastSeenMap[data.sid] = Date.now();
    const list = document.getElementById('viewers-list');
    let item = document.getElementById(`viewer-${data.sid}`);

    // БЕЗОПАСНАЯ АВАТАРКА: Если data.avatar нет, используем заглушку
    const avatarSrc = data.avatar || `https://ui-avatars.com/api/?name=${data.username || 'User'}&background=random`;

    if (!item) {
        item = document.createElement('div');
        item.id = `viewer-${data.sid}`;
        item.className = 'viewer-card status-gray';
        item.innerHTML = `
            <div style="position:relative;">
                <img src="${avatarSrc}" class="viewer-avatar">
                ${(isOwner && data.user_id !== myUserId) ?
                  `<div onclick="banUser(${data.user_id}, '${data.username}')"
                        style="position:absolute; top:-5px; right:-5px; background:red; color:white;
                               width:15px; height:15px; border-radius:50%; font-size:10px;
                               cursor:pointer; display:flex; justify-content:center; align-items:center;"
                        title="Бан">⛔</div>`
                  : ''}
            </div>
            <span class="viewer-name">${data.username}</span>
        `;
        list.appendChild(item);
    } else {
        // Если элемент уже есть, на всякий случай обновим аватар, если он вдруг стал undefined
        const img = item.querySelector('.viewer-avatar');
        if (img && img.src.endsWith('undefined')) {
            img.src = avatarSrc;
        }
    }

    // Расчет цвета (остается прежним)
    let statusClass = 'status-gray';
    if (data.state === 'playing') {
        const diff = Math.abs(player.currentTime - data.timestamp);
        if (diff < 1.5) statusClass = 'status-green';
        else if (diff < 4.0) statusClass = 'status-yellow';
        else statusClass = 'status-red';
    }

    item.className = `viewer-card ${statusClass}`;
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

/* --- Sidebar Logic --- */

function switchSidebar(tab) {
    document.querySelectorAll('.sidebar-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.sidebar-tab').forEach(el => el.classList.remove('active'));

    document.getElementById(`sidebar-${tab}`).classList.add('active');
    document.querySelector(`button[onclick="switchSidebar('${tab}')"]`).classList.add('active');
}

/* --- Chat --- */

function sendMessage() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    socket.emit('chat_message', {
        room_uuid: ROOM_UUID,
        message: text
    });
    input.value = '';
}

function handleChatKey(e) {
    if (e.key === 'Enter') sendMessage();
}

function addSystemMessage(text) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'system-message';
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

/* --- Playlist & Upload --- */

function renderPlaylist(videos) {
    const container = document.getElementById('playlist-container');
    if (videos.length === 0) {
        container.innerHTML = '<div class="empty-state">Нет видео</div>';
        return;
    }

    // Сохраняем текущее видео, чтобы не сбить подсветку при обновлении
    const activeId = currentVideoId;

    container.innerHTML = videos.map(v => {
        // Делаем элемент неактивным визуально, если видео не готово
        const isReady = v.status === 'ready';
        const itemClass = `video-item ${v.id === activeId ? 'active' : ''} ${!isReady ? 'disabled' : ''}`;
        const durationText = isReady ? formatDuration(v.duration) : '';

        // Индикатор статуса
        let statusBadge = '';
        if (v.status === 'processing') statusBadge = '⏳ Обработка...';
        else if (v.status === 'uploading') statusBadge = '⬆️ Загрузка...';
        else if (v.status === 'error') statusBadge = '❌ Ошибка';

        const safeTitle = v.title.replace(/'/g, "\\'").replace(/"/g, '&quot;');

        let fct = false;
        if (isOwner || isAllowedGuestControl) { fct = true; }

        const controls = fct ? `
            <div class="item-actions">
                <button class="btn-icon-action btn-edit" onclick="renameVideo(event, ${v.id}, '${safeTitle}')" title="Переименовать">✏️</button>
                <button class="btn-icon-action btn-delete" onclick="deleteVideo(event, ${v.id})" title="Удалить">×</button>
            </div>
        ` : '';

        return `
        <div class="${itemClass}"
             data-id="${v.id}"
             onclick="${isReady ? `changeVideo(${v.id})` : ''}"
             style="${!isReady ? 'opacity: 0.6; cursor: default;' : ''}">

            <div class="video-item-info">
                <div class="video-title" title="${v.title}">${v.title}</div>
                <div class="video-status">
                    ${statusBadge || durationText}
                    ${v.size_mb ? `<span style="margin-left:5px; opacity:0.7;">(${v.size_mb} MB)</span>` : ''}
                </div>
            </div>
            ${controls}
        </div>
        `;
    }).join('');
}

async function renameVideo(e, id, oldTitle) {
    e.stopPropagation();
    const newTitle = prompt("Введите новое название:", oldTitle);
    if (newTitle === null || !newTitle.trim() || newTitle === oldTitle) return;

    try {
        const res = await fetch(`/api/videos/${id}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ title: newTitle.trim() })
        });

        if (res.ok) {
            showSuccess('Переименовано');
        } else {
            const data = await res.json();
            showError(data.error || 'Ошибка');
        }
    } catch (e) {
        showError('Ошибка сети');
    }
}

function changeVideo(videoId) {
    console.log("Clicked video ID:", videoId);

    if (!isOwner && !isAllowedGuestControl) {
        console.warn("You are not the owner, cannot change video.");
        return;
    }

    // Эмитим событие на сервер
    socket.emit('change_video', {
        room_uuid: ROOM_UUID,
        video_id: videoId
    });
}

function triggerUpload() {
    if (document.getElementById('upload-progress').style.display == 'none') {
        document.getElementById('file-upload').click();
        document.getElementById('video-upload-bt').disabled = true;
    } else {alert("Дождитесь полной загрузки текущего видео или перезагрузите страницу.")}

}

async function uploadVideo() {
    const input = document.getElementById('file-upload');
    if (!input.files.length) return;

    const file = input.files[0];
    const isVideo = file.type.startsWith('video/') || /\.(mp4|avi|mov|mkv|webm)$/i.test(file.name);
    if (!isVideo) {
        alert("Пожалуйста, выберите видеофайл (MP4, AVI, MKV, MOV).");
        input.value = ''; // Сбрасываем выбор
        document.getElementById('video-upload-bt').disabled = false;
        return;
    }
    // --- ПРОВЕРКА РАЗМЕРА ---
    if (file.size > MAX_VIDEO_SIZE) {
        const sizeMB = Math.round(file.size / 1024 / 1024);
        const limitMB = Math.round(MAX_VIDEO_SIZE / 1024 / 1024);
        alert(`Файл слишком большой (${sizeMB} МБ). Лимит: ${limitMB} МБ.`);
        // Очищаем инпут
        input.value = '';
        document.getElementById('video-upload-bt').disabled = false;
        return;
    }
    // ------------------------
    const formData = new FormData();
    formData.append('video', file);

    document.getElementById('upload-progress').style.display = 'block';
    const bar = document.getElementById('progress-fill');
    const text = document.getElementById('upload-percent');

    // XMLHttpRequest для прогресса (fetch не умеет)
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/rooms/${ROOM_UUID}/upload`);

    xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
            const percent = (e.loaded / e.total) * 100;
            bar.style.width = percent + '%';
            text.textContent = Math.round(percent) + '%';
        }
    };

    xhr.onload = async () => {
        document.getElementById('video-upload-bt').disabled = false;
        if (xhr.status === 200) {
            console.log('Видео загружено и обрабатывается. Оно появится в списке автоматически.');
            document.getElementById('upload-progress').style.display = 'none';
            // Перезагрузим список (для простоты)
            loadRoomData();
        } else {
            alert('Ошибка загрузки');
        }
    };

    xhr.send(formData);
}

async function deleteVideo(e, id) {
    e.stopPropagation();
    if (!confirm('Удалить?')) return;

    await fetch(`/api/videos/${id}`, { method: 'DELETE' });
    loadRoomData();
}

function formatDuration(sec) {
    if (!sec) return '';
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s < 10 ? '0'+s : s}`;
}

async function copyLink() {
    const url = window.location.href;

    try {
        // Пробуем современный API
        await navigator.clipboard.writeText(url);
        showNotification('Ссылка скопирована в буфер обмена!', 'success');
    } catch (err) {
        // Fallback для старых браузеров или HTTP
        try {
            const textArea = document.createElement('textarea');
            textArea.value = url;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            textArea.style.top = '-999999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();

            const successful = document.execCommand('copy');
            document.body.removeChild(textArea);

            if (successful) {
                showNotification('Ссылка скопирована!', 'success');
            } else {
                throw new Error('Не удалось скопировать');
            }
        } catch (fallbackErr) {
            // Последний вариант - показать ссылку для ручного копирования
            prompt('Скопируйте ссылку вручную:', url);
            showNotification('Ссылка готова для копирования', 'info');
        }
    }
}

function showNotification(message, type = 'info') {
    // Удаляем старое уведомление если есть
    const oldNotification = document.querySelector('.copy-notification');
    if (oldNotification) {
        oldNotification.remove();
    }

    // Создаем новое уведомление
    const notification = document.createElement('div');
    notification.className = `copy-notification notification-${type}`;
    notification.textContent = message;
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px 24px;
        background: ${type === 'success' ? '#4CAF50' : '#2196F3'};
        color: white;
        border-radius: 4px;
        z-index: 10000;
        font-family: sans-serif;
        box-shadow: 0 2px 5px rgba(0,0,0,0.2);
        animation: fadeInOut 3s ease-in-out;
    `;

    // Добавляем стили для анимации
    const style = document.createElement('style');
    style.textContent = `
        @keyframes fadeInOut {
            0% { opacity: 0; transform: translateY(-10px); }
            10% { opacity: 1; transform: translateY(0); }
            90% { opacity: 1; transform: translateY(0); }
            100% { opacity: 0; transform: translateY(-10px); }
        }
    `;
    document.head.appendChild(style);

    document.body.appendChild(notification);

    // Автоматически удаляем через 3 секунды
    setTimeout(() => {
        if (notification.parentNode) {
            notification.remove();
        }
        if (style.parentNode) {
            style.remove();
        }
    }, 3000);
}