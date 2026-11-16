"""Shared Redis connection setup for local and hosted environments."""

import os

import redis


def create_redis_client(
    redis_url=None,
    *,
    socket_connect_timeout=3,
    socket_timeout=None,
):
    """Create a decoded redis-py client using REDIS_URL or local settings.

    A ``rediss://`` URL enables TLS automatically. For local development,
    REDIS_HOST / REDIS_PORT / REDIS_PASSWORD can be used; defaults target
    the user's local Redis instance.
    """
    redis_url = redis_url or os.getenv("REDIS_URL")
    options = {
        "decode_responses": True,
        "socket_connect_timeout": socket_connect_timeout,
    }
    if socket_timeout is not None:
        options["socket_timeout"] = socket_timeout

    if redis_url:
        return redis.Redis.from_url(redis_url, **options)

    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        **options,
    )
