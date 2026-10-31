"""许可证识别：只从包内已有的证据推断，认不出就写 unknown。

为什么宁可 unknown 也不猜：许可证标识是分发依据，猜错比标"不知道"更糟。
所以每条结论都带 basis（凭哪句话）与 confidence（high=正文标准抬头 / medium=声明或徽章 / unknown）。
"""

import json
import re
import time
from pathlib import Path
from typing import Any

RECORD_FILE = ".astrmai-license.json"
LICENSE_FILE_NAMES = (
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    "LICENSE-MIT",
    "LICENCE",
    "LICENCE.txt",
    "LICENCE.md",
    "COPYING",
    "COPYING.txt",
    "COPYING.LESSER",
)
EVIDENCE_CHARS = 8000


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _later_clause(norm: str) -> str:
    """GPL 家族里"或（按你的选择）任何后续版本"是 -or-later 的判据。"""

    if "any later version" in norm or "(at your option) any later" in norm:
        return "-or-later"
    return "-only"


def _version(norm: str, candidates: tuple[tuple[str, str], ...]) -> str:
    """把正文里的"Version 3"/"2.1"归一成 SPDX 用的版本号。"""

    for needle, out in candidates:
        if needle in norm:
            return out
    return ""


def _bsd(norm: str) -> str:
    """BSD 家族：真文本常常通篇不写 "BSD" 这个词，只能按抬头条款与编号条款数判。"""

    if "neither the name" in norm or "endorse or promote" in norm or "3-clause" in norm:
        return "BSD-3-Clause"
    if "2-clause" in norm or "simplified" in norm:
        return "BSD-2-Clause"
    if "4." in norm and "advertising" in norm:
        return "BSD-4-Clause"
    counted = sum(1 for marker in ("1.", "2.", "3.") if marker in norm)
    if counted >= 3:
        return "BSD-3-Clause"
    if counted == 2:
        return "BSD-2-Clause"
    return "BSD-unknown-clauses"


def classify(text: str) -> tuple[str, str, str]:
    """返回 (标识, 依据, 置信度)。认不出返回 ("unknown", 原因, "unknown")。"""

    raw = str(text or "")
    norm = _norm(raw)
    if not norm:
        return "unknown", "没有可分析的文本", "unknown"

    head = " ".join(str(text or "").split())[:120]
    later = _later_clause(norm)

    if "gnu affero general public license" in norm:
        ver = _version(norm, (("version 3", "3.0"), ("version 1", "1.0")))
        return (f"AGPL-{ver}{later}" if ver else "AGPL-unknown-version"), "正文含 GNU AFFERO GENERAL PUBLIC LICENSE 抬头", "high"
    if "gnu lesser general public license" in norm or "gnu gpl smaller license" in norm:
        ver = _version(norm, (("version 3", "3.0"), ("version 2.1", "2.1"), ("version 2", "2.0")))
        return (f"LGPL-{ver}{later}" if ver else "LGPL-unknown-version"), "正文含 GNU LESSER GENERAL PUBLIC LICENSE 抬头", "high"
    if "gnu general public license" in norm:
        ver = _version(norm, (("version 3", "3.0"), ("version 2", "2.0")))
        return (f"GPL-{ver}{later}" if ver else "GPL-unknown-version"), "正文含 GNU GENERAL PUBLIC LICENSE 抬头", "high"
    if "mozilla public license" in norm:
        ver = _version(norm, (("2.0", "2.0"), ("1.1", "1.1")))
        return (f"MPL-{ver}" if ver else "MPL-unknown-version"), "正文含 Mozilla Public License 抬头", "high"
    if "apache license" in norm and "version 2.0" in norm:
        return "Apache-2.0", "正文含 Apache License Version 2.0 抬头", "high"
    if "apache license" in norm:
        return "Apache-unknown-version", "正文提到 Apache License 但没读到版本号", "medium"
    if "mit license" in norm or "permission is hereby granted, free of charge" in norm:
        return "MIT", "正文含 MIT License / 免费授权声明", "high"
    if "redistribution and use in source and binary forms" in norm:
        ident = _bsd(norm)
        conf = "high" if ident in ("BSD-3-Clause", "BSD-2-Clause") else "medium"
        return ident, "正文含 BSD 系标准条款（按条款数判定）", conf
    if "bsd" in norm:
        ident = _bsd(norm)
        return ident, "正文提到 BSD", ("high" if ident.endswith(("3-Clause", "2-Clause")) else "medium")
    if "the unlicense" in norm or "free and unencumbered" in norm:
        return "Unlicense", "正文含 Unlicense / free and unencumbered 声明", "high"
    if "cc0" in norm and "universal" in norm:
        return "CC0-1.0", "正文含 CC0 1.0 Universal 声明", "high"
    if "isc license" in norm:
        return "ISC", "正文含 ISC License 抬头", "high"
    if "eula" in norm or "proprietary" in norm or "all rights reserved" in norm or "保留一切权利" in raw:
        return "unknown", "正文像专有/EULA 声明，不能当作开源许可", "unknown"
    return "unknown", f"没有可辨识的标准抬头（首行：{head}）", "unknown"


def from_files(files: dict[str, str]) -> dict:
    """按证据强度依次尝试：LICENSE 文件正文 > metadata 的 license 字段 > README 声明。

    files 是 {小写文件名: 文本}；只吃已经在手的包内容，不额外下载。
    """

    for name in LICENSE_FILE_NAMES:
        text = files.get(name.lower())
        if text and text.strip():
            ident, basis, conf = classify(text)
            if ident != "unknown":
                return {"license": ident, "basis": f"{name} 文件：{basis}", "confidence": conf}

    for meta_name in ("metadata.yaml", "_manifest.json", "astrmai.toml", "plugin.json"):
        text = files.get(meta_name.lower())
        if not text:
            continue
        field = _license_field(text, meta_name.lower())
        if field:
            ident, basis, conf = classify(field)
            if ident == "unknown":
                # 字段里常写 SPDX 短名（MIT / GPL-3.0），分类器读不出抬头时直接采信字段值
                ident, basis, conf = _short_name(field), f"{meta_name} 的 license 字段", "medium"
            if ident != "unknown":
                return {"license": ident, "basis": basis, "confidence": conf}

    readme = files.get("readme.md") or files.get("readme") or files.get("readme.rst") or ""
    badge = _badge_license(readme)
    if badge:
        return {"license": badge, "basis": "README 里的许可证徽章/声明", "confidence": "medium"}

    lic = files.get("license") or files.get("licence") or files.get("copying") or ""
    if lic.strip():
        ident, basis, conf = classify(lic)
        if ident == "unknown":
            return {"license": "unknown", "basis": f"有 LICENSE 文件但认不出条款（{basis}）", "confidence": "unknown"}
        return {"license": ident, "basis": basis, "confidence": conf}

    return {"license": "unknown", "basis": "包里既没有 LICENSE 正文，也没有 metadata/README 声明", "confidence": "unknown"}


def _license_field(text: str, name: str) -> str:
    if name.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        value = data.get("license") if isinstance(data, dict) else None
        if isinstance(value, dict):
            value = value.get("type") or value.get("name") or ""
        return str(value or "").strip()
    match = re.search(r"^\s*license\s*[:=]\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip().strip("\"'") if match else ""


def _short_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z.+-]", "", str(value).strip())
    return cleaned or "unknown"


def _badge_license(readme: str) -> str:
    text = str(readme or "")[:EVIDENCE_CHARS]
    patterns = (
        r"license[\s_\-]*(badge|shields\.io)[^\n]*?(AGPL[-0-9.\-]+|GPL[-0-9.\-]+|LGPL[-0-9.\-]+|MIT|Apache[-\s]?2\.0|BSD[-0-9A-Za-z.\-]+|MPL[-0-9.]+)",
        r"(?im)^\s*(?:\*\*)?(?:license|许可证|开源协议)(?:\*\*)?\s*[:：]\s*([A-Za-z0-9.+\- ]{2,40})",
        r"[Ll]icensed under (?:the )?([A-Za-z0-9.+\- ]{2,40}?)(?:\s+license|[,\.\n])",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        candidate = m.group(1).strip()
        ident, _basis, conf = classify(candidate)
        if ident != "unknown" and conf != "unknown":
            return ident
        short = _short_name(candidate)
        if short and short.lower() not in ("unknown", "none"):
            return short
    return ""


def evidence_from_dir(target: Path) -> dict:
    """从已落地的目录里取顶层证据文件（安装后补记录、或本地复制安装时用）。"""

    files: dict[str, str] = {}
    base = Path(target)
    if not base.exists():
        return files
    for name in (*LICENSE_FILE_NAMES, "README.md", "README", "metadata.yaml", "_manifest.json", "astrmai.toml", "plugin.json"):
        candidate = base / name
        if candidate.is_file():
            try:
                files[name.lower()] = candidate.read_text(encoding="utf-8", errors="replace")[:EVIDENCE_CHARS]
            except OSError:
                continue
    return files


def write_record(target: Path, payload: dict) -> Path:
    """把许可证结论写进插件目录，安装即留证；分发时它就是依据。"""

    data = {**payload, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "record_version": 1}
    path = Path(target) / RECORD_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_record(target: Path) -> dict:
    """读已装组件的许可证；没有记录就现读包内证据，读不出标 unknown。"""

    target = Path(target)
    record = target / RECORD_FILE
    if record.exists():
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("license"):
                return data
        except (OSError, ValueError):
            pass

    # 麦麦市场装的那份 release 取证文件里也带着 license/commit
    release = target / ".maibot-release.json"
    if release.exists():
        try:
            data = json.loads(release.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("license"):
                return {
                    "license": str(data["license"]),
                    "basis": "麦麦 release 取证文件（.maibot-release.json）",
                    "confidence": "medium",
                    "commit": str(data.get("commit") or ""),
                }
        except (OSError, ValueError):
            pass

    files: dict[str, str] = {}
    for name in (*LICENSE_FILE_NAMES, "README.md", "README", "metadata.yaml", "_manifest.json", "astrmai.toml", "plugin.json"):
        candidate = target / name
        if candidate.is_file():
            try:
                files[name.lower()] = candidate.read_text(encoding="utf-8", errors="replace")[:EVIDENCE_CHARS]
            except OSError:
                continue
    verdict = from_files(files)
    verdict["basis"] = verdict["basis"] + "（安装后现读，非安装时记录）"
    return verdict


def evidence_from_zip(raw: bytes, prefix: str) -> dict:
    """从已经下载在内存里的包中抽出许可证证据文件，不额外发任何请求。"""

    import io
    import zipfile

    files: dict[str, str] = {}
    wanted = {n.lower() for n in (*LICENSE_FILE_NAMES, "README.md", "README", "metadata.yaml", "_manifest.json", "astrmai.toml", "plugin.json")}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                rel = name[len(prefix) :] if name.startswith(prefix) else name
                parts = rel.split("/")
                if len(parts) != 1:
                    continue
                if parts[0].lower() in wanted:
                    files[parts[0].lower()] = zf.read(name).decode("utf-8", "replace")[:EVIDENCE_CHARS]
    except Exception:  # noqa: BLE001 - 取证失败不该让安装失败
        return {}
    return files


def row(component: str, source: str, info: dict, *, path: str = "") -> dict:
    """统一成一行清单；unknown 必须显式写出来，不能留空让人以为"没问题"。"""

    license_id = str(info.get("license") or "unknown")
    return {
        "component": component,
        "source": source,
        "license": license_id,
        "basis": str(info.get("basis") or "无依据"),
        "confidence": str(info.get("confidence") or "unknown"),
        "commit": str(info.get("commit") or ""),
        "path": path,
        "undetermined": license_id.lower() == "unknown",
    }
