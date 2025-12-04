#/core/cache.py
import redis
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import RedisConfig

# Инициализируем соединение с Redis
# decode_responses=True позволяет получать строки, а не байты
cache_client = redis.Redis.from_url(RedisConfig.URL, decode_responses=True)

def get_cache(key: str):
    try:
        return cache_client.get(key)
    except Exception as e:
        print(f"Redis get error: {e}")
        return None

def set_cache(key: str, value: str, ttl: int = 3600):
    try:
        cache_client.setex(key, ttl, value)
    except Exception as e:
        print(f"Redis set error: {e}")