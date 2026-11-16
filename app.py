import os
import time
import uuid
from datetime import datetime
from html import escape as escape_html

import redis
import streamlit as st
from redis.exceptions import RedisError, WatchError
from import_open_data import main as import_open_data_catalog
from redis_config import create_redis_client

PRODUCT_ID = "P003"
INTERNAL_PRODUCT_IDS = {"P003"}
DISPLAY_NAMES_ZH = {
    "P004": "能多益榛果可可醬",
    "P005": "可口可樂原味",
    "P006": "LU Prince 巧克力餅乾",
    "P007": "100% 柳橙汁",
    "P008": "優格",
    "P009": "吐司",
    "P010": "早餐麥片",
    "P011": "洋芋片",
}
PAYMENT_HOLD_SECONDS = 300
RESTOCK_POLL_SECONDS = 5
ORDER_INDEX_VERSION = "1"
IDEMPOTENCY_TTL_SECONDS = 86_400

PLACE_ORDER_LUA = r"""
local cached = redis.call('GET', KEYS[12])
if cached then
    local result = cjson.decode(cached)
    return {
        'DUPLICATE', result.order_id, result.status,
        tostring(result.stock_after), tostring(result.queue_position)
    }
end

if redis.call('EXISTS', KEYS[1]) == 0 then
    return {'NO_PRODUCT'}
end

local product_id = ARGV[1]
local order_id = ARGV[2]
local user_id = ARGV[3]
local quantity = tonumber(ARGV[4])
local cart_id = ARGV[5]
local now = tonumber(ARGV[6])
local hold_seconds = tonumber(ARGV[7])
local idempotency_ttl = tonumber(ARGV[8])

local stock = tonumber(redis.call('HGET', KEYS[1], 'stock') or '0')
local user_limit = tonumber(redis.call('HGET', KEYS[1], 'limit_per_user') or '1')
local user_quantity = tonumber(redis.call('GET', KEYS[3]) or '0')
local queue_length = redis.call('LLEN', KEYS[2])

if not quantity or quantity < 1 then
    return {'INVALID_QUANTITY'}
end
if user_quantity + quantity > user_limit then
    return {'LIMIT', tostring(user_quantity), tostring(user_limit)}
end

local can_buy_now = stock >= quantity and queue_length == 0
local status = can_buy_now and 'PENDING_PAYMENT' or 'WAITLISTED'
local event_name = can_buy_now and 'order_created' or 'order_waitlisted'
local stock_after = can_buy_now and (stock - quantity) or stock
local queue_position = can_buy_now and 0 or (queue_length + 1)
local unit_price = tonumber(redis.call('HGET', KEYS[1], 'price') or '0')
local total_price = unit_price * quantity

redis.call('INCRBY', KEYS[3], quantity)
local payment_deadline = ''
if can_buy_now then
    payment_deadline = tostring(now + hold_seconds)
    redis.call('HINCRBY', KEYS[1], 'stock', -quantity)
    redis.call('ZADD', KEYS[5], payment_deadline, order_id)
else
    redis.call('RPUSH', KEYS[2], order_id)
end

redis.call(
    'HSET', KEYS[4],
    'order_id', order_id,
    'user_id', user_id,
    'product_id', product_id,
    'quantity', tostring(quantity),
    'unit_price', tostring(unit_price),
    'total_price', tostring(total_price),
    'status', status,
    'created_at', tostring(now),
    'cart_id', cart_id
)
if can_buy_now then
    redis.call('HSET', KEYS[4], 'payment_deadline', payment_deadline)
end

redis.call('SADD', KEYS[7], order_id)
if can_buy_now then
    redis.call('SADD', KEYS[8], order_id)
else
    redis.call('SADD', KEYS[9], order_id)
end
redis.call('SADD', KEYS[10], order_id)
redis.call(
    'XADD', KEYS[11], '*',
    'event', event_name,
    'order_id', order_id,
    'product_id', product_id,
    'user_id', user_id,
    'quantity', tostring(quantity),
    'status', status,
    'stock_after', tostring(stock_after),
    'cart_id', cart_id
)

local restock_threshold = tonumber(redis.call('HGET', KEYS[1], 'restock_threshold') or '1')
local restock_delay = tonumber(redis.call('HGET', KEYS[1], 'restock_delay_seconds') or '15')
local restock_batch = redis.call('HGET', KEYS[1], 'restock_batch') or '5'
if can_buy_now and stock_after <= restock_threshold and not redis.call('ZSCORE', KEYS[6], product_id) then
    local due_at = now + restock_delay
    redis.call('ZADD', KEYS[6], due_at, product_id)
    redis.call(
        'XADD', KEYS[11], '*',
        'event', 'restock_scheduled',
        'product_id', product_id,
        'quantity', restock_batch,
        'due_at', tostring(due_at),
        'stock_after', tostring(stock_after),
        'cart_id', cart_id
    )
end

redis.call('HSET', KEYS[14], 'cart_id', cart_id, 'user_id', user_id)
redis.call('HSETNX', KEYS[14], 'created_at', tostring(now))
redis.call('RPUSH', KEYS[13], order_id)
local result = {
    order_id = order_id,
    status = status,
    stock_after = stock_after,
    queue_position = queue_position
}
redis.call('SET', KEYS[12], cjson.encode(result), 'EX', idempotency_ttl)

return {'CREATED', order_id, status, tostring(stock_after), tostring(queue_position)}
"""


def get_product_display_name(product_id, product):
    return (
        product.get("display_name_zh")
        or DISPLAY_NAMES_ZH.get(product_id)
        or product.get("name", "未命名商品")
    )


def product_orders_index(product_id):
    return f"idx:orders:product:{product_id}"


def product_order_status_index(product_id, status):
    return f"{product_orders_index(product_id)}:status:{status}"


def product_user_orders_index(product_id, user_id):
    return f"{product_orders_index(product_id)}:user:{user_id}"


def queue_order_index_add(pipe, product_id, order_id, user_id, status):
    pipe.sadd(product_orders_index(product_id), order_id)
    pipe.sadd(product_order_status_index(product_id, status), order_id)
    pipe.sadd(product_user_orders_index(product_id, user_id), order_id)


def queue_order_status_change(pipe, product_id, order_id, user_id, old_status, new_status):
    pipe.srem(product_order_status_index(product_id, old_status), order_id)
    pipe.sadd(product_order_status_index(product_id, new_status), order_id)
    pipe.sadd(product_user_orders_index(product_id, user_id), order_id)


def rebuild_order_indexes(db, product_id):
    version_key = f"{product_orders_index(product_id)}:version"
    if db.get(version_key) == ORDER_INDEX_VERSION:
        return

    prefix = product_orders_index(product_id)
    old_index_keys = list(db.scan_iter(match=f"{prefix}*"))
    if old_index_keys:
        db.delete(*old_index_keys)

    order_keys = []
    for key in db.scan_iter(match="order:*"):
        if db.type(key) == "hash":
            order_keys.append(key)

    for offset in range(0, len(order_keys), 250):
        batch = order_keys[offset : offset + 250]
        read_pipe = db.pipeline(transaction=False)
        for key in batch:
            read_pipe.hgetall(key)
        orders = read_pipe.execute()

        write_pipe = db.pipeline(transaction=False)
        indexed_count = 0
        for key, order in zip(batch, orders):
            if order.get("product_id") != product_id:
                continue
            order_id = key.removeprefix("order:")
            status = order.get("status", "UNKNOWN")
            user_id = order.get("user_id", "UNKNOWN")
            queue_order_index_add(
                write_pipe, product_id, order_id, user_id, status
            )
            indexed_count += 1
        if indexed_count:
            write_pipe.execute()

    db.set(version_key, ORDER_INDEX_VERSION)


def get_indexed_orders(db, product_id, status=None, user_id=None):
    index_keys = [product_orders_index(product_id)]
    if status and status != "全部狀態":
        index_keys.append(product_order_status_index(product_id, status))
    if user_id:
        index_keys.append(product_user_orders_index(product_id, user_id))

    if len(index_keys) == 1:
        order_ids = db.smembers(index_keys[0])
    else:
        order_ids = db.sinter(*index_keys)
    if not order_ids:
        return []
    order_ids = list(order_ids)

    read_pipe = db.pipeline(transaction=False)
    for order_id in order_ids:
        read_pipe.hgetall(f"order:{order_id}")
    rows = []
    for order_id, fields in zip(order_ids, read_pipe.execute()):
        if fields:
            fields["order_id"] = order_id
            rows.append(fields)
    return sorted(rows, key=lambda row: int(row.get("created_at", 0)), reverse=True)


def get_indexed_customer_ids(db, product_ids):
    customer_ids = set()
    for product_id in product_ids:
        user_index_prefix = product_user_orders_index(product_id, "")
        for index_key in db.scan_iter(match=f"{user_index_prefix}*"):
            customer_id = index_key.removeprefix(user_index_prefix)
            if customer_id:
                customer_ids.add(customer_id)
    return sorted(customer_ids)


def get_order_analytics(db, product_id):
    statuses = (
        "PENDING_PAYMENT",
        "WAITLISTED",
        "PAID",
        "CANCELLED",
        "EXPIRED",
    )
    pipe = db.pipeline(transaction=False)
    for status in statuses:
        pipe.scard(product_order_status_index(product_id, status))
    counts = dict(zip(statuses, pipe.execute()))

    paid_ids = db.smembers(product_order_status_index(product_id, "PAID"))
    if paid_ids:
        revenue_pipe = db.pipeline(transaction=False)
        for order_id in paid_ids:
            revenue_pipe.hget(f"order:{order_id}", "total_price")
        revenue = sum(int(value or 0) for value in revenue_pipe.execute())
    else:
        revenue = 0

    return {
        "total_orders": db.scard(product_orders_index(product_id)),
        "status_counts": counts,
        "paid_revenue": revenue,
    }


def get_notification_worker_metrics(db):
    worker_online = next(
        db.scan_iter(match="worker:notifications:heartbeat:*", count=10), None
    ) is not None
    processed = int(db.get("worker:notifications:processed") or 0)
    dead_letter_count = int(db.get("worker:notifications:dead_letter_count") or 0)
    pending = 0
    try:
        for group in db.xinfo_groups("order:events"):
            if group.get("name") == "order-notification-workers":
                pending = int(group.get("pending", 0))
                break
    except RedisError:
        pass
    return {
        "online": worker_online,
        "processed": processed,
        "pending": pending,
        "dead_letter_count": dead_letter_count,
    }


@st.cache_resource
def get_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        try:
            redis_url = st.secrets.get("REDIS_URL")
        except Exception:
            # Local development does not require a Streamlit secrets file.
            redis_url = None
    return create_redis_client(redis_url=redis_url, socket_connect_timeout=2)


@st.cache_resource
def get_place_order_script():
    return get_redis_client().register_script(PLACE_ORDER_LUA)


def place_order(db, product_id, user_id, quantity, cart_id):
    user_id = user_id.strip().upper()
    if not user_id:
        raise ValueError("請輸入顧客代號。")

    product_key = f"product:{product_id}"
    waitlist_key = f"waitlist:{product_id}"
    user_limit_key = f"user:{user_id}:product:{product_id}:quantity"
    order_id = "O" + uuid.uuid4().hex[:10].upper()
    order_key = f"order:{order_id}"
    idempotency_key = f"idempotency:cart:{cart_id}:product:{product_id}"
    cart_orders_key = f"cart:{cart_id}:orders"
    cart_key = f"cart:{cart_id}"

    raw_result = get_place_order_script()(
        keys=[
            product_key,
            waitlist_key,
            user_limit_key,
            order_key,
            "payment:deadlines",
            "restock:deadlines",
            product_orders_index(product_id),
            product_order_status_index(product_id, "PENDING_PAYMENT"),
            product_order_status_index(product_id, "WAITLISTED"),
            product_user_orders_index(product_id, user_id),
            "order:events",
            idempotency_key,
            cart_orders_key,
            cart_key,
        ],
        args=[
            product_id,
            order_id,
            user_id,
            int(quantity),
            cart_id,
            int(time.time()),
            PAYMENT_HOLD_SECONDS,
            IDEMPOTENCY_TTL_SECONDS,
        ],
    )

    result = [value.decode() if isinstance(value, bytes) else str(value) for value in raw_result]
    result_code = result[0]
    if result_code == "NO_PRODUCT":
        raise ValueError(f"找不到商品 {product_id}。")
    if result_code == "INVALID_QUANTITY":
        raise ValueError("購買數量必須大於 0。")
    if result_code == "LIMIT":
        raise ValueError(
            f"顧客 {user_id} 已購 {result[1]} 件，商品 {product_id} 每人限購 {result[2]} 件。"
        )
    if result_code not in ("CREATED", "DUPLICATE"):
        raise RuntimeError(f"Redis 下單腳本回傳未知結果：{result_code}")

    order_id_result = result[1]
    status_result = result[2]
    stock_after_result = int(result[3])
    queue_position = int(result[4]) or None
    if result_code == "DUPLICATE":
        existing_order = db.hgetall(f"order:{order_id_result}")
        if existing_order:
            status_result = existing_order.get("status", status_result)
        stock_after_result = int(db.hget(product_key, "stock") or 0)
        if status_result == "WAITLISTED":
            queued_ids = db.lrange(waitlist_key, 0, -1)
            queue_position = (
                queued_ids.index(order_id_result) + 1
                if order_id_result in queued_ids
                else None
            )
        else:
            queue_position = None
    return {
        "order_id": order_id_result,
        "status": status_result,
        "stock_after": stock_after_result,
        "queue_position": queue_position,
        "duplicate": result_code == "DUPLICATE",
    }


def get_pending_orders(db, product_id):
    order_ids = db.zrevrange("payment:deadlines", 0, -1)
    pending = []
    for order_id in order_ids:
        order = db.hgetall(f"order:{order_id}")
        if (
            order.get("product_id") == product_id
            and order.get("status") == "PENDING_PAYMENT"
        ):
            pending.append(order_id)
    return pending


def get_waitlisted_orders(db, product_id):
    order_ids = db.lrange(f"waitlist:{product_id}", 0, -1)
    waitlisted = []
    for order_id in order_ids:
        order = db.hgetall(f"order:{order_id}")
        if (
            order.get("product_id") == product_id
            and order.get("status") == "WAITLISTED"
        ):
            waitlisted.append(order_id)
    return waitlisted


def confirm_payment(db, order_id):
    order_id = order_id.strip().upper()
    if not order_id:
        raise ValueError("請先選擇待付款訂單。")

    order_key = f"order:{order_id}"

    for _ in range(5):
        with db.pipeline() as pipe:
            try:
                pipe.watch(order_key)
                order = pipe.hgetall(order_key)
                if not order:
                    raise ValueError("找不到這筆訂單。")
                if order.get("status") != "PENDING_PAYMENT":
                    raise ValueError("這筆訂單目前不是待付款狀態。")

                paid_at = int(time.time())
                product_id = order.get("product_id", PRODUCT_ID)

                pipe.multi()
                pipe.hset(
                    order_key,
                    mapping={"status": "PAID", "paid_at": paid_at},
                )
                queue_order_status_change(
                    pipe,
                    product_id,
                    order_id,
                    order.get("user_id", ""),
                    "PENDING_PAYMENT",
                    "PAID",
                )
                pipe.zrem("payment:deadlines", order_id)
                pipe.xadd(
                    "order:events",
                    {
                        "event": "payment_confirmed",
                        "order_id": order_id,
                        "product_id": product_id,
                        "user_id": order.get("user_id", ""),
                        "status": "PAID",
                    },
                )
                pipe.execute()
                return order_id

            except WatchError:
                continue

    raise RuntimeError("訂單狀態持續變動，請稍後再試。")


def close_order(db, order_id, final_status, product_id=None):
    order_id = order_id.strip().upper()
    product_id = product_id or PRODUCT_ID
    if final_status not in ("CANCELLED", "EXPIRED"):
        raise ValueError("不支援的訂單結束狀態。")
    if not order_id:
        raise ValueError("請先選擇要處理的訂單。")

    order_key = f"order:{order_id}"
    product_key = f"product:{product_id}"
    waitlist_key = f"waitlist:{product_id}"

    for _ in range(5):
        with db.pipeline() as pipe:
            try:
                pipe.watch(order_key, product_key, waitlist_key)
                order = pipe.hgetall(order_key)
                if not order:
                    raise ValueError("找不到這筆訂單。")
                if order.get("product_id") != product_id:
                    raise ValueError("這不是目前商品的訂單。")

                status = order.get("status")
                now = int(time.time())
                if final_status == "EXPIRED":
                    if status != "PENDING_PAYMENT":
                        raise ValueError("只有待付款訂單可以逾時。")
                    if int(order.get("payment_deadline", 0)) > now:
                        raise ValueError("這筆訂單尚未到付款期限。")
                elif status not in ("PENDING_PAYMENT", "WAITLISTED"):
                    raise ValueError("只有待付款或候補中的訂單可以取消。")

                quantity = int(order.get("quantity", 1))
                user_id = order.get("user_id", "")
                user_limit_key = f"user:{user_id}:product:{product_id}:quantity"
                pipe.watch(user_limit_key)

                stock = int(pipe.hget(product_key, "stock") or 0)
                candidate_id = None
                candidate = {}
                candidate_key = None
                candidate_quantity = 0
                should_promote = False

                if status == "PENDING_PAYMENT":
                    candidate_id = pipe.lindex(waitlist_key, 0)
                    if candidate_id:
                        candidate_key = f"order:{candidate_id}"
                        pipe.watch(candidate_key)
                        candidate = pipe.hgetall(candidate_key)
                        if (
                            candidate.get("product_id") == product_id
                            and candidate.get("status") == "WAITLISTED"
                        ):
                            candidate_quantity = int(candidate.get("quantity", 1))
                            should_promote = (
                                stock + quantity >= candidate_quantity
                            )

                stock_delta = 0
                if status == "PENDING_PAYMENT":
                    stock_delta = quantity
                    if should_promote:
                        stock_delta -= candidate_quantity

                pipe.multi()
                timestamp_field = (
                    "expired_at" if final_status == "EXPIRED" else "cancelled_at"
                )
                pipe.hset(
                    order_key,
                    mapping={
                        "status": final_status,
                        timestamp_field: now,
                    },
                )
                queue_order_status_change(
                    pipe,
                    product_id,
                    order_id,
                    user_id,
                    status,
                    final_status,
                )
                pipe.decrby(user_limit_key, quantity)

                if status == "PENDING_PAYMENT":
                    pipe.hincrby(product_key, "stock", stock_delta)
                    pipe.zrem("payment:deadlines", order_id)
                else:
                    pipe.lrem(waitlist_key, 1, order_id)

                promoted_deadline = None
                if should_promote:
                    promoted_deadline = now + PAYMENT_HOLD_SECONDS
                    pipe.lpop(waitlist_key)
                    pipe.hset(
                        candidate_key,
                        mapping={
                            "status": "PENDING_PAYMENT",
                            "payment_deadline": promoted_deadline,
                        },
                    )
                    queue_order_status_change(
                        pipe,
                        product_id,
                        candidate_id,
                        candidate.get("user_id", ""),
                        "WAITLISTED",
                        "PENDING_PAYMENT",
                    )
                    pipe.zadd(
                        "payment:deadlines",
                        {candidate_id: promoted_deadline},
                    )
                    pipe.xadd(
                        "order:events",
                        {
                            "event": "order_promoted",
                            "order_id": candidate_id,
                            "product_id": product_id,
                            "user_id": candidate.get("user_id", ""),
                            "status": "PENDING_PAYMENT",
                        },
                    )

                event_name = (
                    "order_expired" if final_status == "EXPIRED" else "order_cancelled"
                )
                pipe.xadd(
                    "order:events",
                    {
                        "event": event_name,
                        "order_id": order_id,
                        "product_id": product_id,
                        "user_id": user_id,
                        "status": final_status,
                        "stock_delta": stock_delta,
                    },
                )
                pipe.execute()
                new_stock = stock + stock_delta
                return order_id, candidate_id if should_promote else None, new_stock

            except WatchError:
                continue

    raise RuntimeError("訂單或庫存持續變動，請稍後再試。")


def cancel_order(db, order_id, product_id=None):
    return close_order(db, order_id, "CANCELLED", product_id=product_id)


def process_expired_orders(db, product_id):
    now = int(time.time())
    due_order_ids = db.zrangebyscore("payment:deadlines", "-inf", now)
    processed = []

    for order_id in due_order_ids:
        order = db.hgetall(f"order:{order_id}")
        if (
            order.get("product_id") != product_id
            or order.get("status") != "PENDING_PAYMENT"
        ):
            continue
        try:
            processed.append(
                close_order(db, order_id, "EXPIRED", product_id=product_id)
            )
        except ValueError:
            latest = db.hgetall(f"order:{order_id}")
            if (
                latest.get("status") == "PENDING_PAYMENT"
                and int(latest.get("payment_deadline", now + 1)) <= now
            ):
                raise

    return processed


def ensure_restock_settings(db, product_id):
    product_key = f"product:{product_id}"
    defaults = {
        "restock_threshold": "1",
        "restock_batch": "5",
        "restock_delay_seconds": "15",
    }
    pipe = db.pipeline(transaction=False)
    for field, value in defaults.items():
        pipe.hsetnx(product_key, field, value)
    pipe.execute()


def schedule_restock_if_needed(db, product_id):
    product_key = f"product:{product_id}"
    for _ in range(5):
        with db.pipeline() as pipe:
            try:
                pipe.watch(product_key, "restock:deadlines")
                product = pipe.hgetall(product_key)
                if not product:
                    return None

                stock = int(product.get("stock", 0))
                threshold = int(product.get("restock_threshold", 1))
                due_at = pipe.zscore("restock:deadlines", product_id)
                if stock > threshold or due_at is not None:
                    return due_at

                due_at = int(time.time()) + int(
                    product.get("restock_delay_seconds", 15)
                )
                pipe.multi()
                pipe.zadd("restock:deadlines", {product_id: due_at})
                pipe.xadd(
                    "order:events",
                    {
                        "event": "restock_scheduled",
                        "product_id": product_id,
                        "quantity": product.get("restock_batch", "5"),
                        "due_at": due_at,
                        "stock_after": stock,
                    },
                )
                pipe.execute()
                return due_at
            except WatchError:
                continue
    raise RuntimeError("補貨資料持續變動，請稍後再試。")


def process_due_restock(db, product_id):
    product_key = f"product:{product_id}"
    waitlist_key = f"waitlist:{product_id}"
    restock_key = "restock:deadlines"
    now = int(time.time())

    for _ in range(5):
        with db.pipeline() as pipe:
            try:
                pipe.watch(product_key, waitlist_key, restock_key)
                product = pipe.hgetall(product_key)
                due_at = pipe.zscore(restock_key, product_id)
                if not product or due_at is None or float(due_at) > now:
                    return None

                stock = int(product.get("stock", 0))
                threshold = int(product.get("restock_threshold", 1))
                batch_size = int(product.get("restock_batch", 5))
                if stock > threshold:
                    pipe.multi()
                    pipe.zrem(restock_key, product_id)
                    pipe.xadd(
                        "order:events",
                        {
                            "event": "restock_cancelled",
                            "product_id": product_id,
                            "stock_after": stock,
                        },
                    )
                    pipe.execute()
                    return {"cancelled": True, "stock": stock, "promoted": []}

                queue_ids = pipe.lrange(waitlist_key, 0, -1)
                candidate_keys = [f"order:{order_id}" for order_id in queue_ids]
                if candidate_keys:
                    pipe.watch(*candidate_keys)
                candidates = [pipe.hgetall(key) for key in candidate_keys]

                available = stock + batch_size
                promoted = []
                for order_id, candidate in zip(queue_ids, candidates):
                    if (
                        candidate.get("product_id") != product_id
                        or candidate.get("status") != "WAITLISTED"
                    ):
                        break
                    quantity = int(candidate.get("quantity", 1))
                    if quantity > available:
                        break
                    promoted.append((order_id, candidate, quantity))
                    available -= quantity

                promoted_quantity = sum(quantity for _, _, quantity in promoted)
                stock_delta = batch_size - promoted_quantity
                final_stock = available
                pipe.multi()
                pipe.hincrby(product_key, "stock", stock_delta)
                pipe.zrem(restock_key, product_id)
                pipe.xadd(
                    "order:events",
                    {
                        "event": "restock_completed",
                        "product_id": product_id,
                        "quantity": batch_size,
                        "stock_after": final_stock,
                    },
                )
                for order_id, candidate, quantity in promoted:
                    deadline = now + PAYMENT_HOLD_SECONDS
                    pipe.lpop(waitlist_key)
                    pipe.hset(
                        f"order:{order_id}",
                        mapping={
                            "status": "PENDING_PAYMENT",
                            "payment_deadline": deadline,
                        },
                    )
                    queue_order_status_change(
                        pipe,
                        product_id,
                        order_id,
                        candidate.get("user_id", ""),
                        "WAITLISTED",
                        "PENDING_PAYMENT",
                    )
                    pipe.zadd("payment:deadlines", {order_id: deadline})
                    pipe.xadd(
                        "order:events",
                        {
                            "event": "order_promoted",
                            "order_id": order_id,
                            "product_id": product_id,
                            "user_id": candidate.get("user_id", ""),
                            "status": "PENDING_PAYMENT",
                            "quantity": quantity,
                            "stock_after": final_stock,
                        },
                    )
                pipe.execute()
                return {
                    "cancelled": False,
                    "batch_size": batch_size,
                    "stock": final_stock,
                    "promoted": [order_id for order_id, _, _ in promoted],
                }
            except WatchError:
                continue

    raise RuntimeError("庫存或候補資料持續變動，請稍後再試。")

def get_recent_events(db, product_id=None, limit=10):
    rows = db.xrevrange("order:events", max="+", min="-", count=100)
    event_names = {
        "order_created": "建立訂單",
        "order_waitlisted": "加入候補",
        "order_promoted": "候補遞補",
        "order_cancelled": "取消訂單",
        "order_expired": "付款逾時",
        "payment_confirmed": "付款完成",
        "restock_scheduled": "已安排補貨",
        "restock_completed": "補貨到貨",
        "restock_cancelled": "取消補貨",
    }
    order_ids = list(
        dict.fromkeys(
            fields.get("order_id")
            for _, fields in rows
            if fields.get("order_id")
        )
    )
    order_pipe = db.pipeline(transaction=False)
    for order_id in order_ids:
        order_pipe.hgetall(f"order:{order_id}")
    order_details = dict(zip(order_ids, order_pipe.execute())) if order_ids else {}

    grouped_events = {}
    for stream_id, fields in rows:
        event_product_id = fields.get("product_id", "")
        if product_id and event_product_id != product_id:
            continue
        event_type = fields.get("event", "")
        order_id = fields.get("order_id", "")
        order_data = order_details.get(order_id, {})
        cart_id = fields.get("cart_id") or order_data.get("cart_id")
        group_key = (cart_id, event_type) if cart_id else (stream_id, event_type)
        timestamp_ms = int(stream_id.split("-", 1)[0])
        group = grouped_events.setdefault(
            group_key,
            {
                "timestamp_ms": timestamp_ms,
                "event": event_type,
                "cart_id": cart_id,
            },
        )
        group["timestamp_ms"] = max(group["timestamp_ms"], timestamp_ms)

    recent = []
    for group in grouped_events.values():
        event_time = datetime.fromtimestamp(
            group["timestamp_ms"] / 1000
        ).strftime("%Y-%m-%d %H:%M:%S")
        recent.append(
            {
                "時間": event_time,
                "購物車編號": group["cart_id"] or "—",
                "事件": event_names.get(group["event"], group["event"]),
            }
        )

    recent.sort(key=lambda event: event["時間"], reverse=True)
    return recent[:limit]

st.set_page_config(
    page_title="超市限量商品搶購系統",
    page_icon="🛒",
    layout="centered",
)

try:
    startup_db = get_redis_client()
    startup_db.ping()
except RedisError as error:
    st.error("目前連不上 Redis。請確認 WSL 裡的 Redis 服務仍在執行。")
    st.code(str(error))
    st.stop()

st.title("🛒 超市限量商品搶購系統")
st.caption("多商品購物車、Lua 原子搶購、FIFO 候補與即時通知")

try:
    db = get_redis_client()
    db.ping()
    available_products = {}
    for product_key in db.scan_iter(match="product:*"):
        if db.type(product_key) != "hash":
            continue
        product_id = product_key.partition(":")[2]
        if product_id in INTERNAL_PRODUCT_IDS:
            continue
        product = db.hgetall(product_key)
        if product_id and product.get("name"):
            available_products[product_id] = product
except RedisError as error:
    st.error("目前連不上 Redis。請確認 WSL 裡的 Redis 服務仍在執行。")
    st.code(str(error))
    st.stop()

if not available_products:
    try:
        # A fresh hosted Redis database has no catalog yet. The importer uses
        # HSETNX, so it is safe to run here without resetting existing stock.
        import_open_data_catalog()
        for product_key in db.scan_iter(match="product:*"):
            if db.type(product_key) != "hash":
                continue
            product_id = product_key.partition(":")[2]
            if product_id in INTERNAL_PRODUCT_IDS:
                continue
            product = db.hgetall(product_key)
            if product_id and product.get("name"):
                available_products[product_id] = product
    except RedisError as error:
        st.error("初始化商品資料時連不上 Redis。請檢查 REDIS_URL 設定。")
        st.code(str(error))
        st.stop()

if not available_products:
    st.warning("找不到可展示的商品，請先建立商品資料。")
    st.stop()

default_product_id = "P004" if "P004" in available_products else sorted(available_products)[0]
product_ids = sorted(available_products)
selected_product_id = st.selectbox(
    "選擇庫存概覽商品",
    options=product_ids,
    index=product_ids.index(default_product_id),
    format_func=lambda product_id: (
        f"{get_product_display_name(product_id, available_products[product_id])}（{product_id}）"
    ),
)
PRODUCT_ID = selected_product_id
st.caption("顧客可在「搶購商品」分頁一次把多項商品加入購物車；上方選擇控制庫存概覽，訂單分析可篩選商品。")

if "order_notice" in st.session_state:
    st.info(st.session_state.pop("order_notice"))

st.button("重新整理商品資料")

try:
    for available_product_id in product_ids:
        ensure_restock_settings(db, available_product_id)
        rebuild_order_indexes(db, available_product_id)
    product = db.hgetall(f"product:{PRODUCT_ID}")
    queue_count = db.llen(f"waitlist:{PRODUCT_ID}")
except RedisError as error:
    st.error("目前連不上 Redis。請確認 WSL 裡的 Redis 服務仍在執行。")
    st.code(str(error))
    st.stop()

if not product:
    st.warning(f"找不到商品 product:{PRODUCT_ID}，請先在 Redis 建立商品資料。")
    st.stop()

st.subheader(get_product_display_name(PRODUCT_ID, product))


@st.fragment(run_every=RESTOCK_POLL_SECONDS)
def automatic_operations_monitor(monitored_product_ids):
    try:
        notices = []
        restock_warnings = []
        for product_id in monitored_product_ids:
            expired_orders = process_expired_orders(db, product_id)
            restock = process_due_restock(db, product_id)
            schedule_restock_if_needed(db, product_id)

            product_name = get_product_display_name(
                product_id, db.hgetall(f"product:{product_id}")
            )
            for expired_id, promoted_id, stock_after in expired_orders:
                if promoted_id:
                    notices.append(
                        f"{product_name}：訂單 {expired_id} 已逾時，"
                        f"候補訂單 {promoted_id} 已遞補，庫存剩 {stock_after} 件。"
                    )
                else:
                    notices.append(
                        f"{product_name}：訂單 {expired_id} 已逾時，庫存已釋出。"
                    )

            if restock:
                if restock["cancelled"]:
                    notices.append(f"{product_name}：庫存已回升，取消排定補貨。")
                else:
                    promoted_text = ""
                    if restock["promoted"]:
                        promoted_text = (
                            "候補訂單 " + ", ".join(restock["promoted"]) + " 已遞補。"
                        )
                    notices.append(
                        f"{product_name}：模擬補貨 {restock['batch_size']} 件到貨，"
                        f"庫存 {restock['stock']} 件。{promoted_text}"
                    )

            product_data = db.hgetall(f"product:{product_id}")
            due_at = db.zscore("restock:deadlines", product_id)
            if due_at is not None:
                seconds_left = max(0, int(float(due_at) - time.time()))
                restock_warnings.append(
                    f"{product_name}：預計約 {seconds_left} 秒後補入 "
                    f"{product_data.get('restock_batch', '5')} 件。"
                )

        if notices:
            st.session_state["order_notice"] = " ".join(notices)
            st.rerun()

        if restock_warnings:
            st.warning("多商品自動補貨中：" + "；".join(restock_warnings))
        else:
            st.caption(
                f"畫面開啟期間每 {RESTOCK_POLL_SECONDS} 秒監控 "
                f"{len(monitored_product_ids)} 項商品的付款期限、候補與補貨。"
            )
    except RedisError as error:
        st.warning("自動補貨監控暫時無法連線 Redis。")
        st.code(str(error))


automatic_operations_monitor(product_ids)

price_col, stock_col, limit_col, queue_col = st.columns(4)
price_col.metric("單價", "$" + product.get("price", "—"))
stock_col.metric("剩餘庫存", product.get("stock", "—"))
limit_col.metric("每人限購", product.get("limit_per_user", "—"))
queue_col.metric("候補人數", queue_count)

st.caption(
    f"待付款保留 {PAYMENT_HOLD_SECONDS // 60} 分鐘；付款為模擬流程，"
    f"頁面開啟期間每 {RESTOCK_POLL_SECONDS} 秒自動檢查補貨與逾時訂單。"
)

if "tab_after_action" in st.session_state:
    st.session_state["main_tabs"] = st.session_state.pop("tab_after_action")

shop_tab, staff_tab, insights_tab = st.tabs(
    ["🛒 搶購商品", "🧾 店員管理", "📊 訂單分析"],
    key="main_tabs",
    on_change="rerun",
)

with shop_tab:
    st.subheader("多商品購物車")
    if "cart_request_id" not in st.session_state:
        st.session_state["cart_request_id"] = uuid.uuid4().hex.upper()
    if "cart_owner" not in st.session_state:
        st.session_state["cart_owner"] = None
    if "cart_submitted_products" not in st.session_state:
        st.session_state["cart_submitted_products"] = []
    if "cart_customer_id_prompt_initialized" not in st.session_state:
        st.session_state["cart_customer_id"] = ""
        st.session_state["cart_customer_id_prompt_initialized"] = True
    if st.session_state.pop("clear_cart_selection_after_submit", False):
        for selected_product_id in st.session_state.get("cart_product_ids", []):
            st.session_state.pop(f"cart_quantity_{selected_product_id}", None)
        st.session_state["cart_product_ids"] = []
        st.session_state["cart_customer_id"] = ""
        st.session_state["cart_request_id"] = uuid.uuid4().hex.upper()
        st.session_state["cart_owner"] = None
        st.session_state["cart_submitted_products"] = []

    customer_id = st.text_input(
        "顧客代號",
        placeholder="請輸入顧客代號，例如 U007",
        key="cart_customer_id",
        disabled=bool(st.session_state["cart_owner"]),
    )
    selected_cart_product_ids = st.multiselect(
        "選擇要購買的商品",
        options=product_ids,
        format_func=lambda product_id: (
            f"{get_product_display_name(product_id, available_products[product_id])}（{product_id}）"
        ),
        key="cart_product_ids",
        placeholder="選擇一項或多項商品",
    )

    line_quantities = {}
    submitted_cart = False
    cart_total = 0
    if selected_cart_product_ids:
        with st.form("cart_order_form"):
            for cart_product_id in selected_cart_product_ids:
                cart_product = available_products[cart_product_id]
                display_name = get_product_display_name(
                    cart_product_id, cart_product
                )
                unit_price = int(cart_product.get("price", 0))
                stock_value = int(cart_product.get("stock", 0))
                limit_value = max(1, int(cart_product.get("limit_per_user", 1)))
                item_col, quantity_col = st.columns([3, 1])
                item_col.markdown(
                    f"**{display_name}**  ·  ${unit_price}  ·  庫存 {stock_value}  ·  每人限購 {limit_value}"
                )
                line_quantities[cart_product_id] = quantity_col.number_input(
                    f"{display_name} 數量",
                    min_value=1,
                    max_value=limit_value,
                    value=1,
                    step=1,
                    key=f"cart_quantity_{cart_product_id}",
                    label_visibility="collapsed",
                    disabled=(
                        cart_product_id
                        in st.session_state["cart_submitted_products"]
                    ),
                )
                cart_total += unit_price * int(
                    line_quantities[cart_product_id]
                )
            st.caption("每個品項會各自建立訂單；缺貨品項可單獨進入該商品的候補佇列。")
            st.markdown(f"**購物車預估金額：${cart_total}**")
            submitted_cart = st.form_submit_button(
                "送出整車搶購",
                type="primary",
                use_container_width=True,
                disabled=not customer_id.strip(),
            )
    else:
        st.info("先選擇一項或多項商品，再設定每項購買數量。")

    normalized_customer_id = customer_id.strip().upper()
    if submitted_cart:
        cart_id = st.session_state["cart_request_id"]
        cart_results_by_product = {}
        if st.session_state.get("cart_results_id") == cart_id:
            cart_results_by_product = {
                item["商品代號"]: item
                for item in st.session_state.get("cart_results", [])
                if item.get("商品代號")
            }
        for cart_product_id, quantity in line_quantities.items():
            try:
                result = place_order(
                    db,
                    cart_product_id,
                    normalized_customer_id,
                    int(quantity),
                    cart_id,
                )
                cart_results_by_product[cart_product_id] = {
                    "商品代號": cart_product_id,
                    "商品": get_product_display_name(
                        cart_product_id,
                        available_products[cart_product_id],
                    ),
                    "訂單編號": result["order_id"],
                    "數量": int(quantity),
                    "結果": (
                        "沿用已建立訂單"
                        if result["duplicate"]
                        else "已建立訂單"
                    ),
                    "狀態": {
                        "PENDING_PAYMENT": "待付款",
                        "WAITLISTED": "候補中",
                        "PAID": "已付款",
                        "CANCELLED": "已取消",
                        "EXPIRED": "已逾時",
                    }.get(result["status"], result["status"]),
                    "庫存剩餘": result["stock_after"],
                    "候補順位": result["queue_position"] or "—",
                    "說明": "",
                }
                st.session_state["cart_owner"] = normalized_customer_id
                if cart_product_id not in st.session_state["cart_submitted_products"]:
                    st.session_state["cart_submitted_products"].append(
                        cart_product_id
                    )
            except (RedisError, ValueError, RuntimeError) as error:
                cart_results_by_product[cart_product_id] = {
                    "商品代號": cart_product_id,
                    "商品": get_product_display_name(
                        cart_product_id,
                        available_products[cart_product_id],
                    ),
                    "訂單編號": "—",
                    "數量": int(quantity),
                    "結果": "未建立",
                    "狀態": "—",
                    "庫存剩餘": available_products[cart_product_id].get(
                        "stock", "—"
                    ),
                    "候補順位": "—",
                    "說明": str(error),
                }
        st.session_state["cart_results"] = list(cart_results_by_product.values())
        st.session_state["cart_results_id"] = cart_id
        st.session_state["clear_cart_selection_after_submit"] = True
        st.rerun()

    if st.session_state.get("cart_results"):
        st.subheader("最近一次購物車訂單")
        st.caption(f"購物車編號：{st.session_state.get('cart_results_id', '')}")
        cart_columns = [
            ("商品代號", "9%"),
            ("商品", "19%"),
            ("訂單編號", "16%"),
            ("數量", "6%"),
            ("結果", "13%"),
            ("狀態", "9%"),
            ("庫存剩餘", "10%"),
            ("候補順位", "10%"),
            ("說明", "8%"),
        ]
        cart_table_rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{escape_html(str(item.get(column, '')))}</td>"
                for column, _ in cart_columns
            )
            + "</tr>"
            for item in st.session_state["cart_results"]
        )
        cart_table_html = f"""
            <style>
                .cart-order-table-wrap {{ width: 100%; max-width: 100%; }}
                .cart-order-table {{
                    width: 100%;
                    table-layout: fixed;
                    border-collapse: collapse;
                    color: var(--text-color, #f4f4f4);
                    font-size: clamp(0.65rem, 1.2vw, 0.9rem);
                    line-height: 1.35;
                }}
                .cart-order-table th,
                .cart-order-table td {{
                    border: 1px solid rgba(150, 145, 138, 0.28);
                    padding: clamp(4px, 0.55vw, 8px) clamp(4px, 0.65vw, 10px);
                    text-align: left;
                    vertical-align: middle;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .cart-order-table th {{
                    background: var(--secondary-background-color, #302d2a);
                    font-weight: 600;
                }}
            </style>
            <div class="cart-order-table-wrap">
                <table class="cart-order-table" aria-label="本次購物車訂單">
                    <colgroup>
                        {''.join(f'<col style="width:{width}">' for _, width in cart_columns)}
                    </colgroup>
                    <thead><tr>{''.join(f'<th>{escape_html(column)}</th>' for column, _ in cart_columns)}</tr></thead>
                    <tbody>{cart_table_rows}</tbody>
                </table>
            </div>
        """
        st.html(cart_table_html, width="stretch")

    with st.expander("查詢顧客訂單"):
        customer_ids = get_indexed_customer_ids(db, product_ids)
        if customer_ids:
            with st.form("customer_order_search"):
                lookup_user = st.selectbox(
                    "顧客代號",
                    options=customer_ids,
                    key="customer_order_search_user",
                )
                lookup_submitted = st.form_submit_button("查詢訂單")
            if lookup_submitted:
                st.session_state["lookup_user_id"] = lookup_user
        else:
            st.info("目前還沒有顧客訂單。")

        searched_user = st.session_state.get("lookup_user_id", "")
        if searched_user and customer_ids:
            indexed_orders = sorted(
                [
                    order
                    for product_id in product_ids
                    for order in get_indexed_orders(
                        db,
                        product_id,
                        user_id=searched_user,
                    )
                ],
                key=lambda order: int(order.get("created_at", 0)),
                reverse=True,
            )
            if indexed_orders:
                status_names = {
                    "PENDING_PAYMENT": "待付款",
                    "WAITLISTED": "候補中",
                    "PAID": "已付款",
                    "CANCELLED": "已取消",
                    "EXPIRED": "已逾時",
                }
                st.dataframe(
                    [
                        {
                            "訂單編號": order["order_id"],
                            "商品": get_product_display_name(
                                order.get("product_id", ""),
                                available_products.get(
                                    order.get("product_id", ""), {}
                                ),
                            ),
                            "數量": order.get("quantity", ""),
                            "金額": "$" + order.get("total_price", "0"),
                            "狀態": status_names.get(
                                order.get("status", ""), order.get("status", "")
                            ),
                        }
                        for order in indexed_orders
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info(f"查無顧客 {searched_user} 的商品訂單。")

with staff_tab:
    st.caption("每個購物車只列一次，並摘要顯示裡面的商品與狀態；各商品仍以獨立子訂單處理。")
    staff_cart_groups = {}
    order_status_labels = {
        "PENDING_PAYMENT": "待付款",
        "WAITLISTED": "候補中",
        "PAID": "已付款",
        "CANCELLED": "已取消",
        "EXPIRED": "已逾時",
    }
    for staff_product_id in product_ids:
        staff_pending_ids = get_pending_orders(db, staff_product_id)
        staff_waitlisted_ids = get_waitlisted_orders(db, staff_product_id)
        for staff_order_id, staff_status in [
            *((order_id, "PENDING_PAYMENT") for order_id in staff_pending_ids),
            *((order_id, "WAITLISTED") for order_id in staff_waitlisted_ids),
        ]:
            order_data = db.hgetall(f"order:{staff_order_id}")
            if not order_data:
                continue
            staff_cart_id = order_data.get("cart_id") or staff_order_id
            cart_group = staff_cart_groups.setdefault(
                staff_cart_id,
                {
                    "user_id": order_data.get("user_id", "—"),
                    "pending_order_ids": [],
                    "waitlisted_order_ids": [],
                },
            )
            if staff_status == "PENDING_PAYMENT":
                cart_group["pending_order_ids"].append(staff_order_id)
            else:
                cart_group["waitlisted_order_ids"].append(staff_order_id)

    for staff_cart_id, cart_group in staff_cart_groups.items():
        cart_order_ids = db.lrange(f"cart:{staff_cart_id}:orders", 0, -1)
        if not cart_order_ids:
            cart_order_ids = (
                cart_group["pending_order_ids"]
                + cart_group["waitlisted_order_ids"]
            )
        item_summaries = []
        for cart_order_id in dict.fromkeys(cart_order_ids):
            cart_order = db.hgetall(f"order:{cart_order_id}")
            if not cart_order:
                continue
            cart_product_id = cart_order.get("product_id", "")
            cart_product = available_products.get(cart_product_id, {})
            cart_product_name = get_product_display_name(
                cart_product_id, cart_product
            )
            cart_status = order_status_labels.get(
                cart_order.get("status", ""), cart_order.get("status", "未知")
            )
            item_summaries.append(
                f"{cart_product_name} ×{cart_order.get('quantity', '1')}（{cart_status}）"
            )
        cart_group["label"] = (
            f"顧客 {cart_group['user_id']}｜購物車 {staff_cart_id}｜"
            + "、".join(item_summaries)
        )

    with st.expander("模擬付款", expanded=True):
        pending_cart_ids = [
            cart_id
            for cart_id, cart_group in staff_cart_groups.items()
            if cart_group["pending_order_ids"]
        ]
        if pending_cart_ids:
            selected_payment_cart_id = st.selectbox(
                "選擇待付款購物車",
                options=pending_cart_ids,
                format_func=lambda cart_id: staff_cart_groups[cart_id]["label"],
                key="payment_cart_select_all_products",
            )
            st.caption("付款會處理此購物車內所有待付款品項；候補品項維持候補狀態。")
            if st.button("模擬付款完成", key="confirm_payment", type="primary"):
                paid_order_ids = []
                payment_errors = []
                for payment_order_id in staff_cart_groups[
                    selected_payment_cart_id
                ]["pending_order_ids"]:
                    try:
                        paid_order_ids.append(confirm_payment(db, payment_order_id))
                    except (RedisError, ValueError, RuntimeError) as error:
                        payment_errors.append(f"{payment_order_id}：{error}")
                payment_message = (
                    f"購物車 {selected_payment_cart_id}："
                    f"{len(paid_order_ids)} 個品項已標記為已付款。"
                )
                if payment_errors:
                    payment_message += " 處理失敗：" + "；".join(payment_errors)
                st.session_state["order_notice"] = payment_message
                st.session_state["tab_after_action"] = "🧾 店員管理"
                st.rerun()
        else:
            st.info("目前沒有待付款訂單。")

    with st.expander("取消訂單與候補遞補", expanded=True):
        cancellable_cart_ids = [
            cart_id
            for cart_id, cart_group in staff_cart_groups.items()
            if cart_group["pending_order_ids"]
            or cart_group["waitlisted_order_ids"]
        ]
        if cancellable_cart_ids:
            selected_cancel_cart_id = st.selectbox(
                "選擇待付款或候補中的購物車",
                options=cancellable_cart_ids,
                format_func=lambda cart_id: staff_cart_groups[cart_id]["label"],
                key="cancel_cart_select_all_products",
            )
            st.caption("取消會處理此購物車內所有待付款或候補品項，並逐項釋出庫存及遞補。")
            if st.button(
                "取消訂單並處理候補",
                key="cancel_order",
                type="primary",
            ):
                cancelled_order_ids = []
                promoted_order_ids = []
                cancellation_errors = []
                selected_cart_group = staff_cart_groups[selected_cancel_cart_id]
                active_cart_order_ids = (
                    selected_cart_group["pending_order_ids"]
                    + selected_cart_group["waitlisted_order_ids"]
                )
                for cancel_order_id in active_cart_order_ids:
                    order_data = db.hgetall(f"order:{cancel_order_id}")
                    try:
                        cancelled_id, promoted_id, _ = cancel_order(
                            db,
                            cancel_order_id,
                            order_data.get("product_id"),
                        )
                        cancelled_order_ids.append(cancelled_id)
                        if promoted_id:
                            promoted_order_ids.append(promoted_id)
                    except (RedisError, ValueError, RuntimeError) as error:
                        cancellation_errors.append(f"{cancel_order_id}：{error}")
                cancel_message = (
                    f"購物車 {selected_cancel_cart_id}："
                    f"已取消 {len(cancelled_order_ids)} 個品項訂單。"
                )
                if promoted_order_ids:
                    cancel_message += (
                        " 已遞補候補訂單：" + "、".join(promoted_order_ids) + "。"
                    )
                if cancellation_errors:
                    cancel_message += " 處理失敗：" + "；".join(cancellation_errors)
                st.session_state["order_notice"] = cancel_message
                st.session_state["tab_after_action"] = "🧾 店員管理"
                st.rerun()
        else:
            st.info("目前沒有可取消或遞補的訂單。")

    st.success("已連線 Redis，商品資料讀取成功。")
    st.caption(
        "訂單事件會先寫入 Redis Stream；背景工作者啟動後，再透過 Pub/Sub 發送通知。"
        "即時通知需另開 PowerShell 執行 worker.py。 "
        f"頁面開啟期間每 {RESTOCK_POLL_SECONDS} 秒檢查付款期限。"
    )

with insights_tab:
    analysis_product_options = ["全部商品"] + product_ids
    selected_analysis_product_id = st.selectbox(
        "篩選分析商品",
        options=analysis_product_options,
        format_func=lambda product_id: (
            product_id
            if product_id == "全部商品"
            else f"{get_product_display_name(product_id, available_products[product_id])}（{product_id}）"
        ),
        key="analytics_product_filter",
    )
    analysis_product_ids = (
        product_ids
        if selected_analysis_product_id == "全部商品"
        else [selected_analysis_product_id]
    )
    product_analytics = [
        get_order_analytics(db, product_id)
        for product_id in analysis_product_ids
    ]
    analytics = {
        "total_orders": sum(item["total_orders"] for item in product_analytics),
        "status_counts": {
            status: sum(item["status_counts"][status] for item in product_analytics)
            for status in (
                "PENDING_PAYMENT",
                "WAITLISTED",
                "PAID",
                "CANCELLED",
                "EXPIRED",
            )
        },
        "paid_revenue": sum(item["paid_revenue"] for item in product_analytics),
    }
    worker_metrics = get_notification_worker_metrics(db)
    counts = analytics["status_counts"]
    metric_cols = st.columns(5)
    metric_cols[0].metric("品項訂單數", analytics["total_orders"])
    metric_cols[1].metric("待付款品項", counts["PENDING_PAYMENT"])
    metric_cols[2].metric("候補品項", counts["WAITLISTED"])
    metric_cols[3].metric("已付款品項", counts["PAID"])
    st.metric("已付款金額", "$" + str(analytics["paid_revenue"]))
    st.caption("上方數字依分析商品篩選統計；下方列表以購物車彙整，顯示整車總金額。")

    st.subheader("背景事件工作者")
    worker_cols = st.columns(3)
    worker_cols[0].metric(
        "通知工作者",
        "運作中" if worker_metrics["online"] else "未啟動",
    )
    worker_cols[1].metric("已處理事件", worker_metrics["processed"])
    worker_cols[2].metric("待處理事件", worker_metrics["pending"])
    st.caption(f"失敗轉入 Dead Letter：{worker_metrics['dead_letter_count']} 筆。")
    if not worker_metrics["online"]:
        st.info("要啟用背景通知，請在另一個 PowerShell 視窗執行：python worker.py")

    status_labels = {
        "全部狀態": None,
        "待付款": "PENDING_PAYMENT",
        "候補中": "WAITLISTED",
        "已付款": "PAID",
        "已取消": "CANCELLED",
        "已逾時": "EXPIRED",
    }
    selected_status_label = st.selectbox(
        "篩選訂單狀態",
        options=list(status_labels),
        key="order_status_filter_analysis",
    )
    filtered_orders = [
        order
        for product_id in analysis_product_ids
        for order in get_indexed_orders(
            db,
            product_id,
            status=status_labels[selected_status_label],
        )
    ]
    if filtered_orders:
        status_names = {value: label for label, value in status_labels.items() if value}
        cart_groups = {}
        for order in filtered_orders:
            cart_id = order.get("cart_id") or order["order_id"]
            cart_groups.setdefault(cart_id, []).append(order)

        cart_summary_rows = []
        for cart_id, matched_orders in cart_groups.items():
            all_cart_order_ids = db.lrange(f"cart:{cart_id}:orders", 0, -1)
            if not all_cart_order_ids:
                all_cart_order_ids = [order["order_id"] for order in matched_orders]
            cart_order_ids = list(dict.fromkeys(all_cart_order_ids))
            order_pipe = db.pipeline(transaction=False)
            for cart_order_id in cart_order_ids:
                order_pipe.hgetall(f"order:{cart_order_id}")
            cart_orders = [
                order_data
                for order_data in order_pipe.execute()
                if order_data
            ]
            if not cart_orders:
                cart_orders = matched_orders

            cart_product_summary = []
            cart_status_labels = []
            for cart_order in cart_orders:
                cart_product_id = cart_order.get("product_id", "")
                cart_product = available_products.get(cart_product_id, {})
                cart_product_name = get_product_display_name(
                    cart_product_id, cart_product
                )
                cart_product_summary.append(
                    f"{cart_product_name} ×{cart_order.get('quantity', '1')}"
                )
                cart_status = status_names.get(
                    cart_order.get("status", ""), cart_order.get("status", "")
                )
                if cart_status and cart_status not in cart_status_labels:
                    cart_status_labels.append(cart_status)

            cart_summary_rows.append(
                {
                    "購物車編號": cart_id,
                    "顧客": cart_orders[0].get("user_id", ""),
                    "商品明細": "、".join(cart_product_summary),
                    "總數量": sum(
                        int(order.get("quantity", 0)) for order in cart_orders
                    ),
                    "總金額": "$"
                    + str(
                        sum(
                            int(order.get("total_price", 0))
                            for order in cart_orders
                        )
                    ),
                    "狀態": "、".join(cart_status_labels),
                    "建立時間": datetime.fromtimestamp(
                        min(int(order.get("created_at", 0)) for order in cart_orders)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        cart_summary_rows.sort(key=lambda row: row["建立時間"], reverse=True)
        st.subheader("購物車訂單總覽")
        st.caption("每個購物車一列，顯示各品項狀態與整車總金額。")
        st.dataframe(
            cart_summary_rows,
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("目前沒有符合此篩選條件的訂單。")

    st.subheader("Redis Stream 最近事件")
    st.caption("依時間記錄事件及購物車編號；沒有對應購物車的補貨事件以「—」顯示。")
    recent_events = get_recent_events(
        db,
        None
        if selected_analysis_product_id == "全部商品"
        else selected_analysis_product_id,
    )
    if recent_events:
        st.dataframe(recent_events, hide_index=True, use_container_width=True)
    else:
        st.info("目前還沒有這項商品的訂單事件。")




