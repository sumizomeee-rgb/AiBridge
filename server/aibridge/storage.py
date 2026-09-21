from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
                """
            )

    def _seed_sources(self) -> None:
        seeds = [
            ("web-doubao", "豆包 Web", "doubao_web", "https://www.doubao.com", "doubao-web", "0", True,
             "F12 → Network，发送一句测试消息，找到 /chat/completion，右键 Copy → Copy as cURL (bash)。推荐粘贴完整 cURL；仅 Cookie 往往不足以通过豆包的动态签名校验。"),
            ("web-qwen", "千问 Web", "qwen_web", "https://chat.qwen.ai", "qwen-web", "qwen3.7-plus", True,
             "登录 chat.qwen.ai 后按 F12 → Network，发送一句测试消息，找到 /api/v2/chat/completions，右键 Copy → Copy as cURL (bash)。粘贴完整 cURL 可同时取得 Cookie 与 bx 风控请求头。"),
            ("web-yuanbao", "元宝 Web", "unsupported_web", "https://yuanbao.tencent.com", "yuanbao-web", "default", False,
             "此适配器尚未接入。后续录制一次完整对话 HAR 或提供 Copy as cURL 后再实现。"),
            ("web-kimi", "Kimi Web", "unsupported_web", "https://www.kimi.com", "kimi-web", "default", False,
             "此适配器尚未接入。后续录制一次完整对话 HAR 或提供 Copy as cURL 后再实现。"),
        ]
        with self._connect() as db:
            for sid, name, protocol, base_url, public_name, upstream, enabled, guide in seeds:
                stamp = now_iso()
                db.execute(
                    "INSERT OR IGNORE INTO sources(id,name,kind,protocol,base_url,enabled,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, name, "web", protocol, base_url, int(enabled), json.dumps({"guide": guide}, ensure_ascii=False), stamp, stamp),
                )
                db.execute(
                    "INSERT OR IGNORE INTO models(id,source_id,public_name,upstream_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (f"model-{sid}", sid, public_name, upstream, int(enabled), stamp, stamp),
                )

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

    @staticmethod
    def _source_public(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["config"] = json.loads(item.pop("config_json") or "{}")
        item["has_credential"] = bool(item.pop("credential_cipher"))
        return item

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM sources ORDER BY kind DESC, created_at").fetchall()
            result = []
            for row in rows:
                models = [dict(x) | {"enabled": bool(x["enabled"])} for x in db.execute("SELECT * FROM models WHERE source_id=? ORDER BY created_at", (row["id"],))]
                item = self._source_public(row)
                item["models"] = models
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
            db.execute(
                """INSERT INTO sources(id,name,kind,protocol,base_url,enabled,config_json,credential_cipher,health_status,health_message,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,protocol=excluded.protocol,base_url=excluded.base_url,
                   enabled=excluded.enabled,config_json=excluded.config_json,credential_cipher=excluded.credential_cipher,
                   health_status=CASE WHEN excluded.credential_cipher IS NULL THEN 'unconfigured' ELSE sources.health_status END,
                   health_message=CASE WHEN excluded.credential_cipher IS NULL THEN '尚未配置凭据' ELSE sources.health_message END,updated_at=excluded.updated_at""",
                (source_id, data["name"], data.get("kind", "api"), data["protocol"], data.get("base_url", "").rstrip("/"), int(data.get("enabled", True)), json.dumps(config, ensure_ascii=False), cipher, "unchecked" if cipher else "unconfigured", "配置已保存，等待健康检查" if cipher else "尚未配置凭据", stamp, stamp),
            )
        return source_id

    def delete_source(self, source_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def update_health(self, source_id: str, status: str, message: str) -> None:
        with self._connect() as db:
            stamp = now_iso()
            db.execute("UPDATE sources SET health_status=?,health_message=?,last_checked_at=?,updated_at=? WHERE id=?", (status, message[:500], stamp, stamp, source_id))

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
