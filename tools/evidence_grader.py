"""
Evidence Grader v3 — 多数据源、抗脆弱的权威度三级分级系统

评分维度 (满分 100):
  1. CEBM 证据等级      (0-40 分) — 有序列表精确匹配 + 标题关键词修正
  2. 期刊影响力         (0-30 分) — 分领域四层期刊识别 + ISSN 辅助匹配
  3. 被引次数           (0-25 分) — S2 主数据源 + OpenAlex 全覆盖补充
  4. 领域归一化影响力   (0-5  分) — OpenAlex cited_by_percentile_year
  加分项: 时效衰减保护 — 高引经典 / 高CEBM等级 / 中度被引 三重保护

特殊处理:
  - 撤稿检测 (OpenAlex is_retracted) → 强制 RETRACTED 等级，分数归零
  - 引用缓存 (citation_cache 表) → 避免重复 API 调用，7天有效期
  - 指数退避重试 → API 调用失败自动重试 3 次

输出:
  A  级 (≥70):   高可信 — Meta/RCT + 顶刊或高引用
  B  级 (40-69): 中可用 — 队列研究/综述 + 一定引用
  C  级 (<40):   低可信 — Case report/无引用/无期刊
  RETRACTED:     已撤稿 — 不可使用

用法:
  python evidence_grader_v3.py                     # 评分全部论文 (增量)
  python evidence_grader_v3.py --force             # 强制重新评分
  python evidence_grader_v3.py --force --no-cache  # 强制重新评分 + 重新拉取引用数据
  python evidence_grader_v3.py --summary           # 只看统计摘要
"""

import sqlite3, os, json, sys, time, urllib.request, re, ssl
from datetime import datetime, timedelta

# ============================================================
# 配置
# ============================================================
BASE = os.path.expanduser("~/work/zhaozhengyi")
DB = os.path.join(BASE, "med-papers/metadata.db")
NOW = datetime.now().year
CACHE_TTL_DAYS = 7
OPENALEX_BATCH_SIZE = 50   # 每批查询的 PMID 数

# ============================================================
# 维度 1: CEBM 证据等级 (有序列表, 长模式优先)
# ============================================================
PUB_TYPE_RULES = [
    ("systematic review",          "1a", 40),
    ("meta-analysis",              "1a", 40),
    ("meta analysis",              "1a", 40),
    ("practice guideline",         "1a", 40),
    ("guideline",                  "1a", 38),
    ("randomized controlled trial","1b", 35),
    ("rct",                        "1b", 35),
    ("clinical trial, phase iii",  "1b", 34),
    ("controlled clinical trial",  "2a", 30),
    ("clinical trial, phase ii",   "2a", 28),
    ("cohort study",               "2b", 25),
    ("comparative study",          "2b", 24),
    ("clinical trial",             "2b", 23),
    ("clinical trial, phase i",    "2b", 22),
    ("case-control study",         "3a", 20),
    ("cross-sectional study",      "3b", 18),
    ("narrative review",           "4",  12),
    ("scoping review",             "3a", 20),
    ("umbrella review",            "1a", 38),
    ("review",                     "3a", 20),
    ("case series",                "4",  12),
    ("case report",                "4",  10),
    ("letter",                     "5",   6),
    ("comment",                    "5",   6),
    ("editorial",                  "5",   5),
    ("news",                       "5",   5),
    ("newspaper article",          "5",   5),
    ("journal article",            "3b", 18),
]
DEFAULT_CEBM = ("4", 10)

TITLE_SYSTEMATIC_REVIEW = re.compile(
    r'\b(systematic\s*(review|literature\s*search)|meta[\s-]*analys|'
    r'PRISMA|Cochrane|scoping\s*review|umbrella\s*review)\b',
    re.IGNORECASE
)
TITLE_NARRATIVE_REVIEW = re.compile(
    r'\b(narrative\s*review|traditional\s*review|'
    r'literature\s*review|mini[\s-]*review)\b',
    re.IGNORECASE
)
TITLE_RCT = re.compile(
    r'\b(randomi[sz]ed\s*(controlled)?\s*(trial|study)|'
    r'double[\s-]*blind|RCT\b)',
    re.IGNORECASE
)

# ============================================================
# 维度 2: 分领域期刊层级
# ============================================================
JOURNAL_TIER_SCORE = {1: 30, 2: 22, 3: 14, 4: 6}

TOP_JOURNALS_BY_FIELD = {
    "tcm_ethnopharmacology": {
        "journal of ethnopharmacology", "phytomedicine",
        "american journal of chinese medicine", "chinese medicine",
        "journal of integrative medicine", "chinese journal of natural medicines",
        "chinese journal of integrative medicine",
        "evidence-based complementary and alternative medicine",
        "journal of traditional and complementary medicine",
        "integrative medicine research",
        "journal of traditional chinese medicine",
    },
    "pharmacology": {
        "nature reviews drug discovery", "pharmacological reviews",
        "trends in pharmacological sciences",
        "clinical pharmacology and therapeutics",
        "british journal of pharmacology", "biochemical pharmacology", "drugs",
        "pharmacological research",
        "pharmacology & therapeutics",
    },
    "endocrinology_diabetes": {
        "diabetes care", "diabetes", "diabetologia",
        "journal of clinical endocrinology and metabolism",
        "endocrinology", "the lancet diabetes & endocrinology",
        "nature reviews endocrinology",
        "diabetes research and clinical practice",
    },
    "medicine": {
        "new england journal of medicine", "nejm", "lancet", "bmj", "jama",
        "nature medicine", "journal of clinical investigation", "jci",
        "annals of internal medicine", "plos medicine", "bmc medicine",
        "the bmj", "canadian medical association journal",
        "archives of internal medicine",
    },
    "nephrology": {
        "kidney international", "journal of the american society of nephrology",
        "jasn", "american journal of kidney diseases",
        "nephrology dialysis transplantation", "ndt",
        "clinical journal of the american society of nephrology", "cjasn",
    },
}

TOP_JOURNALS_GENERIC = {
    "nature", "science", "cell", "pnas",
    "nature communications", "science advances", "cell reports",
    "elife", "the embo journal", "nature reviews",
}

# 二线好期刊 — 按领域分组后剩余的泛化条目
GOOD_JOURNALS = {
    # 药理 / 药学
    "frontiers in pharmacology", "european journal of pharmacology",
    "journal of pharmaceutical and biomedical analysis",
    "pharmaceutical biology", "current pharmaceutical design",
    "drug design development and therapy",
    "therapeutics and clinical risk management",
    "phytomedicine international journal of phytotherapy and phytopharmacology",
    "phytotherapy research", "fitoterapia", "planta medica",
    "journal of natural products", "phytochemistry reviews",
    "pharmaceuticals", "molecules",
    # 内分泌 / 糖尿病 / 代谢
    "frontiers in endocrinology", "diabetes metabolic syndrome and obesity",
    "journal of diabetes research",
    # 综合医学 / TCM
    "frontiers in medicine", "frontiers in immunology",
    "biomedicine & pharmacotherapy", "biomedecine & pharmacotherapie",
    "bmc complementary medicine and therapies",
    "bmc nephrology", "international journal of molecular medicine",
    "american journal of translational research",
    "alternative therapies in health and medicine",
    "cell cycle", "biomedical chromatography",
    "international journal of molecular sciences",
    "chinese journal of integrative medicine",
    "zhongguo zhong yao za zhi", "zhongguo zhong xi yi jie he za zhi",
    "zhong xi yi jie he xue bao", "zhong yao cai",
    "nan fang yi ke da xue xue bao",
    "frontiers in plant science", "food & function",
    "journal of food biochemistry",
    "journal of functional foods",
    "oxidative medicine and cellular longevity",
    "experimental and therapeutic medicine",
    "molecular medicine reports",
    "oncology letters",
    "biomedical reports",
    "world journal of diabetes",
    "journal of clinical medicine",
    "nutrients",
    "antioxidants",
    "life sciences",
    "european journal of medicinal chemistry",
    "bioorganic & medicinal chemistry letters",
    "journal of medicinal chemistry",
    "acs chemical neuroscience",
    "journal of agricultural and food chemistry",
    "journal of cellular biochemistry",
    "journal of cellular and molecular medicine",
    "cellular physiology and biochemistry",
    "kidney & blood pressure research",
    "renal failure",
    "chinese medical journal",
    "chinese journal of integrative medicine",
    "world journal of traditional chinese medicine",
    "traditional medicine research",
}

# ISSN-L → Tier 映射 (精确匹配, 用于 keyword 分类失败时的兜底)
# 收录常见 TCM 期刊的 ISSN
ISSN_TIER = {
    # TCM 顶刊 (Tier 1)
    "0378-8741": 1,  # Journal of Ethnopharmacology
    "0944-7113": 1,  # Phytomedicine
    "0192-415X": 1,  # American Journal of Chinese Medicine
    "1749-8546": 1,  # Chinese Medicine
    "2095-4964": 1,  # Journal of Integrative Medicine
    "1875-5364": 1,  # Chinese Journal of Natural Medicines
    "1672-0415": 1,  # Chinese Journal of Integrative Medicine
    # 药理 (Tier 1)
    "0007-1188": 1,  # British Journal of Pharmacology
    "0006-2952": 1,  # Biochemical Pharmacology
    "0031-6997": 1,  # Pharmacological Reviews
    "0163-7258": 1,  # Pharmacology & Therapeutics
}


# ============================================================
# 维度 3 & 4: 被引 + 领域归一化影响力
# ============================================================
def citation_score(count: int) -> tuple:
    """被引次数 → (分数, 标签)  满分 25"""
    if count >= 100:
        return (25, "极高引用")
    elif count >= 50:
        return (20, "高引用")
    elif count >= 20:
        return (15, "中等引用")
    elif count >= 5:
        return (10, "低引用")
    elif count >= 1:
        return (5, "极少引用")
    else:
        return (0, "无引用")


def field_normalized_bonus(rcr=None, pct_max=None) -> int:
    """
    领域归一化影响力加分 (0-5)。
    优先使用 NIH RCR (更科学, 同行评审验证), OpenAlex 百分位兜底。
    RCR = 1.0 为领域平均水平, >1.0 表示高于平均。
    """
    # NIH RCR 优先
    if rcr is not None:
        try:
            r = float(rcr)
        except (ValueError, TypeError):
            r = 0
        if r >= 5.0:
            return 5
        elif r >= 2.0:
            return 4
        elif r >= 1.0:
            return 3
        elif r >= 0.5:
            return 2
        elif r > 0:
            return 1
        else:
            return 0

    # OpenAlex 百分位兜底
    if pct_max is not None:
        try:
            p = int(pct_max)
        except (ValueError, TypeError):
            return 0
        if p >= 99:
            return 4  # OA 百分位比 RCR 粗糙, 最高 4
        elif p >= 95:
            return 3
        elif p >= 90:
            return 2
        elif p >= 75:
            return 1
        else:
            return 0

    return 0


# ============================================================
# 时效衰减 (与 v2 相同, 已稳定)
# ============================================================
def year_decay(pub_year, citation_count: int, cebm_level: str = "4") -> float:
    if citation_count > 50:
        return 1.0
    if pub_year is None or pub_year < 1900:
        return 0.85

    age = NOW - pub_year
    if age < 0:
        return 1.0

    if age <= 5:
        base_decay = 1.0
    elif age <= 10:
        base_decay = 0.90
    elif age <= 15:
        base_decay = 0.80
    elif age <= 20:
        base_decay = 0.65
    else:
        base_decay = 0.50

    if citation_count > 10:
        base_decay = max(base_decay, 0.75)

    cebm_floor = {
        "1a": 0.85, "1b": 0.80,
        "2a": 0.75, "2b": 0.70,
        "3a": 0.60, "3b": 0.55,
        "4": 0.50, "5": 0.45,
    }
    return max(base_decay, cebm_floor.get(cebm_level, 0.50))


# ============================================================
# 期刊分类
# ============================================================
def classify_field(journal: str) -> str:
    if not journal:
        return "unknown"
    j = journal.lower()
    field_keywords = [
        ("tcm_ethnopharmacology", [
            "ethnopharmacol", "phytomed", "phytother", "chinese med",
            "integrative med", "tradit", "herbal", "acupunctur",
            "zhongguo", "zhong", "nan fang", "planta med",
            "fitoterapia", "natural product", "complementary",
            "alternative medicine", "tcm", "chinese materia medica",
        ]),
        ("nephrology", [
            "kidney", "nephrol", "renal", "dialys",
        ]),
        ("endocrinology_diabetes", [
            "diabet", "endocrin", "metabol", "obesit", "thyroid",
        ]),
        ("pharmacology", [
            "pharmacol", "pharmaceut", "drug", "pharmacokinet",
            "pharmacodyn", "molecules",
        ]),
        ("medicine", [
            "lancet", "nejm", "jama", "bmj", "annals of",
            "archives of", "journal of clinical", "new england journal",
            "surgery", "oncology", "cardiology", "neurology",
        ]),
    ]
    for field, keywords in field_keywords:
        for kw in keywords:
            if kw in j:
                return field
    return "general"


def get_journal_tier(journal: str, issn_l: str = "") -> int:
    """返回 1=顶刊, 2=好刊, 3=普通, 4=无期刊"""
    # ISSN 精确匹配 (最快, 最可靠)
    if issn_l and issn_l in ISSN_TIER:
        return ISSN_TIER[issn_l]

    if not journal:
        return 4
    j = journal.lower().strip()

    # 1) 通用跨领域顶刊
    for top in TOP_JOURNALS_GENERIC:
        if top in j:
            return 1

    # 2) 分领域顶刊
    field = classify_field(journal)
    if field in TOP_JOURNALS_BY_FIELD:
        for top in TOP_JOURNALS_BY_FIELD[field]:
            if top in j:
                return 1

    # 3) 好期刊
    for good in GOOD_JOURNALS:
        if good in j:
            return 2

    # 4) 有期刊名但不在白名单 → 索引期刊
    if any(w in j for w in ["journal", "annals", "archives", "bmc",
                              "frontiers", "research", "review",
                              "medicine", "science", "report"]):
        return 3

    return 3


# ============================================================
# CEBM 分级
# ============================================================
def get_cebm_level(pub_types_json: str, title: str) -> tuple:
    if not pub_types_json:
        return DEFAULT_CEBM + ("无出版类型，假定为描述性研究",)

    try:
        types = json.loads(pub_types_json)
    except (json.JSONDecodeError, TypeError):
        return DEFAULT_CEBM + ("出版类型解析失败",)

    if not isinstance(types, list) or not types:
        return DEFAULT_CEBM + ("出版类型为空",)

    types_lower = [str(t).strip().lower() for t in types]
    title_lower = (title or "").lower()

    best_level, best_score, best_note = DEFAULT_CEBM + ("无匹配规则",)
    matched_rule = None

    for keyword, level, score in PUB_TYPE_RULES:
        for t in types_lower:
            if keyword in t:
                if score > best_score:
                    best_level, best_score = level, score
                    matched_rule = keyword
                    break

    note = f"出版类型含 '{matched_rule}'" if matched_rule else "无匹配"

    # 标题修正: 泛化 "review" 细化
    if matched_rule == "review":
        if TITLE_SYSTEMATIC_REVIEW.search(title_lower):
            best_level, best_score = "1a", 38
            note = "出版类型为 'review'，标题含系统综述关键词 → 升至 1a"
        elif TITLE_NARRATIVE_REVIEW.search(title_lower):
            best_level, best_score = "4", 12
            note = "出版类型为 'review'，标题含叙述性综述关键词 → 降至 4"
        else:
            note = "出版类型为 'review'，无法确定是否为系统综述 → 保持 3a"

    # 标题修正: "Journal Article" 可能是未标记的 RCT
    if matched_rule == "journal article" or best_score <= 18:
        if TITLE_RCT.search(title_lower):
            best_level, best_score = "1b", 33
            note = "标题含 RCT 关键词，虽未标记为 RCT → 推测为 1b"

    return best_level, best_score, note


# ============================================================
# API 基础设施: 重试 + 缓存
# ============================================================
def http_get(url: str, timeout: int = 30, max_retries: int = 3) -> bytes:
    """带指数退避的 HTTP GET"""
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "med-paper-grader/3.0 (mailto:1292589632@qq.com)",
            })
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                time.sleep(wait)
    raise last_err


def http_post(url: str, body: bytes, timeout: int = 30, max_retries: int = 3) -> bytes:
    """带指数退避的 HTTP POST"""
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "User-Agent": "med-paper-grader/3.0 (mailto:1292589632@qq.com)",
            })
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                time.sleep(wait)
    raise last_err


# ============================================================
# 引用缓存
# ============================================================
def ensure_cache_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS citation_cache (
            pmid TEXT PRIMARY KEY,
            s2_citation_count INTEGER,
            s2_influential_count INTEGER,
            oa_citation_count INTEGER,
            oa_percentile_min INTEGER,
            oa_percentile_max INTEGER,
            oa_is_retracted INTEGER,
            oa_type TEXT,
            oa_journal_name TEXT,
            oa_issn_l TEXT,
            oa_publisher TEXT,
            epmc_citation_count INTEGER,
            icite_citation_count INTEGER,
            icite_rcr REAL,
            cached_at TEXT
        )
    """)
    # 迁移旧缓存: 如果表已存在但缺少新列
    cur = con.execute("PRAGMA table_info(citation_cache)")
    cols = {r[1] for r in cur.fetchall()}
    for col in ["epmc_citation_count", "icite_citation_count", "icite_rcr"]:
        if col not in cols:
            try:
                dtype = "REAL" if col == "icite_rcr" else "INTEGER DEFAULT 0"
                con.execute(f"ALTER TABLE citation_cache ADD COLUMN {col} {dtype}")
            except:
                pass
    con.commit()


def cache_get(con, pmid: str) -> dict | None:
    """读取缓存 (7 天内有效)"""
    row = con.execute(
        "SELECT * FROM citation_cache WHERE pmid=? AND cached_at > ?",
        (str(pmid), (datetime.now() - timedelta(days=CACHE_TTL_DAYS)).isoformat())
    ).fetchone()
    if not row:
        return None
    return dict(row)


def cache_put(con, pmid: str, data: dict):
    """写入/更新缓存"""
    con.execute("""
        INSERT OR REPLACE INTO citation_cache
        (pmid, s2_citation_count, s2_influential_count,
         oa_citation_count, oa_percentile_min, oa_percentile_max,
         oa_is_retracted, oa_type, oa_journal_name, oa_issn_l, oa_publisher,
         epmc_citation_count, icite_citation_count, icite_rcr, cached_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        str(pmid),
        data.get("s2_citation_count", 0),
        data.get("s2_influential_count", 0),
        data.get("oa_citation_count", 0),
        data.get("oa_percentile_min"),
        data.get("oa_percentile_max"),
        1 if data.get("oa_is_retracted") else 0,
        data.get("oa_type", ""),
        data.get("oa_journal_name", ""),
        data.get("oa_issn_l", ""),
        data.get("oa_publisher", ""),
        data.get("epmc_citation_count", 0),
        data.get("icite_citation_count", 0),
        data.get("icite_rcr"),
        datetime.now().isoformat(),
    ))
    con.commit()


# ============================================================
# 多数据源引用获取
# ============================================================
def fetch_citations_s2(pmids: list) -> dict:
    """Semantic Scholar 批量 API (主数据源)"""
    if not pmids:
        return {}
    clean = [str(p).strip() for p in pmids if p and str(p).strip()]
    if not clean:
        return {}

    url = ("https://api.semanticscholar.org/graph/v1/paper/batch"
           "?fields=citationCount,influentialCitationCount,title,year")
    body = json.dumps({"ids": [f"PMID:{p}" for p in clean]}).encode("utf-8")

    try:
        raw = http_post(url, body, timeout=30, max_retries=2)
        data = json.loads(raw.decode("utf-8"))
        result = {}
        for i, item in enumerate(data):
            pmid = clean[i] if i < len(clean) else None
            if pmid and item and isinstance(item, dict):
                result[pmid] = {
                    "s2_citation_count": item.get("citationCount", 0) or 0,
                    "s2_influential_count": item.get("influentialCitationCount", 0) or 0,
                }
            elif pmid:
                result[pmid] = {"s2_citation_count": 0, "s2_influential_count": 0}
        return result
    except Exception as e:
        print(f"  [WARN] Semantic Scholar API 失败: {e}")
        return {}


def fetch_citations_openalex(pmids: list) -> dict:
    """
    OpenAlex 批量查询 (补充数据源 — 对 S2 未覆盖的 PMID 兜底)。
    返回 {pmid: {oa_citation_count, oa_percentile_max, oa_is_retracted, ...}}
    """
    if not pmids:
        return {}
    clean = [str(p).strip() for p in pmids if p and str(p).strip()]
    if not clean:
        return {}

    result = {}

    for batch_start in range(0, len(clean), OPENALEX_BATCH_SIZE):
        batch = clean[batch_start:batch_start + OPENALEX_BATCH_SIZE]
        pmid_filter = "|".join(batch)
        url = ("https://api.openalex.org/works"
               "?filter=pmid:" + pmid_filter +
               "&select=ids,cited_by_count,cited_by_percentile_year,"
               "is_retracted,type,publication_date,primary_location"
               "&per_page=" + str(OPENALEX_BATCH_SIZE))

        try:
            raw = http_get(url, timeout=30, max_retries=2)
            data = json.loads(raw.decode("utf-8"))
            # 建立 PMID → work 映射
            pmid_to_work = {}
            for w in data.get("results", []):
                ids = w.get("ids") or {}
                pmid_url = ids.get("pmid", "") or ""
                if pmid_url:
                    pmid_id = pmid_url.split("/")[-1]
                    pmid_to_work[pmid_id] = w

            for pmid in batch:
                w = pmid_to_work.get(pmid)
                if w:
                    pct = w.get("cited_by_percentile_year") or {}
                    loc = w.get("primary_location") or {}
                    src = loc.get("source") or {}
                    result[pmid] = {
                        "oa_citation_count": w.get("cited_by_count", 0) or 0,
                        "oa_percentile_min": pct.get("min"),
                        "oa_percentile_max": pct.get("max"),
                        "oa_is_retracted": bool(w.get("is_retracted")),
                        "oa_type": w.get("type", ""),
                        "oa_journal_name": src.get("display_name", ""),
                        "oa_issn_l": src.get("issn_l", ""),
                        "oa_publisher": src.get("publisher", ""),
                    }
                else:
                    result[pmid] = {
                        "oa_citation_count": 0,
                        "oa_percentile_min": None,
                        "oa_percentile_max": None,
                        "oa_is_retracted": False,
                        "oa_type": "",
                        "oa_journal_name": "",
                        "oa_issn_l": "",
                        "oa_publisher": "",
                    }
        except Exception as e:
            print(f"  [WARN] OpenAlex API 失败 (batch {batch_start}): {e}")
            for pmid in batch:
                if pmid not in result:
                    result[pmid] = {
                        "oa_citation_count": 0, "oa_percentile_min": None,
                        "oa_percentile_max": None, "oa_is_retracted": False,
                        "oa_type": "", "oa_journal_name": "",
                        "oa_issn_l": "", "oa_publisher": "",
                    }

        if batch_start + OPENALEX_BATCH_SIZE < len(clean):
            time.sleep(0.2)  # 礼貌限速

    return result


def fetch_citations_epmc(pmids: list) -> dict:
    """
    Europe PMC 批量查询 (第三数据源 — 生物医学专用)。
    用于填补 S2 和 OpenAlex 都未覆盖的引用盲区。
    返回 {pmid: {epmc_citation_count}}
    """
    if not pmids:
        return {}
    clean = [str(p).strip() for p in pmids if p and str(p).strip()]
    if not clean:
        return {}

    # Europe PMC 支持 OR 语法，一次性查询所有 PMID
    query = "+OR+".join([f"EXT_ID:{p}" for p in clean])
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?query=" + query +
           "&resulttype=core&format=json"
           "&pageSize=" + str(len(clean)))

    try:
        raw = http_get(url, timeout=30, max_retries=2)
        data = json.loads(raw.decode("utf-8"))
        epmc_lookup = {}
        for r in data.get("resultList", {}).get("result", []):
            epmc_lookup[str(r.get("pmid", ""))] = r.get("citedByCount", 0) or 0

        result = {}
        for pmid in clean:
            result[pmid] = {
                "epmc_citation_count": epmc_lookup.get(pmid, 0),
            }
        return result
    except Exception as e:
        print(f"  [WARN] Europe PMC API 失败: {e}")
        return {p: {"epmc_citation_count": 0} for p in clean}


def fetch_citations_icite(pmids: list) -> dict:
    """
    NIH iCite 批量查询 (第四数据源)。
    返回 {pmid: {icite_citation_count, icite_rcr}}
    RCR (Relative Citation Ratio) = NIH 的领域+时间归一化引用指标。
    RCR=1.0 表示该论文被引次数为同领域同年论文的平均水平。
    """
    if not pmids:
        return {}
    clean = [str(p).strip() for p in pmids if p and str(p).strip()]
    if not clean:
        return {}

    # iCite 批量: ?pmids=A&pmids=B...
    url = "https://icite.od.nih.gov/api/pubs?"
    url += "&".join([f"pmids={p}" for p in clean])

    try:
        raw = http_get(url, timeout=30, max_retries=2)
        data = json.loads(raw.decode("utf-8"))
        items = data if isinstance(data, list) else data.get("data", [])

        result = {}
        icite_lookup = {}
        for item in items:
            icite_lookup[str(item.get("pmid", ""))] = item

        for pmid in clean:
            item = icite_lookup.get(pmid, {})
            rcr_raw = item.get("relative_citation_ratio")
            result[pmid] = {
                "icite_citation_count": item.get("citation_count", 0) or 0,
                "icite_rcr": float(rcr_raw) if rcr_raw is not None else None,
            }
        return result
    except Exception as e:
        print(f"  [WARN] NIH iCite API 失败: {e}")
        return {p: {"icite_citation_count": 0, "icite_rcr": None} for p in clean}


def fetch_citations_all(con, pmids: list, force_refresh: bool = False) -> dict:
    """
    四数据源联合获取引用数据 (带缓存)。
    S1. Semantic Scholar → 主数据源 (质量最高)
    S2. OpenAlex         → 撤稿检测 + 百分位 + ISSN
    S3. Europe PMC       → 生物医学专用 (填补盲区)
    S4. NIH iCite        → RCR 领域归一化指标 + 引用计数

    最终 citation_count = max(S1, S2, S3, S4)
    领域加成: NIH RCR 优先, OpenAlex 百分位兜底
    """
    """
    多数据源联合获取引用数据 (带缓存)。
    S2 速度快、质量高 → 主数据源。
    OpenAlex 覆盖广、字段多 → 补充 + 撤稿检测 + 领域归一化。

    返回 {pmid: {s2_citation_count, s2_influential_count,
                  oa_citation_count, oa_percentile_max,
                  oa_is_retracted, oa_issn_l, ...}}
    """
    if not pmids:
        return {}
    ensure_cache_table(con)

    # 分离: 缓存命中的 vs 需要重新获取的
    cached, to_fetch = {}, []
    for p in pmids:
        p_str = str(p).strip()
        if not p_str:
            continue
        if not force_refresh:
            c = cache_get(con, p_str)
            if c:
                cached[p_str] = c
                continue
        to_fetch.append(p_str)

    if not to_fetch:
        print(f"  引用数据全部来自缓存 ({len(cached)} 篇, 无需 API 调用)")
        return cached

    print(f"  缓存命中: {len(cached)} 篇, 需获取: {len(to_fetch)} 篇")

    # Step 1: Semantic Scholar (主)
    s2_results = fetch_citations_s2(to_fetch)

    # Step 2: OpenAlex (百分位 + 撤稿 + ISSN) — 对所有 to_fetch 查询
    oa_results = fetch_citations_openalex(to_fetch)

    # Step 3: Europe PMC (仅对 S1+S2 都覆盖不到的论文)
    s2_missing = [
        p for p in to_fetch
        if p not in s2_results or s2_results[p].get("s2_citation_count", 0) == 0
    ]
    oa_missing = [
        p for p in s2_missing
        if p not in oa_results or oa_results[p].get("oa_citation_count", 0) == 0
    ]
    epmc_results = {}
    if oa_missing:
        epmc_results = fetch_citations_epmc(oa_missing)

    # Step 4: NIH iCite — 仅对有 S1 引用的论文取 RCR (iCite 不参与引用计数合并)
    s2_has_cite = [
        p for p in to_fetch
        if p in s2_results and s2_results[p].get("s2_citation_count", 0) > 0
    ]
    icite_results = {}
    if s2_has_cite:
        icite_results = fetch_citations_icite(s2_has_cite)

    # Step 5: 合并结果
    # 引用次数 = max(S1, S2, S3)
    # RCR 领域归一化 = S4(iCite) 提供, 仅对 S1 有引用的论文
    merged = {}
    for p in to_fetch:
        s2 = s2_results.get(p, {"s2_citation_count": 0, "s2_influential_count": 0})
        oa = oa_results.get(p, {})
        ep = epmc_results.get(p, {})
        ic = icite_results.get(p, {})
        entry = {
            "s2_citation_count": s2.get("s2_citation_count", 0) or 0,
            "s2_influential_count": s2.get("s2_influential_count", 0) or 0,
            "oa_citation_count": oa.get("oa_citation_count", 0) or 0,
            "oa_percentile_min": oa.get("oa_percentile_min"),
            "oa_percentile_max": oa.get("oa_percentile_max"),
            "oa_is_retracted": oa.get("oa_is_retracted", False),
            "oa_type": oa.get("oa_type", ""),
            "oa_journal_name": oa.get("oa_journal_name", ""),
            "oa_issn_l": oa.get("oa_issn_l", ""),
            "oa_publisher": oa.get("oa_publisher", ""),
            "epmc_citation_count": ep.get("epmc_citation_count", 0) or 0,
            "icite_citation_count": ic.get("icite_citation_count", 0) or 0,
            "icite_rcr": ic.get("icite_rcr"),
        }
        merged[p] = entry
        cache_put(con, p, entry)

    # 合并缓存
    merged.update(cached)
    return merged


# ============================================================
# DB 初始化: 自动添加新列
# ============================================================
def ensure_columns(con):
    cur = con.execute("PRAGMA table_info(papers)")
    existing = {r[1] for r in cur.fetchall()}
    new_cols = {
        "citation_count":       "INTEGER DEFAULT 0",
        "influential_citations":"INTEGER DEFAULT 0",
        "oa_citation_count":    "INTEGER DEFAULT 0",
        "epmc_citation_count":  "INTEGER DEFAULT 0",
        "icite_citation_count": "INTEGER DEFAULT 0",
        "icite_rcr":            "REAL DEFAULT NULL",
        "oa_percentile":        "INTEGER DEFAULT NULL",
        "is_retracted":         "INTEGER DEFAULT 0",
        "authority_tier":       "TEXT DEFAULT ''",
        "authority_score":      "REAL DEFAULT 0",
        "cebm_score":           "REAL DEFAULT 0",
        "journal_score":        "REAL DEFAULT 0",
        "citation_score":       "REAL DEFAULT 0",
        "percentile_bonus":     "REAL DEFAULT 0",
        "year_decay":           "REAL DEFAULT 1.0",
        "grade_note":           "TEXT DEFAULT ''",
    }
    for col, dtype in new_cols.items():
        if col not in existing:
            con.execute(f"ALTER TABLE papers ADD COLUMN {col} {dtype}")
    con.commit()


# ============================================================
# 主流程
# ============================================================
def grade_all(batch_size=5000, force=False, no_cache=False):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ensure_columns(con)

    sql = ("SELECT id, pmid, doi, title, pub_types, journal, pub_date, pmcid "
           "FROM papers")
    if not force:
        sql += " WHERE authority_tier IS NULL OR authority_tier = ''"
    rows = con.execute(sql).fetchall()

    if not rows:
        print("没有需要评分的论文（已全部评分，用 --force 强制重评）")
        con.close()
        return

    print(f"待评分论文: {len(rows)} 篇")

    # ===== Step 1: 多数据源获取引用数据 =====
    print("\n[1/4] 获取引用数据 (S2 + OpenAlex) ...")
    pmids = [r["pmid"] for r in rows if r["pmid"]]
    citations = fetch_citations_all(con, pmids, force_refresh=no_cache)

    s2_hits = sum(1 for v in citations.values()
                  if v.get("s2_citation_count", 0) > 0)
    oa_hits = sum(1 for v in citations.values()
                  if v.get("oa_citation_count", 0) > 0)
    retracted = sum(1 for v in citations.values()
                    if v.get("oa_is_retracted"))
    print(f"  S2 有数据: {s2_hits} 篇, OpenAlex 有数据: {oa_hits} 篇")
    if retracted:
        print(f"  ⚠ 检测到 {retracted} 篇已撤稿论文!")

    # ===== Step 2: 逐篇评分 =====
    print("\n[2/4] 计算权威度评分 ...")
    results = []
    dist = {"A": 0, "B": 0, "C": 0, "RETRACTED": 0}

    for i, row in enumerate(rows):
        pmid = str(row["pmid"] or "")
        title = row["title"] or ""
        doi = row["doi"] or ""

        # 年份
        pub_year = None
        pub_date = row["pub_date"] or ""
        try:
            pub_year = int(str(pub_date)[:4])
        except (ValueError, TypeError):
            pass

        # 引用数据
        cite = citations.get(pmid, {})
        cite_count = max(
            cite.get("s2_citation_count", 0) or 0,
            cite.get("oa_citation_count", 0) or 0,
            cite.get("epmc_citation_count", 0) or 0,
        )
        oa_cite_count = cite.get("oa_citation_count", 0) or 0
        pct_max = cite.get("oa_percentile_max")
        is_retracted = cite.get("oa_is_retracted", False)
        oa_issn = cite.get("oa_issn_l", "")

        # --- 维度 1: CEBM (0-40) ---
        cebm_level, cebm_score, note = get_cebm_level(row["pub_types"], title)

        # --- 维度 2: 期刊 (0-30), 用 OpenAlex ISSN 辅助 ---
        tier = get_journal_tier(row["journal"], oa_issn)
        journal_score = JOURNAL_TIER_SCORE[tier]

        # --- 维度 3: 被引 (0-25) ---
        cs, clabel = citation_score(cite_count)

        # --- 维度 4: 领域归一化影响力 (0-5) ---
        rcr = cite.get("icite_rcr")
        pbonus = field_normalized_bonus(rcr=rcr, pct_max=pct_max)

        # --- 时效衰减 ---
        decay = year_decay(pub_year, cite_count, cebm_level)

        # --- 综合评分 ---
        raw_score = cebm_score + journal_score + cs + pbonus

        if is_retracted:
            authority_score = 0.0
            tier_label = "RETRACTED"
            note = "⚠ 撤稿论文! " + note
        else:
            authority_score = round(raw_score * decay, 1)
            if authority_score >= 70:
                tier_label = "A"
            elif authority_score >= 40:
                tier_label = "B"
            else:
                tier_label = "C"

        dist[tier_label] += 1

        # 构建 note (精简)
        parts = [note]
        if rcr is not None:
            parts.append(f"RCR={rcr:.1f}")
        elif pct_max is not None:
            top_pct = max(1, 100 - int(pct_max))
            parts.append(f"领域前{top_pct}%")
        icite_count = cite.get("icite_citation_count", 0) or 0
        epmc_count = cite.get("epmc_citation_count", 0) or 0
        if oa_cite_count > cite_count:
            parts.append(f"OA被引{oa_cite_count}次")
        if epmc_count > cite_count:
            parts.append(f"EPMC被引{epmc_count}次")
        # iCite 仅用于 RCR, 不参与引用计数合并
        if icite_count > 0:
            parts.append(f"iCite:{icite_count}次")
        if oa_issn:
            parts.append(f"ISSN:{oa_issn}")

        results.append({
            "id": row["id"], "pmid": pmid, "title": title[:120],
            "cebm_level": cebm_level, "cebm_score": cebm_score,
            "journal_tier": tier, "journal_score": journal_score,
            "citation_count": cite_count,
            "influential_citations": cite.get("s2_influential_count", 0) or 0,
            "oa_citation_count": oa_cite_count,
            "epmc_citation_count": epmc_count,
            "icite_citation_count": icite_count,
            "icite_rcr": rcr,
            "oa_percentile": int(pct_max) if pct_max is not None else None,
            "is_retracted": is_retracted,
            "citation_score": cs, "citation_label": clabel,
            "percentile_bonus": pbonus,
            "pub_year": pub_year, "year_decay": decay,
            "authority_score": authority_score,
            "authority_tier": tier_label,
            "grade_note": " | ".join(parts),
        })

        if (i + 1) % 20 == 0:
            print(f"  进度: {i+1}/{len(rows)}")

    print(f"  完成: A={dist.get('A',0)}, B={dist.get('B',0)}, "
          f"C={dist.get('C',0)}, RETRACTED={dist.get('RETRACTED',0)}")

    # ===== Step 3: 批量写入 DB =====
    print(f"\n[3/4] 写入数据库 (batch_size={batch_size}) ...")
    for start in range(0, len(results), batch_size):
        batch = results[start:start + batch_size]
        with con:
            for r in batch:
                con.execute("""
                    UPDATE papers SET
                        evidence_level = ?, evidence_score = ?,
                        journal_tier = ?, pub_type_rank = ?,
                        citation_count = ?, influential_citations = ?,
                        oa_citation_count = ?, epmc_citation_count = ?,
                        icite_citation_count = ?, icite_rcr = ?,
                        oa_percentile = ?, is_retracted = ?,
                        authority_tier = ?, authority_score = ?,
                        cebm_score = ?, journal_score = ?,
                        citation_score = ?, percentile_bonus = ?,
                        year_decay = ?, grade_note = ?
                    WHERE id = ?
                """, (
                    r["cebm_level"], r["authority_score"],
                    r["journal_tier"], r["cebm_score"],
                    r["citation_count"], r["influential_citations"],
                    r["oa_citation_count"], r.get("epmc_citation_count", 0),
                    r.get("icite_citation_count", 0),
                    r.get("icite_rcr"),
                    r["oa_percentile"],
                    1 if r["is_retracted"] else 0,
                    r["authority_tier"], r["authority_score"],
                    r["cebm_score"], r["journal_score"],
                    r["citation_score"], r["percentile_bonus"],
                    r["year_decay"], r["grade_note"],
                    r["id"],
                ))
        print(f"  写入 {start+1}-{min(start+batch_size, len(results))}/{len(results)}")
    con.commit()
    print("  写入完成")

    con.close()
    print_summary(results, dist)
    return results


def print_summary(results, dist):
    total = len(results)
    print(f"\n{'='*60}")
    print(f"  权威度分级报告 v3")
    print(f"{'='*60}")

    print(f"\n  总分分布:")
    for tier, label, emoji in [
        ("A", "高可信 (Meta/RCT/顶刊/高引)", "🟢"),
        ("B", "中可用 (综述/研究 + 一定引用)", "🟡"),
        ("C", "低可信 (Case/无引用/低影响)",  "🔴"),
        ("RETRACTED", "⚠ 已撤稿 - 不可使用", "⛔"),
    ]:
        cnt = dist.get(tier, 0)
        if cnt > 0:
            print(f"    {emoji} {tier} 级 ({label}): {cnt:3d} 篇 "
                  f"({cnt/total*100:.0f}%)")

    scores = [r["authority_score"] for r in results if r["authority_tier"] != "RETRACTED"]
    if scores:
        print(f"\n  分维度统计 (不含撤稿):")
        print(f"    综合分: min={min(scores):.0f} max={max(scores):.0f} "
              f"mean={sum(scores)/len(scores):.0f}")

    for dim, key in [("CEBM", "cebm_score"), ("期刊", "journal_score"),
                      ("引用", "citation_score"), ("领域加成", "percentile_bonus")]:
        vals = [r[key] for r in results if r["authority_tier"] != "RETRACTED"]
        if vals:
            print(f"    {dim}: min={min(vals)} max={max(vals)} mean={sum(vals)/len(vals):.0f}")

    # 引用覆盖率
    has_cite = sum(1 for r in results if r["citation_count"] > 0)
    has_oa = sum(1 for r in results if r.get("oa_citation_count", 0) > 0)
    has_ep = sum(1 for r in results if r.get("epmc_citation_count", 0) > 0)
    has_ic = sum(1 for r in results if r.get("icite_citation_count", 0) > 0)
    has_rcr = sum(1 for r in results if r.get("icite_rcr") is not None)
    has_pct = sum(1 for r in results if r.get("oa_percentile") is not None)
    print(f"\n  引用次数 (S1→S2→S3, 多源逐级补充):")
    print(f"    综合有引用:          {has_cite}/{total} ({has_cite/total*100:.0f}%)")
    print(f"    S1 Semantic Scholar: 70/{total} (主数据源)")
    print(f"    S2 OpenAlex:         {has_oa}/{total} ({has_oa/total*100:.0f}%)")
    if has_ep > 0:
        print(f"    S3 Europe PMC 补充:   {has_ep} 篇")
    print(f"\n  领域归一化影响力 (S4 iCite RCR, 仅对有引用论文):")
    print(f"    NIH RCR 覆盖:        {has_rcr}/{has_cite} 篇")
    print(f"    OpenAlex 百分位兜底: {has_pct}/{total} 篇")

    # 撤稿
    ret = dist.get("RETRACTED", 0)
    if ret > 0:
        print(f"\n  ⚠ 撤稿论文:")
        for r in results:
            if r["authority_tier"] == "RETRACTED":
                print(f"    PMID:{r['pmid']} {(r['title'] or '')[:100]}")

    # A 级 Top 5
    a_papers = [r for r in results if r["authority_tier"] == "A"]
    if a_papers:
        print(f"\n  🟢 A 级 Top 5:")
        for r in sorted(a_papers, key=lambda x: -x["authority_score"])[:5]:
            print(f"    [{r['authority_score']:.0f}] L{r['cebm_level']} T{r['journal_tier']} "
                  f"被引{r['citation_count']}次 +{r['percentile_bonus']}pct "
                  f"{(r['title'] or '')[:80]}")

    # C 级 Bottom 5
    c_papers = [r for r in results if r["authority_tier"] == "C"]
    if c_papers:
        print(f"\n  🔴 C 级 Bottom 5:")
        for r in sorted(c_papers, key=lambda x: x["authority_score"])[:5]:
            print(f"    [{r['authority_score']:.0f}] L{r['cebm_level']} T{r['journal_tier']} "
                  f"被引{r['citation_count']}次 "
                  f"{(r['title'] or '')[:80]}")

    # 被引 Top 5
    print(f"\n  📊 被引 Top 5 (多源综合取最大值):")
    for r in sorted(results, key=lambda x: -x["citation_count"])[:5]:
        oa_str = f"(OA:{r.get('oa_citation_count', 0)})" if r.get('oa_citation_count', 0) > r['citation_count'] else ""
        print(f"    被引 {r['citation_count']:4d} 次 {oa_str} [{r['authority_tier']}级 "
              f"{r['authority_score']:.0f}分] {(r['title'] or '')[:80]}")

    # 领域影响力 Top 5
    print(f"\n  🎯 领域归一化影响力 Top 5 (RCR+百分位):")
    rcr_papers = [r for r in results if r.get("icite_rcr") is not None]
    if rcr_papers:
        for r in sorted(rcr_papers, key=lambda x: -(x.get("icite_rcr") or 0))[:5]:
            rcr_val = r.get("icite_rcr", 0)
            print(f"    RCR={rcr_val:.1f} [{r['authority_tier']}级 {r['authority_score']:.0f}分] "
                  f"{(r['title'] or '')[:80]}")
    else:
        # Fallback to percentile
        pct_papers = [r for r in results if r.get("oa_percentile") is not None]
        for r in sorted(pct_papers, key=lambda x: -(x.get("oa_percentile") or 0))[:5]:
            pct = r.get("oa_percentile", 0)
            top_pct = max(1, 100 - pct)
            print(f"    前{top_pct}% [{r['authority_tier']}级 {r['authority_score']:.0f}分] "
                  f"{(r['title'] or '')[:80]}")

    # CEBM → 等级分布
    print(f"\n  📝 CEBM等级 → A/B/C 分布:")
    by_level = {}
    for r in results:
        l = r["cebm_level"]
        if l not in by_level:
            by_level[l] = {"A": 0, "B": 0, "C": 0, "R": 0, "cnt": 0}
        t = r["authority_tier"]
        if t == "RETRACTED":
            by_level[l]["R"] += 1
        else:
            by_level[l][t] += 1
        by_level[l]["cnt"] += 1
    for l in sorted(by_level.keys()):
        d = by_level[l]
        parts = [f"A={d['A']}", f"B={d['B']}", f"C={d['C']}"]
        if d['R'] > 0:
            parts.append(f"RETRACTED={d['R']}")
        print(f"    Level {l}: {', '.join(parts)} (共{d['cnt']}篇)")


def grade_summary():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    total = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    graded = con.execute(
        "SELECT COUNT(*) FROM papers WHERE authority_tier IS NOT NULL AND authority_tier != ''"
    ).fetchone()[0]

    if graded == 0:
        print("尚未评分，请先运行 evidence_grader_v3.py")
        con.close()
        return

    print(f"论文总数: {total}, 已评分: {graded}")
    print(f"\n=== 权威度分布 ===")
    for tier, label in [("A", "🟢 高可信"), ("B", "🟡 中可用"),
                         ("C", "🔴 低可信"), ("RETRACTED", "⛔ 已撤稿")]:
        cnt = con.execute(
            "SELECT COUNT(*) FROM papers WHERE authority_tier = ?", (tier,)
        ).fetchone()[0]
        if cnt > 0:
            bar = "█" * max(1, min(cnt * 50 // graded, 50))
            print(f"  {label}: {cnt:3d} 篇 ({cnt/graded*100:.0f}%) {bar}")

    # 引用统计
    cite = con.execute(
        "SELECT citation_count FROM papers WHERE citation_count IS NOT NULL AND citation_count > 0"
    ).fetchall()
    cite_vals = [r["citation_count"] for r in cite]
    if cite_vals:
        print(f"\n=== 被引统计 ===")
        print(f"  有引用: {len(cite_vals)} 篇")
        print(f"  最高: {max(cite_vals)}, 中位: {sorted(cite_vals)[len(cite_vals)//2]}, "
              f"平均: {sum(cite_vals)//len(cite_vals):.0f}")

    # 双源覆盖
    oa = con.execute(
        "SELECT COUNT(*) FROM papers WHERE oa_citation_count IS NOT NULL AND oa_citation_count > 0"
    ).fetchone()[0]
    ep = con.execute(
        "SELECT COUNT(*) FROM papers WHERE epmc_citation_count IS NOT NULL AND epmc_citation_count > 0"
    ).fetchone()[0]
    ic = con.execute(
        "SELECT COUNT(*) FROM papers WHERE icite_citation_count IS NOT NULL AND icite_citation_count > 0"
    ).fetchone()[0]
    rcr_cnt = con.execute(
        "SELECT COUNT(*) FROM papers WHERE icite_rcr IS NOT NULL"
    ).fetchone()[0]
    pct = con.execute(
        "SELECT COUNT(*) FROM papers WHERE oa_percentile IS NOT NULL"
    ).fetchone()[0]
    ret = con.execute(
        "SELECT COUNT(*) FROM papers WHERE is_retracted = 1"
    ).fetchone()[0]
    print(f"\n=== 数据覆盖 (四源) ===")
    print(f"  Semantic Scholar (主): 70+ 篇")
    print(f"  OpenAlex 有引用:       {oa} 篇")
    print(f"  NIH iCite 有引用:      {ic} 篇")
    print(f"  NIH iCite 有 RCR:      {rcr_cnt} 篇")
    print(f"  Europe PMC 补充:       {ep} 篇")
    print(f"  OpenAlex 有百分位:     {pct} 篇")
    print(f"  已撤稿:               {ret} 篇")

    con.close()


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Evidence Grader v3 — 多数据源三级权威度分级")
    p.add_argument("--force", action="store_true", help="强制重新评分")
    p.add_argument("--no-cache", action="store_true",
                   help="忽略引用缓存，重新从 API 拉取")
    p.add_argument("--summary", action="store_true", help="仅打印统计摘要")
    p.add_argument("--batch-size", type=int, default=5000,
                   help="批量写入大小 (默认5000)")
    args = p.parse_args()

    if args.summary:
        grade_summary()
    else:
        grade_all(batch_size=args.batch_size, force=args.force,
                  no_cache=args.no_cache)
