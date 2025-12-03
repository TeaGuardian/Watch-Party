/* #/static/js/admin.js */
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadUsers();
});

function switchSection(sectionId) {
    document.querySelectorAll('.admin-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    document.getElementById(`section-${sectionId}`).classList.add('active');
    // Active button highlight logic is simplified here
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
    } catch(e) { console.error(e); }
}

/* --- Users --- */
async function loadUsers() {
    const tbody = document.getElementById('users-table-body');
    tbody.innerHTML = '<tr><td colspan="6">Загрузка...</td></tr>';

    try {
        const res = await fetch('/api/admin/users');
        const data = await res.json();

        tbody.innerHTML = data.users.map(user => {
            // Определяем доступные действия
            let actions = '';

            if (user.status === 'tg_verified' || user.status === 'new') {
                actions += `<button class="btn btn-xs btn-primary" onclick="changeStatus(${user.id}, 'approved')">✅ Одобрить</button>`;
            }
            if (user.status !== 'banned') {
                actions += `<button class="btn btn-xs btn-danger" onclick="changeStatus(${user.id}, 'banned')">⛔ Бан</button>`;
            } else {
                actions += `<button class="btn btn-xs btn-secondary" onclick="changeStatus(${user.id}, 'approved')">♻️ Разбан</button>`;
            }

            if (user.role === 'user') {
                actions += `<button class="btn btn-xs" onclick="changeRole(${user.id}, 'moderator')">⬆️ Mod</button>`;
            }

            actions += `<button class="btn btn-xs btn-danger" onclick="deleteUser(${user.id})">🗑️</button>`;

            return `
            <tr>
                <td>${user.id}</td>
                <td>
                    <div style="display:flex;align-items:center;gap:10px">
                        <img src="${user.avatar_url}" style="width:24px;border-radius:50%">
                        ${user.username}
                    </div>
                </td>
                <td>${user.tg_connected ? '✅' : '❌'}</td>
                <td>${user.role}</td>
                <td><span class="badge bg-${user.status.replace('_', '')}">${user.status}</span></td>
                <td class="actions-cell">${actions}</td>
            </tr>
            `;
        }).join('');

    } catch(e) {
        tbody.innerHTML = '<tr><td colspan="6" style="color:red">Ошибка загрузки</td></tr>';
    }
}

async function changeStatus(userId, newStatus) {
    if (!confirm(`Изменить статус на ${newStatus}?`)) return;

    try {
        const res = await fetch(`/api/admin/users/${userId}/status`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ status: newStatus })
        });

        if (res.ok) {
            loadUsers();
            loadStats();
        } else {
            alert('Ошибка обновления');
        }
    } catch(e) { console.error(e); }
}

async function changeRole(userId, newRole) {
    if (!confirm(`Назначить роль ${newRole}?`)) return;

    try {
        const res = await fetch(`/api/admin/users/${userId}/status`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ role: newRole })
        });
        if (res.ok) loadUsers();
    } catch(e) { console.error(e); }
}

async function deleteUser(userId) {
    if (!confirm('Удалить пользователя и ВСЕ его данные? Это необратимо.')) return;

    try {
        const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
        if (res.ok) {
            loadUsers();
            loadStats();
        } else {
            alert('Ошибка удаления');
        }
    } catch(e) { console.error(e); }
}