#!/bin/bash

# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================
# Укажите ваши домены
domains=(w2sys.su)
email="grammarline@yandex.ru"
staging=1  # Начните с тестового режима
rsa_key_size=4096
data_path="./certbot"

# Пути к конфигам (важно!)
current_dir="$(pwd)"
nginx_conf_dir="$current_dir/startup_scripts"
original_nginx_conf="$current_dir/startup_scripts/nginx.conf.original"
temp_nginx_conf="$current_dir/startup_scripts/nginx.conf"

# ==============================================================================
# СКРИПТ
# ==============================================================================

echo "### Проверка структуры директорий..."
echo "Текущая директория: $current_dir"
echo "Директория nginx конфигов: $nginx_conf_dir"
echo "Оригинальный конфиг: $original_nginx_conf"

# Создаем резервную копию оригинального конфига
if [ -f "$temp_nginx_conf" ] && [ ! -f "$original_nginx_conf" ]; then
    echo "### Создание резервной копии оригинального конфига nginx..."
    cp "$temp_nginx_conf" "$original_nginx_conf"
fi

echo "### Остановка всех контейнеров..."
docker compose down

echo "### Очистка старых сертификатов..."
sudo rm -rf "$data_path"

echo "### Создание необходимых директорий..."
mkdir -p "$data_path/conf"
mkdir -p "$data_path/www"
mkdir -p "$data_path/logs"

echo "### Скачивание рекомендованных параметров TLS..."
if [ ! -e "$data_path/conf/options-ssl-nginx.conf" ]; then
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf > "$data_path/conf/options-ssl-nginx.conf"
fi
if [ ! -e "$data_path/conf/ssl-dhparams.pem" ]; then
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem > "$data_path/conf/ssl-dhparams.pem"
fi

echo "### Создание временного конфига nginx для подтверждения домена..."
cat > /tmp/nginx-temp.conf << 'EOF'
client_max_body_size 2000M;

upstream web_upstream {
    server web:8000;
}

upstream voice_upstream {
    server voice_service:8001;
}

server {
    listen 80;
    server_name w2sys.su www.w2sys.su;

    # Важно: Certbot будет сюда класть challenge файлы
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
        try_files $uri =404;
    }

    # Временно отключаем редирект на HTTPS
    location / {
        return 404;
    }
}
EOF

echo "### Копирование временного конфига в рабочую директорию..."
cp /tmp/nginx-temp.conf "$temp_nginx_conf"

echo "### Запуск только nginx для подтверждения домена..."
docker compose up -d nginx

echo "### Ожидание запуска nginx..."
sleep 10

# Проверяем, что nginx запустился
if ! docker compose ps | grep nginx | grep -q "Up"; then
    echo "Ошибка: nginx не запустился"
    docker compose logs nginx
    exit 1
fi

echo "### Проверка, что nginx слушает порт 80..."
docker compose exec nginx netstat -tulpn | grep :80 || echo "Порт 80 не слушается"

echo "### Запрос сертификата Let's Encrypt..."

# Собираем аргументы доменов
domain_args=""
for domain in "${domains[@]}"; do
    domain_args="$domain_args -d $domain"
done

# Выбираем сервер
staging_arg=""
if [ $staging -eq 1 ]; then
    staging_arg="--staging"
    echo "Используется ТЕСТОВЫЙ режим (staging). Сертификаты не настоящие!"
else
    echo "Используется БОЕВОЙ режим."
fi

echo "### Получение сертификата через docker-compose run..."
# Используем docker-compose run для certbot
docker compose run --rm --entrypoint "\
  certbot certonly --webroot \
    -w /var/www/certbot \
    $staging_arg \
    $domain_args \
    --email $email \
    --rsa-key-size $rsa_key_size \
    --agree-tos \
    --non-interactive \
    --verbose" certbot

certbot_result=$?

if [ $certbot_result -eq 0 ]; then
    echo "### Сертификат успешно получен!"

    echo "### Проверка полученных сертификатов..."
    if [ -d "$data_path/conf/live/w2sys.su" ]; then
        echo "Сертификаты найдены в: $data_path/conf/live/w2sys.su"
        ls -la "$data_path/conf/live/w2sys.su/"
    else
        echo "Внимание: сертификаты не найдены в ожидаемом месте"
        # Проверяем альтернативные пути
        find "$data_path" -name "*.pem" -type f | head -5
    fi

    echo "### Остановка временного nginx..."
    docker compose down

    echo "### Восстановление оригинального конфига nginx..."
    if [ -f "$original_nginx_conf" ]; then
        cp "$original_nginx_conf" "$temp_nginx_conf"
        echo "Оригинальный конфиг восстановлен"
    else
        echo "Внимание: оригинальный конфиг не найден, создаем из шаблона..."
        # Создаем конфиг на основе оригинального из вашего вопроса
        cat > "$temp_nginx_conf" << 'EOF'
# Лимиты и настройки
client_max_body_size 2000M;

upstream web_upstream {
    server web:8000;
}

upstream voice_upstream {
    server voice_service:8001;
}

# 1. HTTP -> HTTPS Redirect
server {
    listen 80;
    server_name w2sys.su www.w2sys.su;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# 2. HTTPS Server
server {
    listen 443 ssl;
    http2 on;
    server_name w2sys.su www.w2sys.su;

    # Пути к сертификатам
    ssl_certificate /etc/letsencrypt/live/w2sys.su/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/w2sys.su/privkey.pem;

    # Рекомендованные настройки SSL
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # --- MAIN WEB APP ---
    location / {
        proxy_pass http://web_upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /socket.io/ {
        proxy_pass http://web_upstream/socket.io/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /socket.io/voice/ {
        proxy_pass http://voice_upstream/socket.io/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /content/ {
        proxy_pass http://web_upstream/content/;
        proxy_set_header Host $host;
    }
}
EOF
    fi

    echo "### Запуск всех сервисов..."
    docker compose up -d

    echo "### Ожидание запуска сервисов..."
    sleep 10

    echo "### Проверка статуса контейнеров..."
    docker compose ps

    echo "### Проверка доступности HTTPS..."
    echo "Тестирование w2sys.su:"
    curl -I https://w2sys.su/ 2>/dev/null || echo "HTTPS пока недоступен, это нормально в течение нескольких минут"

    echo "### Логи nginx для проверки SSL..."
    docker compose logs nginx --tail=20

    if [ $staging -eq 1 ]; then
        echo "##############################################"
        echo "### ТЕСТОВЫЙ РЕЖИМ УСПЕШЕН! ###"
        echo "### Теперь поменяйте staging=0 в скрипте и ###"
        echo "### запустите его снова для реальных сертификатов ###"
        echo "##############################################"
    fi

else
    echo "### Ошибка при получении сертификата (код: $certbot_result)"
    echo "### Логи certbot:"
    docker compose logs certbot --tail=20 2>/dev/null || echo "Нет логов certbot"

    echo "### Попробуйте вручную проверить:"
    echo "1. docker compose exec nginx cat /etc/nginx/conf.d/default.conf"
    echo "2. docker compose exec nginx nginx -t"
    echo "3. Проверьте, что папка /var/www/certbot существует в контейнере nginx"
fi