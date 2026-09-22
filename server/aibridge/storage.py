from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .security import SecretBox, hash_token, new_gateway_token, verify_token


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, database_path: Path, secret_key_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path
        self.secrets = SecretBox(secret_key_path)
        self._lock = threading.RLock()
        self._init_schema()
        self._seed_sources()
        self._import_ccswitch_deepseek()
        self._seed_api_source_icons()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('web','api')),
                    protocol TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    credential_cipher TEXT,
                    health_status TEXT NOT NULL DEFAULT 'unconfigured',
                    health_message TEXT NOT NULL DEFAULT '尚未配置',
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    public_name TEXT NOT NULL UNIQUE,
                    upstream_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_keys (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catcher_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    auto_enable INTEGER NOT NULL DEFAULT 0,
                    allowed_sources_json TEXT NOT NULL DEFAULT '[]',
                    pairing_salt TEXT,
                    pairing_digest TEXT,
                    pairing_expires_at TEXT,
                    pairing_attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catcher_clients (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    token_salt TEXT NOT NULL,
                    token_digest TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE TABLE IF NOT EXISTS catcher_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    client_id TEXT,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                """INSERT OR IGNORE INTO catcher_settings(
                       id,enabled,auto_enable,allowed_sources_json,updated_at
                   ) VALUES(1,0,0,?,?)""",
                (json.dumps([
                    "web-deepseek", "web-kimi", "web-qwen", "web-doubao",
                    "web-perplexity", "web-wenxin", "web-longcat",
                ]), now_iso()),
            )
            migration = "catcher-allow-web-longcat-v1"
            if not db.execute("SELECT 1 FROM app_migrations WHERE name=?", (migration,)).fetchone():
                row = db.execute("SELECT allowed_sources_json FROM catcher_settings WHERE id=1").fetchone()
                try:
                    allowed_source_ids = json.loads(row["allowed_sources_json"] or "[]") if row else []
                except json.JSONDecodeError:
                    allowed_source_ids = []
                if "web-longcat" not in allowed_source_ids:
                    allowed_source_ids.append("web-longcat")
                    db.execute(
                        "UPDATE catcher_settings SET allowed_sources_json=?,updated_at=? WHERE id=1",
                        (json.dumps(allowed_source_ids), now_iso()),
                    )
                db.execute(
                    "INSERT INTO app_migrations(name,applied_at) VALUES(?,?)",
                    (migration, now_iso()),
                )

    def _seed_sources(self) -> None:
        seeds = [
            ("web-auto", "WebAuto", "web_auto", "", "web-auto", "auto", True,
             "智能来源只使用已启用且健康的条目；自定义来源严格使用你的名单。调度可选择优先来源或均衡轮询。"),
            ("web-doubao", "豆包 Web", "doubao_web", "https://www.doubao.com", "doubao-web", "0", True,
             "F12 → 网络 → Fetch/XHR → 过滤 completion → 发送一条新消息 → 选择 POST completion → 右键复制 → Copy as cURL (bash)\n请求地址必须包含 a_bogus 与 msToken；找不到时清空过滤后再发一次。"),
            ("web-qwen", "千问 Web", "qwen_web", "https://chat.qwen.ai", "qwen-web", "qwen3.7-plus", True,
             "F12 → 网络 → Fetch/XHR → 过滤 api/v2/chat/completions → 发送一条新消息 → 选择 POST 请求 → 右键复制 → Copy as cURL (bash)\n完整 cURL 应包含 Cookie 与 bx 风控请求头。"),
            ("web-deepseek", "DeepSeek Web", "deepseek_web", "https://chat.deepseek.com", "deepseek-web", "default", True,
             "F12 → 网络 → Fetch/XHR → 过滤 api/v0/chat/completion → 发送一条新消息 → 选择 POST 请求 → 右键复制 → Copy as cURL (bash)\n必须包含 Authorization；请勿使用 HAR。"),
            ("web-yuanbao", "元宝 Web", "unsupported_web", "https://yuanbao.tencent.com", "yuanbao-web", "default", False,
             "此来源暂未接入，无需抓取 cURL。元宝网页请求依赖动态安全签名，当前不建议配置。"),
            ("web-kimi", "Kimi Web", "kimi_web", "https://www.kimi.com", "kimi-web", "k2d6-chat", False,
             "F12 → 网络 → Fetch/XHR → 过滤 ChatService/Chat → 发送一条新消息 → 选择 POST 请求 → 右键复制 → Copy as cURL (bash)\n必须包含 Authorization；手动复制的访问令牌约 15 分钟后过期，建议使用浏览器同步自动获取刷新令牌。"),
            ("web-perplexity", "Perplexity Web", "perplexity_web", "https://www.perplexity.ai", "perplexity-web", "turbo", False,
             "F12 → 网络 → Fetch/XHR → 先发送一条新消息 → 过滤 perplexity_ask（搜不到改搜 rest/sse，并切到“全部”）→ 选择 POST 请求 → 右键复制 → Copy as cURL (bash)\nx-pplx-account 是该请求“标头”里的账号标识，不是过滤关键词；选中请求后在 标头 → 请求标头 中确认。"),
            ("web-wenxin", "文心 Web", "wenxin_web", "https://wenxin.baidu.com", "wenxin-web", "smartMode", False,
             "F12 → 网络 → Fetch/XHR → 过滤 /aichat/api/conversation → 发送一条新消息 → 选择 POST 请求 → 右键复制 → Copy as cURL (bash)\n必须复制完整 cURL（HAR 会移除 Cookie）；请求正文需包含 chat_token。"),
            ("web-longcat", "LongCat Web", "longcat_web", "https://longcat.chat", "longcat-web", "LongCat-2.0-Preview-LongCatAI", False,
             "此来源依赖浏览器实时生成 H5Guard 签名。请安装并配对 AiBridge Catcher，授权 longcat.chat，然后在官网发送一句消息完成中继配置；使用期间需保留 LongCat 页面。"),
        ]
        with self._connect() as db:
            for sid, name, protocol, base_url, public_name, upstream, enabled, guide in seeds:
                stamp = now_iso()
                db.execute(
                    "INSERT OR IGNORE INTO sources(id,name,kind,protocol,base_url,enabled,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, name, "web", protocol, base_url, int(enabled), json.dumps({"guide": guide}, ensure_ascii=False), stamp, stamp),
                )
                row = db.execute("SELECT config_json,protocol,credential_cipher FROM sources WHERE id=?", (sid,)).fetchone()
                config = json.loads(row["config_json"] or "{}")
                changed = False
                if config.get("guide") != guide:
                    config["guide"] = guide
                    changed = True
                if protocol != "web_auto" and "max_concurrency" not in config:
                    config["max_concurrency"] = 1 if protocol == "longcat_web" else 3
                    changed = True
                if protocol == "web_auto" and config.get("routing_mode") not in {"smart", "custom"}:
                    config["routing_mode"] = "smart"
                    config["source_ids"] = []
                    changed = True
                if protocol == "web_auto" and config.get("dispatch_mode") not in {"priority", "balanced"}:
                    config["dispatch_mode"] = "priority"
                    changed = True
                if changed:
                    db.execute("UPDATE sources SET config_json=?,updated_at=? WHERE id=?", (json.dumps(config, ensure_ascii=False), stamp, sid))
                if sid == "web-kimi" and row["protocol"] == "unsupported_web":
                    has_credential = bool(row["credential_cipher"])
                    db.execute(
                        "UPDATE sources SET protocol=?,health_status=?,health_message=?,last_checked_at=NULL,updated_at=? WHERE id=?",
                        (protocol, "unchecked" if has_credential else "unconfigured", "等待健康检查" if has_credential else "尚未配置凭据", stamp, sid),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO models(id,source_id,public_name,upstream_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (f"model-{sid}", sid, public_name, upstream, 1, stamp, stamp),
                )
                db.execute("UPDATE models SET enabled=1,updated_at=? WHERE id=?", (stamp, f"model-{sid}"))
                if sid == "web-kimi":
                    db.execute("UPDATE models SET upstream_name=?,updated_at=? WHERE source_id=? AND upstream_name='default'", (upstream, stamp, sid))

    def _import_ccswitch_deepseek(self) -> None:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM sources WHERE id='api-deepseek'").fetchone():
                return
        source_db = Path.home() / ".cc-switch" / "cc-switch.db"
        if not source_db.exists():
            return
        try:
            source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            row = source.execute(
                "SELECT settings_config FROM providers WHERE app_type='claude' AND lower(name)='deepseek' ORDER BY is_current DESC LIMIT 1"
            ).fetchone()
            source.close()
            if not row:
                return
            raw = json.loads(row["settings_config"])
            env = raw.get("env", raw)
            token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            base_url = env.get("ANTHROPIC_BASE_URL")
            if not token or not base_url:
                return
            stamp = now_iso()
            config = {"auth_mode": "bearer", "imported_from": "CC Switch", "anthropic_version": "2023-06-01"}
            credential = self.secrets.encrypt_json({"api_key": token})
            models: list[str] = []
            for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
                model = env.get(key)
                if model and model not in models:
                    models.append(model)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO sources(id,name,kind,protocol,base_url,enabled,config_json,credential_cipher,health_status,health_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("api-deepseek", "DeepSeek 官方", "api", "anthropic", base_url.rstrip("/"), 1, json.dumps(config), credential, "unchecked", "已从 CC Switch 导入，等待健康检查", stamp, stamp),
                )
                for model in models or ["deepseek-flash"]:
                    db.execute(
                        "INSERT OR IGNORE INTO models(id,source_id,public_name,upstream_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "api-deepseek", model, model, 1, stamp, stamp),
                    )
        except (sqlite3.Error, ValueError, OSError, json.JSONDecodeError):
            return

    def _seed_api_source_icons(self) -> None:
        official_deepseek = (Path(__file__).resolve().parents[1] / "public" / "admin" / "providers" / "deepseek.svg").read_text(encoding="utf-8")
        icons = {
            "api-deepseek": official_deepseek,
        }
        with self._connect() as db:
            for source_id, icon_svg in icons.items():
                row = db.execute("SELECT config_json FROM sources WHERE id=? AND kind='api'", (source_id,)).fetchone()
                if not row:
                    continue
                config = json.loads(row["config_json"] or "{}")
                current = str(config.get("icon_svg") or "")
                if current and "M5 17.5c3.1" not in current:
                    continue
                config["icon_svg"] = icon_svg
                db.execute(
                    "UPDATE sources SET config_json=?,updated_at=? WHERE id=?",
                    (json.dumps(config, ensure_ascii=False), now_iso(), source_id),
                )

    @staticmethod
    def _source_public(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["config"] = json.loads(item.pop("config_json") or "{}")
        item["has_credential"] = bool(item.pop("credential_cipher"))
        return item

    def list_sources(self, include_secret: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM sources
                   ORDER BY CASE
                       WHEN id='web-auto' THEN 0
                       WHEN id='web-deepseek' THEN 1
                       WHEN id='web-qwen' THEN 2
                       WHEN id='web-doubao' THEN 3
                       WHEN id='web-kimi' THEN 4
                       WHEN id='web-perplexity' THEN 5
                       WHEN id='web-wenxin' THEN 6
                       WHEN id='web-longcat' THEN 7
                       WHEN id='web-yuanbao' THEN 8
                       WHEN kind='web' THEN 9
                       ELSE 10
                   END, created_at"""
            ).fetchall()
            result = []
            for row in rows:
                models = [dict(x) | {"enabled": bool(x["enabled"])} for x in db.execute("SELECT * FROM models WHERE source_id=? ORDER BY created_at", (row["id"],))]
                item = self._source_public(row)
                item["models"] = models
                if include_secret:
                    item["credential"] = self.secrets.decrypt_json(row["credential_cipher"])
                result.append(item)
            return result

    def get_source(self, source_id: str, include_secret: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                return None
            item = self._source_public(row)
            if include_secret:
                item["credential"] = self.secrets.decrypt_json(row["credential_cipher"])
            return item

    def save_source(self, data: dict[str, Any], source_id: str | None = None) -> str:
        source_id = source_id or str(uuid.uuid4())
        stamp = now_iso()
        with self._connect() as db:
            old = db.execute("SELECT credential_cipher,config_json FROM sources WHERE id=?", (source_id,)).fetchone()
            credential = data.get("credential")
            if credential and old and old["credential_cipher"]:
                credential = self.secrets.decrypt_json(old["credential_cipher"]) | {k: v for k, v in credential.items() if v not in (None, "", {})}
            cipher = self.secrets.encrypt_json(credential) if credential else (old["credential_cipher"] if old else None)
            config = data.get("config")
            if config is None and old:
                config = json.loads(old["config_json"] or "{}")
            config = config or {}
            if data.get("kind", "api") == "web" and data.get("protocol") != "web_auto":
                try:
                    max_concurrency = int(config.get("max_concurrency", 3))
                except (TypeError, ValueError):
                    max_concurrency = 3
                config["max_concurrency"] = max(1, min(32, max_concurrency))
            if data.get("protocol") == "web_auto":
                config["routing_mode"] = "custom" if config.get("routing_mode") == "custom" else "smart"
                config["dispatch_mode"] = "balanced" if config.get("dispatch_mode") == "balanced" else "priority"
                source_ids = config.get("source_ids") if isinstance(config.get("source_ids"), list) else []
                config["source_ids"] = list(dict.fromkeys(
                    source_id for source_id in source_ids
                    if isinstance(source_id, str) and source_id != "web-auto"
                ))
            db.execute(
                """INSERT INTO sources(id,name,kind,protocol,base_url,enabled,config_json,credential_cipher,health_status,health_message,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,protocol=excluded.protocol,base_url=excluded.base_url,
                   enabled=excluded.enabled,config_json=excluded.config_json,credential_cipher=excluded.credential_cipher,
                   health_status=CASE WHEN excluded.credential_cipher IS NULL THEN 'unconfigured' ELSE 'unchecked' END,
                   health_message=CASE WHEN excluded.credential_cipher IS NULL THEN '尚未配置凭据' ELSE '配置已保存，等待健康检查' END,updated_at=excluded.updated_at""",
                (source_id, data["name"], data.get("kind", "api"), data["protocol"], data.get("base_url", "").rstrip("/"), int(data.get("enabled", True)), json.dumps(config, ensure_ascii=False), cipher, "unchecked" if cipher else "unconfigured", "配置已保存，等待健康检查" if cipher else "尚未配置凭据", stamp, stamp),
            )
        return source_id

    def delete_source(self, source_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE sources SET enabled=?,updated_at=? WHERE id=?", (int(enabled), now_iso(), source_id))

    def update_health(self, source_id: str, status: str, message: str) -> None:
        with self._connect() as db:
            stamp = now_iso()
            db.execute("UPDATE sources SET health_status=?,health_message=?,last_checked_at=?,updated_at=? WHERE id=?", (status, message[:500], stamp, stamp, source_id))

    def update_source_credential(self, source_id: str, credential: dict[str, Any]) -> None:
        cipher = self.secrets.encrypt_json(credential)
        with self._connect() as db:
            db.execute(
                "UPDATE sources SET credential_cipher=?,updated_at=? WHERE id=?",
                (cipher, now_iso(), source_id),
            )

    def catcher_state(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM catcher_settings WHERE id=1").fetchone()
            clients = db.execute(
                """SELECT id,name,token_prefix,enabled,created_at,last_seen_at
                   FROM catcher_clients ORDER BY created_at DESC"""
            ).fetchall()
            events = db.execute(
                """SELECT source_id,client_id,status,message,created_at
                   FROM catcher_events ORDER BY id DESC LIMIT 12"""
            ).fetchall()
        expires_at = row["pairing_expires_at"] if row else None
        pairing_active = False
        if expires_at:
            try:
                pairing_active = datetime.fromisoformat(expires_at) > datetime.now(UTC)
            except ValueError:
                pairing_active = False
        return {
            "enabled": bool(row["enabled"]) if row else False,
            "auto_enable": bool(row["auto_enable"]) if row else False,
            "allowed_source_ids": json.loads(row["allowed_sources_json"] or "[]") if row else [],
            "pairing_active": pairing_active,
            "pairing_expires_at": expires_at if pairing_active else None,
            "clients": [dict(item) | {"enabled": bool(item["enabled"])} for item in clients],
            "events": [dict(item) for item in events],
        }

    def update_catcher_settings(
        self,
        *,
        enabled: bool,
        auto_enable: bool,
        allowed_source_ids: list[str],
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE catcher_settings
                   SET enabled=?,auto_enable=?,allowed_sources_json=?,updated_at=?
                   WHERE id=1""",
                (int(enabled), int(auto_enable), json.dumps(allowed_source_ids), now_iso()),
            )

    def create_catcher_pairing(self) -> tuple[str, str]:
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt, digest = hash_token(code)
        expires_at = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        with self._connect() as db:
            db.execute(
                """UPDATE catcher_settings
                   SET pairing_salt=?,pairing_digest=?,pairing_expires_at=?,pairing_attempts=0,updated_at=?
                   WHERE id=1""",
                (salt, digest, expires_at, now_iso()),
            )
        return code, expires_at

    def consume_catcher_pairing(self, code: str, client_name: str) -> tuple[dict[str, Any], str] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM catcher_settings WHERE id=1").fetchone()
            if not row or not row["enabled"] or not row["pairing_salt"] or not row["pairing_digest"]:
                return None
            try:
                active = datetime.fromisoformat(row["pairing_expires_at"]) > datetime.now(UTC)
            except (TypeError, ValueError):
                active = False
            if not active or row["pairing_attempts"] >= 10:
                return None
            if not verify_token(code.strip(), row["pairing_salt"], row["pairing_digest"]):
                db.execute(
                    "UPDATE catcher_settings SET pairing_attempts=pairing_attempts+1,updated_at=? WHERE id=1",
                    (now_iso(),),
                )
                return None

            token = "abc_" + secrets.token_urlsafe(32)
            salt, digest = hash_token(token)
            client_id = str(uuid.uuid4())
            stamp = now_iso()
            name = (client_name or "Chrome 扩展").strip()[:80] or "Chrome 扩展"
            db.execute(
                """INSERT INTO catcher_clients(
                       id,name,token_prefix,token_salt,token_digest,enabled,created_at
                   ) VALUES(?,?,?,?,?,1,?)""",
                (client_id, name, token[:10], salt, digest, stamp),
            )
            db.execute(
                """UPDATE catcher_settings
                   SET pairing_salt=NULL,pairing_digest=NULL,pairing_expires_at=NULL,pairing_attempts=0,updated_at=?
                   WHERE id=1""",
                (stamp,),
            )
        return {
            "id": client_id,
            "name": name,
            "token_prefix": token[:10],
            "enabled": True,
            "created_at": stamp,
            "last_seen_at": None,
        }, token

    def verify_catcher_client(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,name,token_prefix,token_salt,token_digest,enabled,created_at,last_seen_at
                   FROM catcher_clients WHERE enabled=1"""
            ).fetchall()
        for row in rows:
            if token.startswith(row["token_prefix"]) and verify_token(token, row["token_salt"], row["token_digest"]):
                return dict(row) | {"enabled": True}
        return None

    def touch_catcher_client(self, client_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE catcher_clients SET last_seen_at=? WHERE id=?", (now_iso(), client_id))

    def revoke_catcher_client(self, client_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM catcher_clients WHERE id=?", (client_id,))

    def add_catcher_event(self, source_id: str, client_id: str | None, status: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO catcher_events(source_id,client_id,status,message,created_at) VALUES(?,?,?,?,?)",
                (source_id, client_id, status, message[:500], now_iso()),
            )
            db.execute(
                """DELETE FROM catcher_events WHERE id NOT IN (
                       SELECT id FROM catcher_events ORDER BY id DESC LIMIT 80
                   )"""
            )

    def save_model(self, source_id: str, public_name: str, upstream_name: str, enabled: bool = True, model_id: str | None = None) -> str:
        model_id = model_id or str(uuid.uuid4())
        stamp = now_iso()
        with self._connect() as db:
            db.execute(
                """INSERT INTO models(id,source_id,public_name,upstream_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET public_name=excluded.public_name,upstream_name=excluded.upstream_name,enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (model_id, source_id, public_name.strip(), upstream_name.strip(), int(enabled), stamp, stamp),
            )
        return model_id

    def delete_model(self, model_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM models WHERE id=?", (model_id,))

    def delete_models_except(self, source_id: str, keep_ids: list[str]) -> None:
        with self._connect() as db:
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                db.execute(f"DELETE FROM models WHERE source_id=? AND id NOT IN ({placeholders})", (source_id, *keep_ids))
            else:
                db.execute("DELETE FROM models WHERE source_id=?", (source_id,))

    def resolve_model(self, public_name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT m.* FROM models m JOIN sources s ON s.id=m.source_id WHERE m.public_name=? AND m.enabled=1 AND s.enabled=1",
                (public_name,),
            ).fetchone()
            if not row:
                return None
            source = self.get_source(row["source_id"], include_secret=True)
            return source, dict(row)

    def list_models(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT m.*,s.name source_name FROM models m JOIN sources s ON s.id=m.source_id WHERE m.enabled=1 AND s.enabled=1 ORDER BY m.public_name")]

    def create_key(self, name: str) -> tuple[dict[str, Any], str]:
        token = new_gateway_token()
        salt, digest = hash_token(token)
        key_id = str(uuid.uuid4())
        stamp = now_iso()
        with self._connect() as db:
            db.execute("INSERT INTO gateway_keys(id,name,prefix,salt,digest,created_at) VALUES(?,?,?,?,?,?)", (key_id, name.strip() or "未命名 Token", token[:11], salt, digest, stamp))
        return {"id": key_id, "name": name.strip() or "未命名 Token", "prefix": token[:11], "enabled": True, "created_at": stamp, "last_used_at": None}, token

    def list_keys(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) | {"enabled": bool(row["enabled"])} for row in db.execute("SELECT id,name,prefix,enabled,created_at,last_used_at FROM gateway_keys ORDER BY created_at DESC")]

    def verify_key(self, token: str) -> bool:
        if not token:
            return False
        with self._connect() as db:
            rows = db.execute("SELECT * FROM gateway_keys WHERE enabled=1 AND prefix=?", (token[:11],)).fetchall()
            for row in rows:
                if verify_token(token, row["salt"], row["digest"]):
                    db.execute("UPDATE gateway_keys SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
                    return True
        return False

    def delete_key(self, key_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM gateway_keys WHERE id=?", (key_id,))

    def add_log(self, request_id: str, source: str, model: str, protocol: str, status: str, latency_ms: int | None, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO request_logs(request_id,source_name,model_name,protocol,status,latency_ms,error,created_at) VALUES(?,?,?,?,?,?,?,?)", (request_id, source, model, protocol, status, latency_ms, (error or "")[:500] or None, now_iso()))
            db.execute("DELETE FROM request_logs WHERE id NOT IN (SELECT id FROM request_logs ORDER BY id DESC LIMIT 200)")

    def list_logs(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (min(limit, 200),))]
