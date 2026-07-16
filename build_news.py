# -*- coding: utf-8 -*-
"""小宝专属每日要闻 —— 数据刷新构建脚本（方案 A：以 GitHub 仓库为唯一生成源）。

设计原则（用户要求）：
- 不改动 index.html 的页面模块 / 渲染逻辑 / 样式，只替换其中的 ORIGINAL 数据；
- 数据源抓取方式参考 gen_daily.py：36氪 RSS、中国新闻网 RSS、知乎热榜(readep.com)、
  百度热搜 / 微博热搜 / 夸克24小时热点（各站官方接口），
  纯标准库实现，无需任何 API Key，可在 GitHub Actions 无头环境直接运行。
  注：Bing 新闻 RSS 已于全球范围失效（返回 HTML 而非 RSS）；Google News 国内无梯子打不开，
  故「今日热点」等新闻类板块统一改用 36氪 + 中国新闻网（链接国内可直接打开、实时）。

流程：读取 index.html（页面外壳 + 原数据）→ 实时抓取刷新「新闻类」板块 →
常青 / 收藏类板块保留原精选内容 → 回写 ORIGINAL 与日期并覆盖 index.html。
"""
import json, os, re, html, datetime, email.utils, urllib.request, urllib.parse

# 中国标准时间 = UTC+8（不实行夏令时，固定偏移即可；确保 GitHub Actions(UTC) 与本地都显示北京时间）
_BEIJING = datetime.timezone(datetime.timedelta(hours=8))
_NOW = datetime.datetime.now(_BEIJING)
TODAY = _NOW.strftime("%Y-%m-%d")
SNAP = _NOW.strftime("%Y年%m月%d日")
WK = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][_NOW.weekday()]
UPDATED = _NOW.strftime("%Y年%m月%d日 %H:%M")
_DT = _NOW.date()

# ===================== 抓取工具（参考 gen_daily.py，去掉公司代理，Actions 直连） =====================
def _fop():
    # 原 gen_daily.py 走公司代理 http://10.255.243.177:3128；GitHub Actions 无头环境直连公网即可
    return urllib.request.build_opener()

def _fget(url, timeout=25, tries=2, extra_headers=None):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                    **(extra_headers or {}),
                },
            )
            with _fop().open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
    return ""

def _fjget(url, timeout=25, tries=2, extra_headers=None):
    """抓取并解析 JSON；失败（含解析异常）返回 None。"""
    raw = _fget(url, timeout=timeout, tries=tries, extra_headers=extra_headers)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def _clean(s):
    if not s:
        return ""
    # 解开 CDATA 包裹（否则整个 <![CDATA[...]]> 会被当成标签删空，导致 36氪等 link 丢失）
    _s = "<![CDATA["; _e = "]]>"
    i = s.find(_s)
    while i != -1:
        j = s.find(_e, i + len(_s))
        if j != -1:
            s = s[:i] + s[i + len(_s):j] + s[j + len(_e):]
            i = s.find(_s, i)
        else:
            break
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()

def _pdate(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d:
            if d.tzinfo:
                d = d.astimezone().replace(tzinfo=None)
            return d.date()
    except Exception:
        pass
    for _f in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, _f).date()
        except Exception:
            pass
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s or "")
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None

def _rss_items(url, max_n=40, src="新闻"):
    try:
        raw = _fget(url)
    except Exception:
        return []
    out = []
    for blk in (re.findall(r"<item[ >].*?</item>", raw, re.S) or re.findall(r"<entry[ >].*?</entry>", raw, re.S)):
        t = re.search(r"<title[ >](.*?)</title>", blk, re.S)
        pu = re.search(r"<pubDate[ >](.*?)</pubDate>", blk, re.S) or re.search(r"<updated[ >](.*?)</updated>", blk, re.S)
        if not pu:
            pu = re.search(r"</link>\s*([A-Z][a-z]{2},\s*\d{1,2}-[A-Z][a-z]{2}-\d{4}[^<]*)", blk)
        desc = re.search(r"<description[ >](.*?)</description>", blk, re.S) or re.search(r"<summary[ >](.*?)</summary>", blk, re.S)
        ti = _clean(t.group(1)) if t else ""
        if not ti:
            la = re.search(r'<link[^>]*href="([^"]+)"', blk); ti = _clean(la.group(0)) if la else ""
        if not ti:
            continue
        lk1 = re.search(r"<link[ >](.*?)</link>", blk, re.S); lk = _clean(lk1.group(1)) if lk1 else ""
        if not lk:
            la = re.search(r'<link[^>]*href="([^"]+)"', blk); lk = la.group(1) if la else ""
        # Bing 新闻聚合链接中藏有真实原文（url= 参数，URL 编码），解出后直达原文站而非 Bing 聚合页
        if "bing.com/news/apiclick" in lk:
            try:
                _q = urllib.parse.urlparse(lk).query
                _real = urllib.parse.parse_qs(_q).get("url", [""])[0]
                if _real:
                    lk = urllib.parse.unquote(_real)
            except Exception:
                pass
        pu_s = _clean(pu.group(1)) if pu else ""
        ds = _clean(desc.group(1)) if desc else ""
        out.append({"title": ti, "url": lk, "date": _pdate(pu_s), "summary": ds[:150], "src": src})
        if len(out) >= max_n:
            break
    return out

# 新闻类板块的实时数据源（国内可直接打开、且为当天实时更新）。
# Bing 新闻 RSS 已全球失效、Google News 国内无梯子打不开，故采用以下国内源替代：
#   36氪（科技/创投/财经）、中国新闻网滚动、中国新闻网·财经。
_FEEDS = [
    ("36氪", "https://36kr.com/feed", 30),
    ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml", 30),
    ("中国新闻网·财经", "https://www.chinanews.com.cn/rss/finance.xml", 30),
]
_FRESH_DAYS = 7  # 新鲜度过滤：丢弃超过该天数的陈旧条目（防止误接入停服/陈旧源时混入旧闻）
_POOL = None
CLAIMED = set()   # 跨板块去重：已分配给某板块的新闻键集合，每次 refresh 重置
def _build_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    raw = []
    for src, url, n in _FEEDS:
        try:
            for it in _rss_items(url, max_n=n, src=src):
                if it["date"] is None:
                    it["date"] = _DT   # 实时源解析失败按今日计
                # 新鲜度过滤：丢弃超过 _FRESH_DAYS 天的陈旧条目（避免接入到停服/陈旧源时混入旧闻）
                if it["date"] and (_DT - it["date"]).days > _FRESH_DAYS:
                    continue
                raw.append(it)
        except Exception:
            pass
    seen = set(); uniq = []
    for it in raw:
        norm = re.sub(r"[\s｜|/【】\[\]()（）·•.。,，:：!！?？\"']", "", it["title"])
        k = norm[:18]
        if k in seen:
            continue
        seen.add(k); uniq.append(it)
    uniq.sort(key=lambda x: x["date"] or datetime.date(2000, 1, 1), reverse=True)
    _POOL = uniq
    return uniq

def _route(pool, kw):
    if not kw:
        return pool
    out = [i for i in pool if any(k in (i["title"] + i["summary"]) for k in kw)]
    return out if out else pool

def _claim_key(it):
    """跨板块去重键：优先用归一化标题前 18 字，无标题则退化为 URL。"""
    norm = re.sub(r"[\s｜|/【】\[\]()（）·•.。,，:：!！?？\"']", "", it.get("title", "") or "")
    return ("t:" + norm[:18]) if norm else ("u:" + (it.get("url", "") or ""))

# ===================== 知乎热榜实时抓取（readep.com/zhihu） =====================
def _get_zhihu():
    raw = _fget("https://readep.com/zhihu", timeout=30)
    if not raw:
        return []
    out = []
    for m in re.finditer(r'<a[^>]*href="(https?://(?:www[.])?zhihu[.]com/question/[0-9]+)"[^>]*>(.*?)</a>', raw, re.S):
        url = m.group(1); raw_title = _clean(m.group(2))
        if not raw_title or len(raw_title) < 4:
            continue
        mm = re.match(r'^([0-9]{1,3})[ ]*([0-9]{4}-[0-9]{2}-[0-9]{2})[ ]*(.+)$', raw_title)
        if mm:
            rank = mm.group(1); date = mm.group(2); rest = mm.group(3)
        else:
            rank = ""; date = "今日"; rest = raw_title
        hm = re.search(r'([0-9.]+w[+]?)[ ]*$', rest)
        heat = hm.group(1) if hm else ""
        if hm:
            rest = rest[:hm.start()].strip()
        out.append({"title": rest.strip(), "url": url, "date": date, "heat": heat})
    seen = set(); uniq = []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    return uniq[:10]

# ===================== 公开热榜实时抓取（百度 / 微博，均免 API Key） =====================
def _compact(s):
    """折叠异常空白（热点站标题偶有多余空格），便于展示与去重键归一化。"""
    return re.sub(r"\s+", " ", s or "").strip()

def _get_baidu_hot(max_n=30):
    """百度实时热搜：top.baidu.com/api/board，结构 data.cards[0].content[0].content。"""
    d = _fjget("https://top.baidu.com/api/board?platform=wise&tab=realtime", timeout=30)
    if not d:
        return []
    try:
        content = d["data"]["cards"][0]["content"][0]["content"]
    except Exception:
        return []
    out = []
    for c in content:
        word = _compact(c.get("word"))
        if not word:
            continue
        desc = _compact(c.get("desc"))
        out.append({
            "title": word,
            "url": c.get("url") or "https://top.baidu.com/board",
            "summary": (desc[:80] if desc else "百度实时热搜"),
            "src": "百度热搜", "date": "今日",
        })
        if len(out) >= max_n:
            break
    return out

def _get_weibo_hot(max_n=30):
    """微博热搜：weibo.com/ajax/side/hotSearch（需 Referer，否则 403）。"""
    d = _fjget(
        "https://weibo.com/ajax/side/hotSearch", timeout=30,
        extra_headers={"Referer": "https://weibo.com/", "X-Requested-With": "XMLHttpRequest"},
    )
    if not d:
        return []
    rt = (d.get("data") or {}).get("realtime") or []
    out = []
    for x in rt:
        word = _compact(x.get("word"))
        if not word:
            continue
        url = "https://s.weibo.com/weibo?q=%s" % urllib.parse.quote("#" + word + "#")
        out.append({
            "title": word,
            "url": url,
            "summary": "微博热搜",
            "src": "微博热搜", "date": "今日",
        })
        if len(out) >= max_n:
            break
    return out

def _get_quark_hot(max_n=50):
    """夸克24小时热点：iflow.quark.cn 官方聚合接口（aggregation_id 固定，稳定可用，国内可直接打开）。"""
    u = ("https://iflow.quark.cn/iflow/api/v1/article/aggregation"
         "?aggregation_id=16665090098771297825&count=50&bottom_pos=0")
    d = _fjget(
        u, timeout=30,
        extra_headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
    )
    if not d:
        return []
    out = []
    for a in (d.get("data") or {}).get("articles") or []:
        title = _clean(a.get("title"))
        if not title:
            continue
        aid = a.get("id") or ""
        url = ("https://123.quark.cn/detail?item_id=%s" % aid) if aid else "https://123.quark.cn"
        summary = _clean(a.get("summary")) or "夸克24小时热点"
        out.append({
            "title": title,
            "url": url,
            "summary": summary[:80],
            "src": "夸克热点", "date": "今日",
        })
        if len(out) >= max_n:
            break
    return out

# ===================== 板块构建（按当前 index.html 的板块标签映射） =====================
def _san(s):
    """清洗会破坏 JS 字符串字面量的字符：U+2028/U+2029、零宽字符、全部控制字符（含换行/制表/回车）。"""
    if not isinstance(s, str):
        return s
    s = s.replace("\u2028", " ").replace("\u2029", " ").replace("\u200b", " ")
    s = re.sub(r"[\x00-\x1f]", "", s)
    return s.strip()

def _md(d):
    return ("%d月%d日" % (d.month, d.day)) if d else "今日"

def _item(title, date, src, url, summary, is_new=True):
    return {"title": title, "date": date, "src": src, "url": url, "sum": summary, "is_new": is_new}

def _live_pool_items(kws, n):
    """按关键词从新闻池取 n 条，is_new=True，date=抓取日；跨板块去重（CLAIMED）。
    不足用全池近期补齐，同样跳过已认领项。"""
    pool = _build_pool()
    routed = _route(pool, kws)
    out = []
    for it in routed:
        k = _claim_key(it)
        if k in CLAIMED:
            continue
        CLAIMED.add(k)
        out.append(_item(it["title"], _md(it["date"]), it["src"], it["url"], it["summary"] or "点击查看详情", True))
        if len(out) >= n:
            break
    if len(out) < n:
        for it in pool:
            if any(o["title"] == it["title"] for o in out):
                continue
            k = _claim_key(it)
            if k in CLAIMED:
                continue
            CLAIMED.add(k)
            out.append(_item(it["title"], _md(it["date"]), it["src"], it["url"], it["summary"] or "点击查看详情", True))
            if len(out) >= n:
                break
    return out

def _live_zhihu(n=10):
    try:
        rows = _get_zhihu()
    except Exception:
        rows = []
    out = []
    for r in rows[:n]:
        k = _claim_key({"title": r["title"], "url": r["url"]})
        if k in CLAIMED:
            continue
        CLAIMED.add(k)
        heat = r.get("heat")
        if heat:
            summary = "知乎热榜 %s 热度 · %s" % (heat, r["title"][:30])
        else:
            summary = "知乎热榜 · %s" % r["title"][:40]
        out.append(_item(r["title"], r.get("date") or "今日", "知乎热榜", r["url"], summary, True))
    return out

def _live_hot_list(fn, n):
    """通用热榜板块：调用抓取函数 fn() 取热榜列表，转成卡片并走 CLAIMED 跨板块去重，上限 n 条。
    用于百度热点 / 微博热搜等「单一热点站独立成板块」的场景。"""
    out = []
    for it in fn():
        k = _claim_key(it)
        if k in CLAIMED:
            continue
        CLAIMED.add(k)
        out.append(_item(it["title"], it.get("date") or "今日", it.get("src", "热搜"), it["url"], it.get("summary") or "点击查看详情", True))
        if len(out) >= n:
            break
    return out

# 哪些板块走实时抓取（key = index.html 中的板块 label，value = 抓取函数）
LIVE = {
    "今日热点速览": lambda: _live_pool_items(None, 50),
    "百度热点": lambda: _live_hot_list(_get_baidu_hot, 50),
    "微博热搜": lambda: _live_hot_list(_get_weibo_hot, 50),
    "夸克24小时热点": lambda: _live_hot_list(_get_quark_hot, 50),
    "知乎热榜": lambda: _live_zhihu(10),
    "企业动态与真实问题": lambda: _live_pool_items(
        ["企业", "业绩", "融资", "上市", "IPO", "财报", "公司", "股价", "市值", "营收", "净利", "ST", "立案", "退市"], 14),
    "宏观经济 · 经济与金融": lambda: _live_pool_items(
        ["经济", "GDP", "增长", "消费", "产业", "制造业", "外贸", "内需", "复苏", "国务院", "发改委", "政策", "财政",
         "规划", "改革", "法规", "央行", "货币", "金融", "利率", "信贷", "银行", "汇率", "债券", "保险", "证监会",
         "CPI", "PPI", "统计", "数据", "物价", "指数", "就业", "社融"], 12),
    "地方与院校要闻（东财 / 大连 / 北京 / 辽宁 / 山东）": lambda: _live_pool_items(
        ["辽宁", "大连", "北京", "山东", "东财", "沈阳", "暴雨", "防汛", "本溪", "德州", "哈尔滨"], 14),
}
MIN_LIVE = 3  # 抓取条数低于此值则回退到原精选内容（保证页面永不空）

# 板块展示顺序与成员（build_news.py 为顺序与成员的「唯一权威」，不依赖 index.html 当前顺序）：
# 取消「B站热门」；「百度热点」「微博热搜」移至列表末尾。不在下表且不属删除集的板块保持原序追加。
_SECTION_ORDER = [
    "今日热点速览",
    "知乎热榜",
    "企业动态与真实问题",
    "宏观经济 · 经济与金融",
    "高校 · 教育 · 学术与论文",
    "项目申报相关信息",
    "地方与院校要闻（东财 / 大连 / 北京 / 辽宁 / 山东）",
    "产业链 · 供应链 · 链长制",
    "微信收藏 · 小宝的最新收藏",
    "百度热点",
    "微博热搜",
    "夸克24小时热点",
]
_SECTION_REMOVED = {"B站热门"}  # 用户要求取消的板块

def _is_today(ds):
    if not ds:
        return False
    if re.search(r"\d{4}-\d{2}-\d{2}", ds or ""):
        return ds == TODAY
    m = re.search(r"(\d{1,2})月(\d{1,2})日", ds or "")
    return bool(m) and int(m.group(1)) == _NOW.month and int(m.group(2)) == _NOW.day

# ===================== 读取页面外壳 + 原数据 =====================
def read_template(path="index.html"):
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    m = re.search(r"const ORIGINAL = (\{[\s\S]*?\n\});", txt)
    if not m:
        raise RuntimeError("未在 index.html 中找到 ORIGINAL 数据块")
    raw = m.group(1)
    try:
        obj = json.loads(raw)
    except Exception:
        obj = json.loads(re.sub(r",(\s*[}\]])", r"\1", raw))  # 兜底：清除可能的尾逗号
    return txt, obj

def _clean_item(it):
    out = {}
    for k, v in it.items():
        out[k] = _san(v) if isinstance(v, str) else v
    return out

def refresh(obj):
    global CLAIMED
    CLAIMED = set()
    # 先算各 LIVE 板块内容；填充顺序：具体关键词板块优先，通用板块（今日热点速览）最后，
    # 这样每个板块都能拿到与自己关键词最匹配的内容，且全局互不重复。
    live_items = {}
    general = "今日热点速览"
    order = [l for l in LIVE if l != general] + ([general] if general in LIVE else [])
    for label in order:
        try:
            items = LIVE[label]()
        except Exception:
            items = []
        live_items[label] = items
    # 按权威顺序输出板块：剔除已取消板块，_SECTION_ORDER 内按表序，其余保持原序追加；
    # 并自动补齐 index.html 中尚缺的 LIVE 板块（自举：首次运行注入，之后正常刷新）。
    kept = [s for s in obj.get("sections", []) if s.get("label") not in _SECTION_REMOVED]
    ranked = sorted(
        kept,
        key=lambda s: _SECTION_ORDER.index(s["label"]) if s["label"] in _SECTION_ORDER else len(_SECTION_ORDER),
    )
    present = {s["label"] for s in ranked}
    for label in _SECTION_ORDER:
        if label in LIVE and label not in present:
            ranked.append({"label": label, "items": [], "weather": False})
            present.add(label)
    # 兜底：LIVE 中存在但未列入 _SECTION_ORDER 的板块，追加到末尾，避免遗漏
    for label in LIVE:
        if label not in present:
            ranked.append({"label": label, "items": [], "weather": False})
            present.add(label)
    sections = []
    for sec in ranked:
        label = sec.get("label", "")
        if label in live_items:
            items = live_items[label]
            if len(items) < MIN_LIVE:
                # 回退：保留原精选内容，但按当日重算 is_new（避免旧红点误导）
                items = [dict(_clean_item(it), is_new=_is_today(it.get("date"))) for it in sec.get("items", [])]
            else:
                items = [_clean_item(it) for it in items]
            sections.append({"label": label, "items": items, "weather": bool(sec.get("weather"))})
        else:
            # 常青 / 收藏类（高校·教育、项目申报、产业链·链长制、微信收藏）：原样保留，仅按当日重算 is_new
            items = [dict(_clean_item(it), is_new=_is_today(it.get("date"))) for it in sec.get("items", [])]
            sections.append({"label": label, "items": items, "weather": bool(sec.get("weather"))})
    new_count = sum(1 for s in sections for it in s["items"] if it.get("is_new"))
    return {
        "snap": SNAP, "wk": WK, "updated": UPDATED,
        "new_count": new_count,
        "love_days": obj.get("love_days", 618),
        "sections": sections,
        "sources": [_clean_item(s) for s in obj.get("sources", [])],
        "care": [_san(c) for c in obj.get("care", [])],
    }

def inject(txt, new_obj):
    # 注意：必须用「替换函数」而非替换字符串，否则 json.dumps 转义出的 \\n 会被 re.sub 当成换行转义，
    # 把转义换行还原成真实换行，从而破坏 JSON/JS。
    block = "const ORIGINAL = " + json.dumps(new_obj, ensure_ascii=False, indent=1) + ";"
    txt = re.sub(r"const ORIGINAL = \{[\s\S]*?\n\};", lambda m: block, txt, count=1)
    txt = re.sub(r'(const NEWS_DB = \{ ")[^"]+(":\s*ORIGINAL \};)',
                 lambda m: m.group(1) + TODAY + m.group(2), txt, count=1)
    txt = re.sub(r'(MIN_DATE|MAX_DATE|DEFAULT_DATE)="[^"]*"',
                 lambda m: m.group(1) + '="' + TODAY + '"', txt)
    return txt

def main():
    txt, obj = read_template("index.html")
    new_obj = refresh(obj)
    out = inject(txt, new_obj)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(out)
    print("OK 已刷新 index.html | 日期=%s | 今日要闻=%d 条 | 板块=%d | 跨板块去重认领=%d"
          % (TODAY, new_obj["new_count"], len(new_obj["sections"]), len(CLAIMED)))

if __name__ == "__main__":
    main()
