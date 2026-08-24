"""emoji 门禁（P0-1）自身的回归测试。

门禁脚本最危险的失效方式不是报错，而是「永远返回 0」—— 那样 CI 一直绿灯，
规则形同不存在。所以除了验证当前代码干净，还要验证它真能抓到违规。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_no_emoji.py"


def run_checker(cwd=None):
    return subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=str(cwd or ROOT), capture_output=True, text=True,
    )


def test_current_codebase_is_clean():
    """当前代码必须 0 违规。也确认门禁没有误报。"""
    r = run_checker()
    assert r.returncode == 0, f"门禁报告违规：\n{r.stdout}\n{r.stderr}"
    assert "0 违规" in r.stdout


def test_checker_detects_emoji_in_frontend(tmp_path, monkeypatch):
    """注入 emoji 后必须 exit 1。

    钉住「门禁不能是永久绿灯」这条：脚本若被改成无条件返回 0，本用例会失败。
    """
    target = ROOT / "static" / "index.html"
    original = target.read_bytes()
    try:
        target.write_bytes(original + '<div class="i">\U0001F389</div>\n'.encode())
        r = run_checker()
        assert r.returncode == 1, f"注入 emoji 后门禁仍通过：\n{r.stdout}"
        assert "U+1F389" in r.stdout
    finally:
        target.write_bytes(original)

    # 还原后必须恢复绿灯，确认上面的写入没有残留副作用
    assert run_checker().returncode == 0


def test_list_mode_never_fails():
    """--list 模式只列出不阻塞，便于本地排查。"""
    r = subprocess.run(
        [sys.executable, str(CHECKER), "--list"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0


def test_scripts_have_no_plaintext_admin_cred():
    """scripts/ 不得出现明文管理员凭证。

    CI 一旦生效，首次 push 就会把 scripts/ 推到公开仓库。这是回归防线：
    曾经 7 个脚本硬编码了真实管理员账密。
    """
    leaked = []
    for f in (ROOT / "scripts").glob("*.py"):
        text = f.read_text("utf-8")
        if "chatfirechatfire" in text:
            leaked.append(f.name)
    assert not leaked, f"以下脚本仍含明文管理员密码：{leaked}"


def test_scripts_have_no_hardcoded_abs_path():
    """scripts/ 不得硬编码开发机绝对路径 —— CI runner 上不存在该路径。"""
    bad = []
    for f in (ROOT / "scripts").glob("*.py"):
        if "/Users/betterme" in f.read_text("utf-8"):
            bad.append(f.name)
    assert not bad, f"以下脚本仍含硬编码绝对路径：{bad}"
