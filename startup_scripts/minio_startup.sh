#!/bin/sh
#/startup_scripts/minio_startup.sh
# Ждем запуска MinIO
until mc alias set myminio http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; do
  echo "Waiting for MinIO..."
  sleep 2
done

# Создаем бакет
if ! mc ls myminio/"$S3_BUCKET_NAME" > /dev/null 2>&1; then
  echo "Creating bucket $S3_BUCKET_NAME..."
  mc mb myminio/"$S3_BUCKET_NAME"
  # Делаем бакет публичным для чтения (чтобы работали прямые ссылки на аватарки/видео)
  mc anonymous set download myminio/"$S3_BUCKET_NAME"
fi

# Создаем сервисного пользователя для приложения
if ! mc admin user svcacct list myminio "$S3_ACCESS_KEY" > /dev/null 2>&1; then
  echo "Creating access key for app..."
  mc admin user svcacct add myminio "$MINIO_ROOT_USER" \
    --access-key "$S3_ACCESS_KEY" \
    --secret-key "$S3_SECRET_KEY"
fi

echo "MinIO setup complete."