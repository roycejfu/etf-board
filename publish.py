#!/usr/bin/env python3
"""重点ETF资金看板 · 发布脚本

1. 合并 data/board.json + insight.json -> board_data.js（最新）与 archive/<date>.js（历史快照）
2. 重建 history.js（历史列表，供页面下拉切换）
3. git commit + push 到 GitHub（main 与 gh-pages）
4. 可选 --push：经 ClawBot 推送微信摘要

用法：
  python3 publish.py                 # 合并 + 归档 + 推送 GitHub
  python3 publish.py --push          # 另外推送微信
  python3 publish.py --no-git        # 仅本地生成
"""
import json, os, subprocess, sys, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCH = os.path.join(ROOT, "archive")
os.makedirs(ARCH, exist_ok=True)
SITE_URL = "https://roycejfu.github.io/etf-board/"
PUSH_SH = os.path.expanduser("~/.workbuddy/skills/weixinclaw-proactive-push/push_wechat.sh")
GIT_ID = ["-c", "user.email=" + os.environ.get("GIT_AUTHOR_EMAIL", "roycefu@tencent.com"),
          "-c", "user.name=" + os.environ.get("GIT_AUTHOR_NAME", "roycejfu")]


def die(msg):
    print("[publish] FAILED:", msg); sys.exit(1)


def main():
    board = json.load(open(os.path.join(ROOT, "data", "board.json")))
    day = board["facts"].get("last_trade_day") or board["daily"]["dates"][-1]
    ip = os.path.join(ROOT, "insight.json")
    if not os.path.exists(ip):
        die("缺少 insight.json")
    ins = json.load(open(ip))
    if ins.get("date") != day:
        die(f"insight.json 日期 {ins.get('date')} 与数据日 {day} 不一致，请先撰写当日观察与结论")
    board["insight"] = ins
    board["snapshot_date"] = day
    js = "window.BOARD=" + json.dumps(board, ensure_ascii=False, separators=(",", ":")) + ";"
    open(os.path.join(ROOT, "board_data.js"), "w").write(js)
    open(os.path.join(ARCH, day + ".js"), "w").write(js)
    json.dump(ins, open(os.path.join(ARCH, day + ".insight.json"), "w"), ensure_ascii=False, indent=1)

    hist = []
    for p in sorted(glob.glob(os.path.join(ARCH, "*.insight.json")), reverse=True):
        x = json.load(open(p))
        hist.append({"date": x["date"], "stance": x.get("stance", ""), "headline": x.get("headline", "")})
    open(os.path.join(ROOT, "history.js"), "w").write(
        "window.HISTORY=" + json.dumps(hist, ensure_ascii=False, separators=(",", ":")) + ";")
    print(f"[publish] 本地生成完成：{day}，历史 {len(hist)} 期")

    if "--no-git" not in sys.argv:
        g = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
        g("add", "-A")
        r = subprocess.run(["git", *GIT_ID, "commit", "-q", "-m", f"ETF资金看板 {day}"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode and "nothing to commit" not in (r.stdout + r.stderr):
            die("commit 失败: " + r.stderr)
        for ref in ("main", "main:gh-pages"):
            r = g("push", "-q", "origin", ref, "--force" if ref.endswith("gh-pages") else "-q")
            if r.returncode:
                die(f"push {ref} 失败: " + r.stderr)
        st = g("status", "--porcelain").stdout.strip()
        if st:
            die("仍有未提交文件:\n" + st)
        print("[publish] GitHub 推送完成 →", SITE_URL)

    if "--push" in sys.argv:
        f = board["facts"]
        lines = [f"【ETF资金看板】{day}", "━━━━━━━━━━━━", ins["headline"], "━━━━━━━━━━━━"]
        lines.append(f"20只成交 {f['amt20_last']:.0f}亿（20日均 {f['amt20_avg20']:.0f}亿）｜上证 {f['sh_last']:.2f}（{f['sh_chg_pct']:+.2f}%）")
        lines.append(f"T-1（{f['share_day'][5:]}）20只净流入 {f['in20_ld']:+.1f}亿｜全市场宽基 {f['broad_ld_total']:+.1f}亿")
        lines.append(f"近5日 {f['in20_5d']:+.0f}亿｜近20日 {f['in20_20d']:+.0f}亿｜连续净流入 {f['inflow_streak_days']} 天")
        lines.append("━━━━━━━━━━━━")
        lines.append("【结论】" + ins["conclusion"])
        if ins.get("watch"):
            lines.append("━━━━━━━━━━━━\n【明日观察】")
            lines += [f"{i + 1}. {w}" for i, w in enumerate(ins["watch"])]
        lines.append("━━━━━━━━━━━━")
        lines.append("完整看板：" + SITE_URL)
        text = "\n".join(lines)
        if len(text) > 1500:
            text = text[:1440] + "…\n完整看板：" + SITE_URL
        r = subprocess.run([PUSH_SH, text], capture_output=True, text=True)
        out = r.stdout + r.stderr
        print(out.strip())
        if "[weixinclaw] OK" not in out:
            die("微信推送未成功（未见 [weixinclaw] OK）")


if __name__ == "__main__":
    main()
