r"""把 dist/astrmai-0.1.0 发布到 GitHub：只走 api.github.com 的 Git Data API。

为什么不用 git push：这台机的 GitHub 域名被 hosts 指向本地代理，克隆/推送都到不了远端，
而 REST 是通的（实测 /zen 200）。Data API 允许"一次建树 + 一次建提交 + 一次指 ref"，
不需要本地 git 与远端握手。

token 从 secrets/github_credentials.json 读（tools/get_github_token.cmd 本地录入），
本脚本任何路径都不打印它：异常文本一律先做 token 掩码再输出。

用法：
    python tools/publish_github.py --check            # 只看权限与计划，不写任何东西
    python tools/publish_github.py                    # 建私有仓库并推 dist 树
    python tools/publish_github.py --public           # 建公开仓库（翻已有仓库用 --visibility public）
    python tools/publish_github.py --visibility public  # 只改可见性
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

WS = Path(__file__).resolve().parents[1]
TREE = WS / "dist" / "astrmai-0.1.0"
TOKEN_FILE = WS / "secrets" / "github_credentials.json"
API = "https://api.github.com"
DEFAULT_NAME = "astrmai"


def git_blob_sha(data: bytes) -> str:
    """git 自己的对象算法：sha1("blob <长度>\\0" + 内容)。拿它证明远端存的就是这字节。"""

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit(f"没有 {TOKEN_FILE}：先跑 tools\\get_github_token.cmd 本地录入 token")
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    token = str(data.get("token") or "")
    if len(token) < 20:
        sys.exit("secrets 里的 token 形状不对（太短），重新录入一次")
    return token


class Publisher:
    def __init__(self, token: str) -> None:
        self.token = token
        self.login = ""
        self.tree = TREE
        self.client = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-Client-Version": "astrmai-publish/0.1",
            },
            timeout=60,
        )

    def safe(self, text: str) -> str:
        """任何要打印的东西先掐掉 token —— 报错回显里带请求头就是这个洞。"""

        return str(text).replace(self.token, "<token 已掩码>")

    def call(self, method: str, path: str, *, json_body: dict | None = None, expect=(200, 201, 204)) -> dict:
        r = self.client.request(method, path, json=json_body)
        if r.status_code not in expect:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {self.safe(r.text)[:300]}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    # ---------- 只读检查 ----------

    def whoami(self) -> dict:
        user = self.call("GET", "/user")
        scopes = self.client.get("/rate_limit").headers.get("X-OAuth-Scopes", "")
        return {"login": user.get("login"), "id": user.get("id"), "scopes": scopes.strip()}

    # ---------- 仓库 ----------

    def ensure_repo(self, name: str, private: bool, description: str) -> dict:
        existing = self.client.get(f"/repos/{self.login}/{name}")
        if existing.status_code == 200:
            info = existing.json()
            if bool(info.get("private")) != private:
                self.call(
                    "PATCH",
                    f"/repos/{self.login}/{name}",
                    json_body={"private": private},
                )
                info = self.call("GET", f"/repos/{self.login}/{name}")
            return info
        created = self.call(
            "POST",
            "/user/repos",
            json_body={
                "name": name,
                "private": private,
                "description": description,
                "homepage": "",
                "has_issues": True,
                "has_wiki": False,
                "auto_init": False,
            },
        )
        return created

    def set_visibility(self, name: str, private: bool) -> dict:
        return self.call("PATCH", f"/repos/{self.login}/{name}", json_body={"private": private})

    # ---------- 内容 ----------

    def collect(self) -> list[dict]:
        if not self.tree.exists():
            sys.exit("发布树不存在：先跑 python tools/pack_release.py")
        out = []
        for path in sorted(self.tree.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.tree).as_posix()
            data = path.read_bytes()
            out.append({"path": rel, "mode": "100755" if path.suffix == ".sh" else "100644", "bytes": data, "sha": git_blob_sha(data)})
        return out

    def blob_sha(self, name: str, data: bytes) -> str:
        """用 /git/blobs 落一个 blob（它的 content 默认就是 base64），返回远端 sha。

        为什么不在 /git/trees 里直接带 content：实测 trees 端点不按 encoding=base64 解码，
        会把 base64 字符串原样当文件内容存下来 —— 每个文件都变成 4/3 长的乱码，而路径比对完全看不出来。
        """

        created = self.call(
            "POST",
            f"/repos/{self.login}/{name}/git/blobs",
            json_body={"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        remote_sha = created["sha"]
        if remote_sha != git_blob_sha(data):
            raise RuntimeError(f"blob sha 与本地不一致: {remote_sha} != {git_blob_sha(data)}")
        return remote_sha

    def head_commit(self, name: str, branch: str) -> dict | None:
        r = self.client.get(f"/repos/{self.login}/{name}/git/ref/heads/{branch}")
        if r.status_code != 200:
            return None
        sha = r.json()["object"]["sha"]
        return self.call("GET", f"/repos/{self.login}/{name}/git/commits/{sha}")

    def remote_tree(self, name: str, branch: str) -> dict[str, str]:
        commit = self.head_commit(name, branch)
        if not commit:
            return {}
        # 必须 recursive：非递归只回顶层那几项，拿它比对会误报"远端少了几十个文件"
        tree = self.call("GET", f"/repos/{self.login}/{name}/git/trees/{commit['tree']['sha']}?recursive=1")
        if tree.get("truncated"):
            raise RuntimeError("远端树被截断了，比对结果不可信，先减少文件数再推")
        return {t["path"]: t["sha"] for t in tree.get("tree", []) if t.get("type") == "blob"}

    def seed(self, name: str, branch: str, author: dict) -> None:
        """空仓库上 POST /git/trees 会 409「Git Repository is empty」，所以先放一个占位提交。

        发布树里本来就有 README.md，紧随其后的那次提交会把它覆盖掉，占位内容不会留在最终产物里。
        """

        self.call(
            "PUT",
            f"/repos/{self.login}/{name}/contents/README.md",
            json_body={
                "message": "chore: 初始化仓库（Data API 建树要求仓库先有提交）",
                "content": base64.b64encode(b"# astrmai\n").decode(),
                "branch": branch,
                "author": {"name": author["name"], "email": author["email"]},
                "committer": {"name": author["name"], "email": author["email"]},
            },
        )

    def push(self, name: str, branch: str, message: str, author: dict, orphan: bool = False) -> dict:
        """orphan=True：提交不带父节点，再把 ref 强制指过去。

        用于"仓库里已经有一个坏提交、但 token 没有 delete_repo 权限删不掉"的情况：
        坏提交变成不可达对象，界面与克隆看到的历史就是这一条干净提交。
        """

        files = self.collect()
        head = self.head_commit(name, branch)
        if head is None:
            self.seed(name, branch, author)
            head = self.head_commit(name, branch)
        remote = self.remote_tree(name, branch) if head else {}
        entries = []
        uploaded = 0
        for item in files:
            if remote.get(item["path"]) == item["sha"]:
                # 远端已经是这份字节（同一个 sha 只可能是同一份内容），不必再传一次
                entries.append({"path": item["path"], "mode": item["mode"], "type": "blob", "sha": item["sha"]})
                continue
            sha = self.blob_sha(name, item["bytes"])
            entries.append({"path": item["path"], "mode": item["mode"], "type": "blob", "sha": sha})
            uploaded += 1
        print(f"blob：{len(entries)} 个条目，其中 {uploaded} 个需要上传")
        tree_body: dict = {"tree": entries}
        if head:
            tree_body["base_tree"] = head["tree"]["sha"]
        tree = self.call("POST", f"/repos/{self.login}/{name}/git/trees", json_body=tree_body)
        commit_body = {
            "message": message,
            "tree": tree["sha"],
            "author": author,
            "committer": author,
            "parents": [] if orphan else ([head["sha"]] if head else []),
        }
        commit = self.call("POST", f"/repos/{self.login}/{name}/git/commits", json_body=commit_body)
        if head:
            self.call(
                "PATCH",
                f"/repos/{self.login}/{name}/git/refs/heads/{branch}",
                json_body={"sha": commit["sha"], "force": bool(orphan)},
            )
        else:
            self.call("POST", f"/repos/{self.login}/{name}/git/refs", json_body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return {"commit": commit["sha"], "tree": tree["sha"], "files": len(entries)}

    def git_push(self, name: str, branch: str, message: str, author: dict) -> dict:
        """真 git 推一次：天生单条提交，没有占位提交，也没有不可达垃圾对象。

        token 不进命令行、不进 shell 历史、不进 .git/config：走 GIT_ASKPASS 现问现答。
        """

        def run(*args: str) -> str:
            env = dict(os.environ)
            env["GIT_ASKPASS"] = str(WS / "e2e" / "askpass.cmd")
            env["ASTRMAI_GH_LOGIN"] = self.login
            env["GIT_TERMINAL_PROMPT"] = "0"
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.tree),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=600,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"git {' '.join(args[:2])} 失败: {self.safe(proc.stderr)[:220]}")
            return (proc.stdout or "").strip()

        shutil.rmtree(self.tree / ".git", ignore_errors=True)
        try:
            run("init", "-b", branch)
            run("config", "user.name", author["name"])
            run("config", "user.email", author["email"])
            run("add", "-A")
            run("commit", "-q", "-m", message)
            sha = run("rev-parse", "HEAD")
            out = run("push", "--force", f"https://github.com/{self.login}/{name}.git", f"{sha}:refs/heads/{branch}")
        finally:
            shutil.rmtree(self.tree / ".git", ignore_errors=True)  # 别让发布树里留着一个 .git
        return {"commit": sha, "tree": "", "files": len(self.collect()), "push": out.splitlines()[-1] if out else ""}

    def verify(self, name: str, branch: str, expect_files: int) -> list[str]:
        """核对不是"文件名齐了"，而是"远端每个 blob 的 git sha 与本地字节算出来的一致"。

        上一版只比路径集合，于是 trees 端点把 base64 当内容存下来这件事完全没暴露 —— 69 个文件全是乱码还"核对通过"。
        """

        problems = []
        remote = self.remote_tree(name, branch)
        local = {f["path"]: f["sha"] for f in self.collect()}
        missing = sorted(set(local) - set(remote))
        extra = sorted(set(remote) - set(local))
        if missing:
            problems.append(f"远端缺 {len(missing)} 个文件：{missing[:5]}")
        if extra:
            problems.append(f"远端多出 {len(extra)} 个文件：{extra[:5]}")
        differ = sorted(p for p in set(local) & set(remote) if local[p] != remote[p])
        if differ:
            problems.append(f"{len(differ)} 个文件字节与本地不一致（sha 不同）：{differ[:5]}")
        if len(remote) != expect_files:
            problems.append(f"远端 {len(remote)} 个文件，期望 {expect_files}")
        for probe in ("LICENSE", "README.md", "docs/COVERAGE.md", "astrmai/panel.py", "astrmai_plugin/main.py"):
            if probe not in remote:
                problems.append(f"远端缺 {probe}")
        return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只验权限与计划，不写")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--tree", default="astrmai-0.1.0", help="dist 下要推的目录名（插件包用 astrbot-plugin-astrmai）")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--public", action="store_true", help="建/保留为公开仓库（默认私有）")
    ap.add_argument("--visibility", choices=["public", "private"], help="只改可见性，不推内容")
    ap.add_argument("--orphan", action="store_true", help="推一条无父提交并强制移动分支（用来覆盖坏历史，需要没有 delete_repo 权限时）")
    ap.add_argument("--git", action="store_true", help="用真 git push（单条提交）；不通则自动退回 Data API")
    ap.add_argument("--message", default="astrmai 0.1.0：AstrBot + 麦麦强行融合（含控制台、市场、许可证链、攻击面取证）")
    args = ap.parse_args()

    token = load_token()
    pub = Publisher(token)
    pub.tree = WS / "dist" / args.tree
    try:
        who = pub.whoami()
    except Exception as exc:  # noqa: BLE001
        print("token 校验失败:", pub.safe(exc)[:200])
        return 2
    pub.login = who["login"]
    print(f"账号: {who['login']} (id={who['id']})")
    print(f"token 传统 scope: {who['scopes'] or '（fine-grained，无 scope 头）'}")

    files = pub.collect()
    total = sum(len(f["bytes"]) for f in files)
    print(f"计划发布: {args.name} / {len(files)} 个文件 / 约 {total * 3 // 4 // 1024} KB（base64 前）")
    print(f"可见性: {'公开' if args.public else '私有'}（默认私有，确认内容后翻公开即可）")

    if args.visibility:
        info = pub.set_visibility(args.name, args.visibility == "private")
        print(f"已把 {info['full_name']} 设为 {'私有' if info['private'] else '公开'}: {info['html_url']}")
        return 0
    if args.check:
        print("--check：没有创建任何东西。")
        return 0

    description = "AstrBot 与 MaiBot（麦麦）强行融合成一个进程、一条 QQ 连接：桥接、双向工具联邦、统一市场、许可证链、自建控制台。AGPL-3.0-or-later。"
    info = pub.ensure_repo(args.name, private=not args.public, description=description)
    print(f"仓库就绪: {info['full_name']}（{'私有' if info['private'] else '公开'}）{info['html_url']}")

    # 提交署名不带任何私人邮箱：用 GitHub 的 noreply 形式，既归属账号又不暴露真实邮箱
    author = {
        "name": "astrmai",
        "email": f"{who['id']}+{who['login']}@users.noreply.github.com",
    }
    if args.git:
        try:
            result = pub.git_push(args.name, args.branch, args.message, author)
            print(f"真 git 推送完成：单条提交 {result['commit'][:10]}（{result['files']} 文件），无占位提交、无垃圾对象")
        except Exception as exc:  # noqa: BLE001 - git 通道被本机网络挡住时退回 API
            print(f"git push 不通（{pub.safe(str(exc))[:200]}），退回 Data API 路线")
            result = pub.push(args.name, args.branch, args.message, author, orphan=args.orphan)
            print(f"已推 commit {result['commit'][:10]}（{result['files']} 文件，树 {result['tree'][:10]}）")
    else:
        result = pub.push(args.name, args.branch, args.message, author, orphan=args.orphan)
        print(f"已推 commit {result['commit'][:10]}（{result['files']} 文件，树 {result['tree'][:10]}）")

    problems = pub.verify(args.name, args.branch, result["files"])
    for line in problems:
        print("  核对 FAIL", line)
    if problems:
        return 1
    print(f"核对通过：远端文件集与本地发布树逐一对齐，LICENSE/README/COVERAGE/核心源码都在。")
    print(f"看这里: {info['html_url']}")
    print(f"署名: {author['name']} <{author['email']}>（不含私人邮箱）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
