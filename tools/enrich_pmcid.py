"""用 NCBI PMC ID Converter API 为 papers 表里的 PMID 查找真实 PMCID"""
import sqlite3, os, urllib.request, json, time, sys
os.environ['DATA_DIR'] = os.path.expanduser('~/work/zhaozhengyi/med-papers')
sys.path.insert(0, '/home/featurize/work/zhaozhengyi/med-paper-downloader')
from config import DB_PATH

def lookup_real_pmcid(pmid):
    """Query NCBI PMC ID Converter. Returns real PMCID or None."""
    url = 'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids=%s&format=json' % pmid
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'med-paper-downloader/1.0')
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode('utf-8'))
        records = data.get('records', [])
        if records and records[0].get('pmcid') and records[0].get('status') != 'error':
            return records[0]['pmcid']
    except Exception:
        pass
    return None

# ========== Main ==========
con = sqlite3.connect(str(DB_PATH))
con.row_factory = sqlite3.Row

rows = con.execute(
    "SELECT pmid, pmcid, title FROM papers WHERE (pmcid IS NULL OR pmcid = '') AND pmid IS NOT NULL"
).fetchall()

print('查询 %d 个 PMID ...' % len(rows))
found = 0

for r in rows:
    pmid = r['pmid']
    if not pmid:
        continue
    real_pmcid = lookup_real_pmcid(pmid)
    if real_pmcid:
        found += 1
        con.execute("UPDATE papers SET pmcid=? WHERE pmid=?", (real_pmcid, pmid))
        title = (r['title'] or '')[:60]
        print('  PMID %s -> %s  %s' % (pmid, real_pmcid, title))
    time.sleep(0.3)

con.commit()
print('\n完成: %d 篇找到真实 PMCID（是否 OA 全文需下载时验证）' % found)
con.close()
