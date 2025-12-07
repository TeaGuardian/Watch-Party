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
let processingMap = {};

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
    setInterval(() => {
        console.log("🔄 Periodic room refresh (10 min)");
        loadRoomData();
    }, 10 * 60 * 1000);
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
        if (isOwner || isAllowedGuestControl) {
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
            const area = document.getElementById('settings-area');
            if (area) area.style.display = 'none';
            const nameEl = document.getElementById('room-name');
            if (nameEl) nameEl.textContent = data.name;

        } else {
            showError('Ошибка сохранения');
        }
    } catch(e) {
        console.error("Save error:", e);
        showError('Ошибка выполнения (см. консоль)');
    }
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

/* --- NEW SETTINGS LOGIC --- */

// Состояние поповера
let selectedUserForAction = null;

function toggleSettings() {
    const area = document.getElementById('settings-area');
    const isHidden = area.style.display === 'none';

    if (isHidden) {
        area.style.display = 'block';
        loadSettingsData(); // Загружаем свежие данные
        // Скроллим вниз к настройкам
        area.scrollIntoView({ behavior: 'smooth' });
    } else {
        area.style.display = 'none';
    }
}

function switchSettingsTab(tabName) {
    // Скрываем все колонки
    document.querySelectorAll('.settings-col').forEach(el => el.classList.remove('active'));
    // Показываем нужную
    document.getElementById(`col-${tabName}`).classList.add('active');

    // Обновляем кнопки табов
    document.querySelectorAll('.s-tab-btn').forEach(el => el.classList.remove('active'));
    event.target.classList.add('active');
}

async function loadSettingsData() {
    // Заполняем форму
    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}`);
        const data = await res.json();
        const r = data.room;

        document.getElementById('set-room-name').value = r.name;
        document.getElementById('set-room-color').value = r.header_color;
        document.getElementById('set-is-private').checked = r.is_private;
        document.getElementById('set-guest-control').checked = r.allow_guest_control;

        loadBans(); // Существующая функция (надо убедиться, что она рендерит в правильный div)

        // Логика списка гостей
        if (r.is_private) {
            loadAccessList();
            document.getElementById('guest-access-list').style.display = 'flex';
            document.getElementById('public-room-warning').style.display = 'none';
        } else {
            document.getElementById('guest-access-list').style.display = 'none';
            document.getElementById('public-room-warning').style.display = 'block';
        }

    } catch(e) { console.error(e); }
}

async function loadAccessList() {
    const list = document.getElementById('guest-access-list');
    list.innerHTML = '<div style="color:#777;text-align:center">Загрузка...</div>';

    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/access`);
        if (!res.ok) throw new Error("No access");
        const data = await res.json();

        if (data.users.length === 0) {
            list.innerHTML = '<div style="color:#777;text-align:center">Список пуст</div>';
            return;
        }

        list.innerHTML = data.users.map(u => `
            <div class="list-item">
                <div style="display:flex; align-items:center;">
                    <img src="${u.avatar_url}" class="list-avatar">
                    ${u.username}
                </div>
                <button class="btn-icon-action" style="color:orange" onclick="revokeAccess(${u.id}, '${u.username}')" title="Лишить доступа">✖</button>
            </div>
        `).join('');

    } catch (e) {
        list.innerHTML = '<div style="color:#777;text-align:center">Ошибка</div>';
    }
}

/* --- Popover Logic --- */

function showUserPopover(event, userId, username) {
    event.stopPropagation();
    selectedUserForAction = { id: userId, name: username };

    const popover = document.getElementById('user-popover');
    document.getElementById('pop-username').textContent = username;

    // Показываем кнопку "Выгнать" только если комната приватная
    const kickBtn = document.getElementById('btn-pop-kick');
    kickBtn.style.display = isPrivate ? 'block' : 'none';

    // Позиционирование
    const rect = event.currentTarget.getBoundingClientRect();
    popover.style.top = (window.scrollY + rect.bottom + 5) + 'px';
    popover.style.left = (rect.left - 20) + 'px';
    popover.style.display = 'block';

    // Закрытие по клику вне
    const closeFn = (e) => {
        if (!popover.contains(e.target)) {
            popover.style.display = 'none';
            document.removeEventListener('click', closeFn);
        }
    };
    setTimeout(() => document.addEventListener('click', closeFn), 0);
}

function confirmBanUser() {
    if (!selectedUserForAction) return;
    banUser(selectedUserForAction.id, selectedUserForAction.name); // Используем существующую функцию
    document.getElementById('user-popover').style.display = 'none';
}

function confirmKickUser() {
    if (!selectedUserForAction) return;
    revokeAccess(selectedUserForAction.id, selectedUserForAction.name);
    document.getElementById('user-popover').style.display = 'none';
}

async function revokeAccess(userId, username) {
    if (!confirm(`Закрыть доступ для ${username}?`)) return;

    try {
        const res = await fetch(`/api/rooms/${ROOM_UUID}/access/${userId}`, { method: 'DELETE' });
        if (res.ok) {
            showSuccess(`${username} удален из списка доступа`);
            loadSettingsData(); // Обновляем список
        }
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

    socket.on('processing_progress', (data) => {
        // data: { video_id: 123, percent: 45 }
        updateProcessingProgress(data.video_id, data.percent);
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
        loadRoomData();
    });

    socket.on('stop_playback', () => {
        console.log("🛑 Stop playback command received");
        resetPlayerState();
    });
}

function updateProcessingProgress(videoId, percent) {
    processingMap[videoId] = percent;

    const item = document.querySelector(`.video-item[data-id="${videoId}"]`);
    if (!item) return;

    const statusDiv = item.querySelector('.video-status div:first-child');
    if (statusDiv) {
        statusDiv.innerHTML = `<span style="color:orange; font-weight:bold;">⏳ Обработка: ${percent}%</span>`;
    }
}

function resetPlayerState() {
    // 1. Сбрасываем HLS
    if (hls) {
        hls.destroy();
        hls = null;
    }

    // 2. Сбрасываем нативный плеер
    if (player) {
        player.pause();
        player.removeAttribute('src'); // Убираем источник
        player.load(); // Сбрасываем буфер
    }

    // 3. Сбрасываем переменные
    currentVideoId = null;

    // 4. Обновляем UI
    document.querySelectorAll('.video-item').forEach(el => el.classList.remove('active'));

    // Показываем оверлей "Ожидание"
    const overlay = document.getElementById('video-overlay');
    if (overlay) {
        overlay.style.display = 'flex';
        document.getElementById('overlay-text').textContent = "Выберите видео";
    }

    // Сообщение в чат (локальное)
    addSystemMessage("Текущее видео было удалено.");
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
        item.setAttribute('data-user-id', data.user_id);

        if (isOwner && data.user_id !== myUserId) {
            item.style.cursor = 'pointer';
            item.onclick = (e) => showUserPopover(e, data.user_id, data.username);
        }

        item.innerHTML = `
            <div style="position:relative;">
                <img src="${avatarSrc}" class="viewer-avatar">
                <!-- Старая иконка бана удалена, теперь через меню -->
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

setInterval(updateTTLCounters, 60000);

function renderPlaylist(videos) {
    const container = document.getElementById('playlist-container');
    if (videos.length === 0) {
        container.innerHTML = '<div class="empty-state">Нет видео</div>';
        return;
    }

    const activeId = currentVideoId;

    container.innerHTML = videos.map(v => {
        const isReady = v.status === 'ready';
        const itemClass = `video-item ${v.id === activeId ? 'active' : ''} ${!isReady ? 'disabled' : ''}`;
        const durationText = isReady ? formatDuration(v.duration) : '';

        let statusBadge = '';

        if (v.status === 'processing') {
            const percent = processingMap[v.id];
            if (percent !== undefined) {
                statusBadge = `<span style="color:orange; font-weight:bold;">⏳ Обработка: ${percent}%</span>`;
            } else {
                statusBadge = '<span style="color:orange">⏳ Обработка...</span>';
            }
        }
        else if (v.status === 'uploading') {
            statusBadge = '<span style="color:#3498db">⬆️ Загрузка...</span>';
            // Если видео перешло из processing в uploading (маловероятно, но всё же) или ready, чистим карту
            delete processingMap[v.id];
        }
        else if (v.status === 'error') {
            statusBadge = '<span style="color:red">❌ Ошибка</span>';
            delete processingMap[v.id];
        }
        else if (v.status === 'ready') {
            // Если стало ready - убираем из карты прогресса
            delete processingMap[v.id];
        }

        const safeTitle = v.title.replace(/'/g, "\\'").replace(/"/g, '&quot;');

        let fct = false;
        if (isOwner || isAllowedGuestControl) { fct = true; }

        const controls = fct ? `
            <div class="item-actions">
                <button class="btn-icon-action btn-edit" onclick="renameVideo(event, ${v.id}, '${safeTitle}')" title="Переименовать">✏️</button>
                <button class="btn-icon-action btn-delete" onclick="deleteVideo(event, ${v.id})" title="Удалить">×</button>
            </div>
        ` : '';

        let ttlHtml = '';
        if (isReady && v.last_played_at) {
            ttlHtml = `<div class="ttl-timer" data-last-played="${v.last_played_at}"></div>`;
        }

        return `
        <div class="${itemClass}"
             data-id="${v.id}"
             onclick="${isReady ? `changeVideo(${v.id})` : ''}"
             style="${!isReady ? 'opacity: 0.6; cursor: default;' : ''}">

            <div class="video-item-info">
                <div class="video-title" title="${v.title}">${v.title}</div>
                <div class="video-status" style="display:flex; justify-content:space-between; align-items:center; padding-right:10px;">
                    <div>
                        ${statusBadge || durationText}
                        ${v.size_mb ? `<span style="margin-left:5px; opacity:0.7;">(${v.size_mb} MB)</span>` : ''}
                    </div>
                    ${ttlHtml}
                </div>
            </div>
            ${controls}
        </div>
        `;
    }).join('');

    updateTTLCounters();
}

// Функция обновления таймеров жизни
function updateTTLCounters() {
    const timers = document.querySelectorAll('.ttl-timer');
    const retentionMs = RETENTION_HOURS * 60 * 60 * 1000;
    const now = new Date().getTime();

    timers.forEach(el => {
        const lastPlayedStr = el.getAttribute('data-last-played');
        if (!lastPlayedStr) return;

        // lastPlayedStr приходит в ISO (UTC или локальное сервера),
        // JS Date.parse обычно корректно это ест.
        const lastPlayedTs = new Date(lastPlayedStr).getTime();
        const deathTime = lastPlayedTs + retentionMs;
        const diff = deathTime - now;

        if (diff <= 0) {
            el.innerHTML = '<span style="color:red; font-size:10px;">Удаление...</span>';
        } else {
            // Форматируем остаток
            const hours = Math.floor(diff / (1000 * 60 * 60));
            const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

            let color = '#888'; // Обычный серый
            if (hours === 0 && minutes < 30) color = 'orange';
            if (hours === 0 && minutes < 10) color = 'red';

            el.innerHTML = `<span style="color:${color}; font-size:10px;" title="Автоудаление через...">♻️ ${hours}ч ${minutes}м</span>`;
        }
    });
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
    } else {alert("Дождитесь полной загрузки текущего видео или перезагрузите страницу.")}

}

async function uploadVideo() {
    const input = document.getElementById('file-upload');
    const btn = document.getElementById('video-upload-bt');
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
    btn.disabled = true;
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
        btn.disabled = false;
        document.getElementById('video-upload-bt').disabled = false;
        if (xhr.status === 200) {
            console.log('Видео загружено и обрабатывается. Оно появится в списке автоматически.');
            document.getElementById('upload-progress').style.display = 'none';
            input.value = '';
            loadRoomData();
        } else {
            document.getElementById('upload-progress').style.display = 'none';
            try {
                const err = JSON.parse(xhr.responseText);
                alert('Ошибка: ' + (err.error || 'Неизвестная ошибка'));
            } catch (e) {
                alert('Ошибка загрузки (код ' + xhr.status + ')');
            }
        }
    };

    xhr.onerror = () => {
        btn.disabled = false;
        document.getElementById('upload-progress').style.display = 'none';
        alert('Ошибка сети при загрузке файла.');
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
    const url = `${window.location.href}?ref=${encodeURIComponent(myUsername)}`;

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