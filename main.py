"""
astrbot_plugin_jm_downloader v1.0.0
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

JMComic 漫画下载器 — 通过 /jm 命令下载禁漫天堂漫画并转为 PDF 上传至 QQ 群文件。

命令:
  /jm <编号>         下载漫画并上传 PDF（已下载过的直接发送）
  /jm list            列出所有已下载的漫画（所有人可用）
  /jm delete <编号>   删除指定编号的漫画（仅管理员）
  /jm delete all      删除全部漫画（仅管理员，需二次确认）

依赖:
  pip install jmcomic Pillow
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ── 可选依赖（按需导入，支持热安装无需重启） ──────────────

def _get_jmcomic():
    """按需导入 jmcomic，支持 pip install 后无需重启即生效。"""
    try:
        import jmcomic as jm
        return jm
    except ImportError as e:
        logger.error(f"[JMDownloader] jmcomic 导入失败: {e}")
        return None

def _get_pil_image():
    """按需导入 PIL.Image。"""
    try:
        from PIL import Image as img
        return img
    except ImportError as e:
        logger.error(f"[JMDownloader] Pillow 导入失败: {e}")
        return None


# ── 插件注册 ──────────────────────────────────────────────

@register(
    "astrbot_plugin_jm_downloader",
    "bentianjia",
    "JMComic 漫画下载器 - 下载禁漫天堂漫画并转为 PDF 上传至 QQ 群文件",
    "1.0.0",
)
class JmDownloaderPlugin(Star):
    """
    JMComic 漫画下载器插件。

    基于 JMComic-Crawler-Python API，支持:
    - 指定漫画编号下载所有章节图片
    - 自动合并为单个 PDF
    - 上传 PDF 到 QQ 群文件
    - 已下载的漫画直接复用，不重复下载
    - 管理员可管理（列表/删除）
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._semaphore = asyncio.Semaphore(3)  # 最大允许 3 个并发下载任务
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="jm_")
        self._pending_confirm: Dict[str, float] = {}  # delete all 二次确认
        self._cleanup_tasks: Dict[str, asyncio.Task] = {}
        self.search_context = {}  # 保存用户的搜索上下文 (sender_id, group_id) -> query

    def _schedule_pdf_cleanup(self, album_id: str, pdf_path: Path, zip_path: Path) -> None:
        """后台挂起，5分钟后清理PDF（按需打包ZIP）"""
        if album_id in self._cleanup_tasks:
            self._cleanup_tasks[album_id].cancel()

        async def cleanup() -> None:
            try:
                # 1. 立即将PDF打包为ZIP（如果还没打包过）
                if pdf_path.exists() and not zip_path.exists():
                    def do_zip():
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            zf.write(pdf_path, arcname=pdf_path.name)
                    await asyncio.get_event_loop().run_in_executor(self._executor, do_zip)

                # 2. 等待 5 分钟
                await asyncio.sleep(300)

                # 3. 时间到，删除原始 PDF 以释放空间
                if pdf_path.exists():
                    pdf_path.unlink(missing_ok=True)
                    logger.info(f"[JMDownloader] 已清理 {album_id} 的原始 PDF，仅保留压缩包。")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[JMDownloader] 清理任务异常: {e}")

        self._cleanup_tasks[album_id] = asyncio.create_task(cleanup())

        if _get_jmcomic() is None:
            logger.error(
                "[JMDownloader] jmcomic 未安装，请运行: pip install jmcomic"
            )
        if _get_pil_image() is None:
            logger.error(
                "[JMDownloader] Pillow 未安装，请运行: pip install Pillow"
            )

    async def terminate(self) -> None:
        """插件卸载时清理线程池。"""
        if self._executor:
            self._executor.shutdown(wait=False)
        logger.info("[JMDownloader] 插件已卸载")

    # ══════════════════════════════════════════════════════════
    #  配置
    # ══════════════════════════════════════════════════════════

    def _get_config(self) -> Dict[str, Any]:
        """读取 WebUI 配置，返回带默认值的 dict。"""
        try:
            cfg = self.context.get_config()
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}

        base_dir_raw = cfg.get("download_base_dir", "")
        if not base_dir_raw or not str(base_dir_raw).strip():
            base_dir_raw = "data/jm_downloads"
            
        base_dir = str(base_dir_raw).strip()
        if not os.path.isabs(base_dir):
            base_dir = os.path.abspath(os.path.join(os.getcwd(), base_dir))
        else:
            base_dir = os.path.abspath(base_dir)
            
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"[JMDownloader] 创建下载根目录失败: {e}")

        return {
            "enable": bool(cfg.get("enable", True)),
            "download_base_dir": base_dir,
            "pdf_quality": max(1, min(100, int(cfg.get("pdf_quality", 95)))),
            "progress_updates": bool(cfg.get("progress_updates", True)),
        }
    # ══════════════════════════════════════════════════════════
    #  工具方法
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为 AstrBot 管理员。"""
        try:
            return bool(event.is_admin())
        except Exception:
            try:
                return getattr(event, "role", "") == "admin"
            except Exception:
                return False

    @staticmethod
    async def _get_bot(event: AstrMessageEvent):
        """从事件中获取 bot 客户端。"""
        try:
            bot = getattr(event, "bot", None)
            if bot and hasattr(bot, "call_action"):
                return bot
        except Exception:
            pass
        try:
            bot = event.get_bot()
            if bot and hasattr(bot, "call_action"):
                return bot
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_int(val) -> Optional[int]:
        """安全转 int，空值返回 None。"""
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    async def _get_sender_info(
        event: AstrMessageEvent,
    ) -> Tuple[Optional[int], Optional[int]]:
        """返回 (sender_id, group_id)。私聊时 group_id 为 None。"""
        sender_id = None
        group_id = None
        try:
            sender_id = event.get_sender_id()
        except Exception:
            pass
        try:
            group_id = event.get_group_id()
        except Exception:
            pass
        # 回退：从 message_obj 提取
        if not sender_id:
            try:
                sender_id = getattr(event.message_obj, "user_id", None)
            except Exception:
                pass
        if not group_id:
            try:
                group_id = getattr(event.message_obj, "group_id", None)
            except Exception:
                pass
        return (
            JmDownloaderPlugin._safe_int(sender_id),
            JmDownloaderPlugin._safe_int(group_id),
        )

    def _parse_args(self, event: AstrMessageEvent) -> List[str]:
        """提取命令后面的参数列表，兼容任意自定义唤醒前缀。"""
        try:
            text = event.message_str.strip()
        except Exception:
            return []
        # 用正则去除任意前缀 + "jm"，只保留后面的参数
        # 兼容 /jm, #jm, >>jm, !jm, bot jm 等任意自定义唤醒词
        text = re.sub(r'^.*?jm\s*', '', text, count=1, flags=re.IGNORECASE)
        if not text:
            return []
        return text.split()

    # ══════════════════════════════════════════════════════════
    #  元数据持久化
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _load_downloads(base_dir: Path) -> Dict[str, Any]:
        """从 downloads.json 读取下载记录。自动迁移旧格式的列表到多重作用域字典。"""
        path = base_dir / "downloads.json"
        if not path.exists():
            return {"albums": {}, "disabled_groups": [], "blacklist_jm": {"global": [], "group": {}, "user": {}}, "blacklist": [], "blacklist_tag": {"global": [], "group": {}, "user": {}}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON content is not a dict")
            data.setdefault("albums", {})
            data.setdefault("disabled_groups", [])
            data.setdefault("blacklist", [])
            
            data.setdefault("batch_enabled", True)
            data.setdefault("batch_max", 10)
            for key in ["blacklist_jm", "blacklist_tag"]:
                val = data.get(key)
                if isinstance(val, list):
                    data[key] = {"global": val, "group": {}, "user": {}}
                elif isinstance(val, dict):
                    data[key].setdefault("global", [])
                    data[key].setdefault("group", {})
                    data[key].setdefault("user", {})
                else:
                    data[key] = {"global": [], "group": {}, "user": {}}
            return data
        except Exception as e:
            logger.error(f"[JMDownloader] 无法读取或解析 downloads.json: {e}")
            raise RuntimeError(f"读取配置数据失败，防止覆盖原数据已中止操作: {e}")

    @staticmethod
    def _save_downloads(base_dir: Path, data: Dict[str, Any]) -> bool:
        """原子写入 downloads.json（先写 .tmp 再 replace）。"""
        path = base_dir / "downloads.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
        except OSError as e:
            logger.error(f"[JMDownloader] 无法写入 downloads.json: {e}")
            return False

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小。"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _sanitize_filename(name: str, max_len: int = 30) -> str:
        """清理文件名中的非法字符，截断长度。"""
        name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
        name = name.strip().strip(".")
        if len(name) > max_len:
            name = name[:max_len]
        return name or "unknown"

    # ══════════════════════════════════════════════════════════
    #  群聊黑名单
    # ══════════════════════════════════════════════════════════

    def _is_group_disabled(self, group_id: Optional[int]) -> bool:
        """检查群是否被禁用。私聊（group_id=None）不受影响。"""
        if group_id is None:
            return False
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        return str(group_id) in meta.get("disabled_groups", [])

    def _set_group_state(self, group_id: str, enabled: bool) -> bool:
        """启用/禁用群。返回 True 表示操作成功。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        meta = self._load_downloads(base_dir)
        disabled = meta.setdefault("disabled_groups", [])
        if enabled:
            if group_id in disabled:
                disabled.remove(group_id)
        else:
            if group_id not in disabled:
                disabled.append(group_id)
        return self._save_downloads(base_dir, meta)

    def _is_user_blacklisted(self, user_id: Optional[int]) -> bool:
        """检查用户是否在黑名单中。"""
        if user_id is None:
            return False
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        return str(user_id) in meta.get("blacklist", [])

    def _set_user_blacklist(self, user_id: str, add: bool) -> bool:
        """添加/移除用户黑名单。返回 True 表示操作成功。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        meta = self._load_downloads(base_dir)
        blacklist = meta.setdefault("blacklist", [])
        if add:
            if user_id not in blacklist:
                blacklist.append(user_id)
        else:
            if user_id in blacklist:
                blacklist.remove(user_id)
        return self._save_downloads(base_dir, meta)

    def _get_blacklist(self) -> List[str]:
        """获取黑名单列表。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        return meta.get("blacklist", [])

    def _update_blacklist(self, key_name: str, scope: str, target_id: Optional[str], item: Optional[str], action: str) -> bool:
        """统一管理作用域黑名单字典。action: add, remove, remove_all"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        meta = self._load_downloads(base_dir)
        
        bdict = meta.setdefault(key_name, {"global": [], "group": {}, "user": {}})
        if scope == "global":
            lst = bdict.setdefault("global", [])
            if action == "add" and item and item not in lst:
                lst.append(item)
            elif action == "remove" and item and item in lst:
                lst.remove(item)
            elif action == "remove_all":
                bdict["global"] = []
        elif scope in ["group", "user"]:
            if not target_id: return False
            scope_dict = bdict.setdefault(scope, {})
            if action == "remove_all":
                scope_dict.pop(target_id, None)
            else:
                lst = scope_dict.setdefault(target_id, [])
                if action == "add" and item and item not in lst:
                    lst.append(item)
                elif action == "remove" and item and item in lst:
                    lst.remove(item)
        return self._save_downloads(base_dir, meta)

    def _get_blacklist_items(self, key_name: str, scope: str, target_id: Optional[str]) -> List[str]:
        """获取指定作用域的黑名单列表"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        bdict = meta.get(key_name, {"global": [], "group": {}, "user": {}})
        if scope == "global":
            return bdict.get("global", [])
        elif scope in ["group", "user"]:
            if not target_id: return []
            return bdict.get(scope, {}).get(target_id, [])
        return []

    def _is_jm_blacklisted(self, album_id: str, sender_id: Optional[int], group_id: Optional[int]) -> bool:
        """检查漫画编号是否被拉黑。综合 global、group、user"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        bm = meta.get("blacklist_jm", {"global": [], "group": {}, "user": {}})
        if album_id in bm.get("global", []): return True
        if group_id is not None and album_id in bm.get("group", {}).get(str(group_id), []): return True
        if sender_id is not None and album_id in bm.get("user", {}).get(str(sender_id), []): return True
        return False

    def _get_combined_blacklisted_tags(self, sender_id: Optional[int], group_id: Optional[int]) -> List[str]:
        """获取当前环境综合生效的标签黑名单（并集）"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        bt = meta.get("blacklist_tag", {"global": [], "group": {}, "user": {}})
        tags = set(bt.get("global", []))
        if group_id is not None:
            tags.update(bt.get("group", {}).get(str(group_id), []))
        if sender_id is not None:
            tags.update(bt.get("user", {}).get(str(sender_id), []))
        return list(tags)

    # ══════════════════════════════════════════════════════════
    #  下载与 PDF 转换（同步，在线程池中执行）
    # ══════════════════════════════════════════════════════════

    def _download_and_convert(
        self,
        album_id: str,
        base_dir: Path,
        pdf_quality: int,
        sender_id: Optional[int] = None,
        group_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        同步函数：下载漫画 → 收集图片 → 合并为 PDF。
        使用 jmcomic 默认配置，避免自定义 option 触发的 API 兼容问题。

        返回:
          {"success": bool, "title": str, "pdf_path": str,
           "page_count": int, "pdf_size": int, "album_id": str, "error": str}
        """
        jmcomic = _get_jmcomic()
        Image = _get_pil_image()
        if jmcomic is None or Image is None:
            return {
                "success": False,
                "error": "jmcomic 或 Pillow 未安装",
                "title": album_id,
                "album_id": album_id,
            }

        tmp_dir = base_dir / f"_tmp_{album_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        title = album_id

        try:
            # ── 步骤 1: 内存生成 Option 并开启多线程 ──
            # 采用 16 线程并发下载以提升速度，并配置 base_dir 保证路径不冲突
            option_str = f"""
download:
  threading:
    image: 16
dir_rule:
  base_dir: '{str(tmp_dir).replace('\\', '/')}'
"""
            option = jmcomic.create_option_by_str(option_str)
            client = option.build_jm_client()
            album = client.get_album_detail(album_id)
            
            # 黑名单标签检查拦截
            blacklisted_tags = self._get_combined_blacklisted_tags(sender_id, group_id)
            if blacklisted_tags:
                for tag in album.tags:
                    if tag in blacklisted_tags:
                        return {
                            "success": False,
                            "error": f"由于包含黑名单标签「{tag}」，拦截下载。",
                            "title": album.title,
                            "album_id": album_id,
                        }
            
            jmcomic.download_album(album_id, option)

            # ── 步骤 2: 创建目标目录 ──
            target_dir = base_dir / album_id
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

            # 尝试通过 tmp_dir 下的子目录名来获取真实的本子标题（如果有）
            subdirs = sorted([d for d in tmp_dir.iterdir() if d.is_dir() and d.name != "__pycache__"])
            if subdirs:
                title = subdirs[0].name

            # ── 步骤 3: 递归搜索所有图片并平铺移动 ──
            image_extensions = {".webp", ".jpg", ".jpeg", ".png"}
            image_files_found = []
            for p in tmp_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in image_extensions:
                    image_files_found.append(p)

            # 去重
            image_files_found = list(dict.fromkeys(image_files_found))
            
            if not image_files_found:
                return {
                    "success": False,
                    "error": "下载后未找到任何图片文件",
                    "title": title,
                    "album_id": album_id,
                }

            # ── 步骤 4: 健壮的自然排序 ──
            def _natural_sort_key(p: Path) -> list:
                return [int(text) if text.isdigit() else text.lower() 
                        for text in re.split(r'(\d+)', p.stem)]

            image_files_sorted = sorted(image_files_found, key=_natural_sort_key)

            # ── 步骤 5: 移动图片到目标目录 ──
            final_image_paths = []
            seen_names = set()
            for img_path in image_files_sorted:
                dest_name = img_path.name
                if dest_name in seen_names:
                    # 如果有同名图片，前置其父目录名避免覆盖
                    dest_name = f"{img_path.parent.name}_{dest_name}"
                seen_names.add(dest_name)
                
                target_path = target_dir / dest_name
                shutil.move(str(img_path), str(target_path))
                final_image_paths.append(target_path)

            logger.info(
                f"[JMDownloader] #{album_id} 下载完成，"
                f"标题: {title}, 提取并移动 {len(final_image_paths)} 张图片"
            )

            image_files = final_image_paths
            logger.info(
                f"[JMDownloader] 找到 {len(image_files)} 张图片，生成 PDF..."
            )

            # ── 步骤 5: 合并 PDF ──
            pdf_path = target_dir / f"{album_id}.pdf"
            images = []
            try:
                for img_path in image_files:
                    try:
                        img = Image.open(img_path)
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        images.append(img)
                    except Exception as e:
                        logger.warning(
                            f"[JMDownloader] 跳过损坏图片 "
                            f"{img_path.name}: {e}"
                        )
                        continue

                if not images:
                    return {
                        "success": False,
                        "error": "所有图片均无法打开",
                        "title": title,
                        "album_id": album_id,
                    }

                first = images[0]
                rest = images[1:] if len(images) > 1 else []
                first.save(
                    pdf_path, "PDF",
                    save_all=True, append_images=rest,
                    quality=pdf_quality,
                )
            finally:
                for img in images:
                    try:
                        img.close()
                    except Exception:
                        pass
                
                # ── 删除散图以释放一半磁盘空间 ──
                for img_path in image_files:
                    try:
                        if os.path.exists(img_path):
                            os.remove(img_path)
                    except Exception:
                        pass

            pdf_size = pdf_path.stat().st_size
            logger.info(
                f"[JMDownloader] PDF 完成: {pdf_path} "
                f"({len(images)} 页, {self._format_size(pdf_size)})"
            )

            return {
                "success": True,
                "title": title,
                "pdf_path": str(pdf_path),
                "page_count": len(images),
                "pdf_size": pdf_size,
                "album_id": album_id,
            }

        except Exception as e:
            import traceback
            logger.error(
                f"[JMDownloader] 下载/转换 #{album_id} 失败:\n"
                f"{traceback.format_exc()}"
            )
            return {
                "success": False,
                "error": str(e),
                "title": title,
                "album_id": album_id,
            }
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════
    #  上传 PDF 到群文件
    # ══════════════════════════════════════════════════════════

    async def _upload_file(
        self,
        event: AstrMessageEvent,
        group_id: Optional[int],
        sender_id: int,
        file_path: str,
        album_id: str,
        title: str,
    ) -> bool:
        """上传文件（PDF 或 ZIP）。"""
        bot = await self._get_bot(event)
        if not bot:
            return False

        ext = Path(file_path).suffix
        display_name = f"JM{album_id}_{self._sanitize_filename(title, 20)}{ext}"

        try:
            if group_id:
                resp = await bot.call_action(
                    "upload_group_file",
                    group_id=group_id,
                    file=file_path,
                    name=display_name,
                )
            else:
                resp = await bot.call_action(
                    "upload_private_file",
                    user_id=sender_id,
                    file=file_path,
                    name=display_name,
                )
            logger.info(
                f"[JMDownloader] 上传成功: {display_name}, resp={resp}"
            )
            return True
        except Exception as e:
            logger.error(f"[JMDownloader] 上传失败: {display_name}: {e}")
            raise

    # ══════════════════════════════════════════════════════════
    #  命令路由: /jm
    # ══════════════════════════════════════════════════════════

    @filter.command("jm")
    async def cmd_jm(self, event: AstrMessageEvent):
        """
        /jm 主命令路由器。

        /jm <编号>         → 下载
        /jm list           → 列出所有下载
        /jm delete <编号>  → 删除单个
        /jm delete all     → 删除全部（二次确认）
        """
        cfg = self._get_config()
        if not cfg["enable"]:
            return

        args = self._parse_args(event)
        sender_id, group_id = await self._get_sender_info(event)
        ctx_key = (sender_id, group_id)
        
        if args and args[0].lower() == "page":
            if ctx_key not in self.search_context:
                yield event.plain_result("❌ 没有可翻页的搜索记录，请先使用 /jm search <关键词>")
                return
            query = self.search_context[ctx_key]
            # 转换为 /jm search <query> <page>
            args = ["search"] + query.split() + args[1:]
            
        if not args:
            yield event.plain_result(
                "📚 JMComic 漫画下载器\n"
                "用法:\n"
                "  /jm <编号>        下载漫画并上传 PDF\n"
                "  /jm search <关键词> 搜索漫画\n"
                "  /jm page <页码>    搜索翻页\n"
                "  /jm batch <编号> [编号2...] 批量下载多个漫画\n"
                "  /jm list           列出已下载的漫画\n"
                "  /jm delete <编号>  删除指定漫画缓存（管理员）\n"
                "  /jm delete all     删除全部漫画缓存（管理员）\n"
                "  /jm batch on/off   开启/关闭批量下载（管理员）\n"
                "  /jm batch max <数> 设置批量单次上限（管理员）\n"
                "  /jm group off <群号> 对该群禁用（管理员）\n"
                "  /jm group on <群号>  对该群恢复（管理员）\n"
                "  /jm black add <QQ号> 拉黑用户（管理员）\n"
                "  /jm black remove <QQ号> 移除拉黑（管理员）\n"
                "  /jm black list    查看用户黑名单（管理员）\n"
                "  /jm black_jm add <编号> 拉黑单本漫画（管理员）\n"
                "  /jm black_jm remove <编号> 移除单本漫画拉黑（管理员）\n"
                "  /jm black_jm list  查看单本拉黑列表（管理员）\n"
                "  /jm black_tag add <作用域> <标签> 拉黑特定标签（管理员）\n"
                "  /jm black_tag remove <作用域> <标签> 移除标签拉黑（管理员）\n"
                "  /jm black_tag list <作用域> 查看已拉黑的标签（管理员）\n"
                "  /jm black_tag remove_all <作用域> confirm 清空该作用域标签拉黑（管理员）"
            )
            return

        sub = args[0].lower()
        
        if sub == "search":
            # 保存搜索上下文 (不包含页码)
            page_query = args[1:]
            if page_query and page_query[-1].isdigit():
                page_query = page_query[:-1]
            if page_query:
                self.search_context[ctx_key] = " ".join(page_query)
        else:
            self.search_context.pop(ctx_key, None)

        if sub == "list":
            async for result in self._handle_list(event):
                yield result
        elif sub == "delete":
            async for result in self._handle_delete(event, args[1:]):
                yield result
        elif sub == "group":
            async for result in self._handle_group(event, args[1:]):
                yield result
        elif sub == "black":
            async for result in self._handle_black(event, args[1:]):
                yield result
        elif sub == "black_jm":
            async for result in self._handle_black_jm(event, args[1:]):
                yield result
        elif sub == "black_tag":
            async for result in self._handle_black_tag(event, args[1:]):
                yield result
        elif sub == "search":
            async for result in self._handle_search(event, args[1:]):
                yield result
        elif sub == "batch":
            async for result in self._handle_batch(event, args[1:]):
                yield result
        elif sub.isdigit():
            async for result in self._handle_download(event, sub):
                yield result
        else:
            yield event.plain_result(
                f"❌ 未知子命令: {sub}\n"
                f"请输入 /jm 查看帮助。"
            )

    # ══════════════════════════════════════════════════════════
    #  /jm search — 搜索漫画
    # ══════════════════════════════════════════════════════════

    async def _handle_search(self, event: AstrMessageEvent, args: List[str]):
        """处理 /jm search 关键词... [页码] 命令"""
        if not args:
            yield event.plain_result("❌ 请输入搜索关键词。用法: /jm search 关键词 [页码]")
            return
            
        # 解析关键词和页码
        page = 1
        if args[-1].isdigit():
            page = int(args[-1])
            query = " ".join(args[:-1])
        else:
            query = " ".join(args)
            
        if not query:
            yield event.plain_result("❌ 请输入有效的搜索关键词。")
            return

        jmcomic = _get_jmcomic()
        if not jmcomic:
            yield event.plain_result("❌ jmcomic 库未安装。")
            return

        yield event.plain_result(f"🔍 正在搜索「{query}」第 {page} 页，并校验违禁词，请稍候...")
        
        loop = asyncio.get_event_loop()
        sender_id, group_id = await self._get_sender_info(event)
        blacklisted_tags = self._get_combined_blacklisted_tags(sender_id, group_id)
        
        def _do_search():
            client = jmcomic.JmOption.default().build_jm_client()
            search_page = client.search_site(query, page=page)
            
            raw_items = list(search_page.iter_id_title_tag())
            if not raw_items:
                return {"items": [], "filtered": 0, "total": 0}
                
            # 只取前 5 个结果进行校验，减少接口请求时间和图片刷屏
            items_to_check = raw_items[:5]
            valid_results = []
            filtered_count = 0
            
            # 如果没有任何全局或个人的标签黑名单，则无需获取详情，极大提升速度
            if not blacklisted_tags:
                for item in items_to_check:
                    if self._is_jm_blacklisted(item[0], sender_id, group_id):
                        filtered_count += 1
                    else:
                        valid_results.append((item[0], item[1]))
                return {"items": valid_results, "filtered": filtered_count, "total": len(raw_items)}
            
            # 否则并发获取详细信息以检查 tags
            import concurrent.futures
            results_with_order = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_to_index = {
                    executor.submit(client.get_album_detail, item[0]): (i, item)
                    for i, item in enumerate(items_to_check)
                }
                
                for future in concurrent.futures.as_completed(future_to_index):
                    i, item = future_to_index[future]
                    try:
                        album_detail = future.result()
                        is_safe = True
                        for t in album_detail.tags:
                            if t in blacklisted_tags:
                                is_safe = False
                                break
                                
                        if self._is_jm_blacklisted(item[0], sender_id, group_id):
                            is_safe = False
                            
                        if is_safe:
                            results_with_order.append((i, (item[0], item[1])))
                        else:
                            filtered_count += 1
                    except Exception as e:
                        logger.error(f"[JMDownloader] 搜索校验异常 {item[0]}: {e}")
                        
            # 恢复原始搜索排序
            results_with_order.sort(key=lambda x: x[0])
            valid_results = [r[1] for r in results_with_order]
            return {"items": valid_results, "filtered": filtered_count, "total": len(raw_items)}

        try:
            res = await loop.run_in_executor(self._executor, _do_search)
        except Exception as e:
            logger.error(f"[JMDownloader] 搜索失败: {e}")
            yield event.plain_result(f"❌ 搜索请求失败: {e}")
            return
            
        items = res["items"]
        if not items:
            msg = f"📭 「{query}」第 {page} 页没有找到可用结果。"
            if res["filtered"] > 0:
                msg += f"\n(拦截了 {res['filtered']} 个包含违禁词的本子)"
            yield event.plain_result(msg)
            return
            
        # 并发获取所有结果的封面
        try:
            cfg = self._get_config()
            base_dir = Path(cfg["download_base_dir"]).absolute()
            search_covers_dir = base_dir / "search_covers"
            search_covers_dir.mkdir(parents=True, exist_ok=True)
            
            cover_paths = [str(search_covers_dir / f"cover_search_{aid}.jpg") for aid, _ in items]
            
            def _download_all_covers():
                client = jmcomic.JmOption.default().build_jm_client()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    for aid, _ in items:
                        cover_path = str(search_covers_dir / f"cover_search_{aid}.jpg")
                        if not Path(cover_path).exists():
                            executor.submit(client.download_album_cover, aid, cover_path)
                            
            await loop.run_in_executor(self._executor, _download_all_covers)
            
            result = event.make_result()
            
            info_text = f"🔍 搜索「{query}」结果 (第 {page} 页):\n"
            if res["filtered"] > 0:
                info_text += f"🛡️ 自动拦截了 {res['filtered']} 个包含违禁词/黑名单的结果。\n"
            result.message(info_text)
            
            for aid, title in items:
                result.message(f"\n📦 [{aid}] {title}\n")
                cover_path = str(search_covers_dir / f"cover_search_{aid}.jpg")
                if Path(cover_path).exists():
                    result.file_image(cover_path)
                    
            result.message(f"\n💡 下载发送 /jm <编号>，下一页发送 /jm page {page + 1}")
            
            # 5 分钟后自动清理搜索封面的任务
            async def cleanup_search_covers(paths_to_clean):
                await asyncio.sleep(300)
                for p in paths_to_clean:
                    try:
                        p_obj = Path(p)
                        if p_obj.exists():
                            p_obj.unlink()
                    except Exception as e:
                        logger.error(f"[JMDownloader] 清理搜索封面异常 {p}: {e}")
            
            task_id = f"search_cleanup_{time.time()}"
            task = asyncio.create_task(cleanup_search_covers(cover_paths))
            self._cleanup_tasks[task_id] = task
            task.add_done_callback(lambda t: self._cleanup_tasks.pop(task_id, None))
            
            yield result
            return
            
        except Exception as e:
            logger.error(f"[JMDownloader] 获取封面失败: {e}")
            
        # 回退逻辑
        lines = [f"🔍 搜索「{query}」结果 (第 {page} 页):"]
        for aid, title in items:
            lines.append(f"📦 [{aid}] {title}")
        if res["filtered"] > 0:
            lines.append(f"\n🛡️ 自动拦截了 {res['filtered']} 个包含违禁词/黑名单的结果。")
        lines.append(f"💡 下载发送 /jm <编号>，下一页发送 /jm page {page + 1}")
        yield event.plain_result("\n".join(lines))

    # ══════════════════════════════════════════════════════════
    #  /jm batch — 批量下载
    # ══════════════════════════════════════════════════════════

    async def _handle_batch(self, event: AstrMessageEvent, args: List[str]):
        """处理 /jm batch 命令"""
        if not args:
            yield event.plain_result(
                "用法:\n"
                "  /jm batch <编号> [编号2...]  批量下载多个本子\n"
                "  /jm batch on/off           开启/关闭批量下载 (管理员)\n"
                "  /jm batch max <数量>       设置单次批量上限 (管理员，-1 为无上限)"
            )
            return

        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        meta = self._load_downloads(base_dir)

        sub = args[0].lower()
        is_admin = self._is_admin(event)

        if sub in ["on", "off"]:
            if not is_admin:
                yield event.plain_result("⛔ 仅管理员可设置批量下载开关。")
                return
            meta["batch_enabled"] = (sub == "on")
            self._save_downloads(base_dir, meta)
            yield event.plain_result(f"✅ 批量下载功能已{'开启' if sub == 'on' else '关闭'}。")
            return
            
        if sub == "max":
            if not is_admin:
                yield event.plain_result("⛔ 仅管理员可设置批量下载上限。")
                return
            if len(args) < 2 or not (args[1].isdigit() or args[1] == "-1"):
                yield event.plain_result("❌ 请输入有效的数量 (数字，-1为无上限)。")
                return
            meta["batch_max"] = int(args[1])
            self._save_downloads(base_dir, meta)
            yield event.plain_result(f"✅ 批量下载单次上限已设为: {'无上限' if args[1] == '-1' else args[1]}。")
            return

        # 执行批量下载
        if not meta.get("batch_enabled", True):
            yield event.plain_result("⛔ 批量下载功能当前已被管理员关闭。")
            return

        # 解析编号：将所有参数拼起来，把非数字字符全部视作分隔符
        raw_str = " ".join(args)
        import re
        ids = list(dict.fromkeys(re.findall(r'\d+', raw_str))) # 提取并去重
        
        if not ids:
            yield event.plain_result("❌ 未检测到有效的漫画编号。")
            return

        batch_max = meta.get("batch_max", 10)
        if batch_max != -1 and len(ids) > batch_max:
            yield event.plain_result(f"⚠️ 单次最多允许批量下载 {batch_max} 本，您提交了 {len(ids)} 本。请分批下载。")
            return

        yield event.plain_result(f"🚀 收到批量下载请求，共 {len(ids)} 本。已加入后台队列，将按顺序发送，请耐心等待...")
        
        # 逐个触发下载
        for aid in ids:
            async for res in self._handle_download(event, aid):
                yield res


    # ══════════════════════════════════════════════════════════
    #  /jm <编号> — 下载
    # ══════════════════════════════════════════════════════════

    async def _handle_download(self, event: AstrMessageEvent, album_id: str):
        """处理 /jm <编号> 下载命令。全程静默，仅发送最终结果一条消息。"""
        # 按需检查依赖（支持热安装，无需重启）
        jmcomic = _get_jmcomic()
        Image = _get_pil_image()
        if jmcomic is None:
            yield event.plain_result(
                "❌ jmcomic 库未安装。请管理员运行: pip install jmcomic"
            )
            return
        if Image is None:
            yield event.plain_result(
                "❌ Pillow 库未安装。请管理员运行: pip install Pillow"
            )
            return

        cfg = self._get_config()
        sender_id, group_id = await self._get_sender_info(event)

        # 黑名单检查
        if self._is_user_blacklisted(sender_id):
            yield event.plain_result("⛔ 你已被管理员拉黑，无法使用此功能。")
            return
        if self._is_group_disabled(group_id):
            yield event.plain_result("⛔ 本群已被管理员禁用此功能。")
            return
        if self._is_jm_blacklisted(album_id, sender_id, group_id):
            yield event.plain_result(
                f"⛔ 漫画 #{album_id} 已在当前环境中被禁止下载。"
            )
            return

        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        album_dir = base_dir / album_id
        pdf_path = album_dir / f"{album_id}.pdf"
        zip_path = album_dir / f"{album_id}.zip"

        # ── 检查是否有缓存（ZIP 或 PDF） ──
        if not pdf_path.exists() and zip_path.exists():
            if cfg.get("progress_updates", True):
                yield event.plain_result(f"⏳ 正在从压缩包中为您提取漫画 #{album_id}，请稍候...")
            try:
                def do_unzip():
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        zf.extract(pdf_path.name, path=album_dir)
                await asyncio.get_event_loop().run_in_executor(self._executor, do_unzip)
            except Exception as e:
                logger.error(f"[JMDownloader] 解压 #{album_id} 异常: {e}")
                yield event.plain_result(f"❌ 解压缓存文件失败: {e}")
                return

        if pdf_path.exists():
            meta = self._load_downloads(base_dir)
            info = meta.get("albums", {}).get(album_id, {})
            title = info.get("title", album_id)
            page_count = info.get("page_count", "?")
            pdf_size = info.get("pdf_size", pdf_path.stat().st_size)

            try:
                await self._upload_file(
                    event, group_id, sender_id, str(pdf_path), album_id, title
                )
                yield event.plain_result(
                    f"✅ [CQ:at,qq={sender_id}] "
                    f"漫画 #{album_id}《{title}》"
                    f"（{page_count} 页, {self._format_size(pdf_size)}）"
                    f"已上传至群文件"
                )
            except Exception as e:
                yield event.plain_result(
                    f"⚠️ PDF 上传失败: {e}\n"
                    f"[CQ:at,qq={sender_id}] 正在尝试为您上传 ZIP 压缩包作为替代..."
                )
                try:
                    if not zip_path.exists():
                        def do_zip():
                            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                                zf.write(pdf_path, arcname=pdf_path.name)
                        await asyncio.get_event_loop().run_in_executor(self._executor, do_zip)
                    
                    await self._upload_file(
                        event, group_id, sender_id, str(zip_path), album_id, title
                    )
                    yield event.plain_result(
                        f"✅ [CQ:at,qq={sender_id}] "
                        f"ZIP 压缩包已成功作为替代上传！"
                    )
                except Exception as e2:
                    yield event.plain_result(
                        f"❌ ZIP 上传也失败了: {e2}\n"
                        f"文件路径: {pdf_path}\n"
                        f"[CQ:at,qq={sender_id}] 请联系管理员手动上传"
                    )
            
            # 无论成功与否，重置5分钟清理倒计时
            self._schedule_pdf_cleanup(album_id, pdf_path, zip_path)
            return

        # ── 并发检查 ──
        if self._semaphore.locked():
            yield event.plain_result(
                "⏳ 当前下载队列已满（最大并发 3），请稍后重试。"
            )
            return

        # ── 开始下载+转PDF+上传 ──
        if cfg.get("progress_updates", True):
            yield event.plain_result(f"⏳ 开始下载漫画 #{album_id}，请耐心等待...")

        loop = asyncio.get_event_loop()

        async with self._semaphore:
            try:
                result = await loop.run_in_executor(
                    self._executor,
                    self._download_and_convert,
                    album_id,
                    base_dir,
                    cfg["pdf_quality"],
                    sender_id,
                    group_id,
                )
            except Exception as e:
                logger.error(f"[JMDownloader] 下载 #{album_id} 异常: {e}")
                yield event.plain_result(f"❌ 下载失败: {e}")
                return

        if not result["success"]:
            yield event.plain_result(
                f"❌ 处理失败: {result.get('error', '未知错误')}"
            )
            return

        title = result["title"]
        pdf_path_str = result["pdf_path"]
        page_count = result["page_count"]
        pdf_size = result["pdf_size"]

        # ── 记录元数据 ──
        meta = self._load_downloads(base_dir)
        meta["albums"][album_id] = {
            "title": title,
            "page_count": page_count,
            "pdf_path": pdf_path_str,
            "pdf_size": pdf_size,
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_downloads(base_dir, meta)

        try:
            await self._upload_file(
                event, group_id, sender_id, pdf_path_str, album_id, title
            )
            yield event.plain_result(
                f"✅ [CQ:at,qq={sender_id}] "
                f"漫画 #{album_id}《{title}》"
                f"（{page_count} 页, {self._format_size(pdf_size)}）"
                f"已上传至群文件"
            )
        except Exception as e:
            yield event.plain_result(
                f"⚠️ PDF 已生成但上传失败: {e}\n"
                f"[CQ:at,qq={sender_id}] 正在尝试为您上传 ZIP 压缩包作为替代..."
            )
            try:
                if not zip_path.exists():
                    def do_zip():
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            zf.write(pdf_path, arcname=pdf_path.name)
                    await asyncio.get_event_loop().run_in_executor(self._executor, do_zip)
                
                await self._upload_file(
                    event, group_id, sender_id, str(zip_path), album_id, title
                )
                yield event.plain_result(
                    f"✅ [CQ:at,qq={sender_id}] "
                    f"ZIP 压缩包已成功作为替代上传！"
                )
            except Exception as e2:
                yield event.plain_result(
                    f"❌ ZIP 上传也失败了: {e2}\n"
                    f"📁 文件: {pdf_path_str}\n"
                    f"[CQ:at,qq={sender_id}] 请联系管理员手动上传"
                )
        
        # 触发 5 分钟倒计时清理
        self._schedule_pdf_cleanup(album_id, pdf_path, zip_path)

    # ══════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════
    #  /jm group — 群聊黑名单管理
    # ══════════════════════════════════════════════════════════

    async def _handle_group(
        self, event: AstrMessageEvent, args: List[str]
    ):
        """处理 /jm group on|off <群号>（管理员）。"""
        if not self._is_admin(event):
            yield event.plain_result("⛔ 仅管理员可使用此命令。")
            return

        if len(args) < 2:
            yield event.plain_result(
                "用法:\n"
                "  /jm group off <群号>    对该群禁用\n"
                "  /jm group on <群号>     对该群开启"
            )
            return

        action = args[0].lower()
        target = args[1]

        if not target.isdigit():
            yield event.plain_result("❌ 群号必须是数字。")
            return

        if action == "off":
            ok = self._set_group_state(target, enabled=False)
            yield event.plain_result(
                f"✅ 已对群 {target} 禁用 JM 下载功能。"
                if ok else "❌ 操作失败。"
            )
        elif action == "on":
            ok = self._set_group_state(target, enabled=True)
            yield event.plain_result(
                f"✅ 已对群 {target} 恢复 JM 下载功能。"
                if ok else "❌ 操作失败。"
            )
        else:
            yield event.plain_result(
                f"❌ 未知操作: {action}。支持: on / off"
            )

    # ══════════════════════════════════════════════════════════
    #  /jm black — 用户黑名单管理
    # ══════════════════════════════════════════════════════════

    async def _handle_black(
        self, event: AstrMessageEvent, args: List[str]
    ):
        """处理 /jm black add|remove|list（管理员）。"""
        if not self._is_admin(event):
            yield event.plain_result("⛔ 仅管理员可使用此命令。")
            return

        if not args:
            yield event.plain_result(
                "用法:\n"
                "  /jm black add <QQ号>     拉黑用户\n"
                "  /jm black remove <QQ号>  移除拉黑\n"
                "  /jm black list           查看黑名单"
            )
            return

        action = args[0].lower()

        if action == "list":
            blacklist = self._get_blacklist()
            if not blacklist:
                yield event.plain_result("📭 黑名单为空。")
            else:
                lines = [f"🚫 黑名单 ({len(blacklist)} 人):"]
                for uid in blacklist:
                    lines.append(f"  {uid}")
                yield event.plain_result("\n".join(lines))
        elif action in ("add", "remove"):
            if len(args) < 2:
                yield event.plain_result(
                    f"用法: /jm black {action} <QQ号>"
                )
                return
            target = args[1]
            if not target.isdigit():
                yield event.plain_result("❌ QQ 号必须是数字。")
                return
            add = (action == "add")
            ok = self._set_user_blacklist(target, add)
            act = "拉黑" if add else "移除拉黑"
            yield event.plain_result(
                f"✅ 已{act}用户 {target}。" if ok else "❌ 操作失败。"
            )
        else:
            yield event.plain_result(
                f"❌ 未知操作: {action}。支持: add / remove / list"
            )

    # ══════════════════════════════════════════════════════════
    #  /jm black_jm — 漫画编号黑名单
    # ══════════════════════════════════════════════════════════

    def _parse_blacklist_cmd(self, args: List[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
        """解析多作用域的黑名单指令。
        返回: (action, scope, target_id, item)
        """
        if not args: return "", "", None, None
        if len(args) < 2: return args[0].lower(), "", None, None
        
        # 容错处理：允许将 scope 和 action 写反，例如 "global list" 或 "global add"
        if args[0].lower() in ["global", "group", "user"] and args[1].lower() in ["add", "remove", "list", "remove_all"]:
            args[0], args[1] = args[1], args[0]

        action = args[0].lower()
        scope = args[1].lower()
        if scope not in ["global", "group", "user"]:
            return action, "", None, None
            
        if scope == "global":
            item = args[2] if len(args) > 2 else None
            return action, scope, None, item
        else:
            if len(args) < 3: return action, scope, None, None
            target_id = args[2]
            item = args[3] if len(args) > 3 else None
            return action, scope, target_id, item

    async def _handle_blacklist_common(self, event: AstrMessageEvent, args: List[str], cmd_name: str, key_name: str):
        if not self._is_admin(event):
            yield event.plain_result("⛔ 仅管理员可使用此命令。")
            return
        if not args:
            yield event.plain_result(
                f"用法:\n"
                f"  /jm {cmd_name} add|remove global <目标>\n"
                f"  /jm {cmd_name} add|remove group|user <群号/QQ号> <目标>\n"
                f"  /jm {cmd_name} list global\n"
                f"  /jm {cmd_name} list group|user <群号/QQ号>\n"
                f"  /jm {cmd_name} remove_all global confirm\n"
                f"  /jm {cmd_name} remove_all group|user <群号/QQ号> confirm"
            )
            return

        action, scope, target_id, item = self._parse_blacklist_cmd(args)
        if not scope:
            yield event.plain_result("❌ 必须指定作用域：global, group, 或 user。")
            return

        scope_disp = "全局" if scope == "global" else (f"群聊 {target_id}" if scope == "group" else f"用户 {target_id}")

        if action == "list":
            blacked = self._get_blacklist_items(key_name, scope, target_id)
            if not blacked:
                yield event.plain_result(f"📭 {scope_disp} 暂无限制。")
            else:
                yield event.plain_result(f"🚫 {scope_disp} 已拉黑 ({len(blacked)}):\n" + ", ".join(blacked))
        elif action in ["add", "remove"]:
            if not item:
                yield event.plain_result("❌ 缺少目标内容。")
                return
            ok = self._update_blacklist(key_name, scope, target_id, item, action)
            act = "添加拉黑" if action == "add" else "移除拉黑"
            yield event.plain_result(f"✅ {scope_disp} 已{act}: {item}。" if ok else "❌ 操作失败。")
        elif action == "remove_all":
            if item != "confirm":
                yield event.plain_result(f"⚠️ 危险操作！清空 {scope_disp} 的所有限制，请在末尾加上 confirm 以确认。\n例如: remove_all {scope} {target_id + ' ' if target_id else ''}confirm")
                return
            ok = self._update_blacklist(key_name, scope, target_id, None, "remove_all")
            yield event.plain_result(f"✅ 已清空 {scope_disp} 的所有限制。" if ok else "❌ 操作失败。")
        else:
            yield event.plain_result(f"❌ 未知操作: {action}。支持: add / remove / list / remove_all")

    async def _handle_black_jm(
        self, event: AstrMessageEvent, args: List[str]
    ):
        """处理 /jm black_jm（管理员）。"""
        async for r in self._handle_blacklist_common(event, args, "black_jm", "blacklist_jm"):
            yield r

    # ══════════════════════════════════════════════════════════
    #  /jm black_tag — 标签黑名单
    # ══════════════════════════════════════════════════════════

    async def _handle_black_tag(
        self, event: AstrMessageEvent, args: List[str]
    ):
        """处理 /jm black_tag（管理员）。"""
        async for r in self._handle_blacklist_common(event, args, "black_tag", "blacklist_tag"):
            yield r

    # ══════════════════════════════════════════════════════════
    #  /jm list — 列出所有下载
    # ══════════════════════════════════════════════════════════

    async def _handle_list(self, event: AstrMessageEvent):
        """处理 /jm list 命令（所有人可用）。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])

        if not base_dir.exists():
            yield event.plain_result("📭 暂无已下载的漫画。")
            return

        meta = self._load_downloads(base_dir)
        albums = meta.get("albums", {})

        # 也扫描磁盘上存在的目录（元数据可能丢失）
        disk_ids = set()
        for item in base_dir.iterdir():
            if item.is_dir() and item.name.isdigit():
                pdf_file = item / f"{item.name}.pdf"
                if pdf_file.exists():
                    disk_ids.add(item.name)

        # 合并元数据和磁盘
        all_ids = set(albums.keys()) | disk_ids

        if not all_ids:
            yield event.plain_result("📭 暂无已下载的漫画。")
            return

        lines = [f"📚 已下载的漫画 ({len(all_ids)} 部):"]
        for aid in sorted(all_ids, key=lambda x: int(x) if x.isdigit() else 0):
            info = albums.get(aid, {})
            title = info.get("title", aid)
            pages = info.get("page_count", "?")
            size_bytes = info.get("pdf_size", 0)
            size_str = self._format_size(size_bytes) if size_bytes else "?"
            downloaded = info.get("downloaded_at", "")
            lines.append(
                f"  #{aid} 《{title}》 — {pages}页, {size_str}"
            )
            if downloaded:
                lines[-1] += f" ({downloaded})"

        result = "\n".join(lines)
        if len(result) > 4000:
            # 分段发送
            chunks = [result[i:i + 4000] for i in range(0, len(result), 4000)]
            for chunk in chunks:
                yield event.plain_result(chunk)
        else:
            yield event.plain_result(result)

    # ══════════════════════════════════════════════════════════
    #  /jm delete — 删除
    # ══════════════════════════════════════════════════════════

    async def _handle_delete(
        self, event: AstrMessageEvent, delete_args: List[str]
    ):
        """处理 /jm delete 命令。"""
        # 权限检查
        if not self._is_admin(event):
            yield event.plain_result("⛔ 仅管理员可使用删除命令。")
            return

        if not delete_args:
            yield event.plain_result(
                "用法:\n"
                "  /jm delete <编号>    删除指定漫画\n"
                "  /jm delete all        删除全部（需二次确认）"
            )
            return

        target = delete_args[0].lower()

        if target == "all":
            async for result in self._handle_delete_all(event):
                yield result
        elif target.isdigit():
            async for result in self._handle_delete_one(event, target):
                yield result
        else:
            yield event.plain_result(
                f"❌ 无效参数: {target}\n"
                f"用法: /jm delete <编号> 或 /jm delete all"
            )

    async def _handle_delete_one(
        self, event: AstrMessageEvent, album_id: str
    ):
        """删除指定编号的漫画。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        album_dir = base_dir / album_id

        if not album_dir.exists():
            yield event.plain_result(f"❌ 未找到漫画 #{album_id} 的下载文件。")
            return

        try:
            shutil.rmtree(album_dir)
            # 更新元数据
            meta = self._load_downloads(base_dir)
            removed = meta["albums"].pop(album_id, None)
            self._save_downloads(base_dir, meta)

            title = removed.get("title", album_id) if removed else album_id
            yield event.plain_result(f"✅ 已删除漫画 #{album_id}《{title}》")
            logger.info(
                f"[JMDownloader] 管理员删除了漫画 #{album_id}"
            )
        except OSError as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    async def _handle_delete_all(self, event: AstrMessageEvent):
        """删除全部漫画（二次确认机制）。"""
        sender_id, _ = await self._get_sender_info(event)
        uid = str(sender_id) if sender_id else "unknown"
        now = time.time()

        # 检查是否有待确认的请求（60 秒内有效）
        pending_key = f"delete_all_{uid}"
        if pending_key in self._pending_confirm:
            if now - self._pending_confirm[pending_key] < 60:
                # 确认删除
                del self._pending_confirm[pending_key]
                async for result in self._do_delete_all(event):
                    yield result
                return
            else:
                del self._pending_confirm[pending_key]

        # 首次请求，记录并提示二次确认
        self._pending_confirm[pending_key] = now
        yield event.plain_result(
            "⚠️ 确认删除所有已下载的漫画吗？\n"
            "请在 60 秒内再次输入 /jm delete all 确认操作。\n"
            "输入其他任意命令可取消。"
        )

    async def _do_delete_all(self, event: AstrMessageEvent):
        """实际执行全部删除。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])

        if not base_dir.exists():
            yield event.plain_result("📭 没有可删除的漫画。")
            return

        deleted = 0
        errors = 0

        for item in list(base_dir.iterdir()):
            if item.is_dir() and item.name.isdigit():
                try:
                    shutil.rmtree(item)
                    deleted += 1
                except OSError as e:
                    errors += 1
                    logger.error(
                        f"[JMDownloader] 删除 {item.name} 失败: {e}"
                    )

        # 清空元数据
        meta = self._load_downloads(base_dir)
        meta["albums"] = {}
        self._save_downloads(base_dir, meta)

        result = f"✅ 已删除 {deleted} 部漫画。"
        if errors:
            result += f"（{errors} 个删除失败）"
        yield event.plain_result(result)
        logger.info(
            f"[JMDownloader] 管理员执行了全部删除: {deleted} 成功, {errors} 失败"
        )
