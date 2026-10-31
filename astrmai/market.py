"""统一插件市场：一个入口搜/装三家的插件。

- astrbot：目录源优先 AstrBot 官方链；本机实测 `api.soulter.top` 301 到被重置的域名，
  于是回落到官方合集仓库 `AstrBot_Plugins_Collection`（走可达的 codeload）。
  插件本体用 api.github.com 解默认分支 + codeload 下 ZIP（github.com 的 archive/raw 在本机不通）。
- maibot：官方 REST 目录（实测可达，392 个插件）；安装正常走 git clone tag，本机 git 到 github
  不通，所以改成 codeload 按 tag/HEAD 取包，并补写麦麦的 `.maibot-release.json` 凭据文件。
- astrmai：原生插件，本地目录安装。

任何一步做不到就明确报 not-run 原因，不假装成功。
"""

import io
import json
import os
import re
import shutil
import tempfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

AB_REGISTRY_URLS = (
    "https://api.soulter.top/astrbot/plugins",
    "https://raw.githubusercontent.com/AstrBotDevs/AstrBot_Plugins_Collection/refs/heads/main/plugin_cache_original.json",
)
AB_COLLECTION = ("AstrBotDevs", "AstrBot_Plugins_Collection")
MB_REPO = ("Mai-with-u", "plugin-repo")
MB_BASE = "http://hyybuth.xyz:10059/api/v2/market"
UA = {"User-Agent": "astrmai-market/0.1"}
CACHE_TTL = 1800.0

SOURCES = ("astrbot", "maibot", "astrmai")


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _network_off() -> bool:
    """`ASTRMAI_MARKET_NETWORK=0`：整个市场的出网总闸，两条链路（AstrBot 与麦麦）一起关。"""

    return os.environ.get("ASTRMAI_MARKET_NETWORK", "1").strip().lower() in ("0", "off", "false")


def _require_network(what: str) -> None:
    if _network_off():
        raise RuntimeError(
            f"市场网络已被 ASTRMAI_MARKET_NETWORK=0 关闭，本次拒绝{what}"
            "（麦麦市场与遥测同域：市场 :10059 / 遥测 :10058；AstrBot 那条走 api.soulter.top 与 codeload.github.com）"
        )


def _codeload(owner: str, name: str, ref: str) -> str:
    return f"https://codeload.github.com/{owner}/{name}/zip/refs/{ref}"


def _split_repo(url: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+)", str(url or ""))
    if not m:
        return None
    return m.group(1), m.group(2).removesuffix(".git").strip("/")


class Market:
    def __init__(
        self,
        workspace: Path,
        *,
        astrbot_root: Path,
        maibot_root: Path,
        native_root: Path,
        maibot_version: str = "1.3.18",
    ) -> None:
        self.ws = Path(workspace)
        self.cache_dir = self.ws / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ab_plugins = Path(astrbot_root) / "data" / "plugins"
        self.mb_plugins = Path(maibot_root) / "plugins"
        self.native_plugins = Path(native_root)
        self.maibot_version = maibot_version

    # ---------------- 目录 ----------------

    def _cached(self, key: str) -> Any:
        path = self.cache_dir / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if _now() - float(data.get("at", 0)) > CACHE_TTL:
            return None
        return data.get("value")

    def _store(self, key: str, value: Any) -> None:
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps({"at": _now(), "value": value}, ensure_ascii=False), encoding="utf-8"
        )

    async def _fetch_ab_catalog(self, force: bool = False) -> tuple[list[dict], str]:
        # 开关在缓存之前判：说"彻底关闭"就不能让本地缓存当后门（AstrBot 这条以前漏在闸门外）
        _require_network("取 AstrBot 目录")
        cached = None if force else self._cached("ab_catalog")
        if cached is not None:
            return cached, "cache"

        notes: list[str] = []
        async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as client:
            for url in AB_REGISTRY_URLS:
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        entries = self._parse_ab_json(r.json())
                        self._store("ab_catalog", entries)
                        return entries, f"registry:{url.split('/')[2]}"
                    notes.append(f"{url.split('/')[2]} -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{url.split('/')[2]} -> {type(exc).__name__}")

            # 回落：官方合集仓库里就带着同一份目录，走本机可达的 codeload
            owner, name = AB_COLLECTION
            r = await client.get(_codeload(owner, name, "heads/main"), timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"目录全部不可达: {'; '.join(notes)}; 合集 -> {r.status_code}")
            entries = self._parse_ab_json(self._read_zip_json(r.content, "plugins.json"))
            self._store("ab_catalog", entries)
            return entries, f"collection-zip ({'; '.join(notes)})"

    @staticmethod
    def _read_zip_json(raw: bytes, wanted: str) -> dict:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for n in zf.namelist():
                if n.split("/")[-1] == wanted:
                    return json.loads(zf.read(n).decode("utf-8"))
        raise RuntimeError(f"合集包里没有 {wanted}")

    @staticmethod
    def _parse_ab_json(data: Any) -> list[dict]:
        out = []
        if isinstance(data, dict):
            items = data.items()
        else:  # 列表形态
            items = [(str(e.get("name", i)), e) for i, e in enumerate(data)]
        for key, entry in items:
            if not isinstance(entry, dict):
                continue
            # 目录根部的保留键（$meta 之类）不是插件
            if str(key).startswith("$") or str(entry.get("name", "")).startswith("$"):
                continue
            repo = entry.get("repo") or entry.get("repository") or ""
            if not repo or _split_repo(repo) is None:
                continue
            out.append(
                {
                    "source": "astrbot",
                    "id": str(entry.get("id") or key),
                    "name": str(entry.get("name") or key.split("/")[-1]),
                    "display_name": str(entry.get("display_name") or ""),
                    "author": str(entry.get("author") or ""),
                    "description": str(entry.get("desc") or entry.get("description") or ""),
                    "version": str(entry.get("version") or ""),
                    "repo": repo,
                    "download_url": entry.get("download_url") or "",
                    # AstrBot 官方目录根本不带 license 字段：明写 unknown，安装时才从包里读
                    "license": str(entry.get("license") or "") or "unknown",
                    "license_note": "" if entry.get("license") else "AstrBot 目录不提供许可证，安装时从包内 LICENSE/metadata/README 判定",
                }
            )
        return out

    async def _fetch_mb_index(self) -> dict[str, dict]:
        """麦麦官方 release 索引：manifest_id -> {tag, commit, license, 版本区间}。

        它的 `plugins` 是列表不是字典，且 raw.githubusercontent 在本机不通，
        所以同样走可达的 codeload 取整个索引仓库。
        """
        cached = self._cached("mb_index")
        if cached is not None:
            return cached
        _require_network("取麦麦 release 索引")

        owner, name = MB_REPO
        async with httpx.AsyncClient(timeout=120, headers=UA, follow_redirects=True) as client:
            r = await client.get(_codeload(owner, name, "heads/main"))
            if r.status_code != 200:
                raise RuntimeError(f"麦麦 release 索引不可达: HTTP {r.status_code}")
            raw = r.content

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            wanted = [n for n in zf.namelist() if n.split("/")[-1] == "plugin_versions.json"]
            if not wanted:
                raise RuntimeError("索引仓库里没有 plugin_versions.json")
            data = json.loads(zf.read(wanted[0]).decode("utf-8"))

        out: dict[str, dict] = {}
        for entry in data.get("plugins", []) or []:
            manifest_id = str(entry.get("manifest_id") or entry.get("id") or "")
            versions = [v for v in (entry.get("versions") or []) if not v.get("yanked")]
            if not manifest_id or not versions:
                continue
            latest = versions[0]
            manifest = latest.get("manifest") or {}
            host = manifest.get("host_application") or {}
            out[manifest_id.lower()] = {
                "repository": str(entry.get("repositoryUrl") or ""),
                "tag": str(latest.get("tag") or ""),
                "version": str(latest.get("version") or ""),
                "commit": str(latest.get("commit") or ""),
                "license": str(manifest.get("license") or ""),
                "host_min": str(host.get("min_version") or ""),
                "host_max": str(host.get("max_version") or ""),
                "capabilities": sorted(str(c) for c in (manifest.get("capabilities") or [])),
            }
        self._store("mb_index", out)
        return out

    async def _fetch_mb_catalog(self, query: str, force: bool = False) -> tuple[list[dict], str]:
        # 网络开关在缓存之前判：设成 0 就是"一个字都不出网"，不能让缓存变成后门
        _require_network("取麦麦目录")
        # 实测这个接口不理 limit：请求多少都整本返回（399 条一次给完）。
        # 所以整本缓存，翻页与筛选在本地切 —— 否则每翻一页都重新拖一遍全目录。
        cached = None if force else self._cached("mb_catalog")
        if cached is not None:
            return cached, "cache"
        params = {"limit": 500, "page": 1, "maibot_version": self.maibot_version}
        if query:
            params["q"] = query
        async with httpx.AsyncClient(timeout=30, headers=UA) as client:
            r = await client.get(f"{MB_BASE}/plugins/summary", params=params)
            if r.status_code != 200:
                raise RuntimeError(f"麦麦目录不可达: HTTP {r.status_code} {r.text[:120]}")
            data = r.json()
        out = []
        try:
            index = await self._fetch_mb_index()
        except Exception:  # noqa: BLE001 - 索引只是补充许可证/版本信息，拿不到不影响搜索
            index = {}
        for e in data.get("plugins", []):
            pid = str(e.get("id"))
            rel = index.get(pid.lower(), {})
            row = {
                "source": "maibot",
                "id": pid,
                "name": str(e.get("name") or pid),
                "author": str((e.get("author") or {}).get("name") or ""),
                "description": str(e.get("description") or ""),
                "version": str(rel.get("version") or e.get("latest_version") or e.get("version") or ""),
                "repo": str(e.get("repositoryUrl") or e.get("repository_url") or rel.get("repository") or ""),
                "plugin_type": str(e.get("plugin_type") or ""),
                "license": str(rel.get("license") or "").strip() or "unknown",
                "license_note": "" if rel.get("license") else "官方索引没给 license 字段，安装时从包内判定",
            }
            if rel.get("tag"):
                row["tag"] = rel["tag"]
            out.append(row)
        self._store("mb_catalog", out)
        return out, "maibot-official"

    def _installed_keys(self, source: str) -> set[str]:
        """把已装插件的目录名与标识归一化成小写键。市场页靠它标"已安装"，
        省掉"装了三次才发现按钮没反应"这种排查。"""

        keys: set[str] = set()
        try:
            rows = self.list_installed(source)
        except (OSError, ValueError):
            return keys
        for row in rows:
            for field in ("name", "id"):
                val = str(row.get(field) or "").strip().lower()
                if val:
                    keys.add(val)
            leaf = Path(str(row.get("path") or "")).name.strip().lower()
            if leaf:
                keys.add(leaf)
        return keys

    def _mark_installed(self, source: str, entries: list[dict]) -> None:
        installed = self._installed_keys(source)
        for entry in entries:
            candidates = {
                str(entry.get(k) or "").strip().lower()
                for k in ("id", "name", "display_name")
            }
            repo = _split_repo(str(entry.get("repo") or ""))
            if repo:
                candidates.add(repo[1].lower())
            candidates.discard("")
            entry["installed"] = bool(candidates & installed)

    async def search(self, source: str, query: str = "", limit: int = 20, offset: int = 0, force: bool = False) -> dict:
        limit = max(1, min(int(limit or 20), 50))
        offset = max(0, int(offset or 0))
        if source == "astrbot":
            entries, how = await self._fetch_ab_catalog(force)
        elif source == "maibot":
            entries, how = await self._fetch_mb_catalog(query, force)
        elif source == "astrmai":
            entries = self.list_installed("astrmai")
            how = "local"
        else:
            raise ValueError(f"未知来源: {source}")
        q = query.lower()
        hit = [e for e in entries if not q or q in json.dumps(e, ensure_ascii=False).lower()]
        self._mark_installed(source, hit)
        page = hit[offset : offset + limit]
        result = {
            "source": source,
            "catalog": how,
            "total": len(hit),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(hit),
            "plugins": page,
        }
        if source == "maibot":
            if how == "cache":
                # 不许把"这次没出网"说成"这次出网了"：命中缓存就照实讲
                result["stale"] = (
                    "这一页来自 30 分钟内的本地目录缓存，本次没有出网；"
                    "要重新拉，点「重新载入目录」（缓存过期后会自动重新出网）"
                )
            else:
                result["notice"] = (
                    "本次查询出网到 hyybuth.xyz:10059（麦麦官方市场），与麦麦遥测 hyybuth.xyz:10058 同域；"
                    "不需要时设 ASTRMAI_MARKET_NETWORK=0 可彻底关闭"
                )
        elif source == "astrbot" and how != "cache":
            result["notice"] = (
                f"本次查询出网到 {how}（AstrBot 官方目录/合集）；"
                "ASTRMAI_MARKET_NETWORK=0 会把这条与麦麦那条一起关掉"
            )
        return result

    # ---------------- 下载与安装 ----------------

    async def _resolve_branch(self, client: httpx.AsyncClient, owner: str, name: str) -> tuple[str, str]:
        """返回 (分支, 取证说明)。优先 API，退到常见分支名。"""
        try:
            r = await client.get(f"https://api.github.com/repos/{owner}/{name}", timeout=20)
            if r.status_code == 200:
                return str(r.json().get("default_branch") or "main"), "api"
        except Exception:  # noqa: BLE001
            pass
        return "HEAD", "guessed"

    async def _download_repo_zip(self, repo: str, ref_hint: str = "") -> tuple[bytes, str, dict]:
        _require_network("下载插件包")
        owner_name = _split_repo(repo)
        if not owner_name:
            raise RuntimeError(f"不是可识别的 GitHub 仓库地址: {repo}")
        owner, name = owner_name
        async with httpx.AsyncClient(timeout=180, headers=UA, follow_redirects=True) as client:
            branch, how = await self._resolve_branch(client, owner, name)
            ref = f"tags/{ref_hint}" if ref_hint else f"heads/{branch}"
            r = await client.get(_codeload(owner, name, ref))
            if r.status_code != 200 and ref_hint:
                ref = f"heads/{branch}"
                r = await client.get(_codeload(owner, name, ref))
            if r.status_code != 200:
                raise RuntimeError(f"codeload 下载失败: HTTP {r.status_code} ({owner}/{name} {ref})")
            return r.content, f"{owner}/{name}@{ref.split('/')[-1]} ({how})", {"owner": owner, "name": name}

    @staticmethod
    def _zip_entries(raw: bytes) -> tuple[str, list[str]]:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            roots = {n.split("/")[0] for n in names}
            prefix = sorted(roots)[0] + "/" if len(roots) == 1 and roots != {""} else ""
            return prefix, names

    async def install(self, source: str, ident: str, *, dest: Path | None = None) -> dict:
        if source == "astrbot":
            return await self._install_ab(ident, dest)
        if source == "maibot":
            return await self._install_mb(ident, dest)
        if source == "astrmai":
            return self._install_native(ident, dest)
        raise ValueError(f"未知来源: {source}")

    async def probe_license(self, source: str, ident: str) -> dict:
        """装之前先查一个市场条目的许可证：只下载包、不落地、不加载。"""

        from . import licensing

        if source == "astrbot":
            entries, _ = await self._fetch_ab_catalog()
            match = [e for e in entries if e["id"] == ident or e["name"] == ident]
            if not match:
                return {"ok": False, "reason": f"AstrBot 目录里找不到 {ident}"}
            entry = match[0]
            raw, where, _ = await self._download_repo_zip(entry["repo"], "")
            prefix, _names = self._zip_entries(raw)
            verdict = licensing.from_files(licensing.evidence_from_zip(raw, prefix))
            return {"ok": True, "source": source, "id": ident, "package": entry["repo"], "fetched": where, **verdict}

        if source == "maibot":
            index = await self._fetch_mb_index()
            rel = index.get(str(ident).lower()) or {}
            if rel.get("license"):
                return {
                    "ok": True,
                    "source": source,
                    "id": ident,
                    "package": str(rel.get("repository") or ""),
                    "license": str(rel["license"]),
                    "basis": "麦麦官方 release 索引的 license 字段",
                    "confidence": "high",
                }
            repo = str(rel.get("repository") or "")
            if not repo:
                return {"ok": False, "reason": f"索引里没有 {ident} 的许可证与仓库地址"}
            raw, where, _ = await self._download_repo_zip(repo, str(rel.get("tag") or ""))
            prefix, _names = self._zip_entries(raw)
            verdict = licensing.from_files(licensing.evidence_from_zip(raw, prefix))
            return {"ok": True, "source": source, "id": ident, "package": repo, "fetched": where, **verdict}

        return {"ok": False, "reason": f"未知来源: {source}"}

    async def _install_ab(self, ident: str, dest: Path | None) -> dict:
        entries, _ = await self._fetch_ab_catalog()
        match = [e for e in entries if e["id"] == ident or e["name"] == ident or e["repo"].rstrip("/").endswith("/" + ident)]
        if not match:
            raise RuntimeError(f"目录里找不到 {ident}")
        entry = match[0]
        raw, where, _ = await self._download_repo_zip(entry["repo"], "")
        prefix, names = self._zip_entries(raw)
        meta_name = f"{prefix}metadata.yaml"
        if meta_name not in names:
            return {"ok": False, "reason": f"{where} 包里没有 metadata.yaml，不安装", "entry": entry}
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            metadata = zf.read(meta_name).decode("utf-8", "replace")
        dir_name = self._meta_field(metadata, "name") or entry["name"]
        for key in ("desc", "version", "author"):
            if not self._meta_field(metadata, key):
                return {"ok": False, "reason": f"metadata.yaml 缺 {key}，AstrBot 不会加载", "entry": entry}
        target = Path(dest or self.ab_plugins) / dir_name
        return self._place(raw, prefix, target, source="astrbot", where=where, entry=entry)

    async def _resolve_commit(self, owner: str, name: str, tag: str) -> tuple[str, str]:
        """tag -> commit sha。

        `git ls-remote` 在这台机上被连接重置，所以走可达的 api.github.com；
        标注 tag 还要 peel 一层拿真正的 commit。
        """
        _require_network("解析 tag 提交号")
        async with httpx.AsyncClient(timeout=30, headers=UA) as client:
            r = await client.get(f"https://api.github.com/repos/{owner}/{name}/git/ref/tags/{tag}")
            if r.status_code != 200:
                return "", f"ref 查询失败 HTTP {r.status_code}"
            obj = (r.json() or {}).get("object", {}) or {}
            sha = str(obj.get("sha", ""))
            if obj.get("type") == "tag":
                r2 = await client.get(f"https://api.github.com/repos/{owner}/{name}/git/tags/{sha}")
                if r2.status_code == 200:
                    return str((r2.json() or {}).get("object", {}).get("sha", "")), "annotated-peeled"
                return sha, "annotated-unpeeled"
            return sha, "lightweight"

    async def _install_mb(self, ident: str, dest: Path | None) -> dict:
        """按官方 release 索引安装：tag 与 commit 双向核对，并记录许可证。"""

        index = await self._fetch_mb_index()
        rel = index.get(str(ident).lower())
        if rel is None:
            return {"ok": False, "reason": f"麦麦 release 索引里没有 {ident}（或它没有可用版本）"}
        repo, tag, expected = rel["repository"], rel["tag"], rel["commit"]
        if not repo or not tag:
            return {"ok": False, "reason": f"索引条目缺 repositoryUrl/tag，无法安全安装 {ident}"}

        raw, where, parts = await self._download_repo_zip(repo, tag)
        # where 形如 "Owner/Repo@v4.7.1 (api)"，还带着分支解析说明，只能做包含判断
        if f"@{tag}" not in where:
            return {"ok": False, "reason": f"下载到的不是索引声明的 tag {tag}（实际 {where}）"}
        prefix, names = self._zip_entries(raw)
        if f"{prefix}_manifest.json" not in names:
            return {"ok": False, "reason": f"{where} 包里没有 _manifest.json，不是麦麦插件"}

        # 索引说 tag 指向哪个 commit，再单独问一次 GitHub，两边不一致就不装
        actual, how = await self._resolve_commit(parts["owner"], parts["name"], tag)
        if expected and actual and actual != expected:
            return {"ok": False, "reason": f"commit 不一致：索引 {expected[:10]} vs GitHub {actual[:10]}，拒绝安装"}

        target = Path(dest or self.mb_plugins) / str(ident).replace(".", "_")
        entry = {"id": ident, "name": ident, "version": rel["version"], "repo": repo}
        result = self._place(raw, prefix, target, source="maibot", where=where, entry=entry)
        if not result.get("ok"):
            return result

        pinned = actual or expected
        if actual and expected:
            verification = (
                "已证明：下载的就是该 tag 的归档，且 tag 指向的 commit 与官方索引一致。"
                "未证明：ZIP 内容与该 commit 的树逐字节相等（codeload 不给出可核对的归档摘要）。"
            )
        else:
            verification = f"仅单向核对（索引有值={bool(expected)}，GitHub 解析到={bool(actual)}，方式={how}）"

        receipt = {
            "plugin_id": ident,
            "version": rel["version"],
            "tag": tag,
            "commit": pinned,
            "commit_source": f"官方索引 + api.github.com({how})" if actual else "仅官方索引",
            "license": rel.get("license", ""),
            "repository_url": repo,
            "capabilities": rel.get("capabilities", []),
            "host_application": {"min": rel.get("host_min", ""), "max": rel.get("host_max", "")},
            "installed_by": "astrmai-market",
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "verification": verification,
        }
        (target / ".maibot-release.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        result.update({"receipt": str(target / ".maibot-release.json"), "commit": pinned, "license": rel.get("license", ""), "tag": tag})
        result["note"] = "本机 git clone 不通，用 codeload 按索引 tag 取包并双向核对 commit"

        # 官方 release 索引里的 license 比包内推断更有依据，覆盖掉 _place 写的那份
        from . import licensing

        index_license = str(rel.get("license") or "").strip()
        files = licensing.evidence_from_dir(target)
        fallback = licensing.from_files(files)
        payload = {
            **(
                {"license": index_license, "basis": "麦麦官方 release 索引的 license 字段", "confidence": "high"}
                if index_license
                else fallback
            ),
            "package": repo,
            "commit": pinned,
            "tag": tag,
            "installed_from": str(where),
        }
        licensing.write_record(target, payload)
        result.update({"license": payload["license"], "license_basis": payload["basis"], "license_confidence": payload["confidence"]})
        return result

    def _install_native(self, ident: str, dest: Path | None) -> dict:
        src = Path(ident)
        if not src.is_absolute():
            src = self.ws / ident
        if not (src / "astrmai.toml").exists() or not (src / "main.py").exists():
            return {"ok": False, "reason": f"{src} 缺 astrmai.toml 或 main.py"}
        target = Path(dest or self.native_plugins) / src.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        verdict = self._record_license(target, where=f"local:{src}", entry={"repo": str(src)})
        return {"ok": True, "source": "astrmai", "path": str(target), "where": "local copy", **verdict}

    # ---------------- 许可证清单 ----------------

    def licenses(self) -> list[dict]:
        """已装组件的许可证清单：三家生态 + 两个引擎 + astrmai 自己。

        认不出的一律 license="unknown" 且 undetermined=True，不留空 —— 不分发时它是信息，
        要分发时它就是依据，留空等于让人误判成"没问题"。
        """

        from . import licensing

        rows = [
            licensing.row("astrmai（本融合层 + 控制台）", "astrmai", {"license": "AGPL-3.0-or-later", "basis": "合并作品按 AGPL-3.0-or-later 整体分发（GPLv3 §13 允许 GPL→AGPL 合并）", "confidence": "high"}),
            licensing.row("AstrBot", "engine", {"license": "AGPL-3.0-or-later", "basis": "上游 LICENSE 文件（Affero GPL v3 全文）", "confidence": "high"}),
            licensing.row("MaiBot（麦麦/MaiCore）", "engine", {"license": "GPL-3.0-or-later", "basis": "上游 README 自述 GPL-3.0", "confidence": "high"}),
        ]

        for meta in sorted(self.ab_plugins.glob("*/metadata.yaml")) if self.ab_plugins.exists() else []:
            target = meta.parent
            info = licensing.read_record(target)
            rows.append(licensing.row(self._meta_field(meta.read_text(encoding="utf-8", errors="replace"), "name") or target.name, "astrbot", info, path=str(target)))

        for man in sorted(self.mb_plugins.glob("*/_manifest.json")) if self.mb_plugins.exists() else []:
            target = man.parent
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            info = licensing.read_record(target)
            if info.get("license", "unknown") == "unknown" and data.get("license"):
                info = {"license": str(data["license"]), "basis": "_manifest.json 的 license 字段", "confidence": "medium"}
            rows.append(licensing.row(str(data.get("name") or target.name), "maibot", info, path=str(target)))

        for toml_path in sorted(self.native_plugins.glob("*/astrmai.toml")) if self.native_plugins.exists() else []:
            target = toml_path.parent
            rows.append(licensing.row(target.name, "astrmai", licensing.read_record(target), path=str(target)))

        return rows

    @staticmethod
    def _meta_field(text: str, key: str) -> str:
        """从 metadata.yaml 取一个标量字段。

        AstrBot 的 metadata.yaml 每个值后面都跟中文注释，必须走真 YAML 解析器，
        否则注释会被当成值的一部分（我就这样把目录名装成了 "xxx # 插件唯一识别名…"）。
        """

        try:
            import yaml

            data = yaml.safe_load(text)
            if isinstance(data, dict):
                value = data.get(key)
                return "" if value is None else str(value).strip()
        except Exception:  # noqa: BLE001 - 没有 pyyaml 或解析失败时退回粗剥
            pass
        m = re.search(rf"^{key}\s*:\s*(.+?)\s*$", text, flags=re.M)
        if not m:
            return ""
        raw = m.group(1).strip()
        raw = re.sub(r"(?<=\s)#.*$", "", raw).strip()  # 粗剥行内注释
        return raw.strip("\"'")

    def _place(self, raw: bytes, prefix: str, target: Path, *, source: str, where: str, entry: dict) -> dict:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".astrmai-install-", dir=str(target.parent)))
        try:
            staging_root = staging.resolve()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    rel = name[len(prefix) :] if name.startswith(prefix) else name
                    if not rel or name.endswith("/"):
                        continue
                    # 复检 H2（zip slip）：条目名里的 ../ 或绝对路径能把文件写到 staging 外面。
                    # 先粗筛再 resolve 判属主 —— 越界就整包拒绝，不"跳过坏条目"（别人就是照着清单打的包）。
                    parts = PurePosixPath(rel.replace("\\", "/")).parts
                    if rel.startswith("/") or ":" in rel[:3] or ".." in parts:
                        raise ValueError(f"包内路径越界: {name}")
                    out = (staging / rel).resolve()
                    if not out.is_relative_to(staging_root):
                        raise ValueError(f"包内路径越界（解析后）: {name}")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zf.read(name))
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
            verdict = self._record_license(target, raw=raw, prefix=prefix, where=where, entry=entry)
            return {"ok": True, "source": source, "path": str(target), "where": where, "entry": entry, **verdict}
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "reason": f"落地失败: {type(exc).__name__}: {exc}", "entry": entry}

    @staticmethod
    def _record_license(target: Path, *, raw: bytes = b"", prefix: str = "", where: str = "", entry: dict | None = None) -> dict:
        """安装即留许可证证据：优先读包内文件，认不出就写 unknown，绝不填空糊过去。"""

        from . import licensing

        files = licensing.evidence_from_zip(raw, prefix) if raw else {}
        if not files:
            files = licensing.evidence_from_dir(target)
        verdict = licensing.from_files(files)
        payload = {
            **verdict,
            "package": str((entry or {}).get("repo") or where or ""),
            "commit": str((entry or {}).get("commit") or ""),
            "installed_from": where,
        }
        licensing.write_record(target, payload)
        return {"license": payload["license"], "license_basis": payload["basis"], "license_confidence": payload["confidence"]}

    # ---------------- 已安装清单与卸载 ----------------

    def list_installed(self, source: str) -> list[dict]:
        if source == "astrbot":
            out = []
            for meta in sorted(self.ab_plugins.glob("*/metadata.yaml")):
                text = meta.read_text(encoding="utf-8", errors="replace")
                out.append({"name": self._meta_field(text, "name") or meta.parent.name, "path": str(meta.parent)})
            return out
        if source == "maibot":
            out = []
            for man in sorted(self.mb_plugins.glob("*/_manifest.json")):
                try:
                    d = json.loads(man.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                out.append({"name": d.get("name") or man.parent.name, "id": d.get("id"), "path": str(man.parent)})
            return out
        if source == "astrmai":
            out = []
            for man in sorted(self.native_plugins.glob("*/astrmai.toml")):
                try:
                    d = tomllib.loads(man.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                out.append({"name": d.get("name") or man.parent.name, "version": d.get("version"), "path": str(man.parent)})
            return out
        raise ValueError(source)

    def uninstall(self, source: str, name: str) -> dict:
        roots = {"astrbot": self.ab_plugins, "maibot": self.mb_plugins, "astrmai": self.native_plugins}
        root = Path(roots[source]).resolve()
        target = (root / name).resolve()
        # 只允许删市场目录下面的直接子目录，防手滑
        if target.parent != root or not target.is_dir():
            return {"ok": False, "reason": f"拒绝删除越界路径: {target}"}
        shutil.rmtree(target)
        return {"ok": True, "removed": str(target)}
