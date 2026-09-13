"""Generate and check documentation metadata without executing trading code."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_NAMES = {
    0: "00_文档索引.md", 1: "01_原始需求.md", 2: "02_需求拆解.md",
    3: "03_产品分析.md", 4: "04_系统架构设计.md", 5: "05_核心业务规则设计.md",
    6: "06_开发计划.md", 7: "07_测试与验收方案.md", 8: "08_运维与上线方案.md",
    9: "09_前期准备与规则核验清单.md",
}
DOC_PATHS = {n: ROOT / "docs" / name for n, name in DOC_NAMES.items()}
FENCE = chr(96) * 3


class InvalidDocs(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise InvalidDocs(message)


def read(path):
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def table_rows(text):
    return [line.split("|")[1:-1] for line in text.splitlines() if line.startswith("|")]


def rows(text):
    return [[cell.strip() for cell in row] for row in table_rows(text)]


def table(headers, items):
    def row(cells):
        return "| " + " | ".join(str(c).replace("|", r"\|").replace("\n", " ") for c in cells) + " |"
    return "\n".join([row(headers), row([":---"] * len(headers)), *(row(item) for item in items)])


def natural(value):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", value)]


def join(values):
    return "、".join(values) if values else "—"


def fill(text, tag, content):
    begin = "<!-- BEGIN GENERATED: " + tag + " -->"
    end = "<!-- END GENERATED: " + tag + " -->"
    require(text.count(begin) == 1 and text.count(end) == 1, "Missing/duplicate generated block: " + tag)
    left, rest = text.split(begin, 1)
    _, right = rest.split(end, 1)
    return left + begin + "\n" + content.rstrip() + "\n" + end + right


def source_cases(text):
    pairs = [(row[0], row[:3]) for row in rows(text) if re.fullmatch(r"A\d{2}(?:-\d{2})?", row[0])]
    require(len(pairs) == 36, "Source must retain 29 cases and 7 A25 subcases")
    return dict(pairs)


def source_stages(text):
    result = {}
    for row in rows(text):
        match = re.match(r"\*\*阶段 ([0-7])：", row[0])
        if match:
            result["S" + match[1]] = row[1:3]
    require(len(result) == 8, "Source stages must be S0-S7")
    return result


def unique(items, key, label):
    ids = [item[key] for item in items]
    require(len(ids) == len(set(ids)), "Duplicate " + label)
    return set(ids)


def validate_registry(reg, source):
    require(reg["schema_version"] == 1, "Unsupported registry schema")
    require(reg["baseline"]["execution_status"] == "not_executed", "Docs validation is not trading acceptance")
    req_ids = unique(reg["requirements"], "id", "requirement ID")
    raw_ids = unique(reg["original_requirements"], "id", "R ID")
    case_ids = unique(reg["acceptances"], "id", "acceptance ID")
    check_ids = unique(reg["checks"], "id", "check ID")
    proposal_ids = unique(reg["proposals"], "id", "proposal ID")
    stage_ids = unique(reg["stages"], "id", "stage ID")
    require(stage_ids == {"S" + str(n) for n in range(8)}, "Stage registry must contain S0-S7")
    require(case_ids == set(source_cases(source)), "Acceptance registry differs from source IDs")
    require(raw_ids == {"R-" + str(n).zfill(2) for n in range(1, len(raw_ids) + 1)}, "R IDs are not contiguous")
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', source))
    referred_raw = set()
    pending_requirements = set()
    for raw in reg["original_requirements"]:
        require(raw["source"]["file"] == reg["baseline"]["source_file"], "R source file differs: " + raw["id"])
        require(set(raw["source"]["anchors"]) <= anchors, "Invalid source anchor: " + raw["id"])
    for req in reg["requirements"]:
        ident = req["id"]
        require(re.fullmatch(r"(?:FR-[A-Z]+|NFR)-\d{2}", ident), "Invalid requirement ID: " + ident)
        require(req["status"] in {"formal", "pending"}, "Invalid status: " + ident)
        require(req["applicability"].strip(), "Missing applicability: " + ident)
        require(len(req["acceptance_ids"]) == len(set(req["acceptance_ids"])), "Duplicate acceptance edge: " + ident)
        require(set(req["acceptance_ids"]) <= case_ids, "Unknown acceptance: " + ident)
        require(set(req["acceptance_scope"]) <= set(req["acceptance_ids"]), "Scope without acceptance edge: " + ident)
        require(set(req["checks"]) <= check_ids, "Unknown manual check: " + ident)
        require(set(req["source_requirements"]) <= raw_ids, "Unknown R parent: " + ident)
        if req["status"] == "pending":
            pending_requirements.add(ident)
            require(not req["deliveries"] and not req["acceptance_ids"] and not req["checks"],
                    "Pending requirement entered formal delivery or acceptance: " + ident)
            require(req["proposal_id"] in proposal_ids, "Pending requirement lacks proposal: " + ident)
            require(set(req["proposed_stages"]) <= stage_ids, "Unknown proposed stage: " + ident)
        else:
            require(req["source_requirements"], "Formal requirement lacks R parent: " + ident)
            referred_raw.update(req["source_requirements"])
            require(req["deliveries"], "Formal requirement lacks staged delivery: " + ident)
            require(req["acceptance_ids"] or req["checks"], "Formal requirement lacks verification: " + ident)
            require(req["source"]["file"] == reg["baseline"]["source_file"], "Formal source file differs: " + ident)
            require(set(req["source"]["anchors"]) <= anchors, "Invalid source anchor: " + ident)
            require(req["source"]["anchors"], "Missing source anchor: " + ident)
            require(not req["proposal_id"], "Formal requirement still linked as pending: " + ident)
        seen = set()
        for delivery in req["deliveries"]:
            require(delivery["stage"] in stage_ids, "Unknown stage: " + ident)
            require(delivery["priority"] in {"P0", "P1", "P2"}, "Invalid delivery priority: " + ident)
            require(delivery["scope"].strip(), "Missing delivery scope: " + ident)
            key = (delivery["stage"], delivery["scope"])
            require(key not in seen, "Duplicate staged delivery: " + ident)
            seen.add(key)
    for raw in reg["original_requirements"]:
        require(raw["id"] in referred_raw or raw["other_target"], "Unmapped R row: " + raw["id"])
    for case in case_ids:
        require(any(case in req["acceptance_ids"] for req in reg["requirements"]), "Orphan acceptance: " + case)
    require({p["requirement_id"] for p in reg["proposals"]} == pending_requirements, "Proposal registry not closed")
    for proposal in reg["proposals"]:
        require(proposal["status"] == "pending", "Proposal status must remain pending")
        require(proposal["requirement_id"] in req_ids, "Unknown proposal requirement")
    for check in reg["checks"]:
        require(set(check["stages"]) <= stage_ids and check["source_anchor"] in anchors, "Invalid check metadata")


def staged_label(req, delivery):
    suffix = "" if delivery["scope"] == "适用部分" else "（" + delivery["scope"] + "）"
    return req["id"] + suffix


def render(reg, docs, source):
    docs = dict(docs)
    formal = [req for req in reg["requirements"] if req["status"] == "formal"]
    cases = source_cases(source)
    src_stages = source_stages(source)
    reverse = {case: [req["id"] for req in formal if case in req["acceptance_ids"]] for case in cases}
    raw_rows = []
    for raw in reg["original_requirements"]:
        links = [req["id"] for req in formal if raw["id"] in req["source_requirements"]]
        raw_rows.append([raw["id"], raw["text"], raw["source"]["citation"], join(links) if links else raw["other_target"]])
    docs[1] = fill(docs[1], "raw-requirements", table(["编号", "需求", "来源", "关联需求 / 文档"], raw_rows))

    header_pattern = re.compile(r"^### ((?:FR-[A-Z]+-|NFR-)\d{2})([^\n]*)$", re.M)
    headings = list(header_pattern.finditer(docs[2]))
    require({h.group(1) for h in headings} == {req["id"] for req in reg["requirements"]}, "Requirement headings differ from registry")
    lookup = {req["id"]: req for req in reg["requirements"]}
    for heading in reversed(headings):
        req = lookup[heading.group(1)]
        start = heading.end()
        next_heading = re.search(r"^#{1,3} ", docs[2][start:], re.M)
        end = start + next_heading.start() if next_heading else len(docs[2])
        body = docs[2][start:end]
        expected_title = ("【评审补充】" if req["status"] == "pending" else " ") + req["title"]
        require(heading.group(2) == expected_title, "Requirement title/status differs: " + req["id"])
        stage = " / ".join(x["stage"] + "（" + x["scope"] + "）" for x in req["deliveries"])
        if req["status"] == "pending":
            stage = "待确认（建议 " + join(req["proposed_stages"]) + "）"
        verification = join(req["acceptance_ids"] + req["checks"]) if req["status"] == "formal" else "待定"
        meta = "- 优先级：" + req["priority"] + "  阶段：" + stage + "  来源：" + req["source"]["citation"] + "  验收：" + verification
        trace = "- 原始需求：" + join(req["source_requirements"]) + "；适用范围：" + req["applicability"]
        body, count = re.subn(r"^- 优先级：.*$", lambda _: meta, body, count=1, flags=re.M)
        require(count == 1, "Missing requirement metadata: " + req["id"])
        if re.search(r"^- 原始需求：", body, re.M):
            body = re.sub(r"^- 原始需求：.*$", lambda _: trace, body, count=1, flags=re.M)
        else:
            body = body.replace(meta, meta + "\n" + trace, 1)
        docs[2] = docs[2][:start] + body + docs[2][end:]

    fwd = []
    for req in formal:
        notes = "；".join(case + "：" + text for case, text in req["acceptance_scope"].items()) or req["applicability"]
        fwd.append([req["id"], join(req["source_requirements"]), join(req["acceptance_ids"] + req["checks"]), notes])
    docs[2] = fill(docs[2], "forward-trace", table(["需求", "原始需求", "验收 / 检查", "适用部分"], fwd))
    rev_rows = [[case, cases[case][1], join(reverse[case])] for case in sorted(cases, key=natural)]
    for check in reg["checks"]:
        rev_rows.append([check["id"], check["title"], join([req["id"] for req in formal if check["id"] in req["checks"]])])
    docs[2] = fill(docs[2], "reverse-trace", table(["案例 / 检查", "场景摘要", "对应需求"], rev_rows))
    stage_rows, priority_rows, overview_rows = [], [], []
    for stage in reg["stages"]:
        entries = [(req, delivery) for req in formal for delivery in req["deliveries"] if delivery["stage"] == stage["id"]]
        labels = [staged_label(req, delivery) for req, delivery in entries]
        stage_rows.append([stage["id"], join(labels)])
        priority_rows.append([
            stage["id"],
            join([staged_label(req, delivery) for req, delivery in entries if delivery["priority"] == "P0"]),
            join([staged_label(req, delivery) for req, delivery in entries if delivery["priority"] != "P0"]),
        ])
        overview_rows.append(["**" + stage["id"] + " " + stage["name"] + "**", *src_stages[stage["id"]],
                              stage["exit_conditions"], stage["dependencies"], join(labels)])
    docs[2] = fill(docs[2], "stage-distribution", table(["阶段", "本阶段交付 / 验收子项（前期约束继续适用）"], stage_rows))
    docs[3] = fill(docs[3], "priorities", table(["阶段", "必需 P0（限已启用范围）", "按能力启用 P2 / 优化 P1"], priority_rows))
    docs[6] = fill(docs[6], "stage-overview",
                   table(["阶段", "核心任务", "可验收交付", "出口条件（验收编号）", "前置依赖", "涉及需求编号"], overview_rows))
    pending = [req for req in reg["requirements"] if req["status"] == "pending"]
    docs[2] = fill(docs[2], "pending-requirements",
                   table(["编号", "统一议题", "主题", "状态"], [[req["id"], req["proposal_id"], req["title"], "待确认"] for req in pending]))
    prop_rows = []
    for proposal in reg["proposals"]:
        anchor = '<a id="' + proposal["id"].lower() + '"></a>'
        prop_rows.append([anchor + proposal["id"], proposal["title"], proposal["requirement_id"],
                          proposal["product"], proposal["architecture"], proposal["plan"],
                          proposal["test"], proposal["operations"], "待确认"])
    docs[0] = fill(docs[0], "proposals",
                   table(["议题", "内容", "02 需求", "03 决策", "04 架构", "06 计划", "07 验收建议", "08/09 运维准备", "状态"], prop_rows))
    ac_meta = {case["id"]: case for case in reg["acceptances"]}
    for tag, sub in [("acceptance-main", False), ("acceptance-a25", True)]:
        ac_rows = []
        for case in cases:
            if ("-" in case) != sub:
                continue
            meta = ac_meta[case]
            ac_rows.append([*cases[case], meta["layers"], meta["stages"], meta["prerequisites"], join(reverse[case])])
        docs[7] = fill(docs[7], tag,
                       table(["子编号" if sub else "编号", "输入与边界" if sub else "场景", "通过条件",
                              "验证层级", "所属阶段", "前置数据 / 环境", "关联需求"], ac_rows))
    return docs


def tree_paths(text):
    match = re.search(FENCE + r"text\n(qh_trader/.*?)" + FENCE, text, re.S)
    require(match, "Missing planned project tree")
    paths, stack = set(), {}
    for line in match.group(1).splitlines()[1:]:
        match_line = re.match(r"(.*?)[├└]── (.*)", line)
        if not match_line:
            continue
        depth = len(match_line.group(1)) // 4
        name = match_line.group(2).split("#", 1)[0].strip().rstrip("/")
        for level in list(stack):
            if level >= depth:
                del stack[level]
        path = "/".join([stack[n] for n in sorted(stack)] + [name])
        paths.add(path)
        stack[depth] = name
    return paths


def validate_preservation(reg, docs, source):
    require(source_cases(docs[7]) == source_cases(source), "Original acceptance conditions changed")
    actual_stages = {}
    for row in rows(docs[6]):
        match = re.match(r"\*\*(S[0-7]) ", row[0])
        if match:
            actual_stages[match[1]] = row[1:3]
    require(actual_stages == source_stages(source), "Original stage tasks/deliveries changed")
    def scripts(text):
        return [row[:2] for row in rows(text) if re.fullmatch(chr(96) + r"\w+\.py" + chr(96), row[0])]
    require(len(scripts(source)) == 21 and scripts(docs[8]) == scripts(source), "Script responsibilities changed")
    gates = dict(re.findall(r"^- \*\*(研究交付|模拟盘交付|首笔实盘门槛|扩大资金门槛|生产就绪)\*\*：(.*)$", source, re.M))
    current = {row[0].strip("*"): row[1] for row in rows(docs[8]) if row[0].strip("*") in gates}
    require(len(gates) == 5 and current == gates, "Original delivery/live gates changed")
    sql = lambda text: [block.strip() for block in re.findall(FENCE + r"sql\n(.*?)" + FENCE, text, re.S)]
    require(sql(docs[4]) == sql(source), "Original SQL examples changed")
    document_moves = reg["baseline"].get("document_moves", {})
    require(isinstance(document_moves, dict), "Document moves must be a mapping")
    for previous, current in document_moves.items():
        require(Path(previous).name == previous and previous.endswith(".md"),
                "Only root Markdown documents may be relocated: " + previous)
        require(Path(current).name == previous, "Relocated document name differs: " + previous)
        target = (ROOT / current).resolve()
        require(target.is_relative_to(ROOT / "docs") and target.is_file(),
                "Relocated document missing or outside docs: " + current)
        require(not (ROOT / previous).exists(), "Old root document still exists: " + previous)
    planned_paths = {document_moves.get(path, path) for path in tree_paths(source)}
    require(planned_paths <= tree_paths(docs[4]), "Original planned module/document path missing")
    for number, text in docs.items():
        require("| 版本 | " + reg["baseline"]["version"] + " |" in text, "Document version differs: " + DOC_NAMES[number])
        require(text.count("<!-- BEGIN GENERATED:") == text.count("<!-- END GENERATED:"), "Unbalanced generated markers")


def validate_fixtures(reg, source):
    fixture_ids = set()
    known = {case["id"] for case in reg["acceptances"]}
    source_anchors = set(re.findall(r'<a id="([^"]+)"></a>', source))
    for relative in reg["fixture_files"]:
        path = (ROOT / relative).resolve()
        require(path.is_relative_to(ROOT), "Fixture path outside repository")
        data = json.loads(read(path))
        require(data["schema_version"] == 1 and data["execution_status"] == "not_executed", "Invalid fixture status: " + relative)
        require(data["fixture_id"] not in fixture_ids, "Duplicate fixture ID")
        fixture_ids.add(data["fixture_id"])
        require(data["cases"], "Empty fixture: " + relative)
        unique(data["cases"], "id", "fixture case ID in " + relative)
        require(data["oracle"]["method"] == "independent_specification", "Fixture lacks independent expected specification")
        require(data["source_refs"], "Fixture lacks source references: " + relative)
        for reference in data["source_refs"]:
            source_file, separator, anchor = reference.partition("#")
            require(source_file == reg["baseline"]["source_file"], "Fixture source path differs: " + relative)
            require(separator and anchor in source_anchors, "Invalid fixture source anchor: " + relative)
        for case in data["cases"]:
            require(set(case["acceptance_ids"]) <= known and case["acceptance_ids"], "Unknown fixture acceptance")
            require(case["inputs"] and case["expected"], "Fixture lacks inputs or expected result")
    return len(fixture_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Regenerate derived documentation blocks")
    mode.add_argument("--check", action="store_true", help="Check without writing (default)")
    args = parser.parse_args()
    reg = json.loads(read(ROOT / "docs" / "requirements.json"))
    source_path = ROOT / reg["baseline"]["source_file"]
    require(source_path.resolve().is_relative_to(ROOT), "Source outside repository")
    require(hashlib.sha256(source_path.read_bytes()).hexdigest().upper() == reg["baseline"]["source_sha256"],
            "Frozen source SHA-256 differs")
    source = read(source_path)
    validate_registry(reg, source)
    current = {n: read(path) for n, path in DOC_PATHS.items()}
    rendered = render(reg, current, source)
    validate_preservation(reg, rendered, source)
    count = validate_fixtures(reg, source)
    require((ROOT / reg["baseline"]["changes_file"]).is_file(), "Missing change record")
    changed = [n for n in current if current[n] != rendered[n]]
    if changed and not args.write:
        raise InvalidDocs("Generated documents are stale: " + ", ".join(DOC_NAMES[n] for n in changed) +
                          ". Run python scripts/check_docs.py --write")
    for number in changed:
        DOC_PATHS[number].write_text(rendered[number], encoding="utf-8", newline="\n")
    formal = sum(req["status"] == "formal" for req in reg["requirements"])
    edges = sum(len(req["acceptance_ids"]) for req in reg["requirements"])
    print("PASS: {} R entries, {} formal requirements, {} pending, {} acceptance edges, {} fixture specs.".format(
        len(reg["original_requirements"]), formal, len(reg["requirements"]) - formal, edges, count))
    print("Preserved: 36 acceptance rows, 8 stage task/delivery rows, 21 scripts, 5 gates, SQL and planned paths (document moves applied).")
    print("Trading tests and broker verification: NOT EXECUTED.")
    if args.write:
        print("Regenerated: " + (", ".join(DOC_NAMES[n] for n in changed) if changed else "no changes"))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (InvalidDocs, OSError, ValueError, KeyError, TypeError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        sys.exit(1)
