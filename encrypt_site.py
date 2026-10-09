#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点内容加密锁 — 静态仓库的 lock / unlock 工具。

原理:
  - lock  : 把看板/分析页等敏感内容用 AES-256-GCM 加密后写回原路径,
            访客只能看到密文; HTML 页面会变成一个"密码解锁壳",
            访客输入正确密码后由浏览器(WebCrypto)解密渲染。
  - unlock: 构建前把密文还原成明文 (云端 Actions / 本地脚本需要读明文)。

密码来源(按优先级):
  1. 命令行 --password "密码1,密码2"   (第一个为当前密码, 其余为待迁移的旧密码)
  2. 环境变量 SITE_PASSWORDS           (GitHub Secrets 里配置, 同上逗号分隔)
  3. 仓库根目录 secrets_local.py       (本地使用, 已被 .gitignore 排除):
         SITE_PASSWORDS = "当前密码,旧密码"

换密码(月度轮换): 把 Secrets 里 SITE_PASSWORDS 改成 "新密码,旧密码",
  下一次 lock 会自动把旧密码加密的文件迁移到新密码; 确认迁移完成后可删掉旧密码。

用法:
  python encrypt_site.py lock     # 发布前加密
  python encrypt_site.py unlock   # 构建前解密 (有解不开的文件时退出码=1, 阻断构建)
  python encrypt_site.py lock --password "abc" --root /path/to/repo
"""
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ── 常量 ──────────────────────────────────────────────────────────────
MAGIC_RAW = "# CAICAI-LOCKED v1"          # 文本/二进制容器头
MAGIC_HTML = "<!-- CAICAI-LOCKED v1 -->"  # HTML 解锁壳标记
ITERATIONS = 250_000                      # PBKDF2 迭代次数 (浏览器端同值)
CHK_TAG = b"caicai-chk-v1"

# 需要保护的文件 (相对仓库根; 支持通配符; 不存在的自动跳过)
HTML_TARGETS = [
    "index.html",                        # 着陆页/看板入口
    "web/dashboard.html",                # 看板本体
    "watchlist_us/earnings_calendar.html",  # 财报日历页
    "analysis/*.html",                   # 六步分析子页面
]
RAW_TARGETS = [
    # 六步分析源稿 (云端脚本构建时需要读, 平时加密存放)
    "analysis/港美股/**/*.md",
    "analysis/港美股/**/*.xlsx",
    # 构建缓存 (含 AI 新闻解读/财务指标/自选股买卖价位等)
    "news_cache.json",
    "morning_brief_cache.json",
    "oil_brief_cache.json",
    "oil_news_cache.json",
    "metrics_cache.json",
    "watchlist_us/earnings_calendar.json",
    "watchlist_us/config.json",
]


# ── 密钥派生 ─────────────────────────────────────────────────────────
def derive_key(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS
    ).derive(password.encode("utf-8"))


def chk_of(key: bytes) -> str:
    return hmac.new(key, CHK_TAG, hashlib.sha256).hexdigest()[:12]


def load_passwords(args_pw: str | None, root: Path) -> list[str]:
    """返回密码列表, 第一个为当前密码。"""
    raw = args_pw
    if not raw:
        raw = os.environ.get("SITE_PASSWORDS", "").strip()
    if not raw:
        f = root / "secrets_local.py"
        if f.exists():
            ns: dict = {}
            try:
                exec(f.read_text(encoding="utf-8"), {"__builtins__": {}}, ns)
            except Exception as e:  # noqa: BLE001
                print(f"[错误] 解析 {f.name} 失败: {e}")
                sys.exit(1)
            raw = ns.get("SITE_PASSWORDS") or ns.get("SITE_PASSWORD") or ""
            if isinstance(raw, (list, tuple)):
                raw = ",".join(str(x) for x in raw)
    pws = [p.strip() for p in str(raw).replace("\n", ",").split(",") if p.strip()]
    if not pws:
        print(
            "[错误] 未找到密码。请设置环境变量 SITE_PASSWORDS (GitHub Secrets)"
            "或仓库根目录 secrets_local.py 中的 SITE_PASSWORDS。"
        )
        sys.exit(1)
    return pws


def build_keys(passwords: list[str]) -> list[dict]:
    """每个密码派生一次主密钥 (salt 由密码确定性派生, 不需要额外存储)。"""
    keys = []
    for pw in passwords:
        salt = hashlib.sha256(b"caicai-lock-salt|" + pw.encode("utf-8")).digest()[:16]
        key = derive_key(pw, salt)
        keys.append({"pw": pw, "salt": salt, "key": key, "chk": chk_of(key)})
    return keys


# ── 容器格式 ─────────────────────────────────────────────────────────
def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def encrypt_bytes(plain: bytes, cur: dict) -> str:
    """加密任意字节 → 文本容器 (md/json/xlsx 等用)。"""
    iv = os.urandom(12)
    ct = AESGCM(cur["key"]).encrypt(iv, plain, None)
    return (
        f"{MAGIC_RAW} chk={cur['chk']} salt={b64e(cur['salt'])} iv={b64e(iv)}\n"
        + b64e(ct)
        + "\n"
    )


def parse_container(text: str) -> dict | None:
    if not text.startswith(MAGIC_RAW):
        return None
    head, _, body = text.partition("\n")
    fields = dict(kv.split("=", 1) for kv in head.split()[2:] if "=" in kv)
    fields["data"] = body.strip()
    return fields


def decrypt_container(fields: dict, keys: list[dict]) -> bytes | None:
    iv, ct = b64d(fields["iv"]), b64d(fields["data"])
    for k in keys:
        if k["chk"] != fields.get("chk"):
            continue
        try:
            return AESGCM(k["key"]).decrypt(iv, ct, None)
        except Exception:  # noqa: BLE001
            return None
    return None


# ── HTML 解锁壳 ──────────────────────────────────────────────────────
SHELL_TMPL = r"""<!DOCTYPE html>
<!-- CAICAI-LOCKED v1 -->
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>订阅内容 · 请输入访问密码</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(160deg, #0b0f17 0%, #101826 55%, #0b0f17 100%);
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: #d7dde6;
  }
  .card {
    width: min(92vw, 380px); padding: 40px 32px 32px; text-align: center;
    background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
    border-radius: 16px; backdrop-filter: blur(6px);
  }
  .lock { width: 44px; height: 44px; margin: 0 auto 18px; opacity: .85; }
  h1 { font-size: 19px; font-weight: 600; letter-spacing: .5px; margin-bottom: 8px; }
  .hint { font-size: 13px; color: #8b96a5; margin-bottom: 22px; line-height: 1.6; }
  input[type=password] {
    width: 100%; padding: 12px 14px; font-size: 15px; color: #e8edf4;
    background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.14);
    border-radius: 10px; outline: none; text-align: center; letter-spacing: 2px;
  }
  input[type=password]:focus { border-color: #4a8cff; }
  .row { display: flex; align-items: center; justify-content: center; gap: 6px;
         margin: 14px 0 18px; font-size: 13px; color: #8b96a5; }
  button {
    width: 100%; padding: 12px; font-size: 15px; font-weight: 600; color: #fff;
    background: linear-gradient(135deg, #2f6fed, #4a8cff); border: none;
    border-radius: 10px; cursor: pointer; letter-spacing: 4px;
  }
  button:hover { filter: brightness(1.1); }
  #msg { min-height: 20px; margin-top: 14px; font-size: 13px; color: #8b96a5; }
  #msg.err { color: #ff6b6b; }
  .shake { animation: shake .35s; }
  @keyframes shake { 0%,100%{transform:translateX(0)} 25%{transform:translateX(-6px)}
                     50%{transform:translateX(6px)} 75%{transform:translateX(-4px)} }
</style>
</head>
<body>
<div class="card">
  <svg class="lock" viewBox="0 0 24 24" fill="none" stroke="#8b96a5" stroke-width="1.6"
       stroke-linecap="round" stroke-linejoin="round">
    <rect x="4" y="10.5" width="16" height="10" rx="2.5"/>
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>
    <circle cx="12" cy="15.5" r="1.6" fill="#8b96a5" stroke="none"/>
  </svg>
  <h1>订阅专属内容</h1>
  <p class="hint">本站内容经加密保护，输入访问密码即可查看<br>密码可通过订阅获取</p>
  <input type="password" id="pw" placeholder="访问密码" autocomplete="off" autofocus>
  <div class="row"><input type="checkbox" id="remember" checked><label for="remember">在本机记住密码</label></div>
  <button id="go">解 锁</button>
  <p id="msg"></p>
</div>
<script id="caicai-lock-data" type="application/json">__PAYLOAD__</script>
<script>
(function () {
  "use strict";
  var P = JSON.parse(document.getElementById("caicai-lock-data").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var iv = Uint8Array.from(atob(P.iv), function (c) { return c.charCodeAt(0); });
  var ct = Uint8Array.from(atob(P.data), function (c) { return c.charCodeAt(0); });
  var enc = new TextEncoder();

  function derive(pw) {
    var salt = Uint8Array.from(atob(P.salt), function (c) { return c.charCodeAt(0); });
    return crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveKey"])
      .then(function (km) {
        return crypto.subtle.deriveKey(
          { name: "PBKDF2", salt: salt, iterations: P.it, hash: "SHA-256" },
          km, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
      });
  }
  function decryptWith(pw) {
    return derive(pw).then(function (k) {
      return crypto.subtle.decrypt({ name: "AES-GCM", iv: iv }, k, ct);
    }).then(function (pt) { return new TextDecoder().decode(pt); });
  }
  function fail(msg) {
    var m = $("msg"); m.textContent = msg; m.className = "err";
    var c = document.querySelector(".card");
    c.classList.remove("shake"); void c.offsetWidth; c.classList.add("shake");
  }
  function open(html, pw, save) {
    try { if (save) localStorage.setItem("caicai_pw", pw); } catch (e) {}
    document.open(); document.write(html); document.close();
  }
  function attempt(pw, save) {
    return decryptWith(pw).then(function (html) { open(html, pw, save); return true; },
                                function () { return false; });
  }
  $("go").addEventListener("click", function () {
    var pw = $("pw").value;
    if (!pw) { fail("请输入密码"); return; }
    $("msg").textContent = "解密中..."; $("msg").className = "";
    attempt(pw, $("remember").checked).then(function (ok) { if (!ok) fail("密码不正确"); });
  });
  $("pw").addEventListener("keydown", function (e) { if (e.key === "Enter") $("go").click(); });

  var saved = null;
  try { saved = localStorage.getItem("caicai_pw"); } catch (e) {}
  if (saved) {
    $("msg").textContent = "正在自动解锁...";
    attempt(saved, true).then(function (ok) {
      if (!ok) { try { localStorage.removeItem("caicai_pw"); } catch (e) {} $("msg").textContent = ""; }
    });
  }
})();
</script>
</body>
</html>
"""


def make_shell(plain_html: str, cur: dict) -> str:
    iv = os.urandom(12)
    ct = AESGCM(cur["key"]).encrypt(iv, plain_html.encode("utf-8"), None)
    payload = json.dumps(
        {
            "v": 1,
            "it": ITERATIONS,
            "salt": b64e(cur["salt"]),
            "chk": cur["chk"],
            "iv": b64e(iv),
            "data": b64e(ct),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return SHELL_TMPL.replace("__PAYLOAD__", payload)


def parse_shell(text: str) -> dict | None:
    if MAGIC_HTML not in text and 'id="caicai-lock-data"' not in text:
        return None
    m_start = text.find('<script id="caicai-lock-data" type="application/json">')
    if m_start < 0:
        return None
    m_start += len('<script id="caicai-lock-data" type="application/json">')
    m_end = text.find("</script>", m_start)
    if m_end < 0:
        return None
    try:
        return json.loads(text[m_start:m_end])
    except Exception:  # noqa: BLE001
        return None


def decrypt_payload(p: dict, keys: list[dict]) -> str | None:
    iv, ct = b64d(p["iv"]), b64d(p["data"])
    for k in keys:
        if k["chk"] != p.get("chk"):
            continue
        try:
            return AESGCM(k["key"]).decrypt(iv, ct, None).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    return None


# ── 主逻辑 ───────────────────────────────────────────────────────────
def collect_targets(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in HTML_TARGETS + RAW_TARGETS:
        files.extend(root.glob(pattern))
    return sorted({f for f in files if f.is_file()})


def main() -> int:
    mode = None
    args_pw = None
    root = Path(__file__).resolve().parent
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("lock", "unlock"):
            mode = a
        elif a == "--password" and i + 1 < len(argv):
            args_pw = argv[i + 1]
            i += 1
        elif a == "--root" and i + 1 < len(argv):
            root = Path(argv[i + 1]).resolve()
            i += 1
        i += 1
    if mode not in ("lock", "unlock"):
        print(__doc__)
        return 1

    keys = build_keys(load_passwords(args_pw, root))
    cur = keys[0]
    targets = collect_targets(root)
    if not targets:
        print("[警告] 未找到任何待处理文件")
        return 0

    n_ok = n_skip = n_fail = 0
    failed: list[str] = []
    for f in targets:
        rel = f.relative_to(root).as_posix()
        try:
            data = f.read_bytes()
        except OSError as e:
            print(f"[跳过] {rel}: 读取失败 {e}")
            continue

        # HTML 解锁壳
        shell = parse_shell(data.decode("utf-8", errors="ignore"))
        if shell is not None:
            if mode == "lock" and shell.get("chk") == cur["chk"]:
                n_skip += 1
                continue
            plain = decrypt_payload(shell, keys)
            if plain is None:
                if mode == "unlock":
                    failed.append(rel)
                    print(f"[失败] {rel}: 无可用密码解密")
                else:
                    n_skip += 1
                    print(f"[警告] {rel}: 旧密码加密且未提供旧密码, 保持原样")
                continue
            if mode == "unlock":
                f.write_bytes(plain.encode("utf-8"))
            else:
                f.write_text(make_shell(plain, cur), encoding="utf-8", newline="")
            n_ok += 1
            continue

        # 文本/二进制容器 (md / json / xlsx)
        text = None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        fields = parse_container(text) if text is not None else None
        if fields is not None:
            if mode == "lock" and fields.get("chk") == cur["chk"]:
                n_skip += 1
                continue
            plain = decrypt_container(fields, keys)
            if plain is None:
                if mode == "unlock":
                    failed.append(rel)
                    print(f"[失败] {rel}: 无可用密码解密")
                else:
                    n_skip += 1
                    print(f"[警告] {rel}: 旧密码加密且未提供旧密码, 保持原样")
                continue
            f.write_bytes(plain if mode == "unlock" else encrypt_bytes(plain, cur).encode("ascii"))
            n_ok += 1
            continue

        # 明文文件
        if mode == "lock":
            if f.suffix.lower() == ".html":
                plain_html = text if text is not None else data.decode("utf-8", errors="replace")
                f.write_text(make_shell(plain_html, cur), encoding="utf-8", newline="")
            elif text is not None:
                f.write_text(encrypt_bytes(text.encode("utf-8"), cur), encoding="utf-8", newline="")
            else:
                f.write_bytes(encrypt_bytes(data, cur).encode("ascii"))
            n_ok += 1
        else:
            n_skip += 1

    tag = "上锁" if mode == "lock" else "解锁"
    print(f"[{tag}完成] 处理 {n_ok} 个, 跳过 {n_skip} 个, 失败 {len(failed)} 个 (共 {len(targets)} 个目标)")
    if failed:
        print("[失败清单] " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
