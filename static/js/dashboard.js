/* #/static/js/dashboard.js */
let currentUser = null;
let roomsPollInterval = null; // Для автообновления
let cropper = null;
let easyMDE = null;

// --- Инициализация ---
document.addEventListener('DOMContentLoaded', async () => {
    await loadUserProfile();

    // Если профиль загрузился, открываем первую вкладку или показываем блок ограничения
    if (currentUser) {
        if (canAccessContent()) {
            openTab('news'); // По умолчанию новости
        } else {
            openTab('profile'); // Иначе профиль
        }
        if (currentUser.role === 'admin') {
            initMarkdownEditor();
        }
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
        }

    } catch (e) {
        console.error("Auth check failed", e);
    }
}

function canAccessContent() {
    return currentUser && (currentUser.status === 'approved' || currentUser.role === 'admin');
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

async function loadNews() {
    const container = document.getElementById('news-container');
    try {
        const res = await fetch('/api/news');
        const data = await res.json();

        if (data.news.length === 0) {
            container.innerHTML = '<p style="text-align:center;color:#999">Новостей пока нет</p>';
            return;
        }

        // [NEW] Используем marked.js и DOMPurify
        container.innerHTML = data.news.map(post => {
            // Парсим Markdown в HTML
            const rawHtml = marked.parse(post.content);
            // Очищаем от скриптов (XSS защита)
            const safeHtml = DOMPurify.sanitize(rawHtml);

            return `
            <div class="news-post">
                <div class="news-meta">
                    <strong>${post.author}</strong> • ${post.created_at}
                </div>
                <div class="news-content markdown-body">
                    ${safeHtml}
                </div>
            </div>
            `;
        }).join('');

    } catch (e) {
        console.error(e);
        container.innerHTML = '<p>Ошибка загрузки новостей</p>';
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

    if (!name) return showError('Введите название');

    try {
        const res = await fetch('/api/rooms', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name, header_color: color, is_private: isPrivate })
        });

        if (res.ok) {
            showSuccess('Комната создана');
            closeCreateRoomModal();
            loadRooms();
        } else {
            const data = await res.json();
            showError(data.error || 'Ошибка');
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