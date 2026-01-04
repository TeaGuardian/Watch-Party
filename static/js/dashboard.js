/* #/static/js/dashboard.js */
let currentUser = null;
let roomsPollInterval = null; // Для автообновления
let cropper = null;
let easyMDE = null;

// --- Инициализация ---
document.addEventListener('DOMContentLoaded', async () => {
    await loadUserProfile();

    const urlParams = new URLSearchParams(window.location.search);
    const requestedTab = urlParams.get('tab');

    if (currentUser) {
        if (requestedTab === 'profile') {
            openTab('profile');
        } else if (canAccessContent()) {
            openTab('news');
        } else {
            openTab('profile');
        }

        if (currentUser.role === 'admin') {
            initMarkdownEditor();
        }
    }

    if (requestedTab) {
        window.history.replaceState({}, document.title, "/");
    }
});


function initMarkdownEditor() {
    const textArea = document.getElementById('new-post-content');
    if (textArea && !easyMDE) {
        easyMDE = new EasyMDE({
            element: textArea,
            spellChecker: false, // Отключаем проверку орфографии (часто глючит на ру)
            placeholder: "Пишите новости здесь... Поддерживается Markdown.",
            status: false, // Скрываем строку статистики внизу
            toolbar: [
                "bold", "italic", "heading", "|",
                "quote", "unordered-list", "ordered-list", "|",
                "link", "image", "|",
                "preview", "guide"
            ],
            minHeight: "150px",
        });
    }
}

// --- API Functions ---

async function loadUserProfile() {
    try {
        const res = await fetch('/api/me');
        if (res.status === 401) {
            window.location.href = '/login';
            return;
        }

        currentUser = await res.json();

        if (currentUser.status === 'banned') {
            document.body.innerHTML = '<h1 style="text-align:center;margin-top:50px">Access Denied (Banned)</h1>';
            return;
        }

        renderProfile(currentUser);

        // Показать элементы админа
        if (currentUser.role === 'admin') {
            document.getElementById('admin-news-controls').style.display = 'block';
            document.getElementById('admin-panel-btn').style.display = 'block';
            const voiceOption = document.getElementById('voice-chat-container');
            if (voiceOption) voiceOption.style.display = 'flex';
        }

    } catch (e) {
        console.error("Auth check failed", e);
    }
}

function canAccessContent() {
    return currentUser && (
        currentUser.status === 'approved' ||
        currentUser.status === 'tg_verified' ||
        currentUser.role === 'admin'
    );
}

// --- Tabs Logic ---
function openTab(tabName) {
    // 1. Сначала убираем active со всех кнопок и контента
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.remove('active');
        // ВАЖНО: Убираем inline-стили, если они вдруг остались
        el.style.display = '';
    });
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.remove('active');
        el.style.display = '';
    });
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    // 2. Проверка доступа
    if (!canAccessContent() && (tabName === 'news' || tabName === 'rooms')) {
        document.getElementById('access-denied-placeholder').style.display = 'block';

        // Подсвечиваем нажатую кнопку
        const btn = document.querySelector(`button[onclick="openTab('${tabName}')"]`);
        if(btn) btn.classList.add('active');
        return;
    } else {
        const deniedEl = document.getElementById('access-denied-placeholder');
        if (deniedEl) deniedEl.style.display = 'none';
    }

    if (roomsPollInterval) {
        clearInterval(roomsPollInterval);
        roomsPollInterval = null;
    }

    const target = document.getElementById(`tab-${tabName}`);
    if(target) {
        target.classList.add('active');

        if (tabName === 'news') loadNews();

        if (tabName === 'rooms') {
            loadRooms(); // Загружаем сразу
            roomsPollInterval = setInterval(loadRooms, 10000);
        }

        if (tabName === 'profile') {
            initStatsPolling();
        } else {
            if (statsTimer) clearTimeout(statsTimer);
        }
    }

    // 4. Подсвечиваем кнопку
    const btn = document.querySelector(`button[onclick="openTab('${tabName}')"]`);
    if(btn) btn.classList.add('active');
}

// --- Render Functions ---

function renderProfile(user) {
    document.getElementById('profile-username').textContent = user.username;
    let avatarUrl = user.avatar_url;
    if (!avatarUrl || avatarUrl === 'undefined') {
        avatarUrl = `https://ui-avatars.com/api/?name=${user.username || 'User'}&background=random&size=200`;
    }
    document.getElementById('profile-avatar').src = avatarUrl;
    const delBtn = document.getElementById('btn-delete-avatar');
    if (user.has_custom_avatar) {
        delBtn.style.display = 'block';
    } else {
        delBtn.style.display = 'none';
    }

    // Статус
    const statusEl = document.getElementById('profile-status');
    statusEl.textContent = user.status;
    statusEl.className = `status-badge status-${user.status}`;

    // Telegram
    const tgStatus = document.getElementById('tg-status');
    const tgBlock = document.getElementById('tg-connect-block');

    const oldUnlinkBtn = document.getElementById('btn-unlink-tg');
    if (oldUnlinkBtn) oldUnlinkBtn.remove();

    if (tgStatus && tgBlock) {
        if (user.tg_connected) {
            tgStatus.textContent = `@${user.tg_username || 'Connected'}`;
            tgStatus.style.color = 'var(--success-color, green)';
            tgBlock.style.display = 'none';

            // Добавляем кнопку отвязки динамически рядом со статусом
            const unlinkBtn = document.createElement('button');
            unlinkBtn.id = 'btn-unlink-tg';
            unlinkBtn.className = 'btn-text-danger'; // Используем тот же класс, что и для аватара
            unlinkBtn.style.fontSize = '12px';
            unlinkBtn.style.border = 'none';
            unlinkBtn.style.background = 'none';
            unlinkBtn.style.cursor = 'pointer';
            unlinkBtn.style.marginLeft = '10px';
            unlinkBtn.textContent = '(Отвязать)';
            unlinkBtn.onclick = unlinkTelegram;

            tgStatus.parentNode.appendChild(unlinkBtn);

        } else {
            tgStatus.textContent = 'Не привязан';
            tgStatus.style.color = 'var(--danger-color, red)';
            tgBlock.style.display = 'block';
        }
    }

    document.getElementById('approved-by').textContent = user.approved_by || '-';
}

async function unlinkTelegram() {
    if (!confirm('Отвязать Telegram? Вы потеряете статус "Approved" и доступ к функционалу, пока не привяжете снова.')) return;

    try {
        const res = await fetch('/api/tg_link', { method: 'DELETE' });
        const data = await res.json();

        if (res.ok) {
            showSuccess('Telegram отвязан');
            loadUserProfile();
        } else {
            showError(data.error);
        }
    } catch(e) { showError('Ошибка сети'); }
}

/* --- Avatar Logic --- */
/*
function triggerAvatarUpload() {
    document.getElementById('avatar-input').click();
}
*/
function handleAvatarSelect(input) {
    if (input.files && input.files[0]) {
        const file = input.files[0];

        // Читаем файл как DataURL для показа в кроппере
        const reader = new FileReader();
        reader.onload = function(e) {
            openCropperModal(e.target.result);
        };
        reader.readAsDataURL(file);
    }
    // Сбрасываем value, чтобы можно было выбрать тот же файл повторно
    input.value = '';
}

function openCropperModal(imageSrc) {
    const modal = document.getElementById('cropper-modal');
    const image = document.getElementById('cropper-image');

    modal.style.display = 'flex';
    image.src = imageSrc;

    // Инициализация Cropper.js
    if (cropper) cropper.destroy();

    cropper = new Cropper(image, {
        aspectRatio: 1, // Квадрат (для круглой аватарки)
        viewMode: 1,    // Ограничить кроппер размерами картинки
        dragMode: 'move', // Можно двигать картинку
        autoCropArea: 1,
        guides: false,
    });
}

function closeCropperModal() {
    document.getElementById('cropper-modal').style.display = 'none';
    if (cropper) {
        cropper.destroy();
        cropper = null;
    }
}

function saveCroppedAvatar() {
    if (!cropper) return;

    // Получаем обрезанное изображение как Canvas -> Blob
    cropper.getCroppedCanvas({
        width: 300, // Ресайз до разумных размеров
        height: 300
    }).toBlob((blob) => {
        // Создаем FormData для отправки файла
        const formData = new FormData();
        formData.append('avatar', blob, 'avatar.jpg');

        // Отправляем на сервер
        fetch('/api/profile/avatar', {
            method: 'POST',
            body: formData
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                showSuccess('Аватар обновлен');
                // Обновляем картинку на странице
                document.getElementById('profile-avatar').src = data.avatar_url;
                document.getElementById('btn-delete-avatar').style.display = 'block';
                closeCropperModal();
            } else {
                showError(data.error || 'Ошибка загрузки');
            }
        })
        .catch(err => showError('Ошибка сети'));
    }, 'image/jpeg', 0.9); // Качество 90%
}

async function deleteAvatar() {
    if (!confirm('Удалить фото профиля?')) return;

    try {
        const res = await fetch('/api/profile/avatar', { method: 'DELETE' });
        const data = await res.json();

        if (data.success) {
            showSuccess('Аватар удален');
            document.getElementById('profile-avatar').src = data.avatar_url;
            document.getElementById('btn-delete-avatar').style.display = 'none';
        }
    } catch(e) { showError('Ошибка сети'); }
}

// --- News Section ---

function formatLocalDate(isoString) {
    if (!isoString) return '';
    try {
        const date = new Date(isoString);
        return date.toLocaleString('ru-RU', {
            day: '2-digit',
            month: 'long',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
    } catch (e) {
        return isoString;
    }
}

let allNewsCache = [];

async function loadNews() {
    const container = document.getElementById('news-container');
    try {
        const res = await fetch('/api/news');
        const data = await res.json();

        if (data.news.length === 0) {
            container.innerHTML = '<p style="text-align:center;color:#999">Новостей пока нет</p>';
            return;
        }
        allNewsCache = data.news;

        container.innerHTML = data.news.map(post => {
            const isAuthor = currentUser && (currentUser.username === post.author || currentUser.role === 'admin');

            const editBtn = isAuthor
                ? `<button class="btn-text-edit" onclick="openEditNewsModal(${post.id})" title="Редактировать">✏️</button>`
                : '';

            const rawHtml = marked.parse(post.content);
            const safeHtml = DOMPurify.sanitize(rawHtml);

            return `
            <div class="news-post">
                <div class="news-meta" style="display:flex; justify-content:space-between; align-items:center;">
                    <span><strong>${post.author}</strong> • ${formatLocalDate(post.created_at)}</span>
                    ${editBtn}
                </div>
                <!-- Добавляем класс markdown-body для красивых отступов списков и цитат -->
                <div class="news-content markdown-body">
                    ${safeHtml}
                </div>
            </div>
        `}).join('');

    } catch (e) {
        container.innerHTML = '<p>Ошибка загрузки новостей</p>';
        console.error(e);
    }
}


// Простой парсер Markdown
function simpleMarkdown(text) {
    if (!text) return '';
    let html = text
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>') // Bold
        .replace(/\*(.*?)\*/g, '<em>$1</em>') // Italic
        .replace(/^# (.*$)/gim, '<h3>$1</h3>') // Header
        .replace(/\n/g, '<br>'); // New lines
    return html;
}

let editEasyMDE = null; // Глобальная переменная для инстанса редактора редактирования

function openEditNewsModal(postId) {
    // Ищем пост в кеше
    const post = allNewsCache.find(p => p.id === postId);
    if (!post) return;

    document.getElementById('edit-news-id').value = post.id;

    const modal = document.getElementById('edit-news-modal');
    modal.style.display = 'flex';

    // Инициализируем редактор ТОЛЬКО один раз при первом открытии
    if (!editEasyMDE) {
        editEasyMDE = new EasyMDE({
            element: document.getElementById('edit-news-content'),
            spellChecker: false,
            status: false,
            toolbar: [
                "bold", "italic", "heading", "|",
                "quote", "unordered-list", "ordered-list", "|",
                "link", "image", "|",
                "preview", "side-by-side", "fullscreen", "guide"
            ],
            minHeight: "300px", // Минимальная высота
        });
    }

    // Устанавливаем текст новости в редактор
    editEasyMDE.value(post.content);

    // ВАЖНО: CodeMirror (движок редактора) некорректно рендерится, если инициализирован в скрытом блоке.
    // Нам нужно принудительно обновить его после того, как модалка стала display: flex.
    setTimeout(() => {
        editEasyMDE.codemirror.refresh();
    }, 200);
}

function closeEditNewsModal() {
    document.getElementById('edit-news-modal').style.display = 'none';
}

async function saveEditedNews() {
    const postId = document.getElementById('edit-news-id').value;

    // [FIX] Берем значение из EasyMDE, а не из textarea
    let content = '';
    if (editEasyMDE) {
        content = editEasyMDE.value();
    } else {
        // Фоллбек, если вдруг редактор не загрузился
        content = document.getElementById('edit-news-content').value;
    }

    if (!content.trim()) return showError('Текст не может быть пустым');

    try {
        const res = await fetch(`/api/news/${postId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ content })
        });

        if (res.ok) {
            showSuccess('Новость обновлена');
            closeEditNewsModal();
            loadNews(); // Перезагружаем список
        } else {
            const d = await res.json();
            showError(d.error || 'Ошибка сохранения');
        }
    } catch(e) {
        showError('Ошибка сети');
    }
}

async function createNewsPost() {
    let content = '';
    if (easyMDE) {
        content = easyMDE.value();
    } else {
        content = document.getElementById('new-post-content').value;
    }

    if (!content.trim()) return showError('Введите текст');

    try {
        const res = await fetch('/api/news', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ content })
        });
        if (res.ok) {
            // Очищаем редактор
            if (easyMDE) {
                easyMDE.value('');
            } else {
                document.getElementById('new-post-content').value = '';
            }
            showSuccess('Новость опубликована');
            loadNews();
        } else {
            showError('Ошибка публикации');
        }
    } catch(e) { showError('Ошибка сети'); }
}

// --- Rooms Section ---

async function loadRooms() {
    const container = document.getElementById('rooms-container');
    if (!container.children.length) {
        container.innerHTML = '<div class="loading-spinner"></div>'; // Можно раскомментировать
    }

    try {
        const res = await fetch('/api/rooms');
        const data = await res.json();

        if (data.rooms.length === 0) {
            container.innerHTML = '<p style="text-align:center;color:#999;grid-column:1/-1">У вас пока нет комнат</p>';
            return;
        }

        container.innerHTML = data.rooms.map(room => {
            // --- ЛОГИКА 20 СИМВОЛОВ ---
            let displayName = room.name;
            if (displayName.length > 20) {
                displayName = displayName.substring(0, 20) + '...';
            }
            // ---------------------------

            const isMine = room.is_owner;
            const ownerLabel = isMine ? 'Вы владелец' : `👑 ${room.owner_name}`;

            const actionButton = isMine
                ? `<button onclick="deleteRoom(event, '${room.uuid}')"
                     class="room-action-btn btn-delete" title="Удалить комнату">🗑️</button>`
                : `<button onclick="leaveRoom(event, '${room.uuid}')"
                     class="room-action-btn btn-leave" title="Покинуть комнату">🚪</button>`;

            return `
                <div class="room-card" onclick="location.href='/room/${room.uuid}'">
                    <div class="room-header" style="background-color: ${room.header_color}; position: relative;">
                        <h3 class="room-title" title="${room.name}">${displayName}</h3>
                        ${actionButton}
                    </div>
                    <div class="room-body">
                        <div class="room-info" style="font-size: 11px; color: var(--primary-color);">
                            ${ownerLabel}
                        </div>
                        <div class="room-info">
                            <span>👥</span>
                            <strong>${room.online_count || 0}</strong> зрителей
                        </div>
                        <div class="room-info" title="${room.now_playing || ''}">
                            <span>🎵</span>
                            <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px;">
                                ${room.now_playing || 'Нет контента'}
                            </span>
                        </div>

                        ${room.is_private ? '<span class="room-badge">🔒 Закрытая</span>' : ''}
                    </div>
                </div>
                `;
            }).join('');

    } catch (e) {
        console.error(e); // Ошибки в консоль, чтобы не спамить в интерфейс при поллинге
    }
}

async function leaveRoom(event, uuid) {
    event.stopPropagation();
    if (!confirm('Выйти из этой комнаты? Вам придется снова просить доступ.')) return;

    try {
        const res = await fetch(`/api/rooms/${uuid}/leave`, { method: 'DELETE' });
        if (res.ok) {
            showSuccess('Вы покинули комнату');
            loadRooms(); // Обновляем список
        } else {
            const d = await res.json();
            showError(d.error || 'Ошибка');
        }
    } catch(e) { showError('Ошибка сети'); }
}

async function deleteRoom(event, uuid) {
    event.stopPropagation(); // Чтобы не кликнулось по карточке
    if (!confirm('Удалить эту комнату и все видео в ней?')) return;

    try {
        const res = await fetch(`/api/rooms/${uuid}`, { method: 'DELETE' });
        if (res.ok) {
            showSuccess('Комната удалена');
            loadRooms(); // Перезагружаем список
        } else {
            showError('Ошибка удаления');
        }
    } catch(e) { showError('Ошибка сети'); }
}

// --- TG Link Logic ---

async function generateTgLink() {
    try {
        const res = await fetch('/api/tg_link');
        const data = await res.json();

        if (res.ok) {
            document.getElementById('tg-link-result').style.display = 'block';
            document.getElementById('tg-link-href').href = data.link;
            document.getElementById('tg-code-text').textContent = data.code;
        } else {
            showError(data.error);
        }
    } catch(e) { showError('Ошибка получения ссылки'); }
}

// --- Room Creation ---

function showCreateRoomModal() {
    document.getElementById('create-room-modal').style.display = 'flex';
}
function closeCreateRoomModal() {
    document.getElementById('create-room-modal').style.display = 'none';
}

async function createRoom() {
    const name = document.getElementById('room-name').value;
    const color = document.getElementById('room-color').value;
    const isPrivate = document.getElementById('room-private').checked;
    const isVoice = document.getElementById('room-voice').checked;

    if (!name) return showError('Введите название');

    try {
        const res = await fetch('/api/rooms', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                name,
                header_color: color,
                is_private: isPrivate,
                voice_chat_enabled: isVoice
            })
        });

        if (res.ok) {
            showSuccess('Комната создана');
            closeCreateRoomModal();
            document.getElementById('room-name').value = '';
            document.getElementById('room-private').checked = false;
            document.getElementById('room-voice').checked = false; // Сброс

            loadRooms();
        } else {
            const data = await res.json();
            if (res.status === 403) {
                if (data.error.includes('Wait')) {
                     showError('Создание комнат доступно после одобрения администратором.');
                } else {
                     showError('Сначала привяжите Telegram в профиле.');
                }
            } else {
                showError(data.error || 'Ошибка');
            }
        }
    } catch(e) { showError('Ошибка сети'); }
}

// --- Account ---

async function logout() {
    await fetch('/api/logout', { method: 'POST' });
    window.location.href = '/login';
}

async function deleteAccount() {
    if (!confirm('Вы уверены? Это удалит все ваши комнаты и файлы!')) return;

    try {
        const res = await fetch('/api/account', { method: 'DELETE' });
        if (res.ok) {
            alert('Аккаунт удален');
            window.location.href = '/login';
        } else {
            showError('Ошибка удаления');
        }
    } catch(e) { showError('Ошибка сети'); }
}

/* /static/js/dashboard.js */

let statsTimer = null;

// Вызываем эту функцию внутри openTab('profile') или при загрузке профиля
function initStatsPolling() {
    // Сбрасываем предыдущий таймер если был
    if (statsTimer) clearTimeout(statsTimer);
    loadStats();
}

async function loadStats() {
    const section = document.getElementById('stats-section');
    const container = document.getElementById('chart-bars');
    const totalEl = document.getElementById('stats-total-value');

    try {
        const res = await fetch('/api/me/stats');

        // Если ошибка API или 500
        if (!res.ok) throw new Error('API Error');

        const data = await res.json();

        // Показываем блок (скрыт по умолчанию)
        section.style.display = 'block';

        // 1. Рендер Итого
        const totalHours = data.total_hours;
        // Склонение: (число, ['час', 'часа', 'часов'])
        const declension = getNoun(Math.floor(totalHours), 'час', 'часа', 'часов');

        // Форматируем общее число (оставим 1 знак после запятой для точности)
        totalEl.textContent = `${totalHours} ${declension}`;

        // 2. Рендер Графика
        renderChart(container, data.history);

        // Успех: следующий опрос через 5 минут (300 сек)
        statsTimer = setTimeout(loadStats, 300000);

    } catch (e) {
        console.warn("Stats load failed, retrying in 10s...", e);
        // Ошибка: пробуем снова через 10 секунд
        statsTimer = setTimeout(loadStats, 10000);
    }
}

function renderChart(container, history) {
    if (!history || history.length === 0) {
        container.innerHTML = '<div style="width:100%;text-align:center;font-size:12px;color:#999">Нет данных</div>';
        return;
    }

    // Находим максимум для масштабирования (чтобы самый высокий столбец был 100%)
    // Берем минимум 1 час, чтобы график не скакал на мелких значениях
    const maxSeconds = Math.max(...history.map(h => h.seconds), 3600);

    // Очищаем и реверсируем (чтобы слева были старые даты, справа новые - хронология)
    // API отдает от новых к старым, поэтому .reverse()
    const sortedHistory = [...history].reverse();

    container.innerHTML = sortedHistory.map(item => {
        const hours = item.seconds / 3600;
        const heightPercent = (item.seconds / maxSeconds) * 100;

        // Логика форматирования: < 9 часов -> 8.5, >= 9 часов -> 10
        let displayVal;
        if (hours < 9) {
            // Если совсем мало (0), показываем 0, иначе до десятых
            displayVal = hours === 0 ? "0" : hours.toFixed(1);
        } else {
            displayVal = Math.round(hours);
        }

        // Формат даты: 2025-12-04 -> 04.12
        const dateObj = new Date(item.date);
        const dateStr = `${String(dateObj.getDate()).padStart(2, '0')}.${String(dateObj.getMonth() + 1).padStart(2, '0')}`;

        return `
            <div class="chart-column">
                <div class="bar-value">${displayVal > 0 ? displayVal : ''}</div>
                <div class="bar-fill" style="height: ${heightPercent}%;" title="${item.minutes} мин."></div>
                <div class="bar-date">${dateStr}</div>
            </div>
        `;
    }).join('');
}

// Хелпер для склонения (1 час, 2 часа, 5 часов)
function getNoun(number, one, two, five) {
    let n = Math.abs(number);
    n %= 100;
    if (n >= 5 && n <= 20) {
        return five;
    }
    n %= 10;
    if (n === 1) {
        return one;
    }
    if (n >= 2 && n <= 4) {
        return two;
    }
    return five;
}