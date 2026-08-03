#!/bin/bash
# ==============================================================
# 论文下载 → 知识图谱抽取 一键 Pipeline (v5 — 时间戳归档版)
#
# v5 改进:
#   - 每次运行自动创建时间戳目录，不再覆盖历史结果
#   - 支持 --tag 自定义运行标签
#   - out/runs/latest -> 指向最新一次运行
#   - 手动分步仍使用 candidate_kg/（向后兼容）
#
# 用法:
#   bash run_pipeline.sh all "astragalus membranaceus" 10
#   bash run_pipeline.sh all "黄芪 糖尿病" 5 --mock
#   bash run_pipeline.sh all "aspirin" 20 --tag aspirin-study
#   bash run_pipeline.sh download "astragalus" 20
#   bash run_pipeline.sh batch-pmc --limit 5
#
# 查看历史结果:
#   ls out/runs/
#   cat out/runs/latest/paper_triples_merged.jsonl  # 最新合并结果
# ==============================================================
set -e

BASE=/home/featurize/work/zhaozhengyi
DOWNLOADER=$BASE/med-paper-downloader
EXTRACTOR=$BASE/paper-kg-extraction
TOOLS=$BASE/tools
BRIDGE=$TOOLS/bridge_papers_to_json.py
PMC_SCRIPT=$TOOLS/download_one_pmc.py
BATCH_PMC=$TOOLS/batch_download_pmc.py
DB=$BASE/med-papers/metadata.db
JSON_DIR=$EXTRACTOR/data/papers_json
# ── 时间戳 & 运行标签 ──
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RUN_TAG=""           # 用户自定义标签（空=自动时间戳）
RUN_BASE=$EXTRACTOR/out/runs
LATEST_LINK=$RUN_BASE/latest

# 解析全局 --tag
for _arg in "$@"; do
    case "$_arg" in
        --tag) NEXT_IS_TAG=1 ;;
        --tag=*) RUN_TAG="${_arg#--tag=}" ;;
        *)
            if [ "${NEXT_IS_TAG:-0}" = "1" ]; then
                RUN_TAG="$_arg"
                NEXT_IS_TAG=0
            fi
            ;;
    esac
done
[ -z "$RUN_TAG" ] && RUN_TAG=$TIMESTAMP

RUN_DIR=$RUN_BASE/$RUN_TAG

# 传统默认输出路径（手动分步时使用）
OUT_LEGACY=$EXTRACTOR/out/candidate_kg/paper_triples.jsonl
MERGED_LEGACY=$EXTRACTOR/out/candidate_kg/paper_triples_merged.jsonl


# ── 数据质量检查函数 ──
check_pmcid() {
    echo "=== 数据质量: PMCID 统计 ==="
    cd $DOWNLOADER
    python -c "
import sqlite3, os
os.environ['DATA_DIR'] = os.path.expanduser('$BASE/med-papers')
from config import DB_PATH
con = sqlite3.connect(str(DB_PATH))
total = con.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
has_pmcid = con.execute(\"SELECT COUNT(*) FROM papers WHERE pmcid IS NOT NULL AND pmcid != ''\").fetchone()[0]
has_ft = con.execute('SELECT COUNT(*) FROM pmc_fulltext').fetchone()[0]
print(f'  papers 表: {total} 篇')
print(f'  有 PMCID: {has_pmcid} 篇')
print(f'  pmc_fulltext 表: {has_ft} 篇（已下载全文）')
if has_ft > 0:
    print(f'  摘要抽取: {total - has_ft} 篇 | 全文抽取: {has_ft} 篇')
con.close()
"
}

# ── 列出历史运行记录 ──
list_runs() {
    echo "=== 历史运行记录 ==="
    if [ -d "$RUN_BASE" ]; then
        for d in "$RUN_BASE"/*/; do
            name=$(basename "$d")
            [ "$name" = "latest" ] && continue
            triples="$d/paper_triples.jsonl"
            merged="$d/paper_triples_merged.jsonl"
            if [ -f "$triples" ]; then
                echo "  $name  triples=$(wc -l < "$triples") 条"
            elif [ -f "$merged" ]; then
                echo "  $name  merged=$(wc -l < "$merged") 条"
            else
                echo "  $name  (空)"
            fi
        done
        if [ -L "$LATEST_LINK" ]; then
            echo "  latest -> $(readlink "$LATEST_LINK" | xargs basename)"
        fi
    else
        echo "  (暂无)"
    fi
}

case "$1" in
  download)
    QUERY=${2:-"Astragalus membranaceus"}
    N=${3:-10}
    echo "=== Step 1: 下载论文摘要: $QUERY (max=$N) ==="
    cd $DOWNLOADER
    python cli.py search -q "$QUERY" -s pubmed -n $N
    echo ""
    echo "下一步:"
    echo "  bash run_pipeline.sh enrich     # 查找 PMCID"
    echo "  bash run_pipeline.sh batch-pmc  # 批量下载全文"
    ;;

  download-full)
    QUERY=${2:-"Astragalus membranaceus"}
    N=${3:-10}
    echo "=== 完整采集: PubMed 摘要 + PMCID + PMC OA 全文 ==="
    bash "$0" download "$QUERY" "$N"
    bash "$0" enrich
    bash "$0" batch-pmc
    echo "=== 完整采集结束；无可用全文的论文保留摘要 ==="
    ;;

  download-batch)
    QUERY_FILE=${2:-queries.tsv}
    if [ ! -f "$QUERY_FILE" ]; then
        echo "错误: 找不到查询表 $QUERY_FILE"
        echo "格式: 第一列 query，第二列 limit，以 Tab 分隔；可包含表头"
        exit 2
    fi
    echo "=== 批量采集查询表: $QUERY_FILE ==="
    _line_no=0
    while IFS=$'\t' read -r QUERY N _rest || [ -n "$QUERY" ]; do
        _line_no=$((_line_no + 1))
        QUERY=${QUERY%$'\r'}
        N=${N%$'\r'}
        [ -z "$QUERY" ] && continue
        case "$QUERY" in \#*) continue ;; esac
        if [ "$_line_no" -eq 1 ] && [ "$QUERY" = "query" ]; then
            continue
        fi
        N=${N:-100}
        case "$N" in
            *[!0-9]*) echo "错误: 第 $_line_no 行 limit 不是整数: $N"; exit 2 ;;
        esac
        echo ""
        echo "=== [$_line_no] 下载: $QUERY (limit=$N) ==="
        bash "$0" download "$QUERY" "$N"
    done < "$QUERY_FILE"

    echo ""
    echo "=== 所有查询完成，统一补全 PMCID ==="
    bash "$0" enrich
    echo ""
    echo "=== 下载所有可用 PMC OA 全文 ==="
    bash "$0" batch-pmc
    echo ""
    echo "=== 批量采集完成；无全文论文保留 PubMed 摘要 ==="
    ;;

  pmc-download)
    PMCID=${2:-"PMC8257355"}
    echo "=== 下载单篇 PMC OA 全文: $PMCID ==="
    cd $BASE
    python $PMC_SCRIPT "$PMCID"
    ;;

  batch-pmc)
    shift
    echo "=== 批量下载 PMC OA 全文 (自动跳过不可用/非OA) ==="
    cd $BASE
    python $BATCH_PMC "$@"
    echo ""
    echo "全文下载完成。现在可用全文抽取:"
    echo "  bash run_pipeline.sh extract   # 自动优先使用全文"
    ;;

  enrich)
    echo "=== Step 2: 查找真实 PMCID (NCBI ID Converter) ==="
    cd $BASE
    python $TOOLS/enrich_pmcid.py
    echo ""
    echo "下一步:"
    echo "  bash run_pipeline.sh batch-pmc  # 批量下载全文"
    ;;

  grade)
    echo "=== Step 3: 论文权威度评分 (四数据源) ==="
    cd $BASE
    python $TOOLS/evidence_grader.py --force
    ;;

  bridge)
    N=${2:-10}
    echo "=== Step 4: 桥接: papers → JSON ($N 篇) ==="
    python $BRIDGE --db $DB --out $JSON_DIR --limit $N --clean
    ;;

  extract)
    shift
    _has_out=""
    _batch_id="batch:pubmed:$(date +%Y%m%d):auto"
    for _a in "$@"; do
        case "$_a" in
            --out|--out=*) _has_out="1" ;;
        esac
    done
    echo "=== Step 5: 抽取知识图谱 (自动混合摘要+全文) ==="
    echo "注: JSON 目录中同时包含摘要和全文，全文优先"
    cd $EXTRACTOR
    if [ -n "$_has_out" ]; then
        python scripts/02_run_extraction.py --from json --input data/papers_json \
          --batch-id "$_batch_id" "$@"
    else
        python scripts/02_run_extraction.py --from json --input data/papers_json \
          --out "$OUT_LEGACY" --batch-id "$_batch_id" "$@"
    fi
    ;;

  extract-fulltext)
    echo "=== Step 5: 从单篇 PMC 全文抽取知识图谱 ==="
    cd $EXTRACTOR
    rm -f $OUT_LEGACY
    python scripts/02_run_extraction.py \
      --from json --input data/single_pmc.json \
      --out $OUT_LEGACY \
      --batch-id "batch:pmc_oa:$(date +%Y%m%d):verified"
    ;;

  merge-interactions)
    shift  # 去掉命令名本身
    _merged_out=$MERGED_LEGACY
    _input=$OUT_LEGACY
    for _a in "$@"; do
        case "$_a" in
            --out) _merged_out="__next__" ;;
            --out=*) _merged_out="${_a#--out=}" ;;
            *)
                if [ "$_merged_out" = "__next__" ]; then
                    _merged_out="$_a"
                elif [ -z "${_input_set:-}" ]; then
                    _input="$_a"
                    _input_set=1
                fi
                ;;
        esac
    done
    echo "=== Step 6: 跨论文合并交互 + 重新计算 ICS v2.0 ==="
    echo "输入: $_input"
    echo "输出: $_merged_out"
    cd $EXTRACTOR
    python scripts/04_merge_interactions.py "$_input" --out "$_merged_out" \
      --report "$(dirname "$_merged_out")/merge_report.json"
    ;;

  validate)
    TARGET=${2:-$OUT_LEGACY}
    echo "=== Step 7: 验收自检 ==="
    cd $EXTRACTOR
    python scripts/03_validate.py "$TARGET"
    ;;

  status)
    echo "=== 数据统计 ==="
    check_pmcid
    echo ""
    echo "=== 权威度分级 (v2 四数据源) ==="
    cd $BASE
    python $TOOLS/evidence_grader.py --summary
    echo ""
    list_runs
    ;;

  all)
    shift
    # ── 解析 all 命令的专属参数 ──
    QUERY=""
    N=""
    MOCK=""
    ALL_TAG=""
    NEXT_IS_TAG=0
    NEXT_IS_N=0
    for _a in "$@"; do
        case "$_a" in
            --tag) NEXT_IS_TAG=1 ;;
            --tag=*) ALL_TAG="${_a#--tag=}" ;;
            --mock) MOCK="--mock" ;;
            *)
                if [ "$NEXT_IS_TAG" = "1" ]; then
                    ALL_TAG="$_a"
                    NEXT_IS_TAG=0
                elif [ -z "$QUERY" ]; then
                    QUERY="$_a"
                elif [ -z "$N" ]; then
                    N="$_a"
                fi
                ;;
        esac
    done
    QUERY=${QUERY:-"Astragalus membranaceus"}
    N=${N:-10}
    ALL_TAG=${ALL_TAG:-$RUN_TAG}

    # ── 创建本次运行的专属目录 ──
    ALL_RUN_DIR=$RUN_BASE/$ALL_TAG
    ALL_OUT=$ALL_RUN_DIR/paper_triples.jsonl
    ALL_MERGED=$ALL_RUN_DIR/paper_triples_merged.jsonl
    ALL_REPORT=$ALL_RUN_DIR/extraction_report.json
    ALL_MERGE_REPORT=$ALL_RUN_DIR/merge_report.json
    ALL_JSON_DIR=$ALL_RUN_DIR/papers_json

    mkdir -p "$ALL_RUN_DIR"

    echo "╔══════════════════════════════════════════╗"
    echo "║  论文 → 知识图谱 Pipeline (v5)          ║"
    echo "║  有全文 → 下全文 | 无全文 → 用摘要      ║"
    echo "║  运行标签: $ALL_TAG                     ║"
    echo "║  输出目录: $ALL_RUN_DIR                 ║"
    echo "╚══════════════════════════════════════════╝"
    check_pmcid
    echo ""

    bash $0 download "$QUERY" $N      # 1. PubMed 搜索 → 摘要
    bash $0 enrich                     # 2. NCBI ID Converter → 找 PMCID
    bash $0 grade                      # 3. 权威度评分 (CEBM+期刊+引用+时效)
    bash $0 batch-pmc                  # 4. 有 PMCID → 自动下载全文
    # 独立输入快照；newest-first 确保选择本次下载后最近的 N 篇。
    python $BRIDGE --db $DB --out "$ALL_JSON_DIR" --limit $N --newest-first --clean

    # 6. LLM 抽取三元组
    echo "=== Step 6/9: 抽取知识图谱 (进度条↓) ==="
    echo "输出: $ALL_OUT"
    cd $EXTRACTOR
    python scripts/02_run_extraction.py \
      --from json --input "$ALL_JSON_DIR" \
      --out "$ALL_OUT" \
      --batch-id "batch:pubmed:$ALL_TAG" $MOCK

    # 7. 跨论文合并交互 + ICS v2.0 评分
    echo "=== Step 7: 跨论文合并交互 + ICS v2.0 评分 ==="
    echo "输入: $ALL_OUT"
    echo "输出: $ALL_MERGED"
    cd $EXTRACTOR
    python scripts/04_merge_interactions.py "$ALL_OUT" --out "$ALL_MERGED" \
      --report "$ALL_MERGE_REPORT"

    # 8. 验收自检
    echo "=== Step 8: 验收自检 ==="
    cd $EXTRACTOR
    python scripts/03_validate.py "$ALL_OUT"
    python scripts/03_validate.py "$ALL_MERGED"

    # 9. 建立 latest 软链接
    rm -f "$LATEST_LINK"
    ln -sfn "$ALL_RUN_DIR" "$LATEST_LINK"

    # 10. 数据统计
    bash $0 status

    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  Pipeline 完成!                         ║"
    echo "║  运行标签: $ALL_TAG                     ║"
    echo "║  输出目录: $ALL_RUN_DIR                 ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "产物:"
    echo "  三元组:     $ALL_OUT"
    echo "  合并后:     $ALL_MERGED"
    echo "  最新快捷:   $LATEST_LINK -> $(basename "$ALL_RUN_DIR")"
    echo ""
    echo "查看历史:  bash run_pipeline.sh list-runs"
    ;;

  extract-all)
    shift
    # ── 解析参数 ──
    ALL_TAG=""
    MOCK=""
    NO_RESUME=""
    RESUME_MODE=""
    for _a in "$@"; do
        case "$_a" in
            --tag) NEXT_IS_TAG=1 ;;
            --tag=*) ALL_TAG="${_a#--tag=}" ;;
            --mock) MOCK="--mock" ;;
            --no-resume) NO_RESUME="--no-resume" ;;
            --resume) RESUME_MODE="1" ;;
            *)
                if [ "${NEXT_IS_TAG:-0}" = "1" ]; then
                    ALL_TAG="$_a"
                    NEXT_IS_TAG=0
                fi
                ;;
        esac
    done

    # --resume: 自动找最新一次运行续跑
    if [ -n "$RESUME_MODE" ] && [ -z "$ALL_TAG" ]; then
        if [ -d "$RUN_BASE" ] && [ "$(ls -A "$RUN_BASE" 2>/dev/null)" ]; then
            ALL_TAG=$(ls -t "$RUN_BASE" | grep -v '^latest$' | head -1)
            echo "== 断点续跑: 自动选择最新运行 '$ALL_TAG' =="
        else
            echo "错误: 没有历史运行记录，无法 --resume"
            exit 1
        fi
    fi
    ALL_TAG=${ALL_TAG:-$RUN_TAG}

    # ── 运行目录 ──
    ALL_RUN_DIR=$RUN_BASE/$ALL_TAG
    ALL_OUT=$ALL_RUN_DIR/paper_triples.jsonl
    ALL_MERGED=$ALL_RUN_DIR/paper_triples_merged.jsonl
    ALL_MERGE_REPORT=$ALL_RUN_DIR/merge_report.json
    ALL_JSON_DIR=$ALL_RUN_DIR/papers_json
    mkdir -p "$ALL_RUN_DIR"

    # ── 检测断点续跑状态 ──
    EXISTING_TRIPLES=0
    EXISTING_MERGED=0
    if [ -f "$ALL_OUT" ]; then
        EXISTING_TRIPLES=$(wc -l < "$ALL_OUT" 2>/dev/null || echo 0)
    fi
    if [ -f "$ALL_MERGED" ]; then
        EXISTING_MERGED=$(wc -l < "$ALL_MERGED" 2>/dev/null || echo 0)
    fi

    if [ "$EXISTING_TRIPLES" -gt 0 ]; then
        echo "╔══════════════════════════════════════════╗"
        echo "║  🔄 断点续跑模式                        ║"
        echo "║  运行标签: $ALL_TAG                     ║"
        echo "║  已有三元组: $EXISTING_TRIPLES 条         ║"
        echo "║  已处理论文将自动跳过                    ║"
        echo "╚══════════════════════════════════════════╝"
    else
        echo "╔══════════════════════════════════════════╗"
        echo "║  全库抽取 (自动下载全文后抽取 DB 中全部论文) ║"
        echo "║  运行标签: $ALL_TAG                     ║"
        echo "║  输出目录: $ALL_RUN_DIR                 ║"
        echo "╚══════════════════════════════════════════╝"
    fi
    check_pmcid
    echo ""

    # ── Step 1: 权威度评分 (总是重跑，秒级) ──
    echo "=== Step 1/5: 论文权威度评分 ==="
    cd $BASE
    python $TOOLS/evidence_grader.py --force
    echo ""

    # ── Step 2: 下载 PMC OA 全文 ──
    echo "=== Step 2/6: 下载 PMC OA 全文 (自动跳过无全文/非OA) ==="
    cd $BASE
    python $TOOLS/batch_download_pmc.py
    echo ""

    # ── Step 3: 创建本次运行的独立输入快照 ──
    echo "=== Step 3/6: 桥接全部论文 → 独立 JSON 快照 ==="
    python $TOOLS/bridge_papers_to_json.py --db $DB --out "$ALL_JSON_DIR" --limit 0 --clean
    echo ""

    # ── Step 4: LLM 抽取 (tqdm 进度条，需在 conda activate 后运行) ──
    echo "=== Step 4/6: 抽取知识图谱 (进度条↓) ==="
    echo "输出: $ALL_OUT"
    cd $EXTRACTOR
    python scripts/02_run_extraction.py \
      --from json --input "$ALL_JSON_DIR" \
      --out "$ALL_OUT" \
      --batch-id "batch:pubmed:$ALL_TAG" $MOCK $NO_RESUME
    echo ""

    # ── Step 5: 跨论文合并 + ICS v2.0 ──
    echo "=== Step 5/6: 合并交互 + ICS v2.0 评分 ==="
    python scripts/04_merge_interactions.py "$ALL_OUT" --out "$ALL_MERGED" \
      --report "$ALL_MERGE_REPORT"
    echo ""

    # ── Step 6: 验收 ──
    echo "=== Step 6/6: 验收自检 ==="
    python scripts/03_validate.py "$ALL_OUT"
    python scripts/03_validate.py "$ALL_MERGED"
    echo ""

    # ── latest 链接 ──
    rm -f "$LATEST_LINK"
    ln -sfn "$ALL_RUN_DIR" "$LATEST_LINK"

    # ── 最终统计 ──
    FINAL_TRIPLES=$(wc -l < "$ALL_OUT" 2>/dev/null || echo 0)
    FINAL_MERGED=$(wc -l < "$ALL_MERGED" 2>/dev/null || echo 0)
    echo "╔══════════════════════════════════════════╗"
    echo "║  全库抽取完成!                          ║"
    echo "║  运行标签: $ALL_TAG                     ║"
    echo "║  三元组:   $FINAL_TRIPLES 条             ║"
    echo "║  合并后:   $FINAL_MERGED 条              ║"
    echo "║  快捷方式: out/runs/latest              ║"
    echo "╚══════════════════════════════════════════╝"
    ;;

  list-runs)
    list_runs
    ;;

  *)
    echo "用法: $0 {download|download-full|download-batch|enrich|batch-pmc|pmc-download|grade|bridge|extract|merge-interactions|extract-fulltext|validate|status|list-runs|all|extract-all} [参数]"
    echo ""
    echo "  v5 时间戳归档: 每次 all 运行自动创建独立目录，不再覆盖历史"
    echo ""
    echo "  一键全流程:"
    echo "    all \"查询词\" N [--mock] [--tag 标签]"
    echo "    示例:"
    echo "      all \"Astragalus\" 20                    # 搜索+下载+抽取"
    echo "      all \"黄芪 糖尿病\" 10 --tag huangqi     # 自定义标签"
    echo ""
    echo "  全库抽取 (跳过下载，直接抽取 DB 中全部论文):"
    echo "    extract-all [--mock] [--tag 标签] [--no-resume] [--resume]"
    echo "    示例:"
    echo "      extract-all                                    # 全量抽取"
    echo "      extract-all --tag full-run-20260715            # 自定义标签"
    echo "      extract-all --mock                             # Mock 测试"
    echo "      extract-all --no-resume                        # 强制重跑（不续跑）"
    echo "      extract-all --resume                           # 自动续跑最新一次运行"
    echo ""
    echo "  分步:"
    echo "    download \"查询词\" N            搜索并下载 PubMed 摘要"
    echo "    download-full \"查询词\" N       单查询: 摘要 + PMCID + 可用全文"
    echo "    download-batch queries.tsv        批量查询后统一补 PMCID/下载全文"
    echo "    enrich                          查找真实 PMCID（NCBI ID Converter）"
    echo "    batch-pmc [--limit N]           批量下载所有可用 PMC OA 全文"
    echo "    pmc-download PMC1234567         下载单篇 PMC OA 全文"
    echo "    grade                           论文权威度评分 (CEBM+期刊+引用+时效 → 0-100)"
    echo "    bridge N                        桥接 N 篇论文为 JSON (含 authority + CEBM)"
    echo "    extract [--mock]                LLM 抽取三元组 (自动混合摘要+全文)"
    echo "    merge-interactions              跨论文合并药物对 + ICS v2.0 评分 (8维)"
    echo "    extract-fulltext                从单篇 PMC 全文 JSON 抽取"
    echo "    validate [file]                 验收自检 (必须 100% 通过)"
    echo "    status                          数据统计 + 质量检查"
    echo "    list-runs                       列出所有历史运行记录"
    ;;
esac
