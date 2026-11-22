"""串流版本更新监控插件（Foundation Sunshine / Moonlight V+ Android / Moonlight V+ PC）。

配置分为三组：
- global：全局设定（启用、推送群、间隔、摘要等）
- notify：发布通知开关（总开关 + 各项目开关，Sunshine 分正式版/预发布版）
- upload：文件上传设置（总开关 + 各项目开关、群文件夹名称、清理策略等）

轮询本身不调用大模型；只有检测到新版本时才调用一次摘要。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

PLUGIN_NAME = "astrbot_plugin_stream_release"
PLUGIN_VERSION = "v1.7.1"

REPOS: list[dict[str, Any]] = [
    {
        "repo": "AlkaidLab/foundation-sunshine",
        "label": "基地版 Foundation Sunshine",
        "notify_stable_key": "sunshine_stable",
        "notify_prerelease_key": "sunshine_prerelease",
        "upload_stable_key": "sunshine_stable",
        "upload_prerelease_key": "sunshine_prerelease",
        "folder_name_key": "sunshine_folder_name",
        "folder_id_key": "sunshine_folder_id",
        "asset_regex": r"WindowsInstaller\.exe$",
        "cleanup_regex": r"^sunshine.*\.(exe|zip)$",
        "rename": lambda tag, asset, ver: f"【稳定版】基地版Sunshine_{ver}_Win安装包.exe",
    },
    {
        "repo": "qiin2333/moonlight-vplus",
        "label": "Moonlight V+",
        "notify_stable_key": "moonlight_vplus",
        "notify_prerelease_key": "moonlight_vplus",
        "upload_stable_key": "moonlight_vplus",
        "upload_prerelease_key": "moonlight_vplus",
        "folder_name_key": "moonlight_android_folder_name",
        "folder_id_key": "moonlight_android_folder_id",
        "asset_regex": r"\.apk$",
        "cleanup_regex": r"^moonlight.*\.(apk|zip)$|^moonlight.*\.apk\.1$",
        "rename": lambda tag, asset, ver: re.sub(r"\.apk$", ".APK", asset, flags=re.IGNORECASE),
    },
    {
        "repo": "qiin2333/moonlight-qt",
        "label": "Moonlight V+ PC",
        "notify_stable_key": "moonlight_pc",
        "notify_prerelease_key": "moonlight_pc",
        "upload_stable_key": "moonlight_pc",
        "upload_prerelease_key": "moonlight_pc",
        "folder_name_key": "moonlight_pc_folder_name",
        "folder_id_key": "moonlight_pc_folder_id",
        "asset_regex": r"Moonlight-VPlus-Setup-[\d.]+\.exe$",
        "cleanup_regex": r"^moonlight.*\.(exe|zip|dmg|appimage)$",
        "rename": lambda tag, asset, ver: f"MoonlightV+PC_{ver}_Win安装包.exe",
    },
]

DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_PLATFORM_ID = ""
DEFAULT_SUMMARY_CHARS = 220
DEFAULT_INITIAL_DELAY = 20
DEFAULT_CLEANUP_KEEP = 2
SPACE_MARGIN_BYTES = 5 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024

PRERELEASE_NOTICE = (
    "⚠️ 这是预览/测试版（Pre-release）：可能不稳定、有 bug 或功能随时调整，"
    "不建议日常使用；想尝鲜或帮忙测试再装，遇到问题请带上版本号和日志反馈。"
)


class StreamReleaseMonitor(Star):
    """串流项目新版本监控 + 安装包搬运。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        # ---- 全局设定 ----
        self.enable: bool = self._cfg_bool("global", "enable", False)
        self.interval_minutes: int = max(1, self._cfg_int("global", "interval_minutes", DEFAULT_INTERVAL_MINUTES))
        self.platform_id: str = self._cfg_str("global", "platform_id", "") or self._detect_platform_id()
        self.include_prerelease: bool = self._cfg_bool("global", "include_prerelease", True)
        self.summary_max_chars: int = max(60, self._cfg_int("global", "summary_max_chars", DEFAULT_SUMMARY_CHARS))
        self.summary_provider_id: str = self._cfg_str("global", "summary_provider_id", "")
        self.dry_run: bool = self._cfg_bool("global", "dry_run", False)
        self.initial_delay: int = max(0, self._cfg_int("global", "initial_delay_seconds", DEFAULT_INITIAL_DELAY))

        # ---- 通知开关 ----
        self.notify_enabled: bool = self._cfg_bool("notify", "enabled", False)

        # ---- 文件上传设置 ----
        self.asset_upload_enabled: bool = self._cfg_bool("upload", "enabled", False)
        self.asset_dry_run: bool = self._cfg_bool("upload", "dry_run", False)
        self.cleanup_when_full: bool = self._cfg_bool("upload", "cleanup_when_full", True)
        self.cleanup_keep_count: int = max(1, self._cfg_int("upload", "cleanup_keep_count", DEFAULT_CLEANUP_KEEP))
        self.download_timeout: int = max(60, self._cfg_int("upload", "download_timeout_seconds", 600))
        self.asset_max_mb: int = max(1, self._cfg_int("upload", "asset_max_mb", 200))

        # ---- 群 / 文件夹 ----
        self.target_umos: list[str] = self._build_target_umos()
        self.upload_group: int = self._resolve_upload_group()
        self._folder_cache: dict[str, str] = {}

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.state_file: Path = self.data_dir / "state.json"
        self.log_file: Path = self.data_dir / "monitor.log"
        self.download_dir: Path = self.data_dir / "downloads"
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        self._register_web_apis()

    # ------------------------------------------------------------ 配置读取（分组 + 兼容旧扁平键）
    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        section = self.config.get(group, None)
        if isinstance(section, dict) and key in section:
            return section[key]
        return self.config.get(f"{group}.{key}", default)

    def _cfg_bool(self, group: str, key: str, default: bool) -> bool:
        value = self._cfg(group, key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _cfg_int(self, group: str, key: str, default: int) -> int:
        try:
            return int(self._cfg(group, key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, group: str, key: str, default: str = "") -> str:
        value = self._cfg(group, key, default)
        if value is None:
            return ""
        return str(value).strip()

    def _cfg_list(self, group: str, key: str, default: list[str] | None = None) -> list[str]:
        value = self._cfg(group, key, default or [])
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [v.strip() for v in value.split(",") if v.strip()]
        return []

    def _detect_platform_id(self) -> str:
        """没有显式配置平台 ID 时，自动找 aiocqhttp 平台，避免写死某个机器人的名字。"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if str(getattr(meta, "name", "")) == "aiocqhttp":
                    return str(getattr(meta, "id", "") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _build_target_umos(self) -> list[str]:
        groups = self._cfg_list("global", "target_groups", [])
        umos: list[str] = []
        for text in groups:
            if ":" in text:
                umos.append(text)
            elif self.platform_id:
                umos.append(f"{self.platform_id}:GroupMessage:{text}")
        return umos

    def _resolve_upload_group(self) -> int:
        explicit = self._cfg_str("global", "upload_group_id")
        if explicit:
            try:
                return int(explicit)
            except ValueError:
                pass
        for umo in self.target_umos:
            try:
                session = MessageSession.from_str(umo)
                sid = str(session.session_id)
                if sid.isdigit():
                    return int(sid)
            except Exception:  # noqa: BLE001
                continue
        return 0

    def _notify_allowed(self, item: dict[str, Any], prerelease: bool) -> bool:
        if not self.notify_enabled:
            return False
        key = item["notify_prerelease_key"] if prerelease else item["notify_stable_key"]
        return self._cfg_bool("notify", str(key), True)

    def _upload_allowed(self, item: dict[str, Any], prerelease: bool) -> bool:
        if not self.asset_upload_enabled or not self.upload_group:
            return False
        if not self._cfg_str("upload", str(item["folder_name_key"])):
            return False
        key = item["upload_prerelease_key"] if prerelease else item["upload_stable_key"]
        return self._cfg_bool("upload", str(key), not prerelease)

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if not self.enable:
            self._log("插件已禁用（global.enable=false），不启动监控")
            return
        if not self.target_umos:
            self._log("没有配置推送群，监控只会记录状态、不会推送")
        mode = "正式版+预发布版" if self.include_prerelease else "仅正式版"
        self._log(
            f"插件启动 {PLUGIN_VERSION}：{mode}，间隔 {self.interval_minutes} 分钟，"
            f"推送群 {self.target_umos}，上传群 {self.upload_group}，"
            f"通知总开关={'开' if self.notify_enabled else '关'}，"
            f"文件上传总开关={'开' if self.asset_upload_enabled else '关'}，"
            f"dry_run={self.dry_run}"
        )
        self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._log("插件已停止")

    async def _loop(self) -> None:
        if self.initial_delay:
            await asyncio.sleep(self.initial_delay)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(f"检查出错: {exc}")
                logger.exception(f"[{PLUGIN_NAME}] 检查出错")
            await asyncio.sleep(self.interval_minutes * 60)

    # ------------------------------------------------------------ 核心检查
    async def check_once(self) -> list[str]:
        async with self._lock:
            state = self._load_state()
            repos_state: dict[str, str] = dict(state.get("repos") or {})
            mode_state = state.get("mode")
            mode_now = "all" if self.include_prerelease else "stable"
            baseline_mode_change = mode_state != mode_now
            report: list[str] = []
            changed = False

            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "astrbot-stream-release-monitor/1.3"},
            ) as client:
                for item in REPOS:
                    repo, label = item["repo"], item["label"]
                    try:
                        if self.include_prerelease:
                            new_tag, lines = await self._check_all_releases(
                                client, item, repos_state.get(repo), baseline_mode_change
                            )
                        else:
                            new_tag, lines = await self._check_stable_only(
                                client, item, repos_state.get(repo)
                            )
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"{repo} 检查失败: {exc}")
                        report.append(f"{label}: 检查失败（{exc}）")
                        continue
                    report.extend(lines)
                    if new_tag and new_tag != repos_state.get(repo):
                        repos_state[repo] = new_tag
                        changed = True

            if baseline_mode_change:
                state["mode"] = mode_now
                changed = True
            if changed:
                state["repos"] = repos_state
            self._save_state(state, touch_check=True)
            return report

    async def _check_stable_only(
        self, client: httpx.AsyncClient, item: dict[str, Any], known: str | None
    ) -> tuple[str | None, list[str]]:
        repo, label = item["repo"], item["label"]
        resp = await client.get(f"https://github.com/{repo}/releases/latest")
        resp.raise_for_status()
        final_url = str(resp.url)
        marker = "/releases/tag/"
        if marker not in final_url:
            return None, [f"{label}: 未取到正式版标签"]
        encoded = final_url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0]
        tag = urllib.parse.unquote(encoded)
        url = f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}"
        if known is None:
            self._log(f"{repo} 首次记录基线: {tag}（不推送）")
            return tag, [f"{label}: 建立基线 {tag}"]
        if known == tag:
            return tag, [f"{label}: 无新版本（{tag}）"]
        published, notes = await self._release_notes(client, repo, tag)
        bullets = await self._summarize(label, tag, notes)
        base_text = self._format_message(label, tag, url, published, bullets, prerelease=False)
        asset_lines = await self._handle_asset(client, item, tag, url, prerelease=False)
        if self._notify_allowed(item, False):
            text = self._compose(base_text, asset_lines)
            await self._push(text, label, tag)
        else:
            self._log(f"{label} {tag} 通知开关关闭，只更新状态")
        return tag, [f"NEW {label} 正式版 {tag}"]

    async def _check_all_releases(
        self,
        client: httpx.AsyncClient,
        item: dict[str, Any],
        known: str | None,
        mode_changed: bool,
    ) -> tuple[str | None, list[str]]:
        repo, label = item["repo"], item["label"]
        entries = await self._atom_entries(client, repo)
        if not entries:
            return None, [f"{label}: 未取到版本条目"]
        newest = entries[0]["tag"]
        if known is None or mode_changed:
            self._log(f"{repo} 与预发布版一起跟踪，基线设为 {newest}（不补发历史版本）")
            return newest, [f"{label}: 建立基线 {newest}（正式版+预发布版）"]
        idx = next((i for i, e in enumerate(entries) if e["tag"] == known), None)
        if idx is None:
            self._log(f"{repo} 上次记录的 {known} 已不在 feed 前 10 条里，只推送最新一条")
            idx = 1
        new_entries = entries[:idx]
        if not new_entries:
            return newest, [f"{label}: 无新版本（{known}）"]
        last_ok = known
        lines: list[str] = []
        for entry in reversed(new_entries):
            tag = entry["tag"]
            prerelease = await self._is_prerelease(client, repo, tag)
            bullets = await self._summarize(label, tag, entry["body"])
            base_text = self._format_message(
                label, tag, entry["url"], entry["published"], bullets, prerelease=prerelease
            )
            asset_lines = await self._handle_asset(
                client, item, tag, entry["url"], prerelease=prerelease
            )
            if self._notify_allowed(item, prerelease):
                text = self._compose(base_text, asset_lines)
                if not await self._push(text, label, tag):
                    lines.append(f"{label}: 推送失败，保持旧基线（{last_ok}）")
                    return last_ok, lines
            else:
                self._log(f"{label} {tag} 通知开关关闭，只更新状态")
            last_ok = tag
            lines.append(f"NEW {label} {'预发布版' if prerelease else '正式版'} {tag}")
        return newest, lines

    @staticmethod
    def _compose(base_text: str, extra_lines: list[str]) -> str:
        if not extra_lines:
            return base_text
        return base_text + "\n\n" + "\n".join(extra_lines)

    # ------------------------------------------------------------ 安装包搬运
    async def _handle_asset(
        self,
        client: httpx.AsyncClient,
        item: dict[str, Any],
        tag: str,
        release_url: str,
        *,
        prerelease: bool,
    ) -> list[str]:
        if not self._upload_allowed(item, prerelease):
            return []
        repo, label = item["repo"], item["label"]
        folder_id, folder_name = await self._resolve_folder(item)
        if not folder_id:
            self._log(f"{repo} 未找到群文件夹（配置名：{self._cfg_str('upload', item['folder_name_key'])}），跳过安装包搬运")
            return [f"⚠️ 没有找到群文件夹「{self._cfg_str('upload', item['folder_name_key'])}」，安装包未上传"]
        try:
            assets = await self._release_assets(client, repo, tag)
            pattern = re.compile(item["asset_regex"], re.IGNORECASE)
            matched = [a for a in assets if pattern.search(a["name"])]
            if not matched:
                names = ", ".join(a["name"] for a in assets[:6]) or "（空）"
                self._log(f"{repo} {tag} 没有匹配 {item['asset_regex']} 的资产，资产：{names}")
                return ["⚠️ 该版本没有找到可上传的安装包（看看 Releases 页面）"]
            asset = matched[0]
            size = await self._remote_size(client, asset["url"])
            if size and size > self.asset_max_mb * 1024 * 1024:
                self._log(f"{repo} {tag} 资产 {asset['name']} 超过 {self.asset_max_mb}MB，跳过")
                return [f"⚠️ 安装包 {size / 1024 / 1024:.0f}MB，超过上限，未上传"]
            ver = self._version_of(repo, tag)
            final_name = item["rename"](tag, asset["name"], ver)
            self._log(
                f"{repo} {tag} 安装包：{asset['name']}（{size / 1024 / 1024:.1f}MB）→ {final_name}"
            )
            if self.asset_dry_run:
                dest = await self._download(client, asset, final_name)
                self._log(f"[asset dry-run] 已下载到 {dest}，不清理、不上传")
                return [f"🧪 dry-run：安装包已下载并改名为 {final_name}（未上传）"]

            lines: list[str] = []
            space = await self._ensure_space(size, folder_id, item["cleanup_regex"])
            if space.get("deleted"):
                names = "、".join(space["deleted"][:5])
                more = " 等" if len(space["deleted"]) > 5 else ""
                lines.append(
                    f"🧹 群文件空间不足，已清理 {len(space['deleted'])} 个旧安装包"
                    f"（{names}{more}，释放约 {space['freed'] / 1024 / 1024:.0f}MB）"
                )
            if not space.get("ok"):
                free_mb = space.get("free", 0) / 1024 / 1024
                need_mb = (size or 0) / 1024 / 1024
                self._log(f"{repo} {tag} 空间不足（需 {need_mb:.0f}MB，剩余 {free_mb:.0f}MB），跳过上传")
                lines.append(
                    f"❗ 群文件空间不足，安装包未上传（需要约 {need_mb:.0f}MB，当前剩余约 {free_mb:.0f}MB）"
                )
                lines.append("请管理员清理群文件后再让我重试，或直接点上面的 GitHub 链接下载。")
                return lines

            dest = await self._download(client, asset, final_name)
            file_uri = "file://" + str(dest)
            try:
                resp = await self._group_api(
                    "upload_group_file",
                    group_id=self.upload_group,
                    file=file_uri,
                    name=final_name,
                    folder_id=folder_id,
                    upload_file=True,
                )
                file_id = (resp or {}).get("file_id") if isinstance(resp, dict) else None
                self._log(f"{repo} {tag} 安装包已上传：{final_name}（file_id={file_id}）")
                lines.append(f"📦 安装包已上传群文件「{folder_name}」：{final_name}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"{repo} {tag} 安装包上传失败: {exc}")
                lines.append(f"⚠️ 安装包上传失败：{exc}")
            finally:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            return lines
        except Exception as exc:  # noqa: BLE001
            self._log(f"{repo} {tag} 安装包处理失败: {exc}")
            return [f"⚠️ 安装包处理失败：{exc}"]

    async def _resolve_folder(self, item: dict[str, Any]) -> tuple[str, str]:
        """按配置的文件夹名称解析 folder_id。

        优先现场查询群文件列表；解析成功会缓存到 state.json（按群隔离），
        这样换个群/换个部署也不用写死 ID。可选的 fallback id 只在查询失败时兜底。
        """
        name = self._cfg_str("upload", item["folder_name_key"])
        fallback_id = self._cfg_str("upload", item["folder_id_key"])
        if not name:
            return "", ""
        group_key = str(self.upload_group)
        state = self._load_state()
        cached = str(((state.get("folders") or {}).get(group_key) or {}).get(name) or "")
        try:
            data = await self._group_api("get_group_root_files", group_id=self.upload_group)
            folders = (data or {}).get("folders") if isinstance(data, dict) else []
            folders = folders or []
            exact = next((f for f in folders if str(f.get("folder_name")) == name), None)
            if exact is None and name:
                exact = next((f for f in folders if name in str(f.get("folder_name"))), None)
            if exact is not None:
                fid = str(exact.get("folder_id") or "")
                fname = str(exact.get("folder_name") or name)
                if fid:
                    self._folder_cache[name] = fid
                    state.setdefault("folders", {}).setdefault(group_key, {})[name] = fid
                    self._save_state(state)
                    return fid, fname
            self._log(f"群文件夹「{name}」未找到（当前群 {group_key}）")
        except Exception as exc:  # noqa: BLE001
            self._log(f"解析群文件夹失败（{name}）：{exc}")
        if cached:
            self._log(f"使用上次缓存到的文件夹 id：{cached}")
            return cached, name
        if fallback_id:
            self._log(f"使用配置的 fallback id：{fallback_id}")
            return fallback_id, name
        return "", name

    @staticmethod
    def _version_of(repo: str, tag: str) -> str:
        if repo == "AlkaidLab/foundation-sunshine":
            m = re.match(r"^(v?\d{4}\.\d{4})", tag)
            return m.group(1) if m else tag
        return tag

    async def _release_assets(
        self, client: httpx.AsyncClient, repo: str, tag: str
    ) -> list[dict[str, str]]:
        url = f"https://github.com/{repo}/releases/expanded_assets/{urllib.parse.quote(tag, safe='')}"
        resp = await client.get(url)
        resp.raise_for_status()
        assets: list[dict[str, str]] = []
        seen: set[str] = set()
        for m in re.finditer(r'href="(/[^"]*?/releases/download/[^"]+)"', resp.text):
            href = html_lib.unescape(m.group(1))
            name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
            if not name or name in seen:
                continue
            seen.add(name)
            assets.append({"name": name, "url": "https://github.com" + href})
        return assets

    async def _remote_size(self, client: httpx.AsyncClient, url: str) -> int:
        try:
            resp = await client.get(url, headers={"Range": "bytes=0-0"})
            if resp.status_code >= 400:
                return 0
            content_range = resp.headers.get("Content-Range", "")
            if content_range and "/" in content_range:
                return int(content_range.rsplit("/", 1)[1])
            return int(resp.headers.get("Content-Length") or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def _download(self, client: httpx.AsyncClient, asset: dict[str, str], final_name: str) -> Path:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        dest = self.download_dir / final_name
        part = dest.with_suffix(dest.suffix + ".part")
        timeout = httpx.Timeout(connect=30.0, read=float(self.download_timeout), write=60.0, pool=30.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "astrbot-stream-release-monitor/1.3"},
        ) as dl:
            async with dl.stream("GET", asset["url"]) as resp:
                resp.raise_for_status()
                expected = int(resp.headers.get("Content-Length") or 0)
                got = 0
                with part.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK):
                        fh.write(chunk)
                        got += len(chunk)
        if expected and got != expected:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"下载大小不符：期望 {expected}，实际 {got}")
        part.replace(dest)
        dest.chmod(0o644)
        self._log(f"下载完成：{dest.name}（{got / 1024 / 1024:.1f}MB）")
        return dest

    # ------------------------------------------------------------ 群文件空间/清理
    async def _group_api(self, action: str, **params) -> Any:
        bot = self._get_bot()
        if bot is None:
            raise RuntimeError("未找到 aiocqhttp 平台实例，无法操作群文件")
        return await bot.call_action(action, **params)

    async def _file_system_info(self) -> dict[str, Any]:
        data = await self._group_api("get_group_file_system_info", group_id=self.upload_group)
        return data if isinstance(data, dict) else {}

    async def _ensure_space(
        self, need_bytes: int, folder_id: str, cleanup_regex: str
    ) -> dict[str, Any]:
        need = (need_bytes or 0) + SPACE_MARGIN_BYTES
        info = await self._file_system_info()
        total = int(info.get("total_space") or 0)
        used = int(info.get("used_space") or 0)
        free = max(0, total - used)
        result: dict[str, Any] = {"ok": free >= need, "free": free, "freed": 0, "deleted": []}
        if result["ok"] or not self.cleanup_when_full:
            return result

        files = await self._list_folder_files(folder_id)
        pattern = re.compile(cleanup_regex, re.IGNORECASE)
        candidates = [
            f for f in files
            if pattern.search(str(f.get("file_name") or "")) and int(f.get("file_size") or 0) > 0
        ]
        candidates.sort(key=lambda f: int(f.get("upload_time") or 0))
        keep_ids = {
            f.get("file_id")
            for f in sorted(candidates, key=lambda f: int(f.get("upload_time") or 0), reverse=True)[
                : self.cleanup_keep_count
            ]
        }
        to_delete = [f for f in candidates if f.get("file_id") not in keep_ids]
        self._log(
            f"群文件空间不足：需 {need / 1024 / 1024:.0f}MB，剩余 {free / 1024 / 1024:.0f}MB，"
            f"候选清理 {len(to_delete)} 个旧安装包（保留最新 {self.cleanup_keep_count} 个）"
        )
        for f in to_delete:
            if result["ok"]:
                break
            try:
                await self._group_api("delete_group_file", group_id=self.upload_group, file_id=f.get("file_id"))
                size = int(f.get("file_size") or 0)
                result["freed"] += size
                result["deleted"].append(str(f.get("file_name")))
                free += size
                result["ok"] = free >= need
            except Exception as exc:  # noqa: BLE001
                self._log(f"删除群文件失败 {f.get('file_name')}: {exc}")
        try:
            info = await self._file_system_info()
            total = int(info.get("total_space") or 0)
            used = int(info.get("used_space") or 0)
            result["free"] = max(0, total - used)
            result["ok"] = result["free"] >= need
        except Exception:  # noqa: BLE001
            pass
        return result

    async def _list_folder_files(self, folder_id: str) -> list[dict[str, Any]]:
        data = await self._group_api(
            "get_group_files_by_folder", group_id=self.upload_group, folder_id=folder_id
        )
        files = (data or {}).get("files") if isinstance(data, dict) else []
        return [f for f in (files or []) if isinstance(f, dict)]

    # ------------------------------------------------------------ GitHub 抓取
    async def _atom_entries(self, client: httpx.AsyncClient, repo: str) -> list[dict[str, str]]:
        resp = await client.get(f"https://github.com/{repo}/releases.atom")
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries: list[dict[str, str]] = []
        for entry in root.findall("a:entry", ns):
            link = entry.find("a:link", ns)
            href = link.attrib.get("href", "") if link is not None else ""
            if "/releases/tag/" not in href:
                continue
            tag = urllib.parse.unquote(href.split("/releases/tag/", 1)[1])
            entries.append(
                {
                    "tag": tag,
                    "url": href,
                    "published": entry.findtext("a:updated", default="", namespaces=ns) or "",
                    "body": self._strip_html(
                        entry.findtext("a:content", default="", namespaces=ns) or ""
                    )[:12000],
                }
            )
        return entries

    async def _release_notes(self, client: httpx.AsyncClient, repo: str, tag: str) -> tuple[str, str]:
        for entry in await self._atom_entries(client, repo):
            if entry["tag"] == tag:
                return entry["published"], entry["body"]
        return "", ""

    async def _is_prerelease(self, client: httpx.AsyncClient, repo: str, tag: str) -> bool:
        url = f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return "Pre-release" in resp.text
        except Exception as exc:  # noqa: BLE001
            self._log(f"{repo} {tag} 无法判断是否预发布（{exc}），按预发布处理")
            return True

    @staticmethod
    def _strip_html(raw: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(p|div|li|h\d)>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    # ------------------------------------------------------------ AI 摘要
    async def _summarize(self, label: str, tag: str, notes: str) -> list[str]:
        notes = (notes or "").strip()
        if not notes:
            return ["（该版本没有附带更新说明）"]
        provider = self._get_provider()
        excerpt = re.sub(r"\s+", " ", notes)[: self.summary_max_chars]
        if provider is None:
            return [f"（未找到可用模型，原文节选：{excerpt}）"]
        prompt = (
            f"项目：{label}\n版本：{tag}\n\n更新说明原文：\n{notes[:9000]}\n\n"
            f"要求：用简体中文输出 3-6 条短句，概括这次更新里对用户有意义的变化"
            f"（新功能、修复、设置变化、已知问题、版本要求）；纯 CI/构建/翻译/依赖类改动"
            f"合并成一条或不写；总字数不超过 {self.summary_max_chars} 字；"
            f"只输出要点，每行一条，以“- ”开头，不要标题、不要寒暄、不要编造原文没有的内容。"
        )
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt="你是串流软件版本更新摘要助手，输出简体中文要点。",
            session_id=uuid.uuid4().hex,
            persist=False,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        bullets: list[str] = []
        for line in text.splitlines():
            line = re.sub(r"^[-*•\d.、)\s]+", "", line.strip()).strip()
            if line and line not in {"-", "•"}:
                bullets.append(line)
        return bullets[:6] or [f"（摘要解析失败，原文节选：{excerpt}）"]

    def _get_provider(self):
        try:
            if self.summary_provider_id:
                provider = self.context.get_provider_by_id(self.summary_provider_id)
                if provider:
                    return provider
            return self.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            self._log(f"获取模型失败: {exc}")
            return None

    def _get_bot(self):
        try:
            platform = self.context.get_platform_inst(self.platform_id)
        except Exception:  # noqa: BLE001
            platform = None
        if platform is None:
            return None
        try:
            return platform.get_client()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 发送（多群）
    def _format_message(
        self,
        label: str,
        tag: str,
        url: str,
        published: str,
        bullets: list[str],
        *,
        prerelease: bool,
    ) -> str:
        kind = "预发布版" if prerelease else "正式版"
        lines = [f"【{label} {kind} {tag}】"]
        if prerelease:
            lines.append(PRERELEASE_NOTICE)
        if published:
            lines.append(f"发布时间：{published}")
        lines.append(f"GitHub：{url}")
        lines.append("更新摘要：")
        lines.extend(f"- {b}" for b in bullets)
        return "\n".join(lines)

    async def _push(self, text: str, label: str, tag: str) -> bool:
        if self.dry_run:
            self._log(f"[dry-run] 不推送 {label} {tag}，消息内容:\n{text}")
            return True
        if not self.target_umos:
            self._log(f"[{label} {tag}] 未配置推送群，跳过推送")
            return True
        ok_any = False
        for umo in self.target_umos:
            try:
                session = MessageSession.from_str(umo)
                ok = await self.context.send_message(session, MessageChain([Plain(text)]))
                if ok is False:
                    raise RuntimeError("没有找到匹配的平台实例")
                ok_any = True
                self._log(f"{label} {tag} 已推送到 {umo}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"{label} {tag} 推送到 {umo} 失败: {exc}")
        return ok_any

    # ------------------------------------------------------------ 状态/日志
    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"repos": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"repos": {}}
            data.setdefault("repos", {})
            return data
        except Exception:  # noqa: BLE001
            self._log("state.json 解析失败，按空状态处理")
            return {"repos": {}}

    def _save_state(self, state: dict[str, Any], *, touch_check: bool = False) -> None:
        if touch_check:
            state["last_check"] = datetime.now().isoformat(timespec="seconds")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _log(self, message: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        logger.info(f"[{PLUGIN_NAME}] {message}")
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ Web 页面 / API
    def _register_web_apis(self) -> None:
        apis = (
            ("settings", self.page_get_settings, ["GET"], "串流监控：读取设置"),
            ("settings", self.page_save_settings, ["POST"], "串流监控：保存设置"),
            ("check", self.page_check_now, ["POST"], "串流监控：立即检查"),
            ("log", self.page_log, ["GET"], "串流监控：最近日志"),
            ("groups", self.page_groups, ["GET"], "串流监控：机器人所在群列表"),
            ("folders", self.page_folders, ["GET"], "串流监控：群文件文件夹列表"),
            ("test/notify", self.page_test_notify, ["POST"], "串流监控：测试通知"),
            ("test/upload", self.page_test_upload, ["POST"], "串流监控：测试上传"),
        )
        for endpoint, handler, methods, desc in apis:
            try:
                self.context.register_web_api(f"/{PLUGIN_NAME}/{endpoint}", handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                self._log(f"注册 Web API 失败（{endpoint}）：{exc}")

    def _public_settings(self) -> dict:
        return {
            "global": {
                "enable": self.enable,
                "platform_id": self.platform_id,
                "target_groups": [self._group_number(u) or u for u in self.target_umos],
                "upload_group_id": self._cfg_str("global", "upload_group_id"),
                "interval_minutes": self.interval_minutes,
                "include_prerelease": self.include_prerelease,
                "summary_max_chars": self.summary_max_chars,
                "summary_provider_id": self.summary_provider_id,
                "dry_run": self.dry_run,
                "initial_delay_seconds": self.initial_delay,
            },
            "notify": {
                "enabled": self.notify_enabled,
                "sunshine_stable": self._cfg_bool("notify", "sunshine_stable", True),
                "sunshine_prerelease": self._cfg_bool("notify", "sunshine_prerelease", True),
                "moonlight_vplus": self._cfg_bool("notify", "moonlight_vplus", True),
                "moonlight_pc": self._cfg_bool("notify", "moonlight_pc", True),
            },
            "upload": {
                "enabled": self.asset_upload_enabled,
                "dry_run": self.asset_dry_run,
                "sunshine_stable": self._cfg_bool("upload", "sunshine_stable", True),
                "sunshine_prerelease": self._cfg_bool("upload", "sunshine_prerelease", False),
                "moonlight_vplus": self._cfg_bool("upload", "moonlight_vplus", True),
                "moonlight_pc": self._cfg_bool("upload", "moonlight_pc", True),
                "sunshine_folder_name": self._cfg_str("upload", "sunshine_folder_name"),
                "moonlight_android_folder_name": self._cfg_str("upload", "moonlight_android_folder_name"),
                "moonlight_pc_folder_name": self._cfg_str("upload", "moonlight_pc_folder_name"),
                "sunshine_folder_id": self._cfg_str("upload", "sunshine_folder_id"),
                "moonlight_android_folder_id": self._cfg_str("upload", "moonlight_android_folder_id"),
                "moonlight_pc_folder_id": self._cfg_str("upload", "moonlight_pc_folder_id"),
                "cleanup_when_full": self.cleanup_when_full,
                "cleanup_keep_count": self.cleanup_keep_count,
                "download_timeout_seconds": self.download_timeout,
                "asset_max_mb": self.asset_max_mb,
            },
            "runtime": {
                "version": PLUGIN_VERSION,
                "running": bool(self._task and not self._task.done()),
                "upload_group": self.upload_group,
                "target_umos": self.target_umos,
                "state": self._load_state(),
            },
        }

    @staticmethod
    def _group_number(umo: str) -> str:
        try:
            session = MessageSession.from_str(umo)
            sid = str(session.session_id)
            return sid if sid.isdigit() else ""
        except Exception:  # noqa: BLE001
            return ""

    async def page_get_settings(self):
        try:
            return json_response(self._public_settings())
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读取设置失败：{exc}")

    async def page_save_settings(self):
        try:
            payload = await request.json(default={})
        except Exception:  # noqa: BLE001
            return error_response("请求格式错误")
        if not isinstance(payload, dict):
            return error_response("请求格式错误")

        try:
            new_conf = self._build_config_from_payload(payload)
            for section, values in new_conf.items():
                self.config[section] = values
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            self._apply_runtime()
            return json_response({"saved": True, "settings": self._public_settings()})
        except Exception as exc:  # noqa: BLE001
            self._log(f"保存设置失败: {exc}")
            return error_response(f"保存失败：{exc}")

    async def page_check_now(self):
        try:
            report = await self.check_once()
            return json_response({"report": report})
        except Exception as exc:  # noqa: BLE001
            return error_response(f"检查失败：{exc}")

    async def page_groups(self):
        try:
            bot = self._get_bot()
            if bot is None:
                return error_response("没有找到 aiocqhttp 平台实例，无法获取群列表")
            data = await bot.call_action("get_group_list")
            groups = [
                {
                    "group_id": str(g.get("group_id") or ""),
                    "group_name": str(g.get("group_name") or ""),
                }
                for g in (data or [])
                if isinstance(g, dict) and g.get("group_id")
            ]
            return json_response({"groups": groups, "platform_id": self.platform_id})
        except Exception as exc:  # noqa: BLE001
            return error_response(f"获取群列表失败：{exc}")

    async def page_folders(self):
        group_id = self.upload_group or self._resolve_upload_group()
        if not group_id:
            return error_response("还没有配置推送群/上传群，无法读取群文件")
        try:
            bot = self._get_bot()
            if bot is None:
                return error_response("没有找到 aiocqhttp 平台实例，无法读取群文件")
            data = await bot.call_action("get_group_root_files", group_id=group_id)
            folders = [
                {
                    "folder_id": str(f.get("folder_id") or ""),
                    "folder_name": str(f.get("folder_name") or ""),
                    "file_count": int(f.get("total_file_count") or 0),
                }
                for f in ((data or {}).get("folders") or [])
                if isinstance(f, dict) and f.get("folder_name")
            ]
            return json_response({"group_id": group_id, "folders": folders})
        except Exception as exc:  # noqa: BLE001
            return error_response(f"获取群文件夹失败：{exc}")

    # -------- 测试通知 / 测试上传 --------
    def _notify_targets(self) -> list[dict[str, Any]]:
        """按通知开关收集需要测试的（项目, 版本类型）。"""
        targets: list[dict[str, Any]] = []
        for item in REPOS:
            if item["repo"] == "AlkaidLab/foundation-sunshine":
                if self._cfg_bool("notify", "sunshine_stable", True):
                    targets.append({"item": item, "want": "stable", "kind": "正式版"})
                if self._cfg_bool("notify", "sunshine_prerelease", True):
                    targets.append({"item": item, "want": "prerelease", "kind": "预发布版"})
            elif item["repo"] == "qiin2333/moonlight-vplus":
                if self._cfg_bool("notify", "moonlight_vplus", True):
                    targets.append({"item": item, "want": "latest", "kind": "最新版"})
            else:
                if self._cfg_bool("notify", "moonlight_pc", True):
                    targets.append({"item": item, "want": "latest", "kind": "最新版"})
        return targets

    def _upload_targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for item in REPOS:
            folders = self._cfg_str("upload", str(item["folder_name_key"]))
            if item["repo"] == "AlkaidLab/foundation-sunshine":
                if self._cfg_bool("upload", "sunshine_stable", True) and folders:
                    targets.append({"item": item, "want": "stable", "kind": "正式版"})
                if self._cfg_bool("upload", "sunshine_prerelease", False) and folders:
                    targets.append({"item": item, "want": "prerelease", "kind": "预发布版"})
            elif item["repo"] == "qiin2333/moonlight-vplus":
                if self._cfg_bool("upload", "moonlight_vplus", True) and folders:
                    targets.append({"item": item, "want": "latest", "kind": "最新版"})
            else:
                if self._cfg_bool("upload", "moonlight_pc", True) and folders:
                    targets.append({"item": item, "want": "latest", "kind": "最新版"})
        return targets

    async def _pick_release(self, client: httpx.AsyncClient, repo: str, want: str) -> dict[str, str] | None:
        """按 stable / prerelease / latest 选一个版本来做测试。"""
        if want == "stable":
            tag = await self._latest_stable_tag(client, repo)
            if not tag:
                return None
            published, body = await self._release_notes(client, repo, tag)
            url = f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}"
            return {"tag": tag, "url": url, "published": published, "body": body, "prerelease": "0"}
        entries = await self._atom_entries(client, repo)
        if not entries:
            return None
        if want == "prerelease":
            for entry in entries[:5]:
                if await self._is_prerelease(client, repo, entry["tag"]):
                    return {**entry, "prerelease": "1"}
            return None
        entry = entries[0]
        prerelease = await self._is_prerelease(client, repo, entry["tag"])
        return {**entry, "prerelease": "1" if prerelease else "0"}

    async def _latest_stable_tag(self, client: httpx.AsyncClient, repo: str) -> str | None:
        resp = await client.get(f"https://github.com/{repo}/releases/latest")
        resp.raise_for_status()
        final_url = str(resp.url)
        marker = "/releases/tag/"
        if marker not in final_url:
            return None
        return urllib.parse.unquote(final_url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0])

    async def page_test_notify(self):
        if not self.notify_enabled:
            return error_response("发布通知总开关未开启，测试通知不会发送；请先打开通知总开关")
        targets = self._notify_targets()
        if not targets:
            return error_response("没有任何开启的通知版本（检查通知开关）")
        results: list[str] = []
        sent = 0
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
            headers={"User-Agent": "astrbot-stream-release-monitor/1.7"},
        ) as client:
            for target in targets:
                item = target["item"]
                label = item["label"]
                try:
                    release = await self._pick_release(client, item["repo"], target["want"])
                    if not release:
                        results.append(f"{label} {target['kind']}：没有找到可测试的版本，跳过")
                        continue
                    prerelease = release["prerelease"] == "1"
                    bullets = await self._summarize(label, release["tag"], release["body"])
                    base = self._format_message(
                        label, release["tag"], release["url"], release["published"], bullets,
                        prerelease=prerelease,
                    )
                    text = "【测试通知（不是新版本，仅验证通道）】\n" + base
                    ok = await self._push(text, label, release["tag"])
                    if ok:
                        sent += 1
                        results.append(f"{label} {target['kind']}：已测试推送 {release['tag']}")
                    else:
                        results.append(f"{label} {target['kind']}：推送失败（见日志）")
                except Exception as exc:  # noqa: BLE001
                    results.append(f"{label} {target['kind']}：出错 {exc}")
        return json_response({"sent": sent, "results": results})

    async def page_test_upload(self):
        if not self.asset_upload_enabled:
            return error_response("文件上传总开关未开启，测试上传不会执行；请先打开文件上传总开关")
        targets = self._upload_targets()
        if not targets:
            return error_response("没有任何开启的上传版本（检查上传开关和群文件夹名称）")
        results: list[str] = []
        uploaded = 0
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
            headers={"User-Agent": "astrbot-stream-release-monitor/1.7"},
        ) as client:
            for target in targets:
                item = target["item"]
                label = item["label"]
                try:
                    release = await self._pick_release(client, item["repo"], target["want"])
                    if not release:
                        results.append(f"{label} {target['kind']}：没有找到可上传的版本，跳过")
                        continue
                    lines = await self._handle_asset(
                        client, item, release["tag"], release["url"],
                        prerelease=release["prerelease"] == "1",
                    )
                    if any(str(line).startswith("📦") for line in lines):
                        uploaded += 1
                    results.extend(f"{label} {target['kind']}：{line}" for line in (lines or ["没有可上传的资产"]))
                except Exception as exc:  # noqa: BLE001
                    results.append(f"{label} {target['kind']}：出错 {exc}")
        return json_response({"uploaded": uploaded, "results": results})

    async def page_log(self):
        try:
            lines = []
            if self.log_file.exists():
                lines = self.log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
            return json_response({"lines": lines})
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读取日志失败：{exc}")

    def _build_config_from_payload(self, payload: dict) -> dict:
        """把页面提交的 JSON 合并成完整配置：没提交的字段保留当前值，避免误清空。"""

        def section(name: str) -> dict:
            value = payload.get(name)
            return value if isinstance(value, dict) else {}

        def pick(sec: dict, key: str, current: Any) -> Any:
            return sec[key] if key in sec else current

        def as_bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是"}
            return default

        def as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = default
            return max(minimum, min(maximum, number))

        def as_str(value: Any, default: str = "") -> str:
            return str(value).strip() if value is not None else default

        glob = section("global")
        notify = section("notify")
        upload = section("upload")

        current_groups = [self._group_number(u) or u for u in self.target_umos]
        raw_groups = glob.get("target_groups")
        groups = list(current_groups)
        if isinstance(raw_groups, list):
            parsed: list[str] = []
            for item in raw_groups:
                text = str(item).strip()
                if not text:
                    continue
                if ":" in text:
                    text = self._group_number(text) or ""
                if text:
                    parsed.append(text)
            if parsed:
                groups = parsed

        global_conf = {
            "enable": as_bool(pick(glob, "enable", self.enable), self.enable),
            "platform_id": as_str(pick(glob, "platform_id", self.platform_id), self.platform_id) or self.platform_id,
            "target_groups": groups,
            "upload_group_id": as_str(pick(glob, "upload_group_id", self._cfg_str("global", "upload_group_id"))),
            "interval_minutes": as_int(pick(glob, "interval_minutes", self.interval_minutes), self.interval_minutes, 1, 1440),
            "include_prerelease": as_bool(pick(glob, "include_prerelease", self.include_prerelease), self.include_prerelease),
            "summary_max_chars": as_int(pick(glob, "summary_max_chars", self.summary_max_chars), self.summary_max_chars, 60, 2000),
            "summary_provider_id": as_str(pick(glob, "summary_provider_id", self.summary_provider_id)),
            "dry_run": as_bool(pick(glob, "dry_run", self.dry_run), self.dry_run),
            "initial_delay_seconds": as_int(pick(glob, "initial_delay_seconds", self.initial_delay), self.initial_delay, 0, 600),
        }
        notify_conf = {
            "enabled": as_bool(pick(notify, "enabled", self.notify_enabled), self.notify_enabled),
            "sunshine_stable": as_bool(pick(notify, "sunshine_stable", self._cfg_bool("notify", "sunshine_stable", True)), True),
            "sunshine_prerelease": as_bool(pick(notify, "sunshine_prerelease", self._cfg_bool("notify", "sunshine_prerelease", True)), True),
            "moonlight_vplus": as_bool(pick(notify, "moonlight_vplus", self._cfg_bool("notify", "moonlight_vplus", True)), True),
            "moonlight_pc": as_bool(pick(notify, "moonlight_pc", self._cfg_bool("notify", "moonlight_pc", True)), True),
        }
        upload_conf = {
            "enabled": as_bool(pick(upload, "enabled", self.asset_upload_enabled), self.asset_upload_enabled),
            "dry_run": as_bool(pick(upload, "dry_run", self.asset_dry_run), self.asset_dry_run),
            "sunshine_stable": as_bool(pick(upload, "sunshine_stable", self._cfg_bool("upload", "sunshine_stable", True)), True),
            "sunshine_prerelease": as_bool(pick(upload, "sunshine_prerelease", self._cfg_bool("upload", "sunshine_prerelease", False)), False),
            "moonlight_vplus": as_bool(pick(upload, "moonlight_vplus", self._cfg_bool("upload", "moonlight_vplus", True)), True),
            "moonlight_pc": as_bool(pick(upload, "moonlight_pc", self._cfg_bool("upload", "moonlight_pc", True)), True),
            "sunshine_folder_name": as_str(pick(upload, "sunshine_folder_name", self._cfg_str("upload", "sunshine_folder_name"))),
            "moonlight_android_folder_name": as_str(pick(upload, "moonlight_android_folder_name", self._cfg_str("upload", "moonlight_android_folder_name"))),
            "moonlight_pc_folder_name": as_str(pick(upload, "moonlight_pc_folder_name", self._cfg_str("upload", "moonlight_pc_folder_name"))),
            "sunshine_folder_id": as_str(pick(upload, "sunshine_folder_id", self._cfg_str("upload", "sunshine_folder_id"))),
            "moonlight_android_folder_id": as_str(pick(upload, "moonlight_android_folder_id", self._cfg_str("upload", "moonlight_android_folder_id"))),
            "moonlight_pc_folder_id": as_str(pick(upload, "moonlight_pc_folder_id", self._cfg_str("upload", "moonlight_pc_folder_id"))),
            "cleanup_when_full": as_bool(pick(upload, "cleanup_when_full", self.cleanup_when_full), self.cleanup_when_full),
            "cleanup_keep_count": as_int(pick(upload, "cleanup_keep_count", self.cleanup_keep_count), self.cleanup_keep_count, 1, 20),
            "download_timeout_seconds": as_int(pick(upload, "download_timeout_seconds", self.download_timeout), self.download_timeout, 60, 3600),
            "asset_max_mb": as_int(pick(upload, "asset_max_mb", self.asset_max_mb), self.asset_max_mb, 1, 4096),
        }
        self._validate_upload_switches(upload_conf)
        return {"global": global_conf, "notify": notify_conf, "upload": upload_conf}

    @staticmethod
    def _validate_upload_switches(upload_conf: dict) -> None:
        """文件上传开关的限制：没设置对应群文件夹名称时不允许开启。"""
        folders = {
            "sunshine_stable": ("Sunshine 正式版", "sunshine_folder_name"),
            "sunshine_prerelease": ("Sunshine 预发布版", "sunshine_folder_name"),
            "moonlight_vplus": ("Moonlight V+（安卓）", "moonlight_android_folder_name"),
            "moonlight_pc": ("Moonlight V+ PC", "moonlight_pc_folder_name"),
        }
        for switch_key, (label, folder_key) in folders.items():
            if upload_conf.get(switch_key) and not upload_conf.get(folder_key):
                raise ValueError(f"「{label}」的群文件夹名称为空，请先填写文件夹名称再开启该上传开关")
        if upload_conf.get("enabled") and not any(
            upload_conf.get(folder_key)
            for _, folder_key in folders.values()
        ):
            raise ValueError("文件上传总开关开启前，请先至少填写一个群文件夹名称")

    def _apply_runtime(self) -> None:
        """保存配置后重新读取运行时参数，并按需启停监控循环。"""
        self.enable = self._cfg_bool("global", "enable", False)
        self.interval_minutes = max(1, self._cfg_int("global", "interval_minutes", DEFAULT_INTERVAL_MINUTES))
        self.platform_id = self._cfg_str("global", "platform_id", DEFAULT_PLATFORM_ID) or DEFAULT_PLATFORM_ID
        self.include_prerelease = self._cfg_bool("global", "include_prerelease", True)
        self.summary_max_chars = max(60, self._cfg_int("global", "summary_max_chars", DEFAULT_SUMMARY_CHARS))
        self.summary_provider_id = self._cfg_str("global", "summary_provider_id", "")
        self.dry_run = self._cfg_bool("global", "dry_run", False)
        self.initial_delay = max(0, self._cfg_int("global", "initial_delay_seconds", DEFAULT_INITIAL_DELAY))
        self.notify_enabled = self._cfg_bool("notify", "enabled", False)
        self.asset_upload_enabled = self._cfg_bool("upload", "enabled", False)
        self.asset_dry_run = self._cfg_bool("upload", "dry_run", False)
        self.cleanup_when_full = self._cfg_bool("upload", "cleanup_when_full", True)
        self.cleanup_keep_count = max(1, self._cfg_int("upload", "cleanup_keep_count", DEFAULT_CLEANUP_KEEP))
        self.download_timeout = max(60, self._cfg_int("upload", "download_timeout_seconds", 600))
        self.asset_max_mb = max(1, self._cfg_int("upload", "asset_max_mb", 200))
        self.target_umos = self._build_target_umos()
        self.upload_group = self._resolve_upload_group()
        self._folder_cache = {}

        running = bool(self._task and not self._task.done())
        if self.enable and not running:
            self._log("配置已启用监控，启动轮询")
            self._task = asyncio.create_task(self._loop())
        elif not self.enable and running:
            self._log("配置已关闭监控，停止轮询")
            self._task.cancel()
            self._task = None
        self._log("设置已保存并应用")

    # ------------------------------------------------------------ 指令
    @filter.command("releasecheck")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_releasecheck(self, event: AstrMessageEvent):
        """立即检查一次串流新版本（管理员）。"""
        try:
            report = await self.check_once()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"检查失败：{exc}")
            return
        yield event.plain_result("检查完成：\n" + "\n".join(report))

    @filter.command("releasestatus")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_releasestatus(self, event: AstrMessageEvent):
        """查看串流版本监控状态（管理员）。"""
        state = self._load_state()
        lines = [
            f"插件版本：{PLUGIN_VERSION}",
            f"监控间隔：{self.interval_minutes} 分钟",
            f"推送群：{self.target_umos or '(未配置)'}",
            f"上传群：{self.upload_group or '(未配置)'}",
            "监控范围：" + ("正式版 + 预发布版" if self.include_prerelease else "仅正式版"),
            f"通知总开关：{'开' if self.notify_enabled else '关'}",
            f"dry_run：{'是' if self.dry_run else '否'}",
            "通知开关：",
            f"  Sunshine 稳定版 {'开' if self._cfg_bool('notify', 'sunshine_stable', True) else '关'}"
            f" / 预发布版 {'开' if self._cfg_bool('notify', 'sunshine_prerelease', True) else '关'}",
            f"  Moonlight V+ {'开' if self._cfg_bool('notify', 'moonlight_vplus', True) else '关'}",
            f"  Moonlight V+ PC {'开' if self._cfg_bool('notify', 'moonlight_pc', True) else '关'}",
            f"文件上传总开关：{'开' if self.asset_upload_enabled else '关'}",
            "上传开关：",
            f"  Sunshine 稳定版 {'开' if self._cfg_bool('upload', 'sunshine_stable', True) else '关'}"
            f" / 预发布版 {'开' if self._cfg_bool('upload', 'sunshine_prerelease', False) else '关'}",
            f"  Moonlight V+ {'开' if self._cfg_bool('upload', 'moonlight_vplus', True) else '关'}",
            f"  Moonlight V+ PC {'开' if self._cfg_bool('upload', 'moonlight_pc', True) else '关'}",
            "群文件夹：",
            f"  Sunshine：{self._cfg_str('upload', 'sunshine_folder_name') or '(未配置)'}",
            f"  Moonlight V+：{self._cfg_str('upload', 'moonlight_android_folder_name') or '(未配置)'}",
            f"  Moonlight V+ PC：{self._cfg_str('upload', 'moonlight_pc_folder_name') or '(未配置)'}",
            f"上次检查：{state.get('last_check') or '还没检查过'}",
            "已记录版本：",
        ]
        for item in REPOS:
            lines.append(f"  {item['label']}：{state.get('repos', {}).get(item['repo']) or '未记录'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("releasesync")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_releasesync(self, event: AstrMessageEvent):
        """把当前版本设为基线，不推送（管理员）。"""
        state = self._load_state()
        repos_state = state.get("repos") or {}
        updated: list[str] = []
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "astrbot-stream-release-monitor/1.3"},
        ) as client:
            for item in REPOS:
                try:
                    entries = await self._atom_entries(client, item["repo"])
                    tag = entries[0]["tag"] if entries else None
                except Exception as exc:  # noqa: BLE001
                    updated.append(f"{item['label']}: 失败 {exc}")
                    continue
                if tag:
                    repos_state[item["repo"]] = tag
                    updated.append(f"{item['label']}: {tag}")
        state["repos"] = repos_state
        state["mode"] = "all" if self.include_prerelease else "stable"
        self._save_state(state, touch_check=True)
        yield event.plain_result("基线已更新：\n" + "\n".join(updated))
