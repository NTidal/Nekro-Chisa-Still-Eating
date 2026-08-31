"""千小妹商会 - DLC/图库下载模块（NekroAgent 移植版）

移植自 AstrBot 插件 v4.2.3 的下载逻辑：多镜像智能测速、逐跳 HTTPS 白名单校验、
响应大小限制、SHA-256 校验、ZIP 安全解压（防路径穿越/符号链接/解压炸弹）。
原网络层基于 aiohttp，本移植版改用 httpx.AsyncClient。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from nekro_agent.api.core import logger

MIRROR_NODES = ("gh-proxy.com", "hk.gh-proxy.com", "gh.dpik.top", "edgeone.gh-proxy.com")

ALLOWED_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "cdn.jsdelivr.net",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
    *MIRROR_NODES,
}

PHOTO_REPO = "dddada123/astrbot_plugin_chisa_still_eating_photo"
CATALOG_URL = f"https://raw.githubusercontent.com/{PHOTO_REPO}/main/index/catalog.json"
RELEASE_URL = f"https://github.com/{PHOTO_REPO}/releases/download/Chisa_Dlc_Store"
FD0000_HASH = "18648dfbd827cc69b1e0c627058d15a5b0a5622967a2a11b852cc8098df499c9"

# 全局下载状态（模块级单例）
download_state = {
    "is_downloading": False,
    "downloaded_bytes": 0,
    "total_bytes": 1,
    "msg": "",
}

_best_node: str | None = None


def _validate_hop(url: str):
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError(f"Blocked download URL: {url}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid download URL port") from exc
    if port not in (None, 443):
        raise ValueError(f"Blocked download port: {port}")


async def _fetch_bytes(client: httpx.AsyncClient, url: str, max_bytes: int, timeout: float) -> bytes | None:
    """逐跳校验后抓取 bytes（最多跟随 5 次重定向）。"""
    current = str(url or "").strip()
    for _ in range(5):
        _validate_hop(current)
        resp = await client.get(
            current,
            timeout=timeout,
            headers={"Accept": "application/octet-stream"},
            follow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            return None
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError(f"Remote response exceeds {max_bytes} bytes")
        total = 0
        chunks = []
        async for chunk in resp.aiter_bytes(256 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Remote response exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    raise ValueError("Too many download redirects")


async def _download_file(
    client: httpx.AsyncClient,
    url: str,
    target_path: Path,
    max_bytes: int,
    timeout: float,
    progress=None,
) -> tuple[str, int]:
    """流式下载到文件，返回 (sha256, 总字节数)。失败时清理临时文件。"""
    current = str(url or "").strip()
    try:
        for _ in range(5):
            _validate_hop(current)
            resp = await client.get(current, timeout=timeout, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    raise RuntimeError("Download redirect has no Location")
                current = urljoin(current, location)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"Remote file exceeds {max_bytes} bytes")
            digest = hashlib.sha256()
            total = 0
            with open(target_path, "wb") as stream:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Remote file exceeds {max_bytes} bytes")
                    stream.write(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(total)
                stream.flush()
                os.fsync(stream.fileno())
            return digest.hexdigest(), total
        raise ValueError("Too many download redirects")
    except Exception:
        if target_path.exists():
            target_path.unlink()
        raise


def extract_zip_safe(zip_path: Path, target_dir: Path):
    """安全解压：限制文件数/总大小，拦截路径穿越与符号链接。"""
    with zipfile.ZipFile(str(zip_path), "r") as archive:
        infos = archive.infolist()
        if len(infos) > 10000:
            raise ValueError("ZIP contains too many files")
        if sum(info.file_size for info in infos) > 2 * 1024 * 1024 * 1024:
            raise ValueError("ZIP expands beyond 2 GiB")
        target_root = os.path.abspath(str(target_dir))
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            mode = (info.external_attr >> 16) & 0o170000
            if not parts or normalized.startswith("/") or ".." in parts or ":" in parts[0] or mode == 0o120000:
                raise ValueError("Unsafe path in ZIP")
            target = os.path.abspath(os.path.join(target_root, *parts))
            if os.path.commonpath((target_root, target)) != target_root:
                raise ValueError("Unsafe path in ZIP")
        archive.extractall(target_root)


async def get_optimal_node() -> str:
    """并发测速选择最优镜像节点，失败回退 direct。结果缓存。"""
    global _best_node
    if _best_node is not None:
        return _best_node

    async def test_node(client: httpx.AsyncClient, node: str) -> tuple[str, float]:
        start = time.time()
        url = f"https://{node}/{CATALOG_URL}"
        try:
            content = await _fetch_bytes(client, url, max_bytes=8 * 1024 * 1024, timeout=10)
            if content:
                return node, int((time.time() - start) * 1000)
        except Exception:
            pass
        return node, float("inf")

    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            tasks = [asyncio.create_task(test_node(client, n)) for n in MIRROR_NODES]
            done, pending = await asyncio.wait(tasks, timeout=10)
            best_node, best_lat = None, float("inf")
            for task in done:
                node, lat = task.result()
                if lat < best_lat:
                    best_lat, best_node = lat, node
            if best_node is None and pending:
                done2, _ = await asyncio.wait(pending, timeout=20)
                for task in done2:
                    node, lat = task.result()
                    if lat < best_lat:
                        best_lat, best_node = lat, node
            if best_node is not None:
                logger.info(f"[千小妹商会] 最优节点锁定为 [{best_node}] ({best_lat}ms)")
                _best_node = best_node
                return best_node
    except Exception as exc:
        logger.warning(f"[千小妹商会] 节点测速异常，回退直连: {exc}")
    logger.warning("[千小妹商会] 国内加速镜像均不可用，回退 Github 直连。")
    _best_node = "direct"
    return "direct"


def _mirror_url(node: str, original: str) -> tuple[str, bool]:
    """返回 (url, trust_env)。镜像节点不走系统代理，直连走系统代理。"""
    if node and node != "direct" and node in MIRROR_NODES:
        return f"https://{node}/{original}", False
    return original, True


def catalog_path(data_dir: Path) -> Path:
    return data_dir / "Webui-PIC" / "Shop" / "index" / "catalog.json"


def read_catalog(data_dir: Path):
    p = catalog_path(data_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data
    except Exception:
        return None


async def sync_catalog(data_dir: Path) -> str:
    """拉取线上商品目录并落盘。返回状态消息。"""
    node = await get_optimal_node()
    if node == "failed":
        return "所有节点响应超时，同步失败，请稍后再试！"
    url, trust_env = _mirror_url(node, CATALOG_URL)
    try:
        async with httpx.AsyncClient(trust_env=trust_env) as client:
            content = await _fetch_bytes(client, url, max_bytes=8 * 1024 * 1024, timeout=15)
        if not content:
            raise RuntimeError("目录响应为空")
        catalog = json.loads(content.decode("utf-8-sig"))
        if not isinstance(catalog, list) or len(catalog) > 5000 or any(not isinstance(i, dict) for i in catalog):
            raise ValueError("目录格式无效")
        p = catalog_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp), str(p))
        return "✅ 同步完成！请再次输入【千小妹商会】开始逛街~"
    except Exception as e:
        return f"同步异常: {e}"


async def download_dlc(data_dir: Path, dlc_id: str, sha256_hash: str = "") -> bool:
    """下载并解压单个 DLC 商品。"""
    import re

    if not re.fullmatch(r"[a-z]{2}\d{4}", str(dlc_id or ""), re.IGNORECASE):
        raise ValueError("Invalid DLC ID")
    sha256_hash = str(sha256_hash or "").strip().lower()
    if sha256_hash and not re.fullmatch(r"[0-9a-f]{64}", sha256_hash):
        raise ValueError("Invalid SHA-256")

    node = await get_optimal_node()
    if node == "failed":
        raise Exception("所有测速节点均无响应")
    original = f"{RELEASE_URL}/{dlc_id}.zip"
    url, trust_env = _mirror_url(node, original)

    data_dir.mkdir(parents=True, exist_ok=True)
    temp_zip = data_dir / f"{dlc_id}_temp.zip"

    def _progress(total):
        download_state["downloaded_bytes"] = total

    try:
        download_state.update(is_downloading=True, downloaded_bytes=0, total_bytes=1, msg=f"正在进货 {dlc_id}")
        async with httpx.AsyncClient(trust_env=trust_env) as client:
            actual_sha, total = await _download_file(
                client, url, temp_zip, max_bytes=512 * 1024 * 1024, timeout=300, progress=_progress
            )
        download_state["total_bytes"] = max(total, 1)
        download_state["downloaded_bytes"] = total
        if sha256_hash and actual_sha != sha256_hash:
            raise Exception(f"校验失败! 预期 {sha256_hash[:8]} 但得到 {actual_sha[:8]}")
        await asyncio.to_thread(extract_zip_safe, temp_zip, data_dir)
        logger.info(f"[千小妹商会] DLC [{dlc_id}] 解压成功")

        # 已购清单
        try:
            json_path = data_dir / "Webui-PIC" / "Shop" / "index" / "downloaded.json"
            json_path.parent.mkdir(parents=True, exist_ok=True)
            records = []
            if json_path.exists():
                try:
                    records = json.loads(json_path.read_text(encoding="utf-8"))
                except Exception:
                    records = []
            if dlc_id not in records:
                records.append(dlc_id)
            json_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        except Exception as ex:
            logger.warning(f"[千小妹商会] 写入已购清单失败: {ex}")
        return True
    finally:
        if temp_zip.exists():
            temp_zip.unlink()
        download_state.update(is_downloading=False, downloaded_bytes=0)


async def download_base_assets(data_dir: Path) -> bool:
    """拉取 99.2MB 基础图库 fd0000.zip，镜像优先 + 直连兜底，SHA-256 校验后解压部署。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    zip_path = data_dir / "assets_temp.zip"
    extract_tmp = data_dir / "extract_tmp"

    try:
        download_state.update(is_downloading=True, downloaded_bytes=0, msg="正在拉取基础图库 (约99.2MB)")
        try:
            best = await get_optimal_node()
        except Exception:
            best = "direct"

        candidates = []
        url_mirror, _ = _mirror_url(best, f"{RELEASE_URL}/fd0000.zip")
        if best not in ("direct", ""):
            candidates.append((url_mirror, False))
        candidates.append((f"{RELEASE_URL}/fd0000.zip", True))  # 直连遵循系统代理

        success = False
        for url, trust_env in candidates:
            try:
                def _progress(total):
                    download_state["downloaded_bytes"] = total
                async with httpx.AsyncClient(trust_env=trust_env) as client:
                    downloaded_hash, total = await _download_file(
                        client, url, zip_path, max_bytes=512 * 1024 * 1024, timeout=300, progress=_progress
                    )
                download_state["total_bytes"] = max(total, 1)
                if downloaded_hash != FD0000_HASH:
                    logger.warning(f"[千小妹商会] 资源包哈希不匹配，预期 {FD0000_HASH[:8]} 实际 {downloaded_hash[:8]}，切换节点")
                    if zip_path.exists():
                        zip_path.unlink()
                    continue
                success = True
                break
            except Exception as e:
                logger.warning(f"[千小妹商会] 节点下载异常: {e}")
                if zip_path.exists():
                    zip_path.unlink()

        if not success:
            logger.error("[千小妹商会] 所有节点拉取失败或哈希校验失败")
            return False

        if extract_tmp.exists():
            shutil.rmtree(extract_tmp, ignore_errors=True)
        await asyncio.to_thread(extract_zip_safe, zip_path, extract_tmp)

        # 找到含 food/drink/chefs 的层级
        src_dir = extract_tmp
        for root, dirs, _files in os.walk(str(extract_tmp)):
            if "food" in dirs or "drink" in dirs or "chefs" in dirs:
                src_dir = Path(root)
                break
        for item in os.listdir(str(src_dir)):
            s = src_dir / item
            d = data_dir / item
            if s.is_dir():
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(str(s), str(d))
            else:
                shutil.copy2(str(s), str(d))
        logger.info("[千小妹商会] 默认图库解压部署完成")
        return True
    finally:
        if zip_path.exists():
            zip_path.unlink()
        if extract_tmp.exists():
            shutil.rmtree(extract_tmp, ignore_errors=True)
        download_state.update(is_downloading=False, downloaded_bytes=0)
