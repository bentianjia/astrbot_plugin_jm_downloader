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
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jm_")
        self._pending_confirm: Dict[str, float] = {}  # delete all 二次确认

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

        base_dir = str(cfg.get("download_base_dir", "data/jm_downloads"))
        if not os.path.isabs(base_dir):
            base_dir = os.path.join(os.getcwd(), base_dir)

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
        """从 downloads.json 读取下载记录。"""
        path = base_dir / "downloads.json"
        if not path.exists():
            return {"albums": {}, "disabled_groups": [], "blacklist_jm": [], "blacklist": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"albums": {}, "disabled_groups": [], "blacklist_jm": [], "blacklist": []}
            data.setdefault("albums", {})
            data.setdefault("disabled_groups", [])
            data.setdefault("blacklist", [])
            data.setdefault("blacklist_jm", [])
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[JMDownloader] 无法读取 downloads.json: {e}")
            return {"albums": {}}

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

    def _is_jm_blacklisted(self, album_id: str) -> bool:
        """检查漫画编号是否被拉黑。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        return album_id in meta.get("blacklist_jm", [])

    def _set_blacklisted_jm(self, album_id: str, add: bool) -> bool:
        """添加/移除漫画编号黑名单。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        meta = self._load_downloads(base_dir)
        jm_list = meta.setdefault("blacklist_jm", [])
        if add:
            if album_id not in jm_list:
                jm_list.append(album_id)
        else:
            if album_id in jm_list:
                jm_list.remove(album_id)
        return self._save_downloads(base_dir, meta)

    def _get_blacklisted_jm(self) -> List[str]:
        """获取拉黑的漫画编号列表。"""
        cfg = self._get_config()
        base_dir = Path(cfg["download_base_dir"])
        meta = self._load_downloads(base_dir)
        return meta.get("blacklist_jm", [])

    # ══════════════════════════════════════════════════════════
    #  下载与 PDF 转换（同步，在线程池中执行）
    # ══════════════════════════════════════════════════════════

    def _download_and_convert(
        self,
        album_id: str,
        base_dir: Path,
        pdf_quality: int,
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

        orig_cwd = os.getcwd()
        tmp_dir = base_dir / f"_tmp_{album_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        title = album_id

        try:
            # ── 步骤 1: 写极简 YAML 指定下载目录（避免 os.chdir 多线程竞态） ──
            import yaml as _yaml_lib
            _yml = tmp_dir / "_opt.yml"
            _yml.write_text(
                _yaml_lib.dump({"dir_rule": {"base_dir": str(tmp_dir)}},
                               allow_unicode=True),
                encoding="utf-8",
            )
            option = jmcomic.create_option_by_file(str(_yml))
            jmcomic.download_album(album_id, option)

            # ── 步骤 2: 平铺所有图片到目标目录 ──
            target_dir = base_dir / album_id
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

            subdirs = sorted([
                d for d in tmp_dir.iterdir()
                if d.is_dir() and d.name != "__pycache__"
            ])
            if subdirs:
                title = subdirs[0].name

            total_moved = 0
            seen_names = set()
            for sd in subdirs:
                for f in sorted(sd.iterdir()):
                    if f.is_file():
                        dest_name = f.name
                        if dest_name in seen_names:
                            dest_name = f"{sd.name}_{f.name}"
                        seen_names.add(dest_name)
                        shutil.move(str(f), str(target_dir / dest_name))
                        total_moved += 1
                shutil.rmtree(sd)

            for f in sorted(tmp_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png"):
                    shutil.move(str(f), str(target_dir / f.name))
                    total_moved += 1

            logger.info(
                f"[JMDownloader] #{album_id} 下载完成，"
                f"标题: {title}, 移动 {total_moved} 个文件"
            )

            if total_moved == 0:
                return {
                    "success": False,
                    "error": "下载后未找到任何图片文件",
                    "title": title,
                    "album_id": album_id,
                }

            # ── 步骤 3: 扫描图片去重 ──
            raw_files: List[Path] = []
            for ext in (".webp", ".jpg", ".jpeg", ".png"):
                raw_files.extend(target_dir.rglob(f"*{ext}"))
                raw_files.extend(target_dir.rglob(f"*{ext.upper()}"))
            image_files = list(dict.fromkeys(raw_files))
            if not image_files:
                return {
                    "success": False,
                    "error": "下载完成但未找到任何图片",
                    "title": title,
                    "album_id": album_id,
                }

            def _sort_key(p: Path) -> tuple:
                nums = re.findall(r"\d+", p.stem)
                return tuple(int(n) for n in nums) if nums else (0, p.stem)

            image_files = sorted(image_files, key=_sort_key)
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
            os.chdir(orig_cwd)
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
        pdf_path: str,
        album_id: str,
        title: str,
    ) -> bool:
        """上传 PDF。群聊用 upload_group_file，私聊用 upload_private_file。"""
        bot = await self._get_bot(event)
        if not bot:
            return False

        display_name = f"JM{album_id}_{self._sanitize_filename(title, 20)}.pdf"

        try:
            if group_id:
                resp = await bot.call_action(
                    "upload_group_file",
                    group_id=group_id,
                    file=pdf_path,
                    name=display_name,
                )
            else:
                resp = await bot.call_action(
                    "upload_private_file",
                    user_id=sender_id,
                    file=pdf_path,
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
        if not args:
            yield event.plain_result(
                "📚 JMComic 漫画下载器\n"
                "用法:\n"
                "  /jm <编号>        下载漫画并上传 PDF\n"
                "  /jm list           列出已下载的漫画\n"
                "  /jm delete <编号>  删除指定漫画（管理员）\n"
                "  /jm delete all     删除全部漫画（管理员）\n"
                "  /jm group off <群号> 对该群禁用（管理员）\n"
                "  /jm group on <群号>  对该群恢复（管理员）\n"
                "  /jm black add <QQ号> 拉黑用户（管理员）\n"
                "  /jm black remove <QQ号> 移除拉黑（管理员）\n"
                "  /jm black list    查看黑名单（管理员）\n"
                "  /jm black_jm add <编号> 拉黑漫画（管理员）\n"
                "  /jm black_jm remove <编号> 移除漫画拉黑（管理员）\n"
                "  /jm black_jm list  查看已拉黑漫画（管理员）"
            )
            return

        sub = args[0].lower()

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
        elif sub.isdigit():
            async for result in self._handle_download(event, sub):
                yield result
        else:
            yield event.plain_result(
                f"❌ 未知子命令: {sub}\n"
                f"请输入 /jm 查看帮助。"
            )

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
        if self._is_jm_blacklisted(album_id):
            yield event.plain_result(
                f"⛔ 漫画 #{album_id} 已被管理员禁止下载。"
            )
            return

        base_dir = Path(cfg["download_base_dir"])
        base_dir.mkdir(parents=True, exist_ok=True)
        album_dir = base_dir / album_id
        pdf_path = album_dir / f"{album_id}.pdf"

        # ── 已下载的漫画直接发送 ──
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
                    f"文件路径: {pdf_path}\n"
                    f"[CQ:at,qq={sender_id}] 请联系管理员手动上传"
                )
            return

        # ── 并发检查 ──
        if self._lock.locked():
            yield event.plain_result(
                "⏳ 已有下载任务在进行中，请稍后重试。"
            )
            return

        # ── 静默下载+转PDF+上传 ──
        loop = asyncio.get_event_loop()

        async with self._lock:
            try:
                result = await loop.run_in_executor(
                    self._executor,
                    self._download_and_convert,
                    album_id,
                    base_dir,
                    cfg["pdf_quality"],
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

        # ── 上传 → 仅一条最终消息 ──
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
                f"📁 文件: {pdf_path_str}\n"
                f"[CQ:at,qq={sender_id}] 请联系管理员手动上传"
            )

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

    async def _handle_black_jm(
        self, event: AstrMessageEvent, args: List[str]
    ):
        """处理 /jm black_jm add|remove|list（管理员）。"""
        if not self._is_admin(event):
            yield event.plain_result("⛔ 仅管理员可使用此命令。")
            return
        if not args:
            yield event.plain_result(
                "用法:\n"
                "  /jm black_jm add <编号>     拉黑漫画\n"
                "  /jm black_jm remove <编号>  移除拉黑\n"
                "  /jm black_jm list           查看已拉黑漫画"
            )
            return
        action = args[0].lower()
        if action == "list":
            blacked = self._get_blacklisted_jm()
            if not blacked:
                yield event.plain_result("📭 没有拉黑的漫画。")
            else:
                yield event.plain_result(
                    f"🚫 已拉黑漫画 ({len(blacked)}):\n" + ", ".join(blacked)
                )
        elif action in ("add", "remove"):
            if len(args) < 2:
                yield event.plain_result(f"用法: /jm black_jm {action} <编号>")
                return
            target = args[1]
            if not target.isdigit():
                yield event.plain_result("❌ 编号必须是数字。")
                return
            add = (action == "add")
            ok = self._set_blacklisted_jm(target, add)
            act = "拉黑" if add else "移除拉黑"
            yield event.plain_result(
                f"✅ 已{act}漫画 #{target}。" if ok else "❌ 操作失败。"
            )
        else:
            yield event.plain_result(
                f"❌ 未知操作: {action}。支持: add / remove / list"
            )

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
