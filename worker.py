import logging
import os
import socket
import time
import uuid

import redis
from redis.exceptions import ResponseError
from redis_config import create_redis_client

STREAM_KEY = "order:events"
NOTIFICATION_STREAM_KEY = "notification:events"
DEAD_LETTER_STREAM_KEY = "order:events:dead_letter"
GROUP_NAME = "order-notification-workers"
RETRY_HASH_KEY = "worker:notifications:retry_counts"
HEARTBEAT_PREFIX = "worker:notifications:heartbeat"
PROCESSED_COUNT_KEY = "worker:notifications:processed"
DEAD_LETTER_COUNT_KEY = "worker:notifications:dead_letter_count"
PENDING_IDLE_MS = 30_000
MAX_ATTEMPTS = 3

CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
HEARTBEAT_KEY = f"{HEARTBEAT_PREFIX}:{CONSUMER_NAME}"


def create_consumer_group(db):
    try:
        # Start at '$': historical demo events remain in the audit stream but
        # are not replayed as fresh notifications on the first worker launch.
        db.xgroup_create(STREAM_KEY, GROUP_NAME, id="$", mkstream=True)
        logging.info("Created Stream consumer group %s", GROUP_NAME)
    except ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def publish_processed_event(db, event_id, fields):
    product_id = fields.get("product_id")
    if not product_id:
        raise ValueError("事件缺少 product_id")

    event = fields.get("event", "unknown")
    parts = [f"event={event}"]
    for field in ("order_id", "status", "stock_after", "quantity"):
        if fields.get(field) is not None:
            parts.append(f"{field}={fields[field]}")
    message = "; ".join(parts)
    processed_at = str(int(time.time()))
    notification = {
        **fields,
        "source_event_id": event_id,
        "source_stream": STREAM_KEY,
        "consumer": CONSUMER_NAME,
        "processed_at": processed_at,
    }

    # Store the durable notification, publish the live message, update worker
    # metrics, and acknowledge the source event as one Redis transaction.
    with db.pipeline(transaction=True) as pipe:
        pipe.xadd(NOTIFICATION_STREAM_KEY, notification)
        pipe.publish(f"product:{product_id}:updates", message)
        pipe.incr(PROCESSED_COUNT_KEY)
        pipe.set(HEARTBEAT_KEY, processed_at, ex=20)
        pipe.hdel(RETRY_HASH_KEY, event_id)
        pipe.xack(STREAM_KEY, GROUP_NAME, event_id)
        pipe.execute()


def dead_letter_event(db, event_id, fields, error):
    dead_letter = {
        **fields,
        "source_event_id": event_id,
        "error": str(error)[:300],
        "failed_at": str(int(time.time())),
        "attempts": str(MAX_ATTEMPTS),
    }
    with db.pipeline(transaction=True) as pipe:
        pipe.xadd(DEAD_LETTER_STREAM_KEY, dead_letter)
        pipe.hdel(RETRY_HASH_KEY, event_id)
        pipe.incr(DEAD_LETTER_COUNT_KEY)
        pipe.xack(STREAM_KEY, GROUP_NAME, event_id)
        pipe.execute()
    logging.error("Moved event %s to dead-letter stream", event_id)


def handle_event(db, event_id, fields):
    try:
        publish_processed_event(db, event_id, fields)
        logging.info("Processed %s (%s)", event_id, fields.get("event", "unknown"))
    except Exception as error:
        attempts = db.hincrby(RETRY_HASH_KEY, event_id, 1)
        logging.exception("Failed to process %s (attempt %s)", event_id, attempts)
        if attempts >= MAX_ATTEMPTS:
            dead_letter_event(db, event_id, fields, error)


def reclaim_stale_events(db):
    # XPENDING/XCLAIM work on Redis 5+, including Ubuntu packages that do not
    # provide the newer XAUTOCLAIM command.
    pending = db.xpending_range(
        STREAM_KEY,
        GROUP_NAME,
        min="-",
        max="+",
        count=25,
    )
    for item in pending:
        event_id = item["message_id"]
        idle_ms = item["time_since_delivered"]
        if int(idle_ms) < PENDING_IDLE_MS:
            continue
        claimed = db.xclaim(
            STREAM_KEY,
            GROUP_NAME,
            CONSUMER_NAME,
            PENDING_IDLE_MS,
            [event_id],
        )
        for claimed_id, fields in claimed:
            handle_event(db, claimed_id, fields)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    db = create_redis_client(socket_connect_timeout=3, socket_timeout=10)
    db.ping()
    create_consumer_group(db)
    logging.info("Worker %s is listening to %s", CONSUMER_NAME, STREAM_KEY)

    try:
        while True:
            db.set(HEARTBEAT_KEY, str(int(time.time())), ex=20)
            reclaim_stale_events(db)
            batches = db.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {STREAM_KEY: ">"},
                count=10,
                block=5000,
            )
            for _stream, entries in batches:
                for event_id, fields in entries:
                    handle_event(db, event_id, fields)
    except KeyboardInterrupt:
        logging.info("Worker stopped by user")
    finally:
        db.delete(HEARTBEAT_KEY)
        db.close()


if __name__ == "__main__":
    main()
