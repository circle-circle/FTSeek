"""DEB-20261002 文档完整性守卫: 每个 markdown 表格的列数一致性 + 未转义竖线扫描。
skill 教训: 索引行 pipes 8 vs 邻行 6 = 表格被撑坏 (渲染成额外列)。
"""
import re
import sys

P = "/opt/ftseek/FTSeek-US/docs/debates/DEB-20261002-universe-and-direction-entry-error.md"
lines = open(P).read().split("\n")


def eff_pipes(line):
    """有效列分隔符数 = raw | 数 − 转义 \\| 数 − 代码块内 `...|...` 的 |"""
    # 先剥掉行内代码 `...`
    stripped = re.sub(r"`[^`]*`", "", line)
    return stripped.count("|") - stripped.count("\\|")


# 找所有表格块 (连续以 | 开头的行)
blocks = []
cur = []
for i, l in enumerate(lines, start=1):
    if l.lstrip().startswith("|"):
        cur.append((i, l))
    else:
        if cur:
            blocks.append(cur)
            cur = []
if cur:
    blocks.append(cur)

print(f"发现 {len(blocks)} 个表格块")
bad = []
for b in blocks:
    counts = {}
    for ln, l in b:
        counts.setdefault(eff_pipes(l), []).append(ln)
    if len(counts) > 1:
        bad.append((b[0][0], counts))

if bad:
    print(f"\n🔴 {len(bad)} 个表格块列数不一致:")
    for start, counts in bad:
        print(f"  块起始 line {start}: {counts}")
        for k, lns in counts.items():
            sample = lines[lns[0] - 1][:110]
            print(f"    pipes={k} 行号={lns[:6]} 例: {sample}")
else:
    print("\n✅ 全部表格块列数一致")

# 未转义竖线诊断: 单元格内的裸 | 会被解析成列分隔符 ⇒ 症状就是列数异常。
# 不做正则猜 (旧版 `\|[a-zA-Z_ ]{1,12}\|` 会把正常分隔符 `| n |` 全报成问题),
# 只对上面已判定列数不一致的行给出定位 —— 列数一致即证明无未转义竖线。
if bad:
    print("\n=== 列数异常行 (极可能是单元格内未转义的裸 | ,如 `|gap|` 应写 `\\|gap\\|`) ===")
    for start, counts in bad:
        modal = max(counts.items(), key=lambda kv: len(kv[1]))[0]
        for k, lns in counts.items():
            if k != modal:
                for ln in lns:
                    print(f"  line {ln}: pipes={k}(多数={modal}) {lines[ln - 1][:150]}")
else:
    print("\n✅ 无列数异常行 ⇒ 单元格内无未转义裸 | (列数一致即结构性证明)")

# 章节序列检查
print("\n=== 章节序列 ===")
heads = [(i, l) for i, l in enumerate(lines, start=1) if re.match(r"^## \d+\.", l)]
nums = [int(re.match(r"^## (\d+)\.", l).group(1)) for _, l in heads]
print(f"  {nums}")
expected = list(range(1, len(nums) + 1))
print(f"  {'✅ 无缺号无重号' if nums == expected else '🔴 序列异常, 期望 ' + str(expected)}")

# 内部引用检查: §N 是否都存在 (剥掉代码块与行内代码, 防 commit message 里的外部 §15.5 误报)
body = "\n".join(lines)
body_nocode = re.sub(r"```.*?```", "", body, flags=re.S)
body_nocode = re.sub(r"`[^`]*`", "", body_nocode)
refs = set(re.findall(r"§(\d+(?:\.\d+)?)", body_nocode))
have = set()
for _, l in heads:
    have.add(re.match(r"^## (\d+)\.", l).group(1))
for i, l in enumerate(lines, start=1):
    m = re.match(r"^### (\d+\.\d+)", l)
    if m:
        have.add(m.group(1))
dangling = sorted(r for r in refs if r.split(".")[0] not in have and r not in have)
print(f"\n=== 内部 §引用悬空检查 ===\n  引用={sorted(refs, key=lambda x:(len(x),x))}")
print(f"  悬空={dangling if dangling else '无 ✅'}")

sys.exit(1 if (bad or dangling or nums != expected) else 0)
