# -*- coding: utf-8 -*-
"""
全球垃圾焚烧发电(Waste-to-Energy / 焚烧)行业日报
-------------------------------------------------
流程:
  1. 从 Google News RSS 按关键词抓取全球最新报道(免费,无需密钥)
  2. 调用 AI 接口,把英文/外文报道筛选 + 翻译 + 摘要成中文
  3. 生成一个排版干净的中文网页 index.html

依赖:feedparser, requests   (见 requirements.txt)
配置:通过环境变量(在 GitHub Secrets 里设置)
  LLM_API_KEY   —— 必填,你的 AI 接口密钥
  LLM_BASE_URL  —— 可选,默认 DeepSeek;也可换成其他 OpenAI 兼容接口
  LLM_MODEL     —— 可选,默认 deepseek-chat
"""

import os
import re
import json
import html
import datetime
import urllib.parse

# requests / feedparser 在使用时再导入(便于离线预览样式)

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# 抓取关键词。多语言、多角度,尽量覆盖全球。可自行增删。
QUERIES = [
    "waste-to-energy plant",
    "energy from waste incineration",
    "waste incineration power plant project",
    "municipal solid waste incineration",
    "garbage incineration power",
    "垃圾焚烧发电",
    "垃圾焚烧 项目",
]

# 每个关键词最多取多少条原始结果
MAX_PER_QUERY = 8
# 最终页面最多展示多少条
MAX_ITEMS = 25
# 只保留最近多少天的新闻
RECENT_DAYS = 3


# ----------------------------------------------------------------------------
# 第一步:抓取原始新闻
# ----------------------------------------------------------------------------
def google_news_rss_url(query: str) -> str:
    q = urllib.parse.quote(query)
    # hl=zh-CN 让 Google 倾向返回带中文界面的结果;ceid 覆盖全球
    return f"https://news.google.com/rss/search?q={q}+when:7d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)          # 去掉 HTML 标签
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fetch_raw_items():
    """返回去重后的原始新闻列表。每条:{title, link, source, published, snippet}"""
    import feedparser
    seen_titles = set()
    items = []
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=RECENT_DAYS)

    for q in QUERIES:
        url = google_news_rss_url(q)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[警告] 抓取失败 query={q}: {e}")
            continue

        for entry in feed.entries[:MAX_PER_QUERY]:
            title = clean_text(getattr(entry, "title", ""))
            if not title:
                continue
            # 简单去重(标题前 40 个字符相同视为重复)
            key = title[:40].lower()
            if key in seen_titles:
                continue

            # 时间过滤
            pub_dt = None
            if getattr(entry, "published_parsed", None):
                try:
                    pub_dt = datetime.datetime(*entry.published_parsed[:6])
                except Exception:
                    pub_dt = None
            if pub_dt and pub_dt < cutoff:
                continue

            source = ""
            if getattr(entry, "source", None) and getattr(entry.source, "title", None):
                source = clean_text(entry.source.title)
            published = ""
            if pub_dt:
                published = pub_dt.strftime("%Y-%m-%d")

            seen_titles.add(key)
            items.append({
                "title": title,
                "link": getattr(entry, "link", ""),
                "source": source,
                "published": published,
                "snippet": clean_text(getattr(entry, "summary", ""))[:300],
            })

    print(f"[信息] 共抓取到 {len(items)} 条原始新闻")
    return items


# ----------------------------------------------------------------------------
# 第二步:调用 AI 做筛选 + 翻译 + 摘要
# ----------------------------------------------------------------------------
PROMPT_SYSTEM = (
    "你是一名能源环保行业的资深编辑,专门追踪全球垃圾焚烧发电(Waste-to-Energy)"
    "领域的项目动态。你的任务是从一批原始新闻中筛选出真正与垃圾焚烧发电项目相关的报道"
    "(包括新建/中标/投产/扩建/技术/政策/招标等),剔除无关内容,并把它们整理成"
    "简洁、专业、准确的中文。"
)

PROMPT_USER_TEMPLATE = """下面是今天抓取到的原始新闻(JSON 数组),字段含义:
title 标题、source 来源媒体、published 日期、link 链接、snippet 摘要片段。

请你:
1. 只保留与「垃圾焚烧发电 / 生活垃圾焚烧 / waste-to-energy」项目真正相关的条目,剔除无关的。
2. 把标题翻译成准确、专业的中文(若已是中文则润色)。
3. 用 1-2 句中文写出该新闻的核心内容摘要。
4. 标注所属国家或地区(region 字段,中文,如「中国」「英国」「东南亚」等;无法判断填「全球」)。
5. 判断该新闻属于「国内」还是「国际」(scope 字段:涉及中国大陆的填「国内」,其余填「国际」)。
6. 保留原始的 source、published、link 不变。

严格只输出一个 JSON 数组,不要任何多余文字、不要 markdown 代码块。
每个元素格式:
{{"title_zh": "...", "summary_zh": "...", "region": "...", "scope": "国内或国际", "source": "...", "published": "...", "link": "..."}}
最多保留 {max_items} 条,按重要性和时间从新到旧排序。

原始新闻:
{raw_json}
"""


def call_llm(raw_items):
    """调用 AI 接口。失败时抛出异常,由上层兜底。"""
    if not LLM_API_KEY:
        raise RuntimeError("未设置 LLM_API_KEY 环境变量")

    import requests

    user_prompt = PROMPT_USER_TEMPLATE.format(
        max_items=MAX_ITEMS,
        raw_json=json.dumps(raw_items, ensure_ascii=False),
    )

    resp = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    # 去掉可能的 ```json ``` 包裹
    content = content.strip()
    content = re.sub(r"^```(json)?", "", content).strip()
    content = re.sub(r"```$", "", content).strip()

    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("AI 返回的不是 JSON 数组")
    print(f"[信息] AI 整理后保留 {len(data)} 条")
    return data


def fallback_items(raw_items):
    """AI 调用失败时的兜底:直接用原始标题,保证页面不崩。"""
    out = []
    for it in raw_items[:MAX_ITEMS]:
        title = it["title"]
        scope = "国内" if any(k in title for k in ["中国", "中标", "国内", "省", "市垃圾"]) else "国际"
        out.append({
            "title_zh": title,
            "summary_zh": it.get("snippet", ""),
            "region": "全球",
            "scope": scope,
            "source": it.get("source", ""),
            "published": it.get("published", ""),
            "link": it.get("link", ""),
        })
    return out


# ----------------------------------------------------------------------------
# 第三步:渲染网页
# ----------------------------------------------------------------------------
DOMESTIC_KEYS = ("中国", "国内", "大陆", "中国大陆", "内地")


def is_domestic(it) -> bool:
    """优先用 AI 返回的 scope 字段;没有则按 region 关键词判断。"""
    if isinstance(it, dict):
        scope = (it.get("scope") or "").strip()
        if scope:
            return scope == "国内"
        region = it.get("region", "") or ""
    else:
        region = it or ""
    return any(k in region for k in DOMESTIC_KEYS)


def _card(it):
    title = html.escape(it.get("title_zh", ""))
    summary = html.escape(it.get("summary_zh", ""))
    region = html.escape(it.get("region", "") or "全球")
    source = html.escape(it.get("source", "") or "")
    published = html.escape(it.get("published", "") or "")
    link = html.escape(it.get("link", "") or "#")
    scope = "domestic" if is_domestic(it) else "intl"

    meta_bits = []
    if source:
        meta_bits.append(f'<span class="src">{source}</span>')
    if published:
        meta_bits.append(f'<span class="date">{published}</span>')
    meta = '<span class="dot">·</span>'.join(meta_bits)

    return f"""
        <article class="card {scope}" data-scope="{scope}">
          <div class="card-head">
            <span class="tag">{region}</span>
            <div class="meta">{meta}</div>
          </div>
          <h2 class="title"><a href="{link}" target="_blank" rel="noopener">{title}</a></h2>
          <p class="summary">{summary}</p>
          <a class="source-link" href="{link}" target="_blank" rel="noopener">查看原文 →</a>
        </article>"""


def _section(label_cn, label_en, scope, items):
    if not items:
        body = '<div class="empty">本栏目暂无新内容。</div>'
    else:
        body = "\n".join(_card(it) for it in items)
    return f"""
      <section class="block" data-block="{scope}">
        <div class="block-head">
          <span class="block-bar {scope}"></span>
          <div>
            <h2 class="block-title">{label_cn}<span class="block-count">{len(items)}</span></h2>
            <div class="block-sub">{label_en}</div>
          </div>
        </div>
        <div class="feed">
          {body}
        </div>
      </section>"""


def render_html(items, generated_at, degraded=False):
    domestic = [it for it in items if is_domestic(it)]
    intl = [it for it in items if not is_domestic(it)]
    count, n_dom, n_intl = len(items), len(domestic), len(intl)

    sections = (
        _section("国内动态", "Domestic", "domestic", domestic)
        + _section("国际动态", "International", "intl", intl)
    )
    if not items:
        sections = '<div class="empty">今日暂未抓取到相关新闻,请稍后再看。</div>'

    degraded_banner = ""
    if degraded:
        degraded_banner = (
            '<div class="banner">提示:今日 AI 整理服务暂时不可用,'
            '以下为原始抓取标题,未经翻译润色。</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>全球垃圾焚烧发电行业日报</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@500;700;900&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1a2733;
    --paper: #f4f7fb;
    --card: #ffffff;
    --accent: #2f6fb0;       /* 国内 主蓝 */
    --accent2: #4a99c9;      /* 国际 浅蓝/青 */
    --accent-soft: #e6eef7;
    --muted: #7a8794;
    --line: #e2e8f0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Noto Sans SC", system-ui, sans-serif;
    color: var(--ink);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
    background:
      radial-gradient(900px 500px at 85% -8%, rgba(47,111,176,.10), transparent 60%),
      radial-gradient(700px 460px at -5% 4%, rgba(74,153,201,.08), transparent 55%),
      var(--paper);
    min-height: 100vh;
  }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 0 22px 90px; }}

  /* ---------- 报头 ---------- */
  header.masthead {{
    position: relative;
    margin: 26px 0 0;
    padding: 44px 34px 36px;
    border-radius: 20px;
    color: #f3f1e8;
    overflow: hidden;
    background:
      linear-gradient(135deg, #2f6fb0 0%, #3f86c4 55%, #4fa0cf 100%);
    box-shadow: 0 22px 48px -22px rgba(47,111,176,.5);
  }}
  header.masthead::after {{
    content: ""; position: absolute; inset: 0;
    background-image: radial-gradient(rgba(255,255,255,.06) 1px, transparent 1px);
    background-size: 16px 16px; opacity: .5; pointer-events: none;
  }}
  .kicker {{
    font-size: 12px; letter-spacing: 4px; color: #cfe6f5;
    text-transform: uppercase; font-weight: 500; margin-bottom: 14px;
  }}
  h1.brand {{
    font-family: "Noto Serif SC", serif; font-weight: 900;
    font-size: clamp(28px, 6vw, 44px); letter-spacing: 1px; line-height: 1.16;
  }}
  .lead {{ margin-top: 12px; color: #e3eff7; font-size: 14.5px; font-weight: 300; max-width: 46ch; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 26px; margin-top: 26px; }}
  .stat .num {{ font-family: "Noto Serif SC", serif; font-size: 30px; font-weight: 700; line-height: 1; }}
  .stat .lab {{ font-size: 12.5px; color: #cfe2f0; margin-top: 6px; letter-spacing: 1px; }}
  .updated {{ position: absolute; top: 22px; right: 26px; font-size: 12px; color: #cfe2f0; }}

  /* ---------- 筛选标签 ---------- */
  .tabs {{ display: flex; gap: 10px; margin: 28px 0 6px; flex-wrap: wrap; }}
  .tab {{
    border: 1px solid var(--line); background: #fff; color: var(--ink);
    padding: 8px 18px; border-radius: 999px; font-size: 14px; cursor: pointer;
    font-family: inherit; transition: all .15s ease;
  }}
  .tab:hover {{ border-color: var(--accent); }}
  .tab.active {{ background: var(--ink); color: #fff; border-color: var(--ink); }}

  .banner {{
    background: #fff4e2; border: 1px solid #f0d9a8; color: #8a5a14;
    padding: 12px 16px; border-radius: 10px; font-size: 14px; margin: 22px 0 0;
  }}

  /* ---------- 分区 ---------- */
  .block {{ margin-top: 38px; }}
  .block-head {{ display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }}
  .block-bar {{ width: 5px; height: 38px; border-radius: 4px; display: inline-block; }}
  .block-bar.domestic {{ background: var(--accent); }}
  .block-bar.intl {{ background: var(--accent2); }}
  .block-title {{
    font-family: "Noto Serif SC", serif; font-size: 23px; font-weight: 700;
    display: flex; align-items: center; gap: 10px;
  }}
  .block-count {{
    font-family: "Noto Sans SC"; font-size: 13px; font-weight: 500; color: var(--muted);
    background: #fff; border: 1px solid var(--line); border-radius: 999px; padding: 1px 10px;
  }}
  .block-sub {{ font-size: 12px; letter-spacing: 3px; color: var(--muted); text-transform: uppercase; }}

  .feed {{ display: flex; flex-direction: column; gap: 18px; }}

  /* ---------- 卡片 ---------- */
  .card {{
    background: var(--card); border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: 14px; padding: 20px 24px;
    transition: transform .18s ease, box-shadow .18s ease;
  }}
  .card.intl {{ border-left-color: var(--accent2); }}
  .card:hover {{ transform: translateY(-2px); box-shadow: 0 14px 34px -16px rgba(20,48,40,.4); }}
  .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 9px; }}
  .tag {{ background: var(--accent); color: #fff; font-size: 12px; font-weight: 500; padding: 3px 11px; border-radius: 6px; white-space: nowrap; }}
  .card.intl .tag {{ background: var(--accent2); }}
  .meta {{ font-size: 13px; color: var(--muted); }}
  .meta .dot {{ margin: 0 7px; opacity: .5; }}
  h2.title {{ font-family: "Noto Serif SC", serif; font-size: 20px; font-weight: 700; line-height: 1.45; margin-bottom: 8px; }}
  h2.title a {{ color: var(--ink); text-decoration: none; }}
  h2.title a:hover {{ color: var(--accent); }}
  .summary {{ color: #41504a; font-size: 15px; font-weight: 300; }}
  .source-link {{ display: inline-block; margin-top: 12px; font-size: 14px; color: var(--accent); text-decoration: none; font-weight: 500; }}
  .card.intl .source-link {{ color: var(--accent2); }}
  .source-link:hover {{ text-decoration: underline; }}

  .empty {{ padding: 40px 0; text-align: center; color: var(--muted); }}

  footer {{ margin-top: 56px; padding-top: 22px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; line-height: 1.9; }}
  footer .big {{ color: var(--ink); font-weight: 500; }}

  .hidden {{ display: none !important; }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <div class="updated">更新时间 {generated_at}</div>
      <div class="kicker">Global Waste-to-Energy Daily</div>
      <h1 class="brand">全球垃圾焚烧发电行业日报</h1>
      <p class="lead">每日自动汇集全球生活垃圾焚烧发电领域的项目、政策与技术动态,经 AI 翻译整理为中文。</p>
      <div class="stats">
        <div class="stat"><div class="num">{count}</div><div class="lab">本期总数</div></div>
        <div class="stat"><div class="num">{n_dom}</div><div class="lab">国内</div></div>
        <div class="stat"><div class="num">{n_intl}</div><div class="lab">国际</div></div>
      </div>
    </header>
    {degraded_banner}

    <div class="tabs">
      <button class="tab active" data-filter="all">全部</button>
      <button class="tab" data-filter="domestic">国内</button>
      <button class="tab" data-filter="intl">国际</button>
    </div>

    <main>
      {sections}
    </main>

    <footer>
      <div class="big">数据来源:Google News 聚合 · AI 自动翻译整理</div>
      所有内容均来自公开新闻报道,点击各条「查看原文」可跳转至原始来源核实。<br>
      本页由自动化程序每日生成,仅供行业信息参考。
    </footer>
  </div>

  <script>
    const tabs = document.querySelectorAll('.tab');
    const blocks = document.querySelectorAll('.block');
    tabs.forEach(t => t.addEventListener('click', () => {{
      tabs.forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      const f = t.dataset.filter;
      blocks.forEach(b => {{
        b.classList.toggle('hidden', f !== 'all' && b.dataset.block !== f);
      }});
    }}));
  </script>
</body>
</html>"""


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)  # 北京时间
    generated_at = now.strftime("%Y-%m-%d %H:%M")

    raw = fetch_raw_items()

    degraded = False
    if not raw:
        items = []
    else:
        try:
            items = call_llm(raw)
        except Exception as e:
            print(f"[警告] AI 调用失败,启用兜底方案: {e}")
            items = fallback_items(raw)
            degraded = True

    html_out = render_html(items, generated_at, degraded=degraded)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"[完成] 已生成 index.html(更新时间 {generated_at})")


if __name__ == "__main__":
    main()
