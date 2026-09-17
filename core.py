"""千小妹还在吃 (NekroAgent 移植版) - 核心逻辑层

框架无关的纯 Python 逻辑：图库扫描、摇号引擎、限频、干饭人扫描、世界/模板配置加载。
移植自 AstrBot 插件 astrbot_plugin_chisa_still_eating v4.2.4 (作者 Rua432, MIT)。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# ============================================================
# 限频 / 防刷屏（移植自 rate_limiter.py）
# ============================================================


class RateLimiter:
    def __init__(self):
        self.user_records: dict[str, list[float]] = {}
        self.repeat_cooldowns: dict[str, float] = {}

    def is_spaming(self, uid: str, threshold: int) -> bool:
        now = time.time()
        timestamps = self.user_records.setdefault(uid, [])
        timestamps[:] = [ts for ts in timestamps if now - ts < 60]
        if len(timestamps) >= threshold:
            return True
        timestamps.append(now)
        return False

    def is_repeat_in_cooldown(self, group_id: str, cooldown_seconds: int) -> bool:
        now = time.time()
        return now - self.repeat_cooldowns.get(group_id, 0.0) < cooldown_seconds

    def record_repeat_trigger(self, group_id: str):
        self.repeat_cooldowns[group_id] = time.time()


# ============================================================
# 摇号引擎（移植自 food_data.py）
# ============================================================


class FoodDataManager:
    def __init__(self, data_dir: Path, cfg: dict):
        self.config = cfg
        self.data_dir = str(data_dir)
        self.history_path = os.path.join(self.data_dir, "group_history.json")
        self.history_limit = int(self.config.get("history_limit", 30))
        self.group_history: dict[str, deque] = {}
        self._load_history_cache()

    def _load_history_cache(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for gid, lst in data.items():
                        self.group_history[gid] = deque(lst, maxlen=self.history_limit)
            except Exception:
                self.group_history = {}

    def _save_history_cache(self):
        try:
            export = {gid: list(deq) for gid, deq in self.group_history.items()}
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def filter_and_pick(self, group_id: str, full_pool: list, active_wv: str):
        if not full_pool:
            return None
        mode_loyal = self.config.get("mode_loyal", False)
        mode_roller = self.config.get("mode_roller", False)
        mode_normie = self.config.get("mode_normie", False)

        filtered_pool = []
        for item in full_pool:
            wv = item["wv"]
            if mode_normie:
                if wv == "common":
                    filtered_pool.append(item)
                continue
            if mode_roller and wv == "common":
                continue
            if mode_loyal and wv != "common" and wv != active_wv:
                continue
            filtered_pool.append(item)

        if not filtered_pool:
            filtered_pool = full_pool

        current_limit = int(self.config.get("history_limit", 30))
        if current_limit != self.history_limit:
            self.history_limit = current_limit
            for gid in list(self.group_history.keys()):
                self.group_history[gid] = deque(list(self.group_history[gid]), maxlen=self.history_limit)

        if group_id not in self.group_history:
            self.group_history[group_id] = deque(maxlen=self.history_limit)
        history = self.group_history[group_id]

        fresh_items = [i for i in filtered_pool if i["raw_name"] not in history]
        final_pool = fresh_items if fresh_items else filtered_pool

        world_groups: dict[str, list] = {}
        for item in final_pool:
            wv = item["wv"]
            world_groups.setdefault(wv, []).append(item)

        available_worlds = list(world_groups.keys())
        weight_config = {
            "common": self.config.get("weight_3d", 70),
            "world1": self.config.get("weight_w1", 20),
            "world2": self.config.get("weight_w2", 5),
            "world3": self.config.get("weight_w3", 5),
            "world4": self.config.get("weight_w4", 0),
            "world5": self.config.get("weight_w5", 0),
        }
        weights = [weight_config.get(w, 10) for w in available_worlds]
        if sum(weights) <= 0:
            weights = [1] * len(available_worlds)

        chosen_world = random.choices(available_worlds, weights=weights, k=1)[0]
        picked = random.choice(world_groups[chosen_world])

        if self.history_limit > 0:
            history.append(picked["raw_name"])
            self._save_history_cache()
        return picked

    def fetch_egg_text(self, char_name: str) -> str:
        vault = {
            "千咲": ["……但是被千咲吃光了！", "……千咲甚至连盘子都舔得发亮！"],
            "派蒙": ["……但是派蒙一不留神吃光了。", "……惹得派蒙在旁边心满意足地打了个饱嗝。"],
            "达妮娅": ["……达妮娅表示在残星会没吃过这么好的。", "……达妮娅顺手连锅带灶一起端回了残星会本部，并成为了人类。"],
        }
        pool = vault.get(char_name, [f"……但是被{char_name}吃光了。"])
        return random.choice(pool)


# ============================================================
# 图库管理（移植自 image_manager.py）
# ============================================================


class ImageManager:
    def __init__(self, data_dir: Path):
        self.data_dir = str(data_dir)
        self.categories = ["food", "drink", "darkfood"]
        self.worlds = ["world1", "world2", "world3", "world4", "common"]
        self.moods = ["think", "like", "speechless", "scared"]
        self._ensure_infrastructure()
        self.cached_pools = {"food": [], "drink": [], "dark": []}
        self.cached_chefs: dict[str, list[str]] = {}
        self.cached_memes: dict[str, dict[str, list[str]]] = {}

    def _ensure_infrastructure(self):
        for cat in self.categories:
            for w in self.worlds:
                os.makedirs(os.path.join(self.data_dir, cat, w), exist_ok=True)
        for w in self.worlds:
            for mood in self.moods:
                os.makedirs(os.path.join(self.data_dir, "memes", w, mood), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "chefs"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "ganfanren"), exist_ok=True)

    def reload_caches(self, wv_settings: dict):
        for cat in ["food", "drink", "dark"]:
            self.cached_pools[cat] = self.scan_all_items(wv_settings, cat)

        self.cached_chefs.clear()
        chef_dir = os.path.join(self.data_dir, "chefs")
        if os.path.exists(chef_dir):
            for file in os.listdir(chef_dir):
                if file.startswith(".") or not file.lower().endswith(IMAGE_EXTS):
                    continue
                parsed_chef, parsed_name = self.parse_filename(file)
                for k in [parsed_name, parsed_chef if parsed_chef else "", file.split(".")[0]]:
                    if k and k != "none":
                        self.cached_chefs.setdefault(k, []).append(os.path.join(chef_dir, file))

        self.cached_memes.clear()
        for w in self.worlds:
            self.cached_memes[w] = {}
            for mood in self.moods:
                self.cached_memes[w][mood] = []
                t_dir = os.path.join(self.data_dir, "memes", w, mood)
                if os.path.exists(t_dir):
                    files = [f for f in os.listdir(t_dir) if f.lower().endswith(IMAGE_EXTS)]
                    if files:
                        self.cached_memes[w][mood] = [os.path.join(t_dir, f) for f in files]

    @staticmethod
    def parse_filename(filename: str):
        pattern = re.compile(r"^(?:【(.*?)】)?(.*?)(?:_\d+)?\.(?:jpg|jpeg|png|gif|webp|bmp)$", re.I)
        match = pattern.match(filename)
        if match:
            return match.group(1), match.group(2).strip()
        return None, os.path.splitext(filename)[0]

    def scan_all_items(self, wv_settings: dict, category: str):
        pool = []
        cat_map = {"food": ("food", "特产食物"), "drink": ("drink", "特产饮品"), "dark": ("darkfood", "黑暗料理")}
        folder_name, food_type = cat_map.get(category, ("food", "特产食物"))

        for w in self.worlds:
            if w != "common":
                w_conf = wv_settings.get(w, {})
                if w_conf.get("enable", True) is False:
                    continue
            target_dir = os.path.join(self.data_dir, folder_name, w)
            if not os.path.exists(target_dir):
                continue
            for file in os.listdir(target_dir):
                if file.startswith(".") or not file.lower().endswith(IMAGE_EXTS):
                    continue
                file_path = os.path.join(target_dir, file)
                chef, food_name = self.parse_filename(file)
                pool.append({
                    "raw_name": food_name, "food": food_name, "chef": chef or "none",
                    "wv": w, "food_type": food_type, "has_image": True, "path": file_path,
                })

        for w_key, conf in wv_settings.items():
            if w_key not in self.worlds:
                continue
            if w_key != "common" and conf.get("enable", True) is False:
                continue
            text_key_map = {"food": "7.文字食物", "drink": "8.文字饮品", "dark": "9.文字黑暗料理"}
            t_key = text_key_map.get(category, "7.文字食物")
            for text_item in conf.get(t_key, []):
                if text_item and not any(p["food"] == text_item and p["wv"] == w_key for p in pool):
                    pool.append({
                        "raw_name": text_item, "food": text_item, "chef": "none",
                        "wv": w_key, "food_type": food_type, "has_image": False, "path": None,
                    })
        return pool

    def get_chef_image(self, chef_name: str):
        if not chef_name or chef_name == "none":
            return None
        matched_files = []
        for k, v in self.cached_chefs.items():
            if chef_name in k or k in chef_name:
                matched_files.extend(v)
        if matched_files:
            gifs = [f for f in matched_files if f.lower().endswith(".gif")]
            if gifs:
                return random.choice(gifs)
            return random.choice(matched_files)
        return None

    def get_bot_meme(self, world_key: str, mood: str):
        files = self.cached_memes.get(world_key, {}).get(mood, [])
        if files:
            return random.choice(files)
        return None

    def get_egg_meme(self, char_name: str):
        char_dir = os.path.join(self.data_dir, "ganfanren", char_name)
        if os.path.exists(char_dir):
            files = [f for f in os.listdir(char_dir) if f.lower().endswith(IMAGE_EXTS)]
            if files:
                return os.path.join(char_dir, random.choice(files))
        return None


# ============================================================
# 干饭人扫描（移植自 main._get_ganfanren_data）
# ============================================================


def scan_ganfanren(data_dir: Path) -> dict:
    pool: dict[str, dict] = {}
    user_dir = data_dir / "ganfanren"
    user_dir.mkdir(parents=True, exist_ok=True)
    if not user_dir.exists():
        return pool
    for folder_name in os.listdir(str(user_dir)):
        folder_path = user_dir / folder_name
        if not folder_path.is_dir():
            continue
        entry = pool.setdefault(folder_name, {"images": [], "words": []})
        for file_name in os.listdir(str(folder_path)):
            file_path = folder_path / file_name
            if file_name.lower().endswith(IMAGE_EXTS):
                entry["images"].append(str(file_path))
            elif file_name.lower() == "words.txt":
                lines = None
                for enc in ("utf-8", "gbk"):
                    try:
                        lines = (folder_path / "words.txt").read_text(encoding=enc).splitlines()
                        break
                    except UnicodeDecodeError:
                        continue
                    except Exception:
                        break
                if lines:
                    entry["words"].extend(line.strip() for line in lines if line.strip())
    for k in [k for k, v in pool.items() if not v["images"]]:
        del pool[k]
    return pool


# ============================================================
# 世界 / 模板 配置加载（数据目录 JSON，首次从 resource 复制默认）
# ============================================================


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    shutil.move(str(tmp), str(path))


def load_worlds(data_dir: Path, resource_dir: Path) -> dict:
    """加载世界配置（wv_settings）。数据目录 worlds.json 缺失时从 resource 复制默认。"""
    target = data_dir / "worlds.json"
    if not target.exists():
        try:
            shutil.copy2(str(resource_dir / "default_worlds.json"), str(target))
        except Exception:
            pass
    try:
        return _load_json(target)
    except Exception:
        return _load_json(resource_dir / "default_worlds.json")


def load_templates(data_dir: Path, resource_dir: Path) -> dict:
    target = data_dir / "templates.json"
    if not target.exists():
        try:
            shutil.copy2(str(resource_dir / "default_templates.json"), str(target))
        except Exception:
            pass
    try:
        return _load_json(target)
    except Exception:
        return _load_json(resource_dir / "default_templates.json")


def rebuild_alias_map(wv_settings: dict, extra_aliases: dict | None = None) -> dict:
    alias_map: dict[str, str] = {}
    for i in range(1, 5):
        w_key = f"world{i}"
        aliases = list(extra_aliases or {}).get(w_key, []) if extra_aliases else []
        inner_conf = wv_settings.get(w_key, {})
        inner_aliases = inner_conf.get("2.世界别称", [])
        world_name = inner_conf.get("1.世界名称", "")
        # 世界主名称（如"鸣潮"）同样作为别名参与 [别名]特产/特饮 触发
        combined = {str(a).strip() for a in (list(aliases) + list(inner_aliases) + [world_name]) if a}
        for alias in combined:
            alias_map[alias] = w_key
    return alias_map


def resolve_active_key(selection: str) -> str:
    if "世界1" in selection:
        return "world1"
    if "世界2" in selection:
        return "world2"
    if "世界3" in selection:
        return "world3"
    if "世界4" in selection:
        return "world4"
    return "world1"
