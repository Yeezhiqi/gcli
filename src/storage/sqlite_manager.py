"""
SQLite 存储管理器
"""

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from log import log


class SQLiteManager:
    """SQLite 数据库管理器"""

    # 状态字段常量 (增加统计字段)
    STATE_FIELDS = {
        "error_codes",
        "disabled",
        "last_success",
        "user_email",
        "model_cooldowns",
        # [新增]
        "daily_usage",
        "daily_usage_pro",
        "last_usage_reset",
    }

    # 所有必需的列定义（用于自动校验和修复）
    REQUIRED_COLUMNS = {
        "credentials": [
            ("disabled", "INTEGER DEFAULT 0"),
            ("error_codes", "TEXT DEFAULT '[]'"),
            ("last_success", "REAL"),
            ("user_email", "TEXT"),
            ("model_cooldowns", "TEXT DEFAULT '{}'"),
            ("rotation_order", "INTEGER DEFAULT 0"),
            ("call_count", "INTEGER DEFAULT 0"),
            ("created_at", "REAL DEFAULT (unixepoch())"),
            ("updated_at", "REAL DEFAULT (unixepoch())"),
            # [新增]
            ("daily_usage", "INTEGER DEFAULT 0"),
            ("daily_usage_pro", "INTEGER DEFAULT 0"),
            ("last_usage_reset", "REAL DEFAULT 0"),
        ],
        "antigravity_credentials": [
            ("disabled", "INTEGER DEFAULT 0"),
            ("error_codes", "TEXT DEFAULT '[]'"),
            ("last_success", "REAL"),
            ("user_email", "TEXT"),
            ("model_cooldowns", "TEXT DEFAULT '{}'"),
            ("rotation_order", "INTEGER DEFAULT 0"),
            ("call_count", "INTEGER DEFAULT 0"),
            ("created_at", "REAL DEFAULT (unixepoch())"),
            ("updated_at", "REAL DEFAULT (unixepoch())"),
            # [新增] 虽然Antigravity目前没用这些，但为了统一性加上
            ("daily_usage", "INTEGER DEFAULT 0"),
            ("daily_usage_pro", "INTEGER DEFAULT 0"),
            ("last_usage_reset", "REAL DEFAULT 0"),
        ]
    }

    def __init__(self):
        self._db_path = None
        self._credentials_dir = None
        self._initialized = False
        self._lock = asyncio.Lock()

        # 内存配置缓存 - 初始化时加载一次
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded = False

    async def initialize(self) -> None:
        """初始化 SQLite 数据库"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                # 获取凭证目录
                self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
                self._db_path = os.path.join(self._credentials_dir, "credentials.db")

                # 确保目录存在
                os.makedirs(self._credentials_dir, exist_ok=True)

                # 创建数据库和表
                async with aiosqlite.connect(self._db_path) as db:
                    # 启用 WAL 模式（提升并发性能）
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("PRAGMA foreign_keys=ON")

                    # 检查并自动修复数据库结构 (这会自动添加新列)
                    await self._ensure_schema_compatibility(db)

                    # 创建表
                    await self._create_tables(db)

                    await db.commit()

                # 加载配置到内存
                await self._load_config_cache()

                self._initialized = True
                log.info(f"SQLite storage initialized at {self._db_path}")

            except Exception as e:
                log.error(f"Error initializing SQLite: {e}")
                raise

    async def _ensure_schema_compatibility(self, db: aiosqlite.Connection) -> None:
        """
        确保数据库结构兼容，自动修复缺失的列
        """
        try:
            # 检查每个表
            for table_name, columns in self.REQUIRED_COLUMNS.items():
                # 检查表是否存在
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                ) as cursor:
                    if not await cursor.fetchone():
                        log.debug(f"Table {table_name} does not exist, will be created")
                        continue

                # 获取现有列
                async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                    existing_columns = {row[1] for row in await cursor.fetchall()}

                # 添加缺失的列
                added_count = 0
                for col_name, col_def in columns:
                    if col_name not in existing_columns:
                        try:
                            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
                            log.info(f"Added missing column {table_name}.{col_name}")
                            added_count += 1
                        except Exception as e:
                            log.error(f"Failed to add column {table_name}.{col_name}: {e}")

                if added_count > 0:
                    log.info(f"Table {table_name}: added {added_count} missing column(s)")

        except Exception as e:
            log.error(f"Error ensuring schema compatibility: {e}")
            # 不抛出异常，允许继续初始化

    async def _create_tables(self, db: aiosqlite.Connection):
        """创建数据库表和索引"""
        # 凭证表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                credential_data TEXT NOT NULL,

                -- 状态字段
                disabled INTEGER DEFAULT 0,
                error_codes TEXT DEFAULT '[]',
                last_success REAL,
                user_email TEXT,

                -- 统计字段 [新增]
                daily_usage INTEGER DEFAULT 0,
                daily_usage_pro INTEGER DEFAULT 0,
                last_usage_reset REAL DEFAULT 0,

                -- 模型级 CD 支持 (JSON: {model_key: cooldown_timestamp})
                model_cooldowns TEXT DEFAULT '{}',

                -- 轮换相关
                rotation_order INTEGER DEFAULT 0,
                call_count INTEGER DEFAULT 0,

                -- 时间戳
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        # Antigravity 凭证表（结构相同但独立存储）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antigravity_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                credential_data TEXT NOT NULL,

                -- 状态字段
                disabled INTEGER DEFAULT 0,
                error_codes TEXT DEFAULT '[]',
                last_success REAL,
                user_email TEXT,

                -- 统计字段 [新增]
                daily_usage INTEGER DEFAULT 0,
                daily_usage_pro INTEGER DEFAULT 0,
                last_usage_reset REAL DEFAULT 0,

                -- 模型级 CD 支持 (JSON: {model_name: cooldown_timestamp})
                model_cooldowns TEXT DEFAULT '{}',

                -- 轮换相关
                rotation_order INTEGER DEFAULT 0,
                call_count INTEGER DEFAULT 0,

                -- 时间戳
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        # 创建索引 - 普通凭证表
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_disabled
            ON credentials(disabled)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_rotation_order
            ON credentials(rotation_order)
        """)

        # 创建索引 - Antigravity 凭证表
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_disabled
            ON antigravity_credentials(disabled)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_rotation_order
            ON antigravity_credentials(rotation_order)
        """)

        # 配置表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        log.debug("SQLite tables and indexes created")

    async def _load_config_cache(self):
        """加载配置到内存缓存（仅在初始化时调用一次）"""
        if self._config_loaded:
            return

        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("SELECT key, value FROM config") as cursor:
                    rows = await cursor.fetchall()

                for key, value in rows:
                    try:
                        self._config_cache[key] = json.loads(value)
                    except json.JSONDecodeError:
                        self._config_cache[key] = value

            self._config_loaded = True
            log.debug(f"Loaded {len(self._config_cache)} config items into cache")

        except Exception as e:
            log.error(f"Error loading config cache: {e}")
            self._config_cache = {}

    async def close(self) -> None:
        """关闭数据库连接"""
        self._initialized = False
        log.debug("SQLite storage closed")

    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("SQLite manager not initialized")

    def _is_antigravity(self, filename: str) -> bool:
        """判断是否为 antigravity 凭证"""
        return filename.startswith("ag_")

    def _get_table_name(self, is_antigravity: bool) -> str:
        """根据 is_antigravity 标志获取对应的表名"""
        return "antigravity_credentials" if is_antigravity else "credentials"

    # ============ SQL 方法 ============

    async def get_next_available_credential(
        self, is_antigravity: bool = False, model_key: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        随机获取一个可用凭证（负载均衡）
        """
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                current_time = time.time()

                # 获取所有候选凭证（未禁用）
                async with db.execute(f"""
                    SELECT filename, credential_data, model_cooldowns
                    FROM {table_name}
                    WHERE disabled = 0
                    ORDER BY RANDOM()
                """) as cursor:
                    rows = await cursor.fetchall()

                    # 如果没有提供 model_key，使用第一个可用凭证
                    if not model_key:
                        if rows:
                            filename, credential_json, _ = rows[0]
                            credential_data = json.loads(credential_json)
                            return filename, credential_data
                        return None

                    # 如果提供了 model_key，检查模型级冷却
                    for filename, credential_json, model_cooldowns_json in rows:
                        model_cooldowns = json.loads(model_cooldowns_json or '{}')

                        # 检查该模型是否在冷却中
                        model_cooldown = model_cooldowns.get(model_key)
                        if model_cooldown is None or current_time >= model_cooldown:
                            # 该模型未冷却或冷却已过期
                            credential_data = json.loads(credential_json)
                            return filename, credential_data

                    return None

        except Exception as e:
            log.error(f"Error getting next available credential (antigravity={is_antigravity}, model_key={model_key}): {e}")
            return None

    async def get_available_credentials_list(self) -> List[str]:
        """
        获取所有可用凭证列表
        """
        self._ensure_initialized()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("""
                    SELECT filename
                    FROM credentials
                    WHERE disabled = 0
                    ORDER BY rotation_order ASC
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]

        except Exception as e:
            log.error(f"Error getting available credentials list: {e}")
            return []

    # ============ StorageBackend 协议方法 ============

    async def store_credential(self, filename: str, credential_data: Dict[str, Any], is_antigravity: bool = False) -> bool:
        """存储或更新凭证"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                # 检查凭证是否存在
                async with db.execute(f"""
                    SELECT disabled
                    FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    existing = await cursor.fetchone()

                if existing:
                    # 更新现有凭证（保留状态）
                    await db.execute(f"""
                        UPDATE {table_name}
                        SET credential_data = ?,
                            updated_at = unixepoch()
                        WHERE filename = ?
                    """, (json.dumps(credential_data), filename))
                else:
                    # 插入新凭证
                    async with db.execute(f"""
                        SELECT COALESCE(MAX(rotation_order), -1) + 1 FROM {table_name}
                    """) as cursor:
                        row = await cursor.fetchone()
                        next_order = row[0]

                    await db.execute(f"""
                        INSERT INTO {table_name}
                        (filename, credential_data, rotation_order, last_success)
                        VALUES (?, ?, ?, ?)
                    """, (filename, json.dumps(credential_data), next_order, time.time()))

                await db.commit()
                log.debug(f"Stored credential: {filename} (antigravity={is_antigravity})")
                return True

        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False

    async def get_credential(self, filename: str, is_antigravity: bool = False) -> Optional[Dict[str, Any]]:
        """获取凭证数据，支持basename匹配以兼容旧数据"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                # 首先尝试精确匹配
                async with db.execute(f"""
                    SELECT credential_data FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return json.loads(row[0])

                # 如果精确匹配失败，尝试使用basename匹配（处理包含路径的旧数据）
                async with db.execute(f"""
                    SELECT credential_data FROM {table_name}
                    WHERE filename LIKE '%' || ? OR filename = ?
                """, (filename, filename)) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        return json.loads(row[0])

                return None

        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None

    async def list_credentials(self, is_antigravity: bool = False) -> List[str]:
        """列出所有凭证文件名（包括禁用的）"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(f"""
                    SELECT filename FROM {table_name} ORDER BY rotation_order
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]

        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []

    async def delete_credential(self, filename: str, is_antigravity: bool = False) -> bool:
        """删除凭证"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                # 首先尝试精确匹配删除
                result = await db.execute(f"""
                    DELETE FROM {table_name} WHERE filename = ?
                """, (filename,))
                deleted_count = result.rowcount

                # 如果精确匹配没有删除任何记录，尝试basename匹配
                if deleted_count == 0:
                    result = await db.execute(f"""
                        DELETE FROM {table_name} WHERE filename LIKE '%' || ?
                    """, (filename,))
                    deleted_count = result.rowcount

                await db.commit()

                if deleted_count > 0:
                    log.debug(f"Deleted {deleted_count} credential(s): {filename} (antigravity={is_antigravity})")
                    return True
                else:
                    log.warning(f"No credential found to delete: {filename} (antigravity={is_antigravity})")
                    return False

        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], is_antigravity: bool = False) -> bool:
        """更新凭证状态"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            # 构建动态 SQL
            set_clauses = []
            values = []

            for key, value in state_updates.items():
                # 检查是否在允许的字段列表中
                if key in self.STATE_FIELDS:
                    if key == "error_codes":
                        set_clauses.append(f"{key} = ?")
                        values.append(json.dumps(value))
                    elif key == "model_cooldowns":
                        set_clauses.append(f"{key} = ?")
                        values.append(json.dumps(value))
                    else:
                        set_clauses.append(f"{key} = ?")
                        values.append(value)

            if not set_clauses:
                return True

            set_clauses.append("updated_at = unixepoch()")
            values.append(filename)

            async with aiosqlite.connect(self._db_path) as db:
                # 首先尝试精确匹配更新
                result = await db.execute(f"""
                    UPDATE {table_name}
                    SET {', '.join(set_clauses)}
                    WHERE filename = ?
                """, values)
                updated_count = result.rowcount

                # 如果精确匹配没有更新任何记录，尝试basename匹配
                if updated_count == 0:
                    result = await db.execute(f"""
                        UPDATE {table_name}
                        SET {', '.join(set_clauses)}
                        WHERE filename LIKE '%' || ?
                    """, values)
                    updated_count = result.rowcount

                await db.commit()
                return updated_count > 0

        except Exception as e:
            log.error(f"Error updating credential state {filename}: {e}")
            return False

    async def get_credential_state(self, filename: str, is_antigravity: bool = False) -> Dict[str, Any]:
        """获取凭证状态"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            # 添加新的统计字段到查询中
            query = f"""
                SELECT disabled, error_codes, last_success, user_email, model_cooldowns,
                       daily_usage, daily_usage_pro, last_usage_reset
                FROM {table_name} WHERE filename = ?
            """
            
            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配
                async with db.execute(query, (filename,)) as cursor:
                    row = await cursor.fetchone()

                # 模糊匹配
                if not row:
                    async with db.execute(f"""
                        SELECT disabled, error_codes, last_success, user_email, model_cooldowns,
                               daily_usage, daily_usage_pro, last_usage_reset
                        FROM {table_name} WHERE filename LIKE '%' || ?
                    """, (filename,)) as cursor:
                        row = await cursor.fetchone()

                if row:
                    error_codes_json = row[1] or '[]'
                    model_cooldowns_json = row[4] or '{}'
                    return {
                        "disabled": bool(row[0]),
                        "error_codes": json.loads(error_codes_json),
                        "last_success": row[2] or time.time(),
                        "user_email": row[3],
                        "model_cooldowns": json.loads(model_cooldowns_json),
                        # [新增]
                        "daily_usage": row[5] or 0,
                        "daily_usage_pro": row[6] or 0,
                        "last_usage_reset": row[7] or 0,
                    }

                return {
                    "disabled": False,
                    "error_codes": [],
                    "last_success": time.time(),
                    "user_email": None,
                    "model_cooldowns": {},
                    "daily_usage": 0,
                    "daily_usage_pro": 0,
                    "last_usage_reset": 0,
                }

        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
            return {}

    async def get_all_credential_states(self, is_antigravity: bool = False) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                # 增加统计字段查询
                async with db.execute(f"""
                    SELECT filename, disabled, error_codes, last_success,
                           user_email, model_cooldowns,
                           daily_usage, daily_usage_pro, last_usage_reset
                    FROM {table_name}
                """) as cursor:
                    rows = await cursor.fetchall()

                    states = {}
                    current_time = time.time()

                    for row in rows:
                        filename = row[0]
                        error_codes_json = row[2] or '[]'
                        model_cooldowns_json = row[5] or '{}'
                        model_cooldowns = json.loads(model_cooldowns_json)

                        if model_cooldowns:
                            model_cooldowns = {
                                k: v for k, v in model_cooldowns.items()
                                if v > current_time
                            }

                        states[filename] = {
                            "disabled": bool(row[1]),
                            "error_codes": json.loads(error_codes_json),
                            "last_success": row[3] or time.time(),
                            "user_email": row[4],
                            "model_cooldowns": model_cooldowns,
                            # [新增]
                            "daily_usage": row[6] or 0,
                            "daily_usage_pro": row[7] or 0,
                            "last_usage_reset": row[8] or 0,
                        }

                    return states

        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        is_antigravity: bool = False,
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取凭证摘要信息"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(is_antigravity)

            async with aiosqlite.connect(self._db_path) as db:
                # 全局统计
                global_stats = {"total": 0, "normal": 0, "disabled": 0}
                async with db.execute(f"""
                    SELECT disabled, COUNT(*) FROM {table_name} GROUP BY disabled
                """) as stats_cursor:
                    stats_rows = await stats_cursor.fetchall()
                    for disabled, count in stats_rows:
                        global_stats["total"] += count
                        if disabled:
                            global_stats["disabled"] = count
                        else:
                            global_stats["normal"] = count

                # WHERE 条件
                where_clauses = []
                if status_filter == "enabled":
                    where_clauses.append("disabled = 0")
                elif status_filter == "disabled":
                    where_clauses.append("disabled = 1")

                filter_value = None
                filter_int = None
                if error_code_filter and str(error_code_filter).strip().lower() != "all":
                    filter_value = str(error_code_filter).strip()
                    try:
                        filter_int = int(filter_value)
                    except ValueError:
                        filter_int = None

                where_clause = ""
                if where_clauses:
                    where_clause = "WHERE " + " AND ".join(where_clauses)

                # 查询数据（增加统计字段）
                all_query = f"""
                    SELECT filename, disabled, error_codes, last_success,
                           user_email, rotation_order, model_cooldowns,
                           daily_usage, daily_usage_pro
                    FROM {table_name}
                    {where_clause}
                    ORDER BY rotation_order
                """

                async with db.execute(all_query) as cursor:
                    all_rows = await cursor.fetchall()
                    current_time = time.time()
                    all_summaries = []

                    for row in all_rows:
                        filename = row[0]
                        error_codes_json = row[2] or '[]'
                        model_cooldowns_json = row[6] or '{}'
                        model_cooldowns = json.loads(model_cooldowns_json)

                        active_cooldowns = {}
                        if model_cooldowns:
                            active_cooldowns = {
                                k: v for k, v in model_cooldowns.items()
                                if v > current_time
                            }

                        error_codes = json.loads(error_codes_json)
                        if filter_value:
                            match = False
                            for code in error_codes:
                                if code == filter_value or code == filter_int:
                                    match = True
                                    break
                                if isinstance(code, str) and filter_int is not None:
                                    try:
                                        if int(code) == filter_int:
                                            match = True
                                            break
                                    except ValueError:
                                        pass
                            if not match:
                                continue

                        summary = {
                            "filename": filename,
                            "disabled": bool(row[1]),
                            "error_codes": error_codes,
                            "last_success": row[3] or current_time,
                            "user_email": row[4],
                            "rotation_order": row[5],
                            "model_cooldowns": active_cooldowns,
                            # [新增]
                            "daily_usage": row[7] or 0,
                            "daily_usage_pro": row[8] or 0,
                        }

                        if cooldown_filter == "in_cooldown":
                            if active_cooldowns:
                                all_summaries.append(summary)
                        elif cooldown_filter == "no_cooldown":
                            if not active_cooldowns:
                                all_summaries.append(summary)
                        else:
                            all_summaries.append(summary)

                    total_count = len(all_summaries)
                    if limit is not None:
                        summaries = all_summaries[offset:offset + limit]
                    else:
                        summaries = all_summaries[offset:]

                    return {
                        "items": summaries,
                        "total": total_count,
                        "offset": offset,
                        "limit": limit,
                        "stats": global_stats,
                    }

        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return {
                "items": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "stats": {"total": 0, "normal": 0, "disabled": 0},
            }

    # ============ 配置管理 ============
    async def set_config(self, key: str, value: Any) -> bool:
        self._ensure_initialized()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO config (key, value, updated_at)
                    VALUES (?, ?, unixepoch())
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (key, json.dumps(value)))
                await db.commit()
            self._config_cache[key] = value
            return True
        except Exception as e:
            log.error(f"Error setting config {key}: {e}")
            return False

    async def reload_config_cache(self):
        self._ensure_initialized()
        self._config_loaded = False
        await self._load_config_cache()
        log.info("Config cache reloaded from database")

    async def get_config(self, key: str, default: Any = None) -> Any:
        self._ensure_initialized()
        return self._config_cache.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return self._config_cache.copy()

    async def delete_config(self, key: str) -> bool:
        self._ensure_initialized()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("DELETE FROM config WHERE key = ?", (key,))
                await db.commit()
            self._config_cache.pop(key, None)
            return True
        except Exception as e:
            log.error(f"Error deleting config {key}: {e}")
            return False

    # ============ 模型级冷却管理 ============
    async def set_model_cooldown(
        self,
        filename: str,
        model_key: str,
        cooldown_until: Optional[float],
        is_antigravity: bool = False
    ) -> bool:
        self._ensure_initialized()
        try:
            table_name = self._get_table_name(is_antigravity)
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(f"""
                    SELECT model_cooldowns FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return False
                    model_cooldowns = json.loads(row[0] or '{}')
                    if cooldown_until is None:
                        model_cooldowns.pop(model_key, None)
                    else:
                        model_cooldowns[model_key] = cooldown_until
                    await db.execute(f"""
                        UPDATE {table_name}
                        SET model_cooldowns = ?, updated_at = unixepoch()
                        WHERE filename = ?
                    """, (json.dumps(model_cooldowns), filename))
                    await db.commit()
                    return True
        except Exception as e:
            log.error(f"Error setting model cooldown: {e}")
            return False