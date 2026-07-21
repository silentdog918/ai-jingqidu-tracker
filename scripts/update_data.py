#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 商业化景气度追踪 · 每日数据抓取
只用 Python 标准库,不需要任何第三方依赖。
每个数据源独立抓取:某一源失败时保留上一份对应的 data/*.json,不影响其他源。
数据源:OpenRouter(模型目录 + 排行统计)· npm registry · pypistats · Google News RSS · 播客 RSS · Yahoo Finance(EOD)
"""
import json
import os
import time
import gzip
import email.utils
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))

UA = "Mozilla/5.0 (compatible; ai-jingqidu-tracker/1.0; personal dashboard)"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, timeout=45, retries=2, headers=None):
    h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
                return b
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def get_json(url, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8", "replace"))


def load_prev(name):
    try:
        return json.load(open(os.path.join(DATA, name), encoding="utf-8"))
    except Exception:
        return None


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  ✓ 写入 data/{name}")


# ---------------- OpenRouter ----------------
def fetch_openrouter():
    cfg = CFG["openrouter"]
    out = {"generated_at": now_iso(), "has_key": True}

    # 1) 模型目录 + 新模型
    models = get_json("https://openrouter.ai/api/v1/models")["data"]
    out["model_count"] = len(models)
    newest = sorted(models, key=lambda m: m.get("created") or 0, reverse=True)
    nm = []
    for m in newest[: cfg["new_models_count"]]:
        pr = m.get("pricing") or {}
        def usd_per_m(v):
            try:
                return round(float(v) * 1e6, 4)
            except Exception:
                return None
        nm.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "created": m.get("created"),
            "prompt_usd_per_m": usd_per_m(pr.get("prompt")),
            "completion_usd_per_m": usd_per_m(pr.get("completion")),
            "context": m.get("context_length"),
        })
    out["new_models"] = nm

    # 2) 每日 Token 排行(接口只给最近几天 → 与仓库里的历史合并,滚动保留 N 天)
    rows = get_json("https://openrouter.ai/api/frontend/v1/rankings/models")["data"]
    agg = {}
    for r in rows:
        d = str(r.get("date", ""))[:10]
        m = r.get("model_permaslug") or r.get("variant_permaslug") or ""
        if not d or not m:
            continue
        t = (r.get("total_prompt_tokens") or 0) + (r.get("total_completion_tokens") or 0) \
            + (r.get("total_native_tokens_reasoning") or 0)
        agg[(d, m)] = agg.get((d, m), 0) + t
    hist = {}
    prev = load_prev("openrouter.json")
    if prev and isinstance(prev.get("daily"), list):
        for r in prev["daily"]:
            hist[(r["d"], r["m"])] = r["t"]
    hist.update({k: v for k, v in agg.items() if v > 0})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["daily_keep_days"])).strftime("%Y-%m-%d")
    by_day = {}
    for (d, m), t in hist.items():
        if d >= cutoff:
            by_day.setdefault(d, []).append((m, t))
    daily = []
    for d in sorted(by_day):
        top = sorted(by_day[d], key=lambda x: -x[1])[: cfg["daily_top_per_day"]]
        daily.extend({"d": d, "m": m, "t": t} for m, t in top)
    out["daily"] = daily

    # 3) 应用榜(按月窗口)
    apps = get_json("https://openrouter.ai/api/frontend/v1/rankings/apps")["data"]
    month = apps.get("month") or apps.get("week") or apps.get("day") or []
    apps_raw = []
    for i, a in enumerate(sorted(month, key=lambda x: x.get("rank") or 999)):
        app = a.get("app") or {}
        apps_raw.append({
            "rank": i + 1,
            "app_id": a.get("app_id"),
            "app_name": app.get("title") or app.get("slug") or str(a.get("app_id")),
            "total_tokens": str(a.get("total_tokens") or "0"),
            "total_requests": a.get("total_requests") or 0,
        })
    out["apps_raw"] = apps_raw

    save("openrouter.json", out)
    return {"ok": True, "count": out["model_count"], "rankings": None}


# ---------------- npm / PyPI 下载量 ----------------
def _dl_stats(series):
    tail = [x["v"] for x in series]
    while tail and tail[-1] == 0:
        tail.pop()
    last7 = sum(tail[-7:]) if len(tail) >= 7 else sum(tail)
    prev7 = sum(tail[-14:-7]) if len(tail) >= 14 else 0
    wow = round((last7 / prev7 - 1) * 100, 1) if prev7 else None
    return last7, prev7, wow


def fetch_downloads():
    cfg = CFG["downloads"]
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=cfg["series_days"] - 1)
    out = {"generated_at": now_iso(), "npm": {}, "pypi": {}, "errors": None}
    errors = []

    for pkg in cfg["npm_packages"]:
        try:
            enc = urllib.parse.quote(pkg, safe="")
            j = get_json(f"https://api.npmjs.org/downloads/range/{start}:{end}/{enc}")
            series = [{"d": x["day"], "v": x["downloads"]} for x in j.get("downloads", [])]
            last7, prev7, wow = _dl_stats(series)
            out["npm"][pkg] = {"series": series, "last7": last7, "prev7": prev7, "wow_pct": wow}
        except Exception as e:
            errors.append(f"npm:{pkg}:{e}")
        time.sleep(0.3)

    for pkg in cfg["pypi_packages"]:
        try:
            j = get_json(f"https://pypistats.org/api/packages/{pkg}/overall?mirrors=false")
            rows = sorted((x for x in j.get("data", []) if x.get("category") == "without_mirrors"),
                          key=lambda x: x["date"])
            rows = [x for x in rows if x["date"] >= str(start)]
            series = [{"d": x["date"], "v": x["downloads"]} for x in rows]
            last7, prev7, wow = _dl_stats(series)
            out["pypi"][pkg] = {"series": series, "last7": last7, "prev7": prev7, "wow_pct": wow}
        except Exception as e:
            errors.append(f"pypi:{pkg}:{e}")
        time.sleep(1.0)

    out["errors"] = errors or None
    if not out["npm"] and not out["pypi"]:
        raise RuntimeError("npm 与 pypi 全部失败: " + "; ".join(errors[:3]))
    save("downloads.json", out)
    return {"ok": True, "count": len(out["npm"]) + len(out["pypi"]), "errors": out["errors"]}


# ---------------- Google News ----------------
def fetch_news():
    cfg = CFG["news"]
    out = {"generated_at": now_iso(), "categories": [c["name"] for c in cfg["categories"]], "items": []}
    failed = []
    for c in cfg["categories"]:
        try:
            url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(c["query"])
                   + f"&hl={c['hl']}&gl={c['gl']}&ceid={urllib.parse.quote(c['ceid'])}")
            root = ET.fromstring(http_get(url).decode("utf-8", "replace"))
            items = []
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                src = (it.findtext("source") or "").strip()
                pub = it.findtext("pubDate")
                try:
                    published = email.utils.parsedate_to_datetime(pub).astimezone(timezone.utc)\
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    published = None
                if title and link:
                    items.append({"title": title, "link": link, "source": src,
                                  "published": published, "category": c["name"], "lang": c["lang"]})
            items.sort(key=lambda x: x["published"] or "", reverse=True)
            out["items"].extend(items[: cfg["per_category"]])
        except Exception as e:
            failed.append(f"{c['name']}:{e}")
        time.sleep(0.5)
    if not out["items"]:
        raise RuntimeError("所有新闻栏目均失败: " + "; ".join(failed[:3]))
    save("news.json", out)
    return {"ok": True, "count": len(out["items"]), "failed_queries": failed or None}


# ---------------- Yahoo 行情(EOD) ----------------
def fetch_quotes():
    cfg = CFG["quotes"]
    out = {"generated_at": now_iso(), "groups": cfg["groups"], "quotes": {}, "errors": None}
    errors = []
    symbols = [s for g in cfg["groups"] for s in g["symbols"]]
    for sym in symbols:
        got = False
        for host in ("query1", "query2"):
            try:
                j = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                             f"{urllib.parse.quote(sym)}?range=6mo&interval=1d", retries=1)
                res = j["chart"]["result"][0]
                ts = res.get("timestamp") or []
                closes = res["indicators"]["quote"][0].get("close") or []
                pts = [(t, c) for t, c in zip(ts, closes) if c is not None]
                if len(pts) < 22:
                    raise RuntimeError("数据点不足")
                cl = [c for _, c in pts]
                def chg(n):
                    return round((cl[-1] / cl[-1 - n] - 1) * 100, 2) if len(cl) > n else None
                out["quotes"][sym] = {
                    "name": cfg["names"].get(sym, sym),
                    "price": round(cl[-1], 2),
                    "chg1d": chg(1), "chg5d": chg(5), "chg20d": chg(20),
                    "spark": [round(c, 2) for c in cl[-cfg["spark_days"]:]],
                    "asof": datetime.fromtimestamp(pts[-1][0], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "currency": (res.get("meta") or {}).get("currency") or "USD",
                }
                got = True
                break
            except Exception as e:
                err = e
        if not got:
            errors.append(f"{sym}:{err}")
        time.sleep(0.6)
    out["errors"] = errors or None
    if not out["quotes"]:
        raise RuntimeError("全部行情失败: " + "; ".join(errors[:3]))
    save("quotes.json", out)
    return {"ok": True, "count": len(out["quotes"]), "errors": out["errors"]}


# ---------------- 播客 RSS ----------------
def fetch_podcasts():
    cfg = CFG["podcasts"]
    out = {"generated_at": now_iso(), "shows": [s["name"] for s in cfg["shows"]], "episodes": []}
    failed = []
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    for s in cfg["shows"]:
        try:
            root = ET.fromstring(http_get(s["feed"]).decode("utf-8", "replace"))
            eps = []
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                if not link:
                    enc = it.find("enclosure")
                    link = enc.get("url") if enc is not None else ""
                pub = it.findtext("pubDate")
                try:
                    published = email.utils.parsedate_to_datetime(pub).astimezone(timezone.utc)\
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    published = None
                dur = (it.findtext("itunes:duration", namespaces=ns) or "").strip()
                if dur.isdigit():
                    sec = int(dur)
                    dur = f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 \
                        else f"{sec // 60}:{sec % 60:02d}"
                if title:
                    eps.append({"show": s["name"], "title": title, "published": published,
                                "link": link, **({"duration": dur} if dur else {})})
                if len(eps) >= cfg["max_per_feed"]:
                    break
            out["episodes"].extend(eps)
        except Exception as e:
            failed.append(f"{s['name']}:{e}")
        time.sleep(0.3)
    out["episodes"].sort(key=lambda x: x["published"] or "", reverse=True)
    out["episodes"] = out["episodes"][: cfg["max_total"]]
    out["failed_feeds"] = failed or None
    if not out["episodes"]:
        raise RuntimeError("所有播客源均失败: " + "; ".join(failed[:3]))
    save("podcasts.json", out)
    return {"ok": True, "count": len(out["episodes"]), "failed_feeds": out["failed_feeds"]}


# ---------------- 主流程 ----------------
def main():
    sources = {}
    for key, fn in [("openrouter", fetch_openrouter), ("downloads", fetch_downloads),
                    ("news", fetch_news), ("quotes", fetch_quotes), ("podcasts", fetch_podcasts)]:
        print(f"[{key}] 抓取中…")
        try:
            sources[key] = fn()
        except Exception as e:
            print(f"  ✗ {key} 失败,保留上一份数据: {e}")
            sources[key] = {"ok": False, "error": str(e)[:200]}
    save("meta.json", {"generated_at": now_iso(), "sources": sources})
    ok = sum(1 for s in sources.values() if s.get("ok"))
    print(f"完成:{ok}/{len(sources)} 个数据源成功。")


if __name__ == "__main__":
    main()
