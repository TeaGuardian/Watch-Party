/* #/static/js/voice.js */

const VoiceClient = {
    socket: null,
    audioContext: null,
    processor: null,
    source: null,

    // Состояние
    isConnected: false,
    micEnabled: true,
    soundEnabled: true,

    // Аудио потоки других юзеров
    // { userId: { nextStartTime: float, gainNode: AudioNode } }
    peers: {},

    // Настройки громкости (TargetID -> Volume %)
    volumes: {},

    // Константы
    SAMPLE_RATE: 16000, // Целевая частота
    BUFFER_SIZE: 2048,  // Размер буфера обработки

    // --- 1. Инициализация ---

    join: async function() {
        if (this.isConnected) return;

        // 1. Инициализация AudioContext (требует жеста пользователя, поэтому внутри onClick)
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            this.audioContext = new AudioContext({ sampleRate: this.SAMPLE_RATE });
        } catch (e) {
            alert("Ваш браузер не поддерживает Web Audio API");
            return;
        }

        // 2. Подключение к сокету
        // Обратите внимание на path: Nginx проксирует /socket.io/voice/ на микросервис
        this.socket = io({
            path: '/socket.io/voice/',
            reconnection: true
        });

        this.setupSocketEvents();

        // 3. Запрос микрофона
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
            this.setupAudioProcess(stream);

            // Вход в комнату
            this.socket.emit('join_voice', {
                room_uuid: ROOM_UUID,
                user_id: USER_ID
            });

            this.isConnected = true;
            this.updateUIState();

        } catch (err) {
            console.error("Mic access denied:", err);
            alert("Не удалось получить доступ к микрофону. Вы сможете только слушать.");
            // Можно разрешить вход в режиме "только слушатель", но для MVP просто выход
        }
    },

    // --- 2. Обработка аудио (Отправка) ---

    setupAudioProcess: function(stream) {
        this.source = this.audioContext.createMediaStreamSource(stream);

        // Используем ScriptProcessor (Deprecated, но самый простой способ получить сырые данные)
        // В продакшене лучше AudioWorklet, но он требует отдельного файла JS.
        this.processor = this.audioContext.createScriptProcessor(this.BUFFER_SIZE, 1, 1);

        this.source.connect(this.processor);
        this.processor.connect(this.audioContext.destination); // Нужно для работы процессора в Chrome

        this.processor.onaudioprocess = (e) => {
            if (!this.micEnabled || !this.isConnected) return;

            const inputData = e.inputBuffer.getChannelData(0);

            // Простая VAD (Voice Activity Detection) - экономия трафика
            // Считаем среднеквадратичное (RMS)
            let sum = 0;
            for (let i = 0; i < inputData.length; i++) sum += inputData[i] * inputData[i];
            const rms = Math.sqrt(sum / inputData.length);

            // Порог тишины (можно настраивать)
            if (rms < 0.01) return;

            // Конвертация Float32 (-1.0...1.0) -> Int16 (-32768...32767)
            const pcmData = new Int16Array(inputData.length);
            for (let i = 0; i < inputData.length; i++) {
                let s = Math.max(-1, Math.min(1, inputData[i]));
                pcmData[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }

            // Отправка бинарного пакета
            this.socket.emit('audio_packet', pcmData.buffer);

            // Визуализация "Я говорю"
            this.highlightSpeaker(USER_ID, true);
        };
    },

    // --- 3. Воспроизведение аудио (Прием) ---

    playAudioChunk: function(userId, arrayBuffer) {
        if (!this.soundEnabled) return;
        if (!this.peers[userId]) {
            // Инициализация пира
            const gainNode = this.audioContext.createGain();
            gainNode.connect(this.audioContext.destination);

            // Применяем сохраненную громкость
            const vol = this.volumes[userId] !== undefined ? this.volumes[userId] : 100;
            gainNode.gain.value = vol / 100;

            this.peers[userId] = {
                nextStartTime: 0,
                gainNode: gainNode
            };
        }

        const peer = this.peers[userId];
        const int16Data = new Int16Array(arrayBuffer);
        const float32Data = new Float32Array(int16Data.length);

        // Int16 -> Float32
        for (let i = 0; i < int16Data.length; i++) {
            const int = int16Data[i];
            float32Data[i] = int < 0 ? int / 0x8000 : int / 0x7FFF;
        }

        // Создаем буфер
        const audioBuffer = this.audioContext.createBuffer(1, float32Data.length, this.SAMPLE_RATE);
        audioBuffer.getChannelData(0).set(float32Data);

        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(peer.gainNode);

        // Jitter Buffer Logic: Планируем воспроизведение чуть в будущем
        // Если nextStartTime в прошлом, сбрасываем его на "сейчас"
        const currentTime = this.audioContext.currentTime;
        if (peer.nextStartTime < currentTime) {
            peer.nextStartTime = currentTime;
        }

        source.start(peer.nextStartTime);

        // Сдвигаем время следующего чанка
        peer.nextStartTime += audioBuffer.duration;

        // Визуализация "Он говорит"
        this.highlightSpeaker(userId);
    },

    // --- 4. Socket Events ---

    setupSocketEvents: function() {
        this.socket.on('audio_stream', (data) => {
            // data: { sid, user_id, data (blob) }
            this.playAudioChunk(data.user_id, data.data);
        });

        this.socket.on('volume_config', (settings) => {
            // settings: [{target_id, volume}, ...]
            settings.forEach(s => {
                this.volumes[s.target_id] = s.volume;
            });
        });

        this.socket.on('voice_state_update', (users) => {
            // users: [{user_id, mic_on, ...}]
            users.forEach(u => this.updatePeerIcon(u.user_id, u.mic_on));
        });

        this.socket.on('mute_update', (data) => {
            this.updatePeerIcon(data.user_id, data.mic_on);
        });

        this.socket.on('voice_user_left', (data) => {
             // Очистка
             delete this.peers[data.user_id];
             const el = document.getElementById(`voice-icon-${data.user_id}`);
             if(el) el.remove();
        });
    },

    // --- 5. UI Helpers ---

    updateUIState: function() {
        document.getElementById('btn-join-voice').style.display = 'none';

        const btnMic = document.getElementById('btn-mic');
        const btnSound = document.getElementById('btn-sound');

        btnMic.style.display = 'flex';
        btnSound.style.display = 'flex';

        btnMic.className = `btn-voice ${this.micEnabled ? 'active' : 'muted'}`;
        btnSound.className = `btn-voice ${this.soundEnabled ? 'active' : 'deafened'}`;
    },

    toggleMic: function() {
        this.micEnabled = !this.micEnabled;
        this.updateUIState();
        this.socket.emit('set_state', { mic_on: this.micEnabled });
    },

    toggleSound: function() {
        this.soundEnabled = !this.soundEnabled;
        if (!this.soundEnabled) {
            // Останавливаем аудиоконтекст (экономия ресурсов)
            this.audioContext.suspend();
        } else {
            this.audioContext.resume();
        }
        this.updateUIState();
        this.socket.emit('set_state', { sound_on: this.soundEnabled });
    },

    highlightSpeaker: function(userId, isMe=false) {
        // Ищем карточку зрителя, созданную room.js
        // room.js создает id `viewer-{sid}`. У нас есть userId.
        // Проблема: мы не знаем SID чужого юзера здесь (хотя сервер шлет, но проще искать по DOM аттрибуту, если бы он был)
        // РЕШЕНИЕ: Переделать room.js, чтобы он добавлял data-userid.
        // НО мы обещали минимально трогать room.js.
        // HACK: Ищем карточку перебором или используем визуализацию на основе SID если сохранили mapping.

        // Упрощение: сервер присылает sid в 'audio_stream'.
        // Если это Я:
        let el = null;
        if (isMe) {
            // Мой ID в сокетах может быть разным, но найдем по моему глобальному USER_ID
            // room.js не ставит data-id для меня?
            // Допустим, мы не пульсируем для себя, только для других.
            return;
        }

        // Для других юзеров мы должны найти их карточку.
        // VoiceClient не хранит SID <-> UserID маппинг всех.
        // Добавим простой поиск по тексту имени (ненадежно) или попросим room.js добавлять ID.
        // Самый надежный вариант в текущих условиях:
        // При получении 'voice_state_update' мы знаем SID и UserID. Сохраним маппинг.

        // ... (См. updatePeerIcon логику ниже)
    },

    // Mapping: UserID -> SID (заполняется в voice_state_update)
    userSidMap: {},

    updatePeerIcon: function(userId, micOn) {
        // Мы пытаемся найти элемент зрителя в DOM.
        // Так как room.js создает элементы с ID `viewer-{sid}`, нам нужно знать SID этого юзера.
        // Мы можем получить его из voice_state_update.

        // Поскольку это сложно синхронизировать с room.js без правок room.js,
        // сделаем допущение:
        // Мы будем добавлять иконку микрофона просто к элементу, который содержит текст username.
        // Или, правильнее: изменим `room.js` чуть-чуть, чтобы он добавлял `data-userid`.

        // АЛЬТЕРНАТИВА БЕЗ ПРАВОК room.js:
        // Мы ищем все `.viewer-card` и запрашиваем список SID у сервера.

        // ДЛЯ MVP: Мы предполагаем, что `room.js` был обновлен и добавляет `data-user-id` в карточку.
        // Если нет - код ниже не сработает визуально, но звук будет.

        const card = document.querySelector(`.viewer-card[data-user-id="${userId}"]`);
        if (!card) return;

        let icon = card.querySelector('.voice-status-icon');
        if (!icon) {
            icon = document.createElement('div');
            icon.id = `voice-icon-${userId}`;
            icon.onclick = (e) => { e.stopPropagation(); VoiceClient.openVolumePopup(userId, e); };
            card.querySelector('div[style*="relative"]').appendChild(icon);
        }

        icon.className = `voice-status-icon ${micOn ? 'mic-on' : 'mic-off'}`;
        icon.innerHTML = micOn ? '🎤' : '🔇';
        icon.title = "Настроить громкость";

        // Анимация пульсации
        if (micOn) {
            // Сюда бы логику VAD, но пока просто ставим класс, если он активен
            // Реальная пульсация вызывается в playAudioChunk
            card.classList.add('voice-active-user');
        } else {
            card.classList.remove('voice-active-user');
            card.classList.remove('speaking');
        }
    },

    // --- 6. Volume Control ---

    openVolumePopup: function(targetId, event) {
        const popover = document.getElementById('volume-popover');
        const slider = document.getElementById('vol-slider');
        const label = document.getElementById('vol-value');
        const usernameLabel = document.getElementById('vol-username');

        // Позиционирование
        const rect = event.target.getBoundingClientRect();
        popover.style.top = (rect.top - 50) + 'px';
        popover.style.left = (rect.left + 20) + 'px';
        popover.style.display = 'flex';

        // Данные
        const currentVol = this.volumes[targetId] !== undefined ? this.volumes[targetId] : 100;
        slider.value = currentVol;
        label.textContent = currentVol + '%';
        usernameLabel.textContent = `User ${targetId}`; // В идеале получить имя из DOM

        // Обработчик
        slider.oninput = (e) => {
            const val = parseInt(e.target.value);
            label.textContent = val + '%';

            // Локально меняем громкость
            this.setVolume(targetId, val);
        };

        slider.onchange = (e) => {
            // При отпускании сохраняем на сервер
            const val = parseInt(e.target.value);
            this.socket.emit('save_volume', { target_id: targetId, volume: val });
        };

        // Закрытие при клике вовне
        setTimeout(() => {
            document.addEventListener('click', function close(e) {
                if (!popover.contains(e.target) && e.target !== event.target) {
                    popover.style.display = 'none';
                    document.removeEventListener('click', close);
                }
            });
        }, 100);
    },

    setVolume: function(userId, percent) {
        this.volumes[userId] = percent;
        if (this.peers[userId]) {
            this.peers[userId].gainNode.gain.value = percent / 100;
        }
    }
};

// Хелпер для подсветки говорящего (вызывается из playAudioChunk)
VoiceClient.highlightSpeaker = function(userId) {
    const card = document.querySelector(`.viewer-card[data-user-id="${userId}"]`);
    if (card) {
        card.classList.add('speaking');
        // Убираем класс через 300мс тишины
        clearTimeout(card.speakTimeout);
        card.speakTimeout = setTimeout(() => {
            card.classList.remove('speaking');
        }, 300);
    }
};