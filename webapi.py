"""千小妹还在吃 (NekroAgent 移植版) - WebUI / WebAPI 路由层

二期移植：AstrBot `register_web_api` 的 32 个 Quart 路由 → FastAPI APIRouter。
行为规格见 chisa_webapi_fastapi_spec.md（信封、状态码、目录结构、安全校验逐字对齐）。

约定：
- 路由统一挂 `/api/<name>`（前端 bridge shim 以 `/api/` 为前缀）。
- 二进制图片全部以 data URL（base64）走 JSON，无二进制响应。
- `{"status":"missing"}` 一律 HTTP 200（前端依赖此约定，不 reject）。
- 网络层复用 shop.py 的逐跳 HTTPS 白名单校验 / 镜像测速 / 安全解压。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from nekro_agent.api.core import logger

from . import shop

# ============================================================
# 常量（与 AstrBot 版逐字一致）
# ============================================================

OFFICIAL_SKIN_SOURCE = "dddada123/astrbot_plugin_chisa_still_eating_photo"
BUILTIN_SKIN_IDS = {"maple_dew", "yy_xuanling", "chisa_red_black", "chisa_red_white"}
BUILTIN_SKIN_ASSETS = {"maple_dew": "03.jpg", "yy_xuanling": "04.jpg"}
SKIN_ASSETS = {"03.jpg": "skin/03.jpg", "04.jpg": "skin/04.jpg"}
SKIN_ASSET_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SKIN_VAR_WHITELIST = {
    "--hover-tint", "--bg", "--panel", "--card", "--text", "--muted",
    "--primary", "--primary-hover", "--primary-contrast", "--line", "--shadow",
    "--surface", "--surface-dark", "--input-bg", "--overlay",
}
COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{6}|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(0|1|0?\.\d+)\s*\))$"
)
SHADOW_RE = re.compile(
    r"^(?:-?(?:0|\d{1,3}(?:\.\d+)?px)\s+){2,4}"
    r"(?:#[0-9a-fA-F]{6}|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(?:0|1|0?\.\d+)\s*\))(?:\s+inset)?$"
)
SKIN_JSON_MAX_BYTES = 1024 * 1024
SKIN_ASSET_MAX_BYTES = 96 * 1024 * 1024
SKIN_ASSET_CHUNK_BYTES = 192 * 1024

REPO_URL_RE = re.compile(
    r"github\.com[/:]([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)"
)
SKIN_SOURCE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)
SKIN_SOURCE_PAIR_RE = re.compile(r"([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")
SKIN_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")

_branch_cache: dict[str, str] = {}


MASCOT_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp"}


def register(plugin, get_state, get_config=None):
    """挂载 WebAPI 路由。get_state() 返回插件运行时 _State；get_config() 返回插件配置。"""

    router = APIRouter()

    # ------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------

    def D() -> Path:
        return get_state().data_dir

    def reload_caches():
        try:
            get_state().reload()
        except Exception as exc:
            logger.warning(f"[千小妹 WebUI] 重载缓存失败: {exc}")

    def _read_json(path: Path, default: Any = None) -> Any:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return default

    def _atomic_write_json(path: Path, data: Any, indent: int = 2):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{id(data)}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))

    def _ok(**kw):
        return JSONResponse({"status": "success", **kw})

    def _missing():
        return JSONResponse({"status": "missing"})

    def _err(message: str, code: int = 400, status: str = "error"):
        return JSONResponse({"status": status, "message": message}, status_code=code)

    def _safe_id(repo_id: Any) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(repo_id))

    def _sniff_image_mime(raw: bytes, fallback: str = "image/png") -> str:
        """按文件魔数嗅探图片类型（自定义吉祥物允许任意格式但沿用 Chisa.gif 路径）。"""
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw[4:12] == b"WEBPVP8 " or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
            return "image/webp"
        return fallback

    def _data_url(path: Path) -> str:
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        raw = Path(path).read_bytes()
        mime = _sniff_image_mime(raw, mime)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _under_base(full: Path, base: Path) -> bool:
        try:
            return os.path.commonpath((str(base.resolve()), str(full.resolve()))) == str(base.resolve())
        except Exception:
            return False

    # ------------------------------------------------------------
    # 商店 / 工坊目录
    # ------------------------------------------------------------

    def _wp() -> Path:
        return D() / "Webui-PIC"

    def _store_dir(store_type: str, repo_id: str = "") -> Path:
        if store_type == "custom":
            if not repo_id or not str(repo_id).strip():
                return _wp() / "Workshop" / "_empty_" / "index"
            return _wp() / "Workshop" / _safe_id(repo_id) / "index"
        return _wp() / "Shop" / "index"

    def _banner_dir() -> Path:
        return _wp() / "banner"

    def _cover_dir(store_type: str, repo_id: str = "") -> Path:
        if store_type == "custom" and repo_id and str(repo_id).strip():
            return (_wp() / "Workshop" / "cover" / _safe_id(repo_id)).resolve()
        return (_wp() / "Shop" / "cover").resolve()

    def _workshop_root() -> Path:
        return _wp() / "Workshop"

    def _skins_root() -> Path:
        return _wp() / "skins"

    # ------------------------------------------------------------
    # 网络层（复用 shop.py）
    # ------------------------------------------------------------

    async def _fetch(url: str, max_bytes: int, trust_env: bool = False, timeout: float = 25) -> bytes | None:
        async with httpx.AsyncClient(trust_env=trust_env) as client:
            return await shop._fetch_bytes(client, url, max_bytes=max_bytes, timeout=timeout)

    async def _download(url: str, target: Path, max_bytes: int, trust_env: bool = False,
                        timeout: float = 300, progress=None) -> tuple[str, int]:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(trust_env=trust_env) as client:
            return await shop._download_file(client, url, Path(target), max_bytes=max_bytes,
                                             timeout=timeout, progress=progress)

    def _reset_node_cache():
        shop._best_node = None

    def _node_valid(node: str) -> bool:
        return node in ("direct", "") or node in shop.MIRROR_NODES

    async def _resolve_node(raw: str) -> str:
        node = (raw or "").strip().lower()
        if node == "smart":
            try:
                node = await shop.get_optimal_node()
            except Exception:
                node = "direct"
        return node

    def _proxy(node: str, original: str) -> tuple[str, bool]:
        """返回 (url, trust_env)。镜像节点绕过系统代理，直连遵循系统代理。"""
        if node and node not in ("direct", "") and node in shop.MIRROR_NODES:
            return f"https://{node}/{original}", False
        return original, True

    def _extract_owner_repo(url: str) -> tuple[str | None, str | None]:
        m = REPO_URL_RE.search(str(url or ""))
        if not m:
            return None, None
        return m.group(1), m.group(2)

    # ------------------------------------------------------------
    # 皮肤：源规范化 / 目录 / 校验
    # ------------------------------------------------------------

    def _normalize_skin_source(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        v = value.strip().rstrip("/")
        if not v:
            return None
        m = SKIN_SOURCE_URL_RE.match(v) or SKIN_SOURCE_PAIR_RE.match(v)
        if not m:
            return None
        owner, repo = m.group(1), m.group(2)
        if owner in (".", "..") or repo in (".", ".."):
            return None
        return f"{owner.lower()}/{repo.lower()}"

    def _skin_custom_sources() -> list[str]:
        data = _read_json(_skins_root() / "_sources.json", [])
        out: list[str] = []
        if isinstance(data, list):
            for item in data:
                n = _normalize_skin_source(item)
                if n and n != OFFICIAL_SKIN_SOURCE and n not in out:
                    out.append(n)
        return out

    def _skin_source_allowed(source: str) -> bool:
        return source == OFFICIAL_SKIN_SOURCE or source in _skin_custom_sources()

    def _skin_store_dir(source: str) -> Path:
        root = _skins_root()
        if source == OFFICIAL_SKIN_SOURCE:
            target = root / "OfficialWS"
        else:
            owner, repo = source.split("/", 1)
            h = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
            target = root / f"{owner}_{repo}_{h}"
            legacy = root / f"{owner}_{repo}"
            if not target.exists() and legacy.exists():
                try:
                    shutil.copytree(str(legacy), str(target))
                except Exception as exc:
                    logger.warning(f"[千小妹皮肤] 旧目录迁移失败: {exc}")
        return target

    def _safe_skin_asset_rel(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        v = value.strip()
        if ".." in v or "\\" in v:
            return None
        if not re.fullmatch(r"skin/[A-Za-z0-9][A-Za-z0-9._-]{0,110}", v):
            return None
        if Path(v).suffix.lower() not in SKIN_ASSET_EXTENSIONS:
            return None
        return v

    def _skin_is_glass(data: dict) -> bool:
        for k in ("type", "skin_type", "theme_type"):
            if str(data.get(k, "")).lower() in ("glass", "frosted", "frosted-glass", "transparent", "毛玻璃"):
                return True
        g = data.get("glass")
        if g is True:
            return True
        if isinstance(g, str) and g.strip().lower() in ("1", "true", "yes", "glass", "frosted", "毛玻璃"):
            return True
        return False

    def _validate_skin_json(data: Any) -> tuple[dict | None, str | None]:
        if not isinstance(data, dict):
            return None, "皮肤配置必须是对象"
        if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
            return None, "schema_version 必须为整数 1"
        sid = data.get("id")
        if not isinstance(sid, str) or not SKIN_ID_RE.match(sid):
            return None, "皮肤 id 非法"
        vars_in = data.get("vars")
        if not isinstance(vars_in, dict) or not vars_in:
            return None, "vars 必须是非空对象"
        vars_out: dict[str, str] = {}
        for k, v in vars_in.items():
            if k not in SKIN_VAR_WHITELIST or not isinstance(v, str):
                continue
            if COLOR_RE.match(v) or (k == "--shadow" and SHADOW_RE.match(v)):
                vars_out[k] = v
        if "--text" not in vars_out or "--bg" not in vars_out:
            return None, "vars 必须包含 --text 与 --bg"
        glass = _skin_is_glass(data)
        cleaned = {
            "schema_version": 1,
            "id": sid,
            "name": str(data.get("name") or sid)[:40],
            "author": str(data.get("author") or "")[:40],
            "type": "glass" if glass else "solid",
            "desc": str(data.get("desc") or "").strip()[:200],
            "vars": vars_out,
            "glass": glass,
            "_skin_type_version": 1,
            "is_custom": True,
        }
        quotes = data.get("quotes")
        if isinstance(quotes, list):
            qs = [s for q in quotes[:6] if (s := str(q)[:80]).strip()]
            if qs:
                cleaned["quotes"] = qs
        assets = data.get("assets")
        bg = assets.get("bg") if isinstance(assets, dict) else None
        if not bg:
            bg = data.get("bg") or data.get("background")
        if isinstance(bg, str) and bg.strip():
            rel = _safe_skin_asset_rel(bg)
            if not rel:
                return None, "assets.bg 路径非法"
            cleaned["assets"] = {"bg": rel}
        return cleaned, None

    def _write_cached_skin(cleaned: dict, source: str):
        store = _skin_store_dir(source)
        payload = dict(cleaned)
        payload["_source"] = source
        payload["_official"] = source == OFFICIAL_SKIN_SOURCE
        _atomic_write_json(store / "skin" / f"{cleaned['id']}.json", payload)

    def _load_cached_skin(skin_id: str, expected_source: str | None = None) -> dict | None:
        candidates: list[tuple[Path, str | None]] = []
        if expected_source:
            candidates.append((_skin_store_dir(expected_source) / "skin" / f"{skin_id}.json", expected_source))
        else:
            for src in [OFFICIAL_SKIN_SOURCE, *_skin_custom_sources()]:
                candidates.append((_skin_store_dir(src) / "skin" / f"{skin_id}.json", src))
            candidates.append((_skins_root() / f"{skin_id}.json", None))  # 旧版平铺

        for path, path_source in candidates:
            if not path.exists():
                continue
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            cleaned, err = _validate_skin_json(data)
            if err or not cleaned or cleaned["id"] != skin_id:
                continue
            source = _normalize_skin_source(data.get("_source")) or path_source or OFFICIAL_SKIN_SOURCE
            if expected_source and source != expected_source:
                continue
            cleaned["_source"] = source
            cleaned["_official"] = source == OFFICIAL_SKIN_SOURCE
            canonical = _skin_store_dir(source) / "skin" / f"{skin_id}.json"
            try:
                if Path(path).resolve() != canonical.resolve():
                    _atomic_write_json(canonical, cleaned)
                    try:
                        Path(path).unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            return cleaned
        return None

    async def _get_or_fetch_skin_config(skin_id: str, source: str, force: bool = False,
                                        preferred_node: str = "") -> tuple[dict | None, str | None, int]:
        """返回 (config, err_message, err_code)。"""
        cached = _load_cached_skin(skin_id, source)
        if cached and not force and cached.get("_skin_type_version") == 1:
            return cached, None, 200
        content = await _skin_fetch_raw(source, f"skin/{skin_id}.json", preferred_node)
        if content is None:
            return None, "皮肤配置拉取失败", 502
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except Exception:
            return None, "皮肤配置不是有效 JSON", 400
        cleaned, err = _validate_skin_json(data)
        if err:
            return None, f"皮肤配置校验失败: {err}", 400
        if cleaned["id"] != skin_id:
            return None, "皮肤配置 id 与请求不一致", 400
        try:
            _write_cached_skin(cleaned, source)
        except Exception:
            pass
        cleaned["_source"] = source
        cleaned["_official"] = source == OFFICIAL_SKIN_SOURCE
        return cleaned, None, 200

    async def _skin_fetch_raw(repo: str, rel_path: str, preferred_node: str = "") -> bytes | None:
        if rel_path != "skin/index.json" and not re.fullmatch(r"skin/[a-z0-9_]{1,40}\.json", rel_path):
            if _safe_skin_asset_rel(rel_path) is None:
                return None
        is_json = rel_path.endswith(".json")
        max_bytes = SKIN_JSON_MAX_BYTES if is_json else SKIN_ASSET_MAX_BYTES

        branch = _branch_cache.get(repo)
        if not branch:
            try:
                raw = await _fetch(f"https://api.github.com/repos/{repo}", 256 * 1024, trust_env=True, timeout=25)
                meta = json.loads(raw.decode("utf-8-sig")) if raw else {}
                b = meta.get("default_branch") if isinstance(meta, dict) else None
                if isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", b) and ".." not in b:
                    branch = b
                else:
                    branch = "main"
            except Exception:
                branch = "main"
            _branch_cache[repo] = branch

        node = (preferred_node or "").strip().lower()
        if node == "smart":
            try:
                node = await shop.get_optimal_node()
            except Exception:
                node = "direct"
        if not _node_valid(node):
            node = "direct"

        refs: list[str] = []
        for r in dict.fromkeys([branch, "main", "master"]):
            if r and r not in refs:
                refs.append(quote(r, safe="/"))

        for ref in refs:
            raw_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{rel_path}"
            attempts: list[tuple[str, bool]] = []
            if node not in ("direct", ""):
                attempts.append((f"https://{node}/{raw_url}", False))
            attempts.append((raw_url, True))
            attempts.append((f"https://cdn.jsdelivr.net/gh/{repo}@{ref}/{rel_path}", True))
            for n in shop.MIRROR_NODES:
                if n == node:
                    continue
                attempts.append((f"https://{n}/{raw_url}", False))
            for url, trust_env in attempts:
                try:
                    content = await _fetch(url, max_bytes, trust_env=trust_env, timeout=25 if is_json else 60)
                    if content:
                        return content
                except Exception:
                    continue
        return None

    def _skin_asset_descriptor(response_id, source: str, path: Path, cached: bool) -> dict:
        size = Path(path).stat().st_size
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return {
            "id": response_id,
            "skin_id": response_id,
            "source": source,
            "file": Path(path).name,
            "mime": mime,
            "size": size,
            "chunk_size": SKIN_ASSET_CHUNK_BYTES,
            "chunk_count": (size + SKIN_ASSET_CHUNK_BYTES - 1) // SKIN_ASSET_CHUNK_BYTES,
            "delivery": "chunked",
            "cached": cached,
        }

    async def _resolve_skin_asset(file: str, skin_id: str, source_q: str, force: bool, node: str,
                                  allow_fetch: bool) -> tuple[dict | None, JSONResponse | None]:
        """返回 (descriptor_info, error_response)。descriptor_info 含 path/source/response_id。"""
        # 内置皮肤分支
        if not skin_id or skin_id in BUILTIN_SKIN_ASSETS:
            expected = BUILTIN_SKIN_ASSETS.get(skin_id, file)
            if expected not in SKIN_ASSETS:
                return None, _err("Unknown built-in skin asset", 404)
            if file and file != expected:
                return None, _err("Built-in skin asset mismatch", 400)
            source = OFFICIAL_SKIN_SOURCE
            local = _skins_root() / expected
            rel = SKIN_ASSETS[expected]
            response_id = skin_id or None
        else:
            if not SKIN_ID_RE.match(skin_id):
                return None, _err("Invalid skin id", 400)
            if skin_id in BUILTIN_SKIN_IDS:
                source = OFFICIAL_SKIN_SOURCE
            elif source_q:
                norm = _normalize_skin_source(source_q)
                if not norm:
                    return None, _err("Invalid skin source", 400)
                source = norm
            else:
                cached_any = _load_cached_skin(skin_id)
                source = cached_any.get("_source") if cached_any else OFFICIAL_SKIN_SOURCE
            if skin_id in BUILTIN_SKIN_IDS and source != OFFICIAL_SKIN_SOURCE:
                return None, _err("Built-in skin source must be official", 403)

            config = _load_cached_skin(skin_id, source)
            if config is None:
                if not _skin_source_allowed(source):
                    return None, _err("Skin source is not subscribed and has no valid local cache", 403)
                if not allow_fetch:
                    return None, _err("Skin config is not cached", 409)
                config, err, code = await _get_or_fetch_skin_config(skin_id, source, force=False, preferred_node=node)
                if err:
                    return None, _err(err, code)
            rel = _safe_skin_asset_rel((config.get("assets") or {}).get("bg"))
            if not rel:
                return None, _err("Skin has no valid background asset", 404)
            asset_name = Path(rel).name
            if file and file != asset_name and file != rel:
                return None, _err("Skin asset does not match cached config", 400)
            local = _skin_store_dir(source) / "skin" / asset_name
            # 旧版 _assets 缓存迁移
            legacy = (_skins_root() / "_assets" / skin_id
                      / hashlib.sha256(source.encode("utf-8")).hexdigest()[:16] / asset_name)
            if not force and not local.exists() and legacy.exists():
                try:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(legacy), str(local))
                except Exception:
                    pass
            response_id = skin_id

        if not force and local.exists() and local.stat().st_size > 0:
            return {"path": local, "source": source, "response_id": response_id, "cached": True}, None

        if not allow_fetch:
            return None, _err("Skin asset is not cached; call skin_asset first", 409)

        content = await _skin_fetch_raw(source, rel, node)
        if content is None:
            return None, _err("Skin asset download failed", 502)
        tmp = local.with_name(local.name + f".tmp.{os.getpid()}.{id(content)}")
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(local))
        return {"path": local, "source": source, "response_id": response_id, "cached": False}, None

    # ============================================================
    # 1. DLC 商城与工坊
    # ============================================================

    @router.get("/api/dlc_catalog")
    async def dlc_catalog(store_type: str = "official", repo_id: str = ""):
        try:
            if store_type == "custom" and not repo_id.strip():
                return _missing()
            store = _store_dir(store_type, repo_id)
            catalog_path = store / "catalog.json"
            if not catalog_path.exists():
                return _missing()
            catalog = _read_json(catalog_path)
            metadata = _read_json(store / "metadata.json", {}) or {}
            return JSONResponse({"status": "success", "data": catalog, "metadata": metadata})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/fetch_dlc_catalog")
    async def fetch_dlc_catalog(payload: dict | None = Body(None)):
        body = payload or {}
        node_in = str(body.get("node", "smart")).strip().lower()
        custom_url = str(body.get("custom_url", "")).strip()
        store_type = str(body.get("store_type", "official"))
        repo_id = str(body.get("repo_id", ""))

        try:
            if store_type == "custom" and not custom_url:
                return _err("请先输入第三方仓库地址", 400)
            owner, repo_name = "dddada123", "astrbot_plugin_chisa_still_eating_photo"
            if store_type == "custom":
                o, r = _extract_owner_repo(custom_url)
                if not o:
                    return _err("无法识别仓库地址，请填写形如 https://github.com/作者名/仓库名 的完整地址", 400)
                owner, repo_name = o, r

            node = await _resolve_node(node_in)
            if not _node_valid(node):
                return _err("Invalid download node", 400)

            bases = [
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/main",
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/refs/heads/main",
                f"https://cdn.jsdelivr.net/gh/{owner}/{repo_name}@main",
            ]

            async def fetch_repo_file(path: str) -> tuple[bytes | None, str]:
                for idx, base in enumerate(bases):
                    host = "cdn.jsdelivr.net" if "jsdelivr" in base else "raw.githubusercontent.com"
                    if "jsdelivr" in base:
                        attempts = [(f"{base}/{path}", True)]
                        if node not in ("direct", ""):
                            attempts.append((f"https://{node}/{base}/{path}", False))
                    else:
                        attempts = []
                        if node not in ("direct", ""):
                            attempts.append((f"https://{node}/{base}/{path}", False))
                        attempts.append((f"{base}/{path}", True))
                    for url, trust_env in attempts:
                        try:
                            content = await _fetch(url, 8 * 1024 * 1024, trust_env=trust_env, timeout=20)
                            if content:
                                return content, f"source{idx + 1}({host})"
                        except Exception:
                            continue
                return None, "ALL_SOURCES_FAILED"

            catalog_res, meta_res = await asyncio.gather(
                fetch_repo_file("index/catalog.json"),
                fetch_repo_file("Chisa_DLC_Metadata.json"),
            )
            catalog_content, _ = catalog_res
            meta_content, _ = meta_res

            if not catalog_content:
                if node_in == "smart":
                    _reset_node_cache()
                return _err("Failed to fetch catalog.json", 500)

            try:
                catalog = json.loads(catalog_content.decode("utf-8-sig"))
            except Exception:
                return _err("Catalog must be an array of at most 5000 objects", 400)
            if not isinstance(catalog, list) or len(catalog) > 5000 or any(not isinstance(i, dict) for i in catalog):
                return _err("Catalog must be an array of at most 5000 objects", 400)

            store = _store_dir(store_type, repo_id)
            store.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(store / "catalog.json", catalog)

            meta_data: dict = {}
            if meta_content:
                try:
                    meta_data = json.loads(meta_content.decode("utf-8-sig"))
                    if not isinstance(meta_data, dict):
                        meta_data = {}
                except Exception:
                    meta_data = {}
            if not meta_data:
                existing = _read_json(store / "metadata.json", {}) or {}
                if isinstance(existing, dict) and existing and not existing.get("is_placeholder"):
                    meta_data = existing
                elif store_type == "custom":
                    meta_data = {
                        "store_name": f"{owner} 的创意工坊",
                        "author": owner,
                        "description": f"来自 {repo_name} 仓库的第三方内容",
                        "is_placeholder": True,
                    }
                else:
                    meta_data = {
                        "store_name": "千小妹官方云仓",
                        "author": "千小妹",
                        "description": "官方精选推荐内容",
                        "is_official": True,
                        "is_placeholder": True,
                    }
            with open(store / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False)

            # Banner 下载
            try:
                banner_dir = _banner_dir()
                banner_dir.mkdir(parents=True, exist_ok=True)
                banner_path = banner_dir / ("shop_banner.jpg" if store_type != "custom"
                                            else f"workshop_{_safe_id(repo_id)}.jpg")
                banner_bytes: bytes | None = None
                banner_url = meta_data.get("banner_url") if isinstance(meta_data, dict) else None
                if isinstance(banner_url, str) and banner_url.startswith("https://raw.githubusercontent.com/"):
                    url, trust_env = _proxy(node, banner_url)
                    try:
                        banner_bytes = await _fetch(url, 16 * 1024 * 1024, trust_env=trust_env, timeout=20)
                    except Exception:
                        banner_bytes = None
                    if not banner_bytes:
                        try:
                            banner_bytes = await _fetch(banner_url, 16 * 1024 * 1024, trust_env=True, timeout=20)
                        except Exception:
                            banner_bytes = None
                    if not banner_bytes:
                        m = re.search(r"raw\.githubusercontent\.com/([^/]+)/([^/]+)/(?:refs/heads/)?(.+)", banner_url)
                        if m:
                            jsd = f"https://cdn.jsdelivr.net/gh/{m.group(1)}/{m.group(2)}@{m.group(3)}"
                            try:
                                banner_bytes = await _fetch(jsd, 16 * 1024 * 1024, trust_env=True, timeout=20)
                            except Exception:
                                banner_bytes = None
                if not banner_bytes:
                    for cand in ("assets/banner.png", "assets/banner.jpg", "assets/banner.gif",
                                 "assets/banner.webp", "banner.png", "banner.jpg", "banner.gif", "banner.webp"):
                        content, _tag = await fetch_repo_file(cand)
                        if content:
                            banner_bytes = content
                            break
                if banner_bytes:
                    with open(banner_path, "wb") as f:
                        f.write(banner_bytes)
            except Exception as exc:
                logger.warning(f"[千小妹商会] banner 下载失败: {exc}")

            if store_type == "custom" and repo_id:
                try:
                    last = {
                        "url": custom_url,
                        "repo_id": repo_id,
                        "store_name": meta_data.get("store_name", "") if isinstance(meta_data, dict) else "",
                    }
                    _atomic_write_json(_workshop_root() / "_last_repo.json", last)
                except Exception:
                    pass

            return JSONResponse({"status": "success", "metadata": meta_data})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/last_custom_repo")
    async def last_custom_repo():
        try:
            data = _read_json(_workshop_root() / "_last_repo.json")
            if isinstance(data, dict) and data.get("repo_id"):
                return JSONResponse({"status": "success", "data": data})
            return _missing()
        except Exception:
            return _missing()

    @router.get("/api/workshop_bookmarks")
    async def workshop_bookmarks():
        try:
            data = _read_json(_workshop_root() / "_bookmarks.json", [])
            if not isinstance(data, list):
                data = []
            return JSONResponse({"status": "success", "data": data})
        except Exception:
            return JSONResponse({"status": "success", "data": []})

    @router.post("/api/save_workshop_bookmarks")
    async def save_workshop_bookmarks(payload: dict | None = Body(None)):
        body = payload or {}
        bookmarks = body.get("bookmarks", [])
        if not isinstance(bookmarks, list):
            return _err("Invalid bookmarks", 400)
        cleaned: list[dict] = []
        for b in bookmarks[:20]:
            if isinstance(b, dict):
                url = str(b.get("url", "")).strip()[:300]
                if not url:
                    continue
                cleaned.append({"url": url, "name": str(b.get("name", ""))[:80]})
            elif isinstance(b, str):
                s = b.strip()[:300]
                if s:
                    cleaned.append({"url": s, "name": ""})
        path = _workshop_root() / "_bookmarks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        return JSONResponse({"status": "success", "data": cleaned})

    @router.get("/api/dlc_metadata")
    async def dlc_metadata(store_type: str = "official", repo_id: str = ""):
        try:
            if store_type == "custom" and not repo_id.strip():
                return _missing()
            meta = _read_json(_store_dir(store_type, repo_id) / "metadata.json")
            if not isinstance(meta, dict):
                return _missing()
            return JSONResponse({"status": "success", "data": meta})
        except Exception:
            return _missing()

    @router.get("/api/store_banner")
    async def store_banner(store_type: str = "official", repo_id: str = ""):
        try:
            name = "shop_banner.jpg" if store_type != "custom" else f"workshop_{_safe_id(repo_id)}.jpg"
            path = _banner_dir() / name
            if not path.exists():
                return _missing()
            return JSONResponse({"status": "success", "data_url": _data_url(path)})
        except Exception:
            return _missing()

    @router.post("/api/fetch_single_cover")
    async def fetch_single_cover(payload: dict | None = Body(None)):
        body = payload or {}
        filename = str(body.get("file", "")).strip()
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return JSONResponse({"status": "error"}, status_code=400)
        node_in = str(body.get("node", "smart"))
        custom_url = str(body.get("custom_url", "")).strip()
        store_type = str(body.get("store_type", "official"))
        repo_id = str(body.get("repo_id", ""))
        try:
            raw_base = f"https://raw.githubusercontent.com/{shop.PHOTO_REPO}/main"
            if store_type == "custom":
                o, r = _extract_owner_repo(custom_url)
                if not o:
                    return _err("Invalid custom repository", 400)
                raw_base = f"https://raw.githubusercontent.com/{o}/{r}/main"
            node = await _resolve_node(node_in)
            if not _node_valid(node):
                return _err("Invalid download node", 400)
            original = f"{raw_base}/covers/{filename}"
            url, trust_env = _proxy(node, original)
            try:
                content = await _fetch(url, 16 * 1024 * 1024, trust_env=trust_env, timeout=15)
            except Exception:
                content = None
            if not content and node not in ("direct", ""):
                try:
                    content = await _fetch(original, 16 * 1024 * 1024, trust_env=True, timeout=15)
                except Exception:
                    content = None
            if not content:
                if node_in.strip().lower() == "smart":
                    _reset_node_cache()
                    return _err("ALL_NODES_FAILED", 500)
                return JSONResponse({"status": "error"}, status_code=500)
            cover_dir = _cover_dir(store_type, repo_id)
            cover_dir.mkdir(parents=True, exist_ok=True)
            full = cover_dir / filename
            tmp = full.with_name(full.name + f".tmp.{os.getpid()}")
            with open(tmp, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(full))
            return _ok()
        except Exception:
            return JSONResponse({"status": "error"}, status_code=500)

    @router.get("/api/dlc_cover")
    async def dlc_cover(file: str = "", store_type: str = "official", repo_id: str = ""):
        try:
            if not file or ".." in file or "/" in file or "\\" in file:
                return JSONResponse({"status": "error"}, status_code=400)
            path = _cover_dir(store_type, repo_id) / file
            if not path.exists():
                return _missing()
            return JSONResponse({"status": "success", "data_url": _data_url(path)})
        except Exception:
            return JSONResponse({"status": "error"}, status_code=500)

    @router.post("/api/download_dlc")
    async def download_dlc_route(payload: dict | None = Body(None)):
        body = payload or {}
        dlc_id = str(body.get("id", "")).strip()
        if not dlc_id or ".." in dlc_id or "/" in dlc_id or "\\" in dlc_id:
            return _err("Invalid DLC ID", 400)
        sha = str(body.get("sha256", "")).strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            return _err("Invalid SHA-256", 400)
        node_in = str(body.get("node", "smart"))
        store_type = str(body.get("store_type", "official"))
        custom_url = str(body.get("custom_url", "")).strip()
        repo_id = str(body.get("repo_id", ""))

        try:
            if store_type == "custom":
                if not custom_url:
                    return _err("Missing custom repository", 400)
                o, r = _extract_owner_repo(custom_url)
                if not o:
                    return _err("无法识别第三方仓库地址", 400)
                release_base = f"https://github.com/{o}/{r}/releases/download/Chisa_Dlc_Store"
            else:
                release_base = shop.RELEASE_URL

            node = await _resolve_node(node_in)
            if not _node_valid(node):
                return _err("Invalid download node", 400)
            original = f"{release_base}/{dlc_id}.zip"
            url, trust_env = _proxy(node, original)

            temp_zip = D() / f"temp_{dlc_id}.zip"
            try:
                actual_sha, _total = await _download(url, temp_zip, 512 * 1024 * 1024,
                                                     trust_env=trust_env, timeout=300)
            except Exception as e:
                if node_in.strip().lower() == "smart":
                    _reset_node_cache()
                    return _err("ALL_NODES_FAILED", 500)
                return _err(str(e), 500)

            if sha and actual_sha != sha:
                if temp_zip.exists():
                    temp_zip.unlink()
                return _err(f"Hash mismatch! Expected {sha[:8]}, got {actual_sha[:8]}", 500)

            try:
                await asyncio.to_thread(shop.extract_zip_safe, temp_zip, D())
            except Exception as e:
                if temp_zip.exists():
                    temp_zip.unlink()
                return _err(f"Extraction failed: {e}", 500)
            if temp_zip.exists():
                temp_zip.unlink()

            try:
                if store_type == "custom" and repo_id.strip():
                    rec_path = _wp() / "Workshop" / _safe_id(repo_id) / "index" / "downloaded.json"
                else:
                    rec_path = _wp() / "Shop" / "index" / "downloaded.json"
                rec_path.parent.mkdir(parents=True, exist_ok=True)
                records = _read_json(rec_path, [])
                if not isinstance(records, list):
                    records = []
                if dlc_id not in records:
                    records.append(dlc_id)
                    with open(rec_path, "w", encoding="utf-8") as f:
                        json.dump(records, f, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[千小妹商会] 写入已购清单失败: {exc}")

            reload_caches()
            return _ok()
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/get_download_progress")
    async def get_download_progress(payload: dict | None = Body(None)):
        body = payload or {}
        dlc_id = str(body.get("id", "")).strip()
        if not dlc_id or ".." in dlc_id or "/" in dlc_id or "\\" in dlc_id:
            return JSONResponse({"status": "error"}, status_code=400)
        temp = D() / f"temp_{dlc_id}.zip"
        size = temp.stat().st_size if temp.exists() else 0
        return JSONResponse({"status": "success", "size": size})

    @router.get("/api/get_dlc_downloaded")
    async def get_dlc_downloaded(store_type: str = "official", repo_id: str = ""):
        try:
            repo_id = (repo_id or "").strip()
            if store_type == "custom" and repo_id:
                rec_path = _wp() / "Workshop" / _safe_id(repo_id) / "index" / "downloaded.json"
            else:
                rec_path = _wp() / "Shop" / "index" / "downloaded.json"
            if not rec_path.exists():
                return JSONResponse({"status": "success", "data": []})
            records = _read_json(rec_path, [])
            if not isinstance(records, list):
                records = []
            return JSONResponse({"status": "success", "data": records})
        except Exception as e:
            return _err(str(e), 500)

    # ============================================================
    # 2. 皮肤工坊
    # ============================================================

    @router.get("/api/skin_index")
    async def skin_index(mode: str = "all", source: str = "", node: str = ""):
        try:
            if mode not in ("official", "custom", "all"):
                return _err("Invalid skin index mode", 400)
            requested = _normalize_skin_source(source) if source.strip() else None
            if source.strip() and not requested:
                return _err("Invalid skin source", 400)

            custom_sources = _skin_custom_sources()
            if mode == "official":
                if requested and requested != OFFICIAL_SKIN_SOURCE:
                    return _err("Source is not official", 403)
                sources = [OFFICIAL_SKIN_SOURCE]
            elif mode == "custom":
                if requested:
                    if requested == OFFICIAL_SKIN_SOURCE or requested not in custom_sources:
                        return _err("Custom skin source is not subscribed", 403)
                    sources = [requested]
                else:
                    sources = list(custom_sources)
            else:
                if requested and not _skin_source_allowed(requested):
                    return _err("Skin source is not subscribed", 403)
                sources = [requested] if requested else [OFFICIAL_SKIN_SOURCE, *custom_sources]

            merged: list[dict] = []
            for src in sources:
                try:
                    content = await _skin_fetch_raw(src, "skin/index.json", node)
                    if not content:
                        continue
                    idx = json.loads(content.decode("utf-8-sig"))
                    if not isinstance(idx, dict) or type(idx.get("schema_version")) is not int \
                            or not isinstance(idx.get("skins"), list):
                        continue
                    store = _skin_store_dir(src)
                    _atomic_write_json(store / "skin" / "index.json", idx)
                    for item in idx["skins"]:
                        if not isinstance(item, dict):
                            continue
                        iid = item.get("id")
                        if not isinstance(iid, str) or not SKIN_ID_RE.match(iid):
                            continue
                        if iid in BUILTIN_SKIN_IDS and src != OFFICIAL_SKIN_SOURCE:
                            continue
                        if item.get("config", f"skin/{iid}.json") != f"skin/{iid}.json":
                            continue
                        bg = item.get("bg")
                        assets = item.get("assets")
                        if not bg and isinstance(assets, dict):
                            bg = assets.get("bg")
                        if bg and _safe_skin_asset_rel(bg) is None:
                            continue
                        installed = iid in BUILTIN_SKIN_IDS or _load_cached_skin(iid, src) is not None
                        entry = {
                            "id": iid,
                            "name": str(item.get("name") or iid)[:40],
                            "author": str(item.get("author") or "")[:40],
                            "desc": str(item.get("desc") or "").strip()[:200],
                            "config": f"skin/{iid}.json",
                            "glass": _skin_is_glass(item),
                            "_source": src,
                            "_official": src == OFFICIAL_SKIN_SOURCE,
                            "_installed": installed,
                        }
                        if bg:
                            entry["bg"] = bg
                        vitem = item.get("vars")
                        if isinstance(vitem, dict):
                            p, b = vitem.get("--primary"), vitem.get("--bg")
                            if isinstance(p, str) and isinstance(b, str) and COLOR_RE.match(p) and COLOR_RE.match(b):
                                entry["vars"] = {"--primary": p, "--bg": b}
                        merged.append(entry)
                except Exception as exc:
                    logger.warning(f"[千小妹皮肤] 索引源 {src} 拉取失败: {exc}")
                    continue

            if mode == "all" and not requested:
                try:
                    _atomic_write_json(_skins_root() / "_index_cache.json", merged)
                except Exception:
                    pass

            return JSONResponse({"status": "success", "data": merged, "sources": sources, "mode": mode})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/skin_get")
    async def skin_get(payload: dict | None = Body(None)):
        body = payload or {}
        sid = str(body.get("id", "")).strip()
        if not SKIN_ID_RE.match(sid):
            return _err("Invalid skin id", 400)
        source = _normalize_skin_source(body.get("source")) or OFFICIAL_SKIN_SOURCE
        if not _normalize_skin_source(body.get("source", "")) and body.get("source"):
            return _err("Invalid skin source", 400)
        force_raw = body.get("force")
        force = force_raw is True or (isinstance(force_raw, str) and force_raw.strip().lower() in ("1", "true"))
        node = str(body.get("node", ""))

        try:
            if sid in BUILTIN_SKIN_IDS and source != OFFICIAL_SKIN_SOURCE:
                return _err("Built-in skin source must be official", 403)
            cached = _load_cached_skin(sid, source) if _skin_source_allowed(source) else _load_cached_skin(sid)
            if not _skin_source_allowed(source) and cached is None:
                return _err("未订阅该皮肤源且无有效本地缓存", 403)
            if cached is not None:
                return JSONResponse({"status": "success", "data": cached})
            config, err, code = await _get_or_fetch_skin_config(sid, source, force=force, preferred_node=node)
            if err:
                return _err(err, code)
            return JSONResponse({"status": "success", "data": config})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/skin_local")
    async def skin_local():
        try:
            skins: dict[tuple[str, str], dict] = {}
            sources = [OFFICIAL_SKIN_SOURCE, *_skin_custom_sources()]
            for src in sources:
                skin_dir = _skin_store_dir(src) / "skin"
                if not skin_dir.exists():
                    continue
                for f in os.listdir(skin_dir):
                    if f == "index.json" or not f.endswith(".json"):
                        continue
                    sid = f[:-5]
                    if not SKIN_ID_RE.match(sid):
                        continue
                    cfg = _load_cached_skin(sid, src)
                    if cfg is not None:
                        cfg["_installed"] = True
                        skins[(sid, cfg.get("_source") or src)] = cfg
            root = _skins_root()
            if root.exists():
                for folder in os.listdir(root):
                    if folder.startswith("_") or not (root / folder).is_dir():
                        continue
                    skin_dir = root / folder / "skin"
                    if not skin_dir.exists():
                        continue
                    for f in os.listdir(skin_dir):
                        if f == "index.json" or not f.endswith(".json"):
                            continue
                        sid = f[:-5]
                        if not SKIN_ID_RE.match(sid):
                            continue
                        data = _read_json(skin_dir / f)
                        src = _normalize_skin_source(data.get("_source")) if isinstance(data, dict) else None
                        cfg = _load_cached_skin(sid, src) if src else _load_cached_skin(sid)
                        if cfg is not None:
                            cfg["_installed"] = True
                            skins[(sid, cfg.get("_source") or OFFICIAL_SKIN_SOURCE)] = cfg
                for f in os.listdir(root):
                    if f.startswith("_") or not f.endswith(".json"):
                        continue
                    sid = f[:-5]
                    if not SKIN_ID_RE.match(sid):
                        continue
                    cfg = _load_cached_skin(sid)
                    if cfg is not None:
                        cfg["_installed"] = True
                        skins[(sid, cfg.get("_source") or OFFICIAL_SKIN_SOURCE)] = cfg

            data = sorted(skins.values(), key=lambda c: (str(c.get("name", "")), c.get("id", ""), c.get("_source", "")))
            return JSONResponse({"status": "success", "data": data})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/skin_delete")
    async def skin_delete(payload: dict | None = Body(None)):
        body = payload or {}
        sid = str(body.get("id", "")).strip()
        if not SKIN_ID_RE.match(sid):
            return _err("Invalid skin id", 400)
        if sid in BUILTIN_SKIN_IDS:
            return _err("Built-in skin cannot be deleted", 403)
        source = _normalize_skin_source(body.get("source"))
        if not body.get("source"):
            return _err("Skin source is required for deletion", 400)
        if not source:
            return _err("Invalid skin source", 400)
        try:
            cached_any = _load_cached_skin(sid)
            if cached_any and cached_any.get("_source") and cached_any["_source"] != source:
                return _err("Cached skin source mismatch", 409)

            pref = _read_json(_skins_root() / "_skin_pref.json", {}) or {}
            if isinstance(pref, dict) and pref.get("skin_id") == sid and (not pref.get("source") or pref.get("source") == source):
                return _err("Active preferred skin cannot be deleted", 409)

            deleted = False
            cfg_path = _skin_store_dir(source) / "skin" / f"{sid}.json"
            if cfg_path.exists():
                cfg_path.unlink()
                deleted = True
            cached = _load_cached_skin(sid, source)
            if cached:
                bg = (cached.get("assets") or {}).get("bg")
                if bg:
                    bg_path = _skin_store_dir(source) / "skin" / Path(bg).name
                    if bg_path.exists():
                        bg_path.unlink()
                        deleted = True
            legacy = _skins_root() / "_assets" / sid / hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
            if legacy.exists():
                shutil.rmtree(legacy, ignore_errors=True)
                deleted = True
                parent = _skins_root() / "_assets" / sid
                try:
                    if parent.exists() and not os.listdir(parent):
                        os.rmdir(parent)
                except Exception:
                    pass

            if not deleted:
                return JSONResponse({"status": "missing",
                                     "data": {"id": sid, "source": source, "deleted": False}})
            return JSONResponse({"status": "success",
                                 "data": {"id": sid, "source": source, "deleted": True}})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/skin_sources")
    async def skin_sources():
        return JSONResponse({"status": "success", "data": [OFFICIAL_SKIN_SOURCE, *_skin_custom_sources()]})

    @router.post("/api/save_skin_sources")
    async def save_skin_sources(payload: dict | None = Body(None)):
        body = payload or {}
        sources = body.get("sources")
        if not isinstance(sources, list):
            return _err("Invalid sources", 400)
        custom: list[str] = []
        for raw in sources:
            norm = _normalize_skin_source(raw)
            if not norm:
                return _err(f"Invalid GitHub skin source: {raw}", 400)
            if norm == OFFICIAL_SKIN_SOURCE or norm in custom:
                continue
            custom.append(norm)
            if len(custom) >= 5:
                break
        _atomic_write_json(_skins_root() / "_sources.json", custom)
        return JSONResponse({"status": "success", "data": [OFFICIAL_SKIN_SOURCE, *custom]})

    @router.get("/api/skin_asset")
    async def skin_asset(file: str = "", skin_id: str = "", source: str = "",
                         force: str = "", node: str = ""):
        try:
            is_force = force.strip().lower() in ("1", "true")
            info, err = await _resolve_skin_asset(file, skin_id, source, is_force, node, allow_fetch=True)
            if err is not None:
                return err
            desc = _skin_asset_descriptor(info["response_id"], info["source"], info["path"], info["cached"])
            return JSONResponse({"status": "success", "data": desc})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/skin_asset_chunk")
    async def skin_asset_chunk(file: str = "", skin_id: str = "", source: str = "",
                               index: str = "-1", node: str = ""):
        try:
            try:
                idx = int(index)
            except Exception:
                return _err("Invalid skin asset chunk index", 400)
            info, err = await _resolve_skin_asset(file, skin_id, source, False, node, allow_fetch=False)
            if err is not None:
                return err
            path: Path = info["path"]
            if not path.exists() or path.stat().st_size <= 0:
                return _err("Skin asset is not cached; call skin_asset first", 409)
            size = path.stat().st_size
            chunk_count = (size + SKIN_ASSET_CHUNK_BYTES - 1) // SKIN_ASSET_CHUNK_BYTES
            if idx < 0 or idx >= chunk_count:
                return _err("Skin asset chunk index out of range", 416)
            with open(path, "rb") as f:
                f.seek(idx * SKIN_ASSET_CHUNK_BYTES)
                chunk = f.read(SKIN_ASSET_CHUNK_BYTES)
            return JSONResponse({
                "status": "success",
                "data": {
                    "id": info["response_id"],
                    "skin_id": info["response_id"],
                    "source": info["source"],
                    "index": idx,
                    "chunk_count": chunk_count,
                    "chunk": base64.b64encode(chunk).decode("ascii"),
                },
            })
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/skin_pref")
    async def get_skin_pref():
        try:
            pref = _read_json(_skins_root() / "_skin_pref.json")
            if not isinstance(pref, dict) or not SKIN_ID_RE.match(str(pref.get("skin_id", ""))):
                return _missing()
            sid = pref["skin_id"]
            try:
                blur = max(0, min(100, int(pref.get("bg_blur", 0))))
            except Exception:
                blur = 0
            if sid in BUILTIN_SKIN_IDS:
                source = OFFICIAL_SKIN_SOURCE
            else:
                source = _normalize_skin_source(pref.get("source"))
                if not source:
                    cached = _load_cached_skin(sid)
                    source = (cached.get("_source") if cached else None) or OFFICIAL_SKIN_SOURCE
            cleaned = {"skin_id": sid, "source": source, "bg_blur": blur}
            if pref != cleaned:
                _atomic_write_json(_skins_root() / "_skin_pref.json", cleaned)
            return JSONResponse({"status": "success", "data": cleaned})
        except Exception:
            return _missing()

    @router.post("/api/save_skin_pref")
    async def save_skin_pref(payload: dict | None = Body(None)):
        body = payload or {}
        sid = str(body.get("skin_id", "")).strip()
        if not SKIN_ID_RE.match(sid):
            return _err("Invalid skin id", 400)
        try:
            blur = max(0, min(100, int(body.get("bg_blur", 0) or 0)))
        except Exception:
            blur = 0
        source: str | None = None
        if body.get("source"):
            source = _normalize_skin_source(body.get("source"))
            if not source:
                return _err("Invalid skin source", 400)
        try:
            if sid in BUILTIN_SKIN_IDS:
                if source and source != OFFICIAL_SKIN_SOURCE:
                    return _err("Built-in skin source must be official", 400)
                source = OFFICIAL_SKIN_SOURCE
            elif not source:
                cached = _load_cached_skin(sid)
                source = (cached.get("_source") if cached else None) or OFFICIAL_SKIN_SOURCE
            if not _skin_source_allowed(source) and _load_cached_skin(sid) is None:
                return _err("Skin source is not subscribed and has no valid local cache", 403)
            pref = {"skin_id": sid, "source": source, "bg_blur": blur}
            _atomic_write_json(_skins_root() / "_skin_pref.json", pref)
            return JSONResponse({"status": "ok", "data": {"saved": True, "preference": pref}})
        except Exception as e:
            return _err(str(e), 500)

    # ============================================================
    # 3. 图库与干饭人管理
    # ============================================================

    @router.get("/api/list_images")
    async def list_images():
        try:
            worlds = ["common", "world1", "world2", "world3", "world4", "world5"]
            result: dict[str, Any] = {
                "food": {w: [] for w in worlds},
                "drink": {w: [] for w in worlds},
                "darkfood": {w: [] for w in worlds},
                "chefs": [],
                "memes": {w: [] for w in worlds},
                "ganfanren": {},
            }
            for cat in ("food", "drink", "darkfood"):
                for w in worlds:
                    tdir = D() / cat / w
                    if tdir.exists():
                        for f in os.listdir(tdir):
                            if not f.startswith(".") and (tdir / f).is_file():
                                result[cat][w].append(f)
            chef_dir = D() / "chefs"
            if chef_dir.exists():
                for f in os.listdir(chef_dir):
                    if not f.startswith(".") and (chef_dir / f).is_file():
                        result["chefs"].append(f)
            meme_root = D() / "memes"
            if meme_root.exists():
                for w in worlds:
                    wdir = meme_root / w
                    if not wdir.exists():
                        continue
                    for mood in os.listdir(wdir):
                        mdir = wdir / mood
                        if mdir.is_dir():
                            for f in os.listdir(mdir):
                                if not f.startswith(".") and (mdir / f).is_file():
                                    result["memes"][w].append(f"{mood}/{f}")
            gf_root = D() / "ganfanren"
            if gf_root.exists():
                for char in os.listdir(gf_root):
                    cdir = gf_root / char
                    if not cdir.is_dir():
                        continue
                    entry = {"words": "", "images": []}
                    words_file = cdir / "words.txt"
                    if words_file.exists():
                        try:
                            entry["words"] = words_file.read_text(encoding="utf-8")
                        except Exception:
                            pass
                    for f in os.listdir(cdir):
                        if not f.startswith(".") and f not in ("words.txt", "lines.txt") and (cdir / f).is_file():
                            entry["images"].append(f)
                    result["ganfanren"][char] = entry
            return JSONResponse({"status": "ok", "data": result})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/image-data")
    async def image_data(path: str = ""):
        try:
            if not path:
                return _err("No path provided", 400)
            base = D().resolve()
            full = (base / path).resolve()
            if not _under_base(full, base):
                return _err("Access denied", 403)
            if not full.exists():
                if path == "Webui-PIC/Chisa.gif":
                    fallback = Path(plugin._get_source_dir()) / "Chisa.gif"
                    if fallback.exists():
                        return JSONResponse({"status": "ok", "data_url": _data_url(fallback)})
                return _err("File not found", 404)
            if full.stat().st_size > 8 * 1024 * 1024:
                return _err("Image too large for preview", 413)
            raw = full.read_bytes()
            mime = _sniff_image_mime(raw, mimetypes.guess_type(str(full))[0] or "image/png")
            return JSONResponse({
                "status": "ok",
                "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
            })
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/add_ganfanren")
    async def add_ganfanren(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            name = str(body.get("name", "")).strip()
            words = str(body.get("words", ""))
            if not name or ".." in name or "/" in name or "\\" in name:
                return _err("干饭人名字为空或包含非法字符", 400)
            gf_base = (D() / "ganfanren").resolve()
            gf_dir = (gf_base / name).resolve()
            if not _under_base(gf_dir, gf_base):
                return _err("非法越权访问", 403)
            gf_dir.mkdir(parents=True, exist_ok=True)
            (gf_dir / "words.txt").write_text(words, encoding="utf-8")
            images = body.get("images", [])
            if isinstance(images, list):
                for img in images:
                    if not isinstance(img, dict):
                        continue
                    fname = str(img.get("filename", ""))
                    if not fname or ".." in fname or "/" in fname or "\\" in fname:
                        continue
                    b64 = str(img.get("data", ""))
                    if not b64:
                        continue
                    if b64.startswith("data:"):
                        b64 = b64.split(",", 1)[1] if "," in b64 else ""
                    target = (gf_dir / fname).resolve()
                    if not _under_base(target, gf_dir):
                        continue
                    try:
                        target.write_bytes(base64.b64decode(b64))
                    except Exception as exc:
                        logger.warning(f"[千小妹 WebUI] 干饭人图片写入失败 {fname}: {exc}")
            reload_caches()
            return JSONResponse({"status": "ok", "message": f"成功招募干饭人 {name}！"})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/update_ganfanren")
    async def update_ganfanren(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            name = str(body.get("name", "")).strip()
            words = str(body.get("words", "")).strip()
            if not name or ".." in name or "/" in name or "\\" in name:
                return JSONResponse({"status": "error"}, status_code=400)
            gf_base = (D() / "ganfanren").resolve()
            gf_dir = (gf_base / name).resolve()
            if not _under_base(gf_dir, gf_base):
                return _err("非法越权访问", 403)
            if not gf_dir.exists():
                return _err("Not found", 404)
            (gf_dir / "words.txt").write_text(words, encoding="utf-8")
            reload_caches()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/delete_ganfanren")
    async def delete_ganfanren(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            name = str(body.get("name", "")).strip()
            if not name:
                return _err("名字不能为空", 400)
            if ".." in name or "/" in name or "\\" in name:
                return _err("非法名称", 400)
            gf_base = (D() / "ganfanren").resolve()
            gf_dir = (gf_base / name).resolve()
            if not _under_base(gf_dir, gf_base):
                return _err("非法越权访问", 403)
            if gf_dir.exists() and gf_dir.is_dir():
                shutil.rmtree(gf_dir)
                reload_caches()
                return JSONResponse({"status": "ok", "message": f"已成功删除干饭人 {name}"})
            return _err("该干饭人不存在", 404)
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/upload_image")
    async def upload_image(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            category = body.get("category")
            world = str(body.get("world", ""))
            if ".." in world or "/" in world or "\\" in world:
                world = ""
            mode = str(body.get("mode", "batch"))
            single_chef = str(body.get("single_chef", "")).strip()
            single_dish = str(body.get("single_dish", "")).strip()
            if ".." in single_chef or "/" in single_chef or "\\" in single_chef:
                single_chef = ""
            if ".." in single_dish or "/" in single_dish or "\\" in single_dish:
                single_dish = ""
            mood = str(body.get("mood", "think"))
            files = body.get("files", [])

            base = D().resolve()
            if category in ("food", "drink", "darkfood"):
                if not world:
                    world = "common"
                target_dir = base / category / world
            elif category == "chefs":
                target_dir = base / "chefs"
            elif category == "memes":
                if not world:
                    world = "common"
                target_dir = base / "memes" / world / mood
            elif category == "ganfanren":
                char_name = str(body.get("char_name", ""))
                if ".." in char_name or "/" in char_name or "\\" in char_name:
                    char_name = ""
                target_dir = base / "ganfanren" / char_name
            else:
                return _err("Invalid category", 400)

            target_dir = target_dir.resolve()
            if not _under_base(target_dir, base):
                return _err("跨目录上传拒绝", 403)
            target_dir.mkdir(parents=True, exist_ok=True)

            for f in files if isinstance(files, list) else []:
                if not isinstance(f, dict):
                    continue
                fname = str(f.get("filename", ""))
                if ".." in fname or "/" in fname or "\\" in fname:
                    continue
                b64 = str(f.get("data", ""))
                if not fname or not b64:
                    continue
                ext = os.path.splitext(fname)[1]
                if mode == "single" and category in ("food", "drink", "darkfood", "chefs"):
                    if category == "chefs":
                        base_name = single_dish
                    else:
                        base_name = f"【{single_chef}】{single_dish}" if single_chef else single_dish
                    final_name = base_name + ext
                    counter = 1
                    while (target_dir / final_name).exists():
                        final_name = f"{base_name}_{counter}{ext}"
                        counter += 1
                    fname = final_name
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1] if "," in b64 else ""
                img_path = (target_dir / fname).resolve()
                if not _under_base(img_path, target_dir):
                    continue
                try:
                    img_path.write_bytes(base64.b64decode(b64))
                except Exception as exc:
                    logger.warning(f"[千小妹 WebUI] 图片写入失败 {fname}: {exc}")

            reload_caches()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            logger.error(f"[千小妹 WebUI] upload error: {e}")
            return _err(str(e), 500)

    @router.post("/api/delete_image")
    async def delete_image(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                paths = []
            if body.get("path") and body["path"] not in paths:
                paths.append(body["path"])
            if not paths:
                return _err("No path provided", 400)
            base = D().resolve()
            deleted = 0
            for p in paths:
                p = str(p)
                if ".." in p:
                    continue
                full = (base / p).resolve()
                if not _under_base(full, base):
                    continue
                if full.exists() and full.is_file():
                    full.unlink()
                    deleted += 1
            if deleted > 0:
                reload_caches()
                return JSONResponse({"status": "ok", "message": f"成功删除 {deleted} 张图片"})
            return _err("File not found", 404)
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/rename_image")
    async def rename_image(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            old_path = str(body.get("old_path", "")).strip()
            new_name = str(body.get("new_name", "")).strip()
            new_ext = str(body.get("new_ext", "")).strip()
            if not old_path or not new_name:
                return _err("参数缺失", 400)
            if new_ext and not new_ext.startswith("."):
                new_ext = "." + new_ext
            base = D().resolve()
            full_old = (base / old_path).resolve()
            if not _under_base(full_old, base) or not full_old.exists():
                return _err("原文件不存在或无权限", 404)
            parent = full_old.parent
            full_new = (parent / (new_name + new_ext)).resolve()
            if not _under_base(full_new, base):
                return _err("非法的新文件名", 400)
            if full_new != full_old:
                counter = 1
                while full_new.exists():
                    full_new = (parent / f"{new_name}_{counter}{new_ext}").resolve()
                    counter += 1
                os.rename(str(full_old), str(full_new))
            reload_caches()
            return JSONResponse({"status": "ok", "message": "重命名成功"})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/frontend_log")
    async def frontend_log(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            msg = body.get("msg", "")
            if msg:
                logger.info(f"[Chisa Skin Front] {msg}")
            return _ok()
        except Exception:
            return JSONResponse({"status": "error"}, status_code=500)

    def _mascot_dir() -> Path:
        p = D() / "mascots"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _mascot_quotes_path() -> Path:
        return D() / "mascot_quotes.txt"

    def _list_mascot_images() -> list[str]:
        d = _mascot_dir()
        out = []
        for f in sorted(os.listdir(d)):
            if f.startswith("."):
                continue
            if (d / f).is_file() and Path(f).suffix.lower() in MASCOT_EXTS:
                out.append(f)
        return out

    def _read_mascot_quotes() -> str:
        p = _mascot_quotes_path()
        if not p.exists():
            return ""
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                return p.read_text(encoding=enc)
            except Exception:
                continue
        return ""

    def _safe_asset_name(filename: str) -> str | None:
        name = os.path.basename(str(filename)).strip()
        if not name or ".." in name:
            return None
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        if Path(name).suffix.lower() not in MASCOT_EXTS:
            return None
        return name

    @router.get("/api/mascot_assets")
    async def mascot_assets():
        try:
            click_switch = True
            if get_config is not None:
                try:
                    click_switch = bool(getattr(get_config(), "MASCOT_CLICK_SWITCH", True))
                except Exception:
                    click_switch = True
            return JSONResponse({
                "status": "ok",
                "images": _list_mascot_images(),
                "quotes": _read_mascot_quotes(),
                "click_switch": click_switch,
            })
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/upload_mascot_asset")
    async def upload_mascot_asset(payload: dict | None = Body(None)):
        """上传图片到吉祥物随机池（数据目录 mascots/），单张 ≤8MB，支持 GIF/PNG/JPG/WebP。"""
        try:
            body = payload or {}
            b64 = str(body.get("data", ""))
            fname = _safe_asset_name(body.get("filename", ""))
            if not b64:
                return _err("No image data", 400)
            if not fname:
                return _err("仅支持 GIF/PNG/JPG/WebP 格式", 400)
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[1] if "," in b64 else ""
            try:
                raw = base64.b64decode(b64, validate=True)
            except Exception:
                return _err("Invalid image data", 400)
            if not raw:
                return _err("Empty image data", 400)
            if len(raw) > 8 * 1024 * 1024:
                return _err("Image too large (max 8MB)", 413)
            if _sniff_image_mime(raw, mimetypes.guess_type(fname)[0] or "") not in (
                "image/gif", "image/png", "image/jpeg", "image/webp"):
                return _err("仅支持 GIF/PNG/JPG/WebP 格式", 400)
            d = _mascot_dir()
            target = d / fname
            if target.exists():
                stem, ext = os.path.splitext(fname)
                counter = 1
                while (d / f"{stem}_{counter}{ext}").exists():
                    counter += 1
                target = d / f"{stem}_{counter}{ext}"
            tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(target))
            return JSONResponse({"status": "ok", "filename": target.name, "images": _list_mascot_images()})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/delete_mascot_asset")
    async def delete_mascot_asset(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            fname = _safe_asset_name(body.get("filename", ""))
            if not fname:
                return _err("Invalid filename", 400)
            d = _mascot_dir().resolve()
            target = (d / fname).resolve()
            if not _under_base(target, d):
                return _err("非法路径", 403)
            if target.exists() and target.is_file():
                target.unlink()
            return JSONResponse({"status": "ok", "images": _list_mascot_images()})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/save_mascot_quotes")
    async def save_mascot_quotes(payload: dict | None = Body(None)):
        try:
            body = payload or {}
            quotes = body.get("quotes", "")
            if not isinstance(quotes, str):
                return _err("Invalid quotes", 400)
            quotes = quotes[:20000]
            p = _mascot_quotes_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(quotes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(p))
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return _err(str(e), 500)

    @router.post("/api/upload_mascot")
    async def upload_mascot(payload: dict | None = Body(None)):
        """NA 移植新增：上传自定义右下角吉祥物，落盘 Webui-PIC/Chisa.gif（按魔数识别格式）。"""
        try:
            body = payload or {}
            b64 = str(body.get("data", ""))
            fname = str(body.get("filename", ""))
            if not b64:
                return _err("No image data", 400)
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[1] if "," in b64 else ""
            try:
                raw = base64.b64decode(b64, validate=True)
            except Exception:
                return _err("Invalid image data", 400)
            if not raw:
                return _err("Empty image data", 400)
            if len(raw) > 8 * 1024 * 1024:
                return _err("Image too large (max 8MB)", 413)
            mime = _sniff_image_mime(raw, mimetypes.guess_type(fname)[0] or "")
            if mime not in ("image/gif", "image/png", "image/jpeg", "image/webp"):
                return _err("仅支持 GIF/PNG/JPG/WebP 格式", 400)
            target = D() / "Webui-PIC"
            target.mkdir(parents=True, exist_ok=True)
            tmp = target / f"Chisa.gif.tmp.{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(target / "Chisa.gif"))
            return JSONResponse({"status": "ok", "message": "吉祥物已更换"})
        except Exception as e:
            return _err(str(e), 500)

    @router.get("/api/test_reflection")
    async def test_reflection():
        # AstrBot 专有调试接口；NA 无 message_components，返回空清单保持前端兼容。
        return JSONResponse({"components": []})

    # ------------------------------------------------------------

    @plugin.mount_router()
    def _chisa_webapi_router() -> APIRouter:  # noqa: N806
        return router

    return router
