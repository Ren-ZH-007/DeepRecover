#!/bin/bash
# DeepRecover APFS test images.
#
# Creates small APFS disk images, writes known files, deletes some of them and
# converts each image to a raw byte image for analysis. Everything happens in
# one new folder (default ~/DeepRecover-APFS-Test); nothing else on the Mac is
# touched. No administrator password is needed. Images are mounted inside that
# folder (hidden from Finder) and always detached at the end.
#
# Each scenario writes <name>.files.jsonl: every file written (size, SHA-256)
# and every deletion, so recovery results can be scored exactly. Results from
# the first run are in docs/APFS_FEASIBILITY.md.
#
# macOS 27 prints deprecation warnings for these hdiutil verbs (it suggests
# `diskutil image`); they still work and the warnings can be ignored.
#
# Usage:  bash scripts/make_apfs_test_images.sh [folder]
set -euo pipefail

WORK="${1:-$HOME/DeepRecover-APFS-Test}"
if [ "$(uname -s)" != Darwin ]; then echo "需要在 macOS 上运行。"; exit 1; fi
if [ -e "$WORK" ] && [ -n "$(ls -A "$WORK" 2>/dev/null)" ]; then
    echo "文件夹 $WORK 已存在且不为空。为避免覆盖任何内容，脚本已停止。"
    exit 1
fi
mkdir -p "$WORK/mnt"
cd "$WORK"
exec > >(tee -a "$WORK/log.txt") 2>&1

MOUNTED=""
cleanup() {
    if [ -n "$MOUNTED" ]; then hdiutil detach "$MOUNTED" -force >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

step() { echo "[$(date +%H:%M:%S)] $*"; }
sha() { shasum -a 256 "$1" | awk '{print $1}'; }

# One JSON line per event: files written (with size and SHA-256) and deletions.
record() {   # scenario relative-path note
    local file="$MNT/$2"
    printf '{"event":"written","path":"%s","size":%s,"sha256":"%s","note":"%s"}\n' \
        "$2" "$(stat -f %z "$file")" "$(sha "$file")" "$3" >> "$WORK/$1.files.jsonl"
}
remove() {   # scenario relative-path
    rm -rf "${MNT:?}/$2"
    printf '{"event":"deleted","path":"%s"}\n' "$2" >> "$WORK/$1.files.jsonl"
}

make_volume() {   # name UDIF|SPARSE size
    local name=$1 type=$2 size=$3
    hdiutil create -size "$size" -fs APFS -volname "$name" -layout GPTSPUD -type "$type" "$WORK/$name" >/dev/null
    if [ "$type" = SPARSE ]; then IMAGE="$WORK/$name.sparseimage"; else IMAGE="$WORK/$name.dmg"; fi
    MNT="$WORK/mnt/$name"
    mkdir -p "$MNT"
    hdiutil attach "$IMAGE" -nobrowse -mountpoint "$MNT" >/dev/null
    MOUNTED="$MNT"
    touch "$MNT/.metadata_never_index"   # keep Spotlight from writing to the test volume
}
settle() { sync; sleep 3; }
finish_volume() { settle; hdiutil detach "$MOUNTED" >/dev/null; MOUNTED=""; }
to_raw() { hdiutil convert "$1" -format UDTO -o "$WORK/$2" >/dev/null; }   # writes $2.cdr

text_file() {   # path lines
    awk -v n="$2" 'BEGIN { for (i = 1; i <= n; i++) printf "DeepRecover APFS 测试文本，第 %d 行。The quick brown fox jumps over the lazy dog.\n", i }' > "$1"
}
random_file() { head -c "$2" /dev/urandom > "$1"; }

populate_basic() {   # scenario
    local s=$1
    mkdir -p "$MNT/docs" "$MNT/media"
    text_file "$MNT/docs/notes.txt" 3000;                         record "$s" docs/notes.txt text
    textutil -inputencoding UTF-8 -convert docx -output "$MNT/docs/report.docx" "$MNT/docs/notes.txt"; record "$s" docs/report.docx docx
    if cupsfilter "$MNT/docs/notes.txt" > "$MNT/docs/notes.pdf" 2>/dev/null && [ -s "$MNT/docs/notes.pdf" ]; then
        record "$s" docs/notes.pdf pdf
    else
        rm -f "$MNT/docs/notes.pdf"; echo "（cupsfilter 不可用，跳过 PDF）"
    fi
    random_file "$MNT/media/big.bin" 1500000;                      record "$s" media/big.bin random
    random_file "$MNT/media/small.bin" 5000;                       record "$s" media/small.bin random
    random_file "$MNT/media/中文名称.bin" 70000;                    record "$s" "media/中文名称.bin" "unicode name"
    text_file "$MNT/plain.txt" 4000
    ditto --hfsCompression "$MNT/plain.txt" "$MNT/compressed.txt"
    rm -f "$MNT/plain.txt"
    if ls -lO "$MNT/compressed.txt" | grep compressed >/dev/null; then note=compressed; else note="compression not applied"; fi
    record "$s" compressed.txt "$note"
    random_file "$MNT/clone_a.bin" 300000;                         record "$s" clone_a.bin "clone source"
    cp -c "$MNT/clone_a.bin" "$MNT/clone_b.bin";                   record "$s" clone_b.bin "clone of clone_a.bin"
    text_file "$MNT/keep.txt" 500;                                 record "$s" keep.txt "live control"
    settle
    remove "$s" docs
    remove "$s" media/big.bin
    remove "$s" compressed.txt
    remove "$s" clone_b.bin
}

step "场景 1：基本删除"
make_volume t1_basic UDIF 64m
populate_basic t1_basic
finish_volume
to_raw "$WORK/t1_basic.dmg" t1_basic_raw

step "场景 2：反复写入删除、碎片文件、删除后继续写入"
make_volume t2_churn UDIF 48m
for round in 1 2 3 4 5; do
    for i in $(seq 1 40); do
        random_file "$MNT/r${round}_$i.bin" $(( (RANDOM % 50 + 1) * 4096 ))
        record t2_churn "r${round}_$i.bin" "round $round"
    done
    settle
    for i in $(seq 1 2 40); do remove t2_churn "r${round}_$i.bin"; done
done
n=0
while [ "$n" -lt 400 ] && [ "$(df -k "$MNT" | awk 'NR==2 {print $4}')" -gt 6144 ]; do
    n=$((n + 1)); random_file "$MNT/filler_$n.bin" 262144
done
step "已写入 $n 个填充文件"
if [ "$n" -ge 2 ]; then for i in $(seq 2 2 "$n"); do rm -f "$MNT/filler_$i.bin"; done; fi
settle
random_file "$MNT/fragmented.bin" 3000000;                         record t2_churn fragmented.bin "expected fragmented"
settle
remove t2_churn fragmented.bin
for i in $(seq 1 8); do random_file "$MNT/after_$i.bin" 200000; record t2_churn "after_$i.bin" "written after deletions"; done
finish_volume
to_raw "$WORK/t2_churn.dmg" t2_churn_raw

step "场景 3、4：稀疏镜像，压缩前后"
make_volume t3_sparse SPARSE 64m
populate_basic t3_sparse
finish_volume
to_raw "$WORK/t3_sparse.sparseimage" t3_sparse_raw
hdiutil compact "$WORK/t3_sparse.sparseimage" >/dev/null 2>&1 || echo "（hdiutil compact 未执行成功）"
to_raw "$WORK/t3_sparse.sparseimage" t3_sparse_compacted_raw
cp t3_sparse.files.jsonl t3_sparse_compacted.files.jsonl

{
    printf '{"macos":"%s","build":"%s","arch":"%s","created":"%s"}\n' \
        "$(sw_vers -productVersion)" "$(sw_vers -buildVersion)" "$(uname -m)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$WORK/info.json"
rmdir "$WORK"/mnt/* "$WORK/mnt" 2>/dev/null || true

step "完成。生成的文件："
ls -lh "$WORK"
echo
echo "测试镜像在：$WORK（分析用 *_raw.cdr 和 *.files.jsonl）"
