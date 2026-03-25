#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import asyncio

import json
import aiomysql
import asyncpg
from datetime import datetime
from typing import List, Tuple, Optional

# ------------------ 环境配置 ------------------

if not os.getenv('GITHUB_ACTIONS'):
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".news.env")

config = {}
# 嘗試載入 JSON 並合併參數
try:
    configuration_json = json.loads(os.getenv('CONFIGURATION', '') or '{}')
    if isinstance(configuration_json, dict):
        config.update(configuration_json)
except Exception as e:
    print(f"⚠️ 無法解析 CONFIGURATION：{e}")


MYSQL_DB_HOST = config.get('db_host', os.getenv('MYSQL_DB_HOST', 'localhost'))
MYSQL_DB_USER = config.get('db_user', os.getenv('MYSQL_DB_USER', ''))
MYSQL_DB_PASSWORD = config.get('db_password', os.getenv('MYSQL_DB_PASSWORD', ''))
MYSQL_DB_NAME = config.get('db_name', os.getenv('MYSQL_DB_NAME', ''))
MYSQL_DB_PORT = int(config.get('db_port', os.getenv('MYSQL_DB_PORT', 3306)))

# MYSQL_DB_HOST = os.getenv("MYSQL_DB_HOST", "127.0.0.1")
# MYSQL_DB_PORT = int(os.getenv("MYSQL_DB_PORT", "3306"))
# MYSQL_DB_USER = os.getenv("MYSQL_DB_USER", "root")
# MYSQL_DB_PASSWORD = os.getenv("MYSQL_DB_PASSWORD", "")
# MYSQL_DB_NAME = os.getenv("MYSQL_DB_NAME", "telebot")

DB_DSN = os.getenv("DB_DSN", "postgres://postgres:postgres@127.0.0.1:5432/telebot")

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1000"))
BUSINESS_TYPE = os.getenv("BUSINESS_TYPE", "salai")

# news_content 迁移相关
START_ID = int(os.getenv("START_ID", "0"))                # 仅迁移 id > START_ID 的数据
DELETE_AFTER_SYNC = int(os.getenv("DELETE_AFTER_SYNC", "1"))  # 1=PG成功后删除MySQL；0=不删（演练）
ALLOW_CREATE_MEMBERSHIP = int(os.getenv("ALLOW_CREATE_MEMBERSHIP", "0"))
# 0 = 只更新已存在的 news_user（不插入）
# 1 = 允许 UPSERT（不存在则插入）


# ------------------ PG 索引保证（以 business_type+content_id 为唯一键） ------------------
PG_ENSURE_INDEXES = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname='public' AND indexname='ux_news_content_business_contentid'
    ) THEN
        CREATE UNIQUE INDEX ux_news_content_business_contentid
        ON news_content (business_type, content_id);
    END IF;
END $$;
"""

async def ensure_pg_indexes(pg: asyncpg.Connection):
    await pg.execute(PG_ENSURE_INDEXES)

# ------------------ membership → news_user ------------------
async def is_user_id_numeric(pg: asyncpg.Connection) -> bool:
    row = await pg.fetchrow("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'news_user'
          AND column_name = 'user_id'
        LIMIT 1;
    """)
    if not row:
        return True
    dt = (row["data_type"] or "").lower()
    return any(k in dt for k in ("int", "numeric", "decimal"))

MYSQL_COUNT_MEMBERSHIP = """
SELECT COUNT(*)
FROM membership
WHERE expire_timestamp > %s;   -- 秒
"""

MYSQL_FETCH_MEMBERSHIP = """
SELECT membership_id, user_id, expire_timestamp
FROM membership
WHERE membership_id > %s
  AND expire_timestamp > %s
ORDER BY membership_id
LIMIT %s;
"""

PG_UPSERT_NEWS_USER = """
INSERT INTO news_user (user_id, business_type, expire_at)
VALUES ($1, $2, to_timestamp($3)) -- $3 传“秒”
ON CONFLICT (user_id, business_type)
DO UPDATE SET
    expire_at = GREATEST(news_user.expire_at, EXCLUDED.expire_at)
WHERE news_user.expire_at IS DISTINCT FROM EXCLUDED.expire_at;
"""

PG_UPDATE_NEWS_USER = """
UPDATE news_user AS t
SET expire_at = to_timestamp($3)
WHERE t.user_id = $1
  AND t.business_type = $2
  AND (
        t.expire_at IS NULL
        OR to_timestamp($3) > t.expire_at
      );
"""


async def fetch_mysql_total_membership(conn: aiomysql.Connection, now_s: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(MYSQL_COUNT_MEMBERSHIP, (now_s,))
        (cnt,) = await cur.fetchone()
        return int(cnt or 0)

async def fetch_mysql_batch_membership(
    conn: aiomysql.Connection,
    last_pk: int,
    now_s: int,
    limit: int
) -> List[Tuple[int, str, int]]:
    async with conn.cursor() as cur:
        await cur.execute(MYSQL_FETCH_MEMBERSHIP, (last_pk, now_s, limit))
        return await cur.fetchall()

async def upsert_news_user_pg(
    pg: asyncpg.Connection,
    rows: List[Tuple[int, str, int]],
    business_type: str,
    numeric_user_id: bool
) -> tuple[int, int]:
    """
    返回 (成功写入/更新条数, 跳过(非数字 user_id) 条数)
    - 当 ALLOW_CREATE_MEMBERSHIP = 1 时：UPSERT（可能新增）
    - 当 ALLOW_CREATE_MEMBERSHIP = 0 时：只 UPDATE 已存在的行（不存在则跳过）
    """
    args = []
    skipped = 0
    for _, user_id_str, expire_ts in rows:
        sec = int(expire_ts)  # 秒
        if numeric_user_id:
            try:
                uid = int(str(user_id_str).strip())
            except ValueError:
                skipped += 1
                continue
            args.append((uid, business_type, sec))
        else:
            args.append((str(user_id_str), business_type, sec))

    if not args:
        return 0, skipped

    if ALLOW_CREATE_MEMBERSHIP:
        # === 模式A：允许创建（UPSERT）===
        async with pg.transaction():
            await pg.executemany(PG_UPSERT_NEWS_USER, args)
        # executemany 无法返回逐条影响行数，这里以提交数作为“成功写入条数”的估计
        return len(args), skipped
    else:
        # === 模式B：只更新已存在 ===
        updated = 0
        async with pg.transaction():
            for a in args:
                status = await pg.execute(PG_UPDATE_NEWS_USER, *a)
                # status like "UPDATE 0"/"UPDATE 1"
                if status.endswith("1"):
                    updated += 1
        return updated, skipped


async def sync_membership_to_news_user(mysql_pool, pg_pool):
    now_s = int(time.time())  # 秒
    async with mysql_pool.acquire() as myconn:
        total = await fetch_mysql_total_membership(myconn, now_s)

    print(f"[SYNC:user] 仅处理未过期：expire_timestamp > {now_s} (s)")
    print(f"[SYNC:user] 预计处理 {total} 行，批大小 {BATCH_SIZE}")

    last_pk = 0
    processed = 0
    upserted_total = 0
    skipped_total = 0
    batch_idx = 0

    async with pg_pool.acquire() as pg:
        await ensure_pg_indexes(pg)
        numeric_user_id = await is_user_id_numeric(pg)

    while True:
        async with mysql_pool.acquire() as myconn:
            rows = await fetch_mysql_batch_membership(myconn, last_pk, now_s, BATCH_SIZE)

        if not rows:
            break

        batch_idx += 1
        last_pk = rows[-1][0]

        async with pg_pool.acquire() as pg:
            upserted, skipped = await upsert_news_user_pg(
                pg, rows, business_type=BUSINESS_TYPE,
                numeric_user_id=bool(numeric_user_id)
            )

        processed += len(rows)
        upserted_total += upserted
        skipped_total += skipped
        print(f"[SYNC:user] 批次 {batch_idx}: 读 {len(rows)} 行，UPSERT {upserted}，跳过 {skipped}（非数字 user_id），累计 {processed}/{total}")

    print(f"[DONE:user] 读取 {processed}，UPSERT {upserted_total}，跳过 {skipped_total}")


# ------------------ 工具函数 ------------------

def parse_created_at(value) -> Optional[datetime]:
    """
    将 MySQL 的 created_at（可能为 datetime、字符串、int 秒）尽量解析为 datetime。
    解析失败返回 None（表示不覆盖 PG 现有值）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # 纯数字（时间戳秒）
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(int(value))
    except Exception:
        pass
    # 常见字符串格式尝试
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(value[:19], fmt)
            except Exception:
                continue
    return None

# ------------------ news_content 迁移（MySQL → PG → 删 MySQL） ------------------
# ------------------ news_content 迁移（MySQL → PG → 删 MySQL） ------------------

MYSQL_COUNT_NEWS = "SELECT COUNT(*) FROM news_content WHERE id > %s;"

async def fetch_mysql_total_news(conn: aiomysql.Connection, start_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(MYSQL_COUNT_NEWS, (start_id,))
        (cnt,) = await cur.fetchone()
        return int(cnt or 0)

async def fetch_mysql_batch_news(conn: aiomysql.Connection, last_id: int, limit: int) -> List[Tuple]:
    MYSQL_FETCH_NEWS = """
    SELECT
        id, title, text, file_id, file_type, button_str, bot_name,
        created_at, business_type, content_id, thumb_file_unique_id
    FROM news_content
    WHERE id > %s and thumb_file_unique_id is not null
    ORDER BY id
    LIMIT %s;
    """
    async with conn.cursor() as cur:
        await cur.execute(MYSQL_FETCH_NEWS, (last_id, limit))
        return await cur.fetchall()

async def upsert_news_content_pg(pg: asyncpg.Connection, rows: List[tuple]) -> None:
    """
    以 (business_type, content_id) 为唯一键进行 UPSERT。
    不写入/不更新 file_id；不使用 MySQL 的 id。
    """
    UPSERT_SQL = """
    INSERT INTO news_content
        (business_type, content_id, title, text, file_type,
         button_str, bot_name, created_at, thumb_file_unique_id)
    VALUES
        ($1, $2, $3, $4, $5,
         $6, $7, $8, $9)
    ON CONFLICT (business_type, content_id) DO UPDATE SET
        title                 = EXCLUDED.title,
        text                  = EXCLUDED.text,
        -- 不触碰 file_id
        file_type             = COALESCE(EXCLUDED.file_type, news_content.file_type),
        button_str            = EXCLUDED.button_str,
        bot_name              = EXCLUDED.bot_name,
        created_at            = COALESCE(EXCLUDED.created_at, news_content.created_at),
        thumb_file_unique_id  = EXCLUDED.thumb_file_unique_id;
    """
    async with pg.transaction():
        for r in rows:
            (
                rid,                # MySQL id（忽略，不使用）
                title,
                text,
                file_id_mysql,      # 来自 MySQL 的 file_id（忽略，不写入）
                file_type,
                button_str,
                bot_name,
                created_at,
                business_type,
                content_id,
                thumb_uid
            ) = r

            created_at_dt = parse_created_at(created_at)

            # 注意：参数不包含 file_id；id 由 PG 自增
            await pg.execute(
                UPSERT_SQL,
                business_type,                                  # $1
                int(content_id) if content_id is not None else None,  # $2
                title,                                          # $3
                text,                                           # $4
                file_type or 'photo',                           # $5
                button_str,                                     # $6
                bot_name or 'salai',                            # $7
                created_at_dt,                                  # $8
                thumb_uid                                       # $9
            )


async def delete_mysql_news_rows(conn: aiomysql.Connection, ids: List[int]) -> int:
    if not ids:
        return 0
    deleted = 0
    CHUNK = 1000
    async with conn.cursor() as cur:
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i+CHUNK]
            placeholders = ",".join(["%s"] * len(chunk))
            sql = f"DELETE FROM news_content WHERE id IN ({placeholders})"
            await cur.execute(sql, chunk)
            deleted += (cur.rowcount or 0)
    return deleted

async def migrate_news_content(mysql_pool, pg_pool):
    async with mysql_pool.acquire() as myconn:
        total = await fetch_mysql_total_news(myconn, START_ID)

    print(f"[SYNC:news] 预计迁移（id > {START_ID}）= {total}，批大小 {BATCH_SIZE}")

    last_id = START_ID
    processed = 0
    migrated_total = 0
    deleted_total = 0
    batch_idx = 0

    async with pg_pool.acquire() as pg:
        await ensure_pg_indexes(pg)

    while True:
        async with mysql_pool.acquire() as myconn:
            rows = await fetch_mysql_batch_news(myconn, last_id, BATCH_SIZE)

        if not rows:
            break

        batch_idx += 1
        last_id = rows[-1][0]
        processed += len(rows)

        # 先写 PG（事务）
        async with pg_pool.acquire() as pg:
            await upsert_news_content_pg(pg, rows)
        migrated_total += len(rows)

        # 成功后删除 MySQL
        if DELETE_AFTER_SYNC:
            ids = [r[0] for r in rows]
            async with mysql_pool.acquire() as myconn:
                deleted = await delete_mysql_news_rows(myconn, ids)
            deleted_total += deleted
            print(f"[SYNC:news] 批次 {batch_idx}: UPSERT {len(rows)} → 删除 MySQL {deleted}（累计 {processed}/{total}）")
        else:
            print(f"[SYNC:news] 批次 {batch_idx}: UPSERT {len(rows)}（未删 MySQL，DELETE_AFTER_SYNC=0） 累计 {processed}/{total}")

    print(f"[DONE:news] UPSERT {migrated_total}；MySQL 删除 {deleted_total}")




# ------------------ 主入口 ------------------
async def main():
    # 连接池（全局共享）
    mysql_pool = await aiomysql.create_pool(
        host=MYSQL_DB_HOST, port=MYSQL_DB_PORT,
        user=MYSQL_DB_USER, password=MYSQL_DB_PASSWORD,
        db=MYSQL_DB_NAME, autocommit=True, minsize=1, maxsize=4,
        charset="utf8mb4"
    )
    pg_pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=8)

    try:
        # 顺序执行：先同步会员有效期，再迁移 news_content
        await sync_membership_to_news_user(mysql_pool, pg_pool)
        await migrate_news_content(mysql_pool, pg_pool)
    finally:
        mysql_pool.close()
        await mysql_pool.wait_closed()
        await pg_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
