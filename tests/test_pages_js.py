"""网页里的脚本有没有语法错误（有一处错整页都不动）。机器上没装 node 就跳过。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["aigroup/web/page.html"]


@pytest.mark.parametrize("rel", PAGES)
def test_page_js_syntax(rel, tmp_path):
    if not shutil.which("node"):
        pytest.skip("没装 node")
    html = (ROOT / rel).read_text(encoding="utf-8")
    for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        f = tmp_path / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:500]
