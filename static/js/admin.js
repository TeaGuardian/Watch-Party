/* #/static/js/admin.js */
let currentModalUserId = null;

document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadUsers();
});

function switchSection(sectionId) {
    document.querySelectorAll('.admin-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    document.getElementById(`section-${sectionId}`).classList.add('active');
    // Добавьте логику подсветки активной кнопки навигации, если нужно
}

/* --- Stats --- */
async function loadStats() {
    try {
        const res = await fetch('/api/admin/stats');
        const data = await res.json();

        document.getElementById('stat-users').textContent = data.users_total;
        document.getElementById('stat-new').textContent = data.users_new;
        document.getElementById('stat-rooms').textContent = data.rooms_total;
        document.getElementById('stat-videos').textContent = data.videos_total;

        // Новые метрики
        document.getElementById('stat-hours').textContent = data.total_watch_hours;
        document.getElementById('stat-storage').textContent = data.storage_used_gb;
    } catch(e) { console.error(e); }
}

/* --- Users List --- */
async function loadUsers() {
    const tbody = document.getElementById('users-table-body');
    tbody.innerHTML = '<tr><td colspan="7">Загрузка...</td></tr>';

    try {
        const res = await fetch('/api/admin/users');
        const data = await res.json();

        tbody.innerHTML = data.users.map(user => {
            // Кнопки действий (сокращенные)
            let actions = '';

            // Если статус требует внимания
            if (user.status === 'tg_verified' || user.status === 'new') {
                actions += `<button class="btn btn-xs btn-primary" onclick="quickAction(${user.id}, 'approved')">✅</button>`;
            }
            // Бан/Разбан
            if (user.status !== 'banned') {
                actions += `<button class="btn btn-xs btn-danger" onclick="quickAction(${user.id}, 'banned')">⛔</button>`;
            } else {
                actions += `<button class="btn btn-xs btn-secondary" onclick="quickAction(${user.id}, 'approved')">♻️</button>`;
            }

            // Открыть модалку (клик по строке или кнопке)
            return `
            <tr style="cursor:pointer" onclick="openUserDetails(${user.id})">
                <td>${user.id}</td>
                <td>
                    <div style="display:flex;align-items:center;gap:10px">
                        <img src="${user.avatar_url}" style="width:24px;border-radius:50%">
                        ${user.username}
                    </div>
                </td>
                <td>${user.tg_connected ? '✅' : '❌'}</td>
                <td><strong>${user.total_hours}</strong></td>
                <td>${user.role}</td>
                <td><span class="badge bg-${user.status.replace('_', '')}">${user.status}</span></td>
                <td class="actions-cell" onclick="event.stopPropagation()">
                    ${actions}
                </td>
            </tr>
            `;
        }).join('');

    } catch(e) {
        tbody.innerHTML = '<tr><td colspan="7" style="color:red">Ошибка загрузки</td></tr>';
    }
}

async function quickAction(userId, status) {
    if (!confirm(`Изменить статус на ${status}?`)) return;
    await updateStatus(userId, status);
    loadUsers();
    loadStats();
}

async function updateStatus(userId, status) {
    try {
        await fetch(`/api/admin/users/${userId}/status`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ status: status })
        });
    } catch(e) { console.error(e); }
}

/* --- User Details Modal --- */

async function openUserDetails(userId) {
    currentModalUserId = userId;
    const modal = document.getElementById('user-details-modal');
    modal.style.display = 'flex';

    // Загрузка данных
    try {
        const res = await fetch(`/api/admin/users/${userId}/details`);
        if(!res.ok) throw new Error("Failed");
        const data = await res.json();
        const u = data.user;

        // Заполнение инфо
        document.getElementById('modal-username').textContent = u.username;
        document.getElementById('modal-avatar').src = u.avatar_url;
        document.getElementById('modal-tg').textContent = u.tg_connected ? `@${u.tg_username}` : 'Нет';
        document.getElementById('modal-rooms-count').textContent = data.rooms_count;
        document.getElementById('modal-videos-count').textContent = data.videos_count;
        document.getElementById('modal-approved-by').textContent = data.approved_by || '-';

        // Статус бейджи
        const tags = document.getElementById('modal-status-tags');
        tags.innerHTML = `
            <span class="badge bg-${u.status.replace('_', '')}">${u.status}</span>
            <span class="badge" style="background:#eee;color:#333">${u.role}</span>
        `;

        // Выбор роли
        document.getElementById('modal-role-select').value = u.role;

        // Кнопки управления
        const btns = document.getElementById('modal-action-buttons');
        btns.innerHTML = '';

        if (u.status !== 'approved' && u.status !== 'banned') {
            btns.innerHTML += `<button class="btn btn-primary" onclick="modalAction('approved')">Одобрить доступ</button>`;
        }
        if (u.status === 'banned') {
            btns.innerHTML += `<button class="btn btn-secondary" onclick="modalAction('approved')">Разбанить</button>`;
        } else {
            btns.innerHTML += `<button class="btn btn-danger" onclick="modalAction('banned')">Забанить</button>`;
        }
        btns.innerHTML += `<button class="btn btn-danger" style="margin-left:auto" onclick="deleteUser(${u.id})">Удалить аккаунт</button>`;

        // Рендер графика
        renderAdminChart(data.history);

    } catch(e) {
        alert("Ошибка загрузки данных пользователя");
        closeUserModal();
    }
}

function closeUserModal() {
    document.getElementById('user-details-modal').style.display = 'none';
    currentModalUserId = null;
}

async function modalAction(status) {
    if(!currentModalUserId) return;
    await updateStatus(currentModalUserId, status);
    openUserDetails(currentModalUserId); // Обновить модалку
    loadUsers(); // Обновить таблицу на фоне
}

async function saveUserRole() {
    if(!currentModalUserId) return;
    const role = document.getElementById('modal-role-select').value;

    if(!confirm(`Изменить роль на ${role}?`)) return;

    try {
        const res = await fetch(`/api/admin/users/${currentModalUserId}/status`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ role: role })
        });
        if(res.ok) {
            alert('Роль обновлена');
            openUserDetails(currentModalUserId);
            loadUsers();
        }
    } catch(e) { alert('Ошибка'); }
}

async function deleteUser(userId) {
    if (!confirm('Удалить пользователя БЕЗВОЗВРАТНО?')) return;
    try {
        const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
        if(res.ok) {
            closeUserModal();
            loadUsers();
            loadStats();
        }
    } catch(e) { alert('Ошибка удаления'); }
}

/* --- Chart Logic (Reused from Dashboard) --- */
function renderAdminChart(history) {
    const container = document.getElementById('admin-chart-bars');
    if (!history || history.length === 0) {
        container.innerHTML = '<div style="width:100%;text-align:center;color:#999;padding-top:50px">Нет данных</div>';
        return;
    }

    const maxSeconds = Math.max(...history.map(h => h.seconds), 3600);
    const sortedHistory = [...history].reverse(); // Чтобы хронология слева направо

    container.innerHTML = sortedHistory.map(item => {
        const heightPercent = (item.seconds / maxSeconds) * 100;
        const displayVal = item.hours > 0 ? item.hours : '';
        const dateObj = new Date(item.date);
        const dateStr = `${String(dateObj.getDate()).padStart(2, '0')}.${String(dateObj.getMonth() + 1).padStart(2, '0')}`;

        return `
            <div class="chart-column">
                <div class="bar-value">${displayVal}</div>
                <div class="bar-fill" style="height: ${heightPercent}%;" title="${Math.round(item.seconds/60)} мин."></div>
                <div class="bar-date">${dateStr}</div>
            </div>
        `;
    }).join('');
}