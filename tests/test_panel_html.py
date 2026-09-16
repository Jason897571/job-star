"""面板前端（static/index.html）里几条不能悄悄退化的不变量。

Important 1（输出转义）和 Important 4（失败横幅要给出入口）的回归测试。
面板是纯静态 HTML + 内联脚本，项目里没有 JS 测试框架——但 `esc()` 是整个
面板唯一的输出转义点，值得用真家伙验证而不是靠读代码。这里的做法是：

  1. 从 index.html 里把真正的 `esc` 定义抠出来，交给 node 执行（不是在
     Python 里重写一份等价实现——那样测的是复制品，原件改坏了测试照样绿）；
  2. 把转义结果按设置视图的模板拼成属性，用 Python 的 HTMLParser 解析回来，
     断言属性个数和取值都没变——属性闭合被破坏时，解析器会看到多出来的
     属性（比如 onfocus），这比在字符串里找关键字可靠。

没装 node 时第 1 组跳过；第 2 组是纯文本结构检查，任何环境都会跑。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from jobstar.collector.boss import SELECTORS

INDEX = Path(__file__).resolve().parents[1] / "src/jobstar/panel/static/index.html"
HTML = INDEX.read_text(encoding="utf-8")

# 设置视图把每个配置项渲染成一个单引号属性（renderSettings）。这一行的
# 单引号是 Important 1 的前提：esc() 不转义单引号时，值里带单引号就能闭合
# 属性、在面板自己的源上挂一个事件处理器。
SETTINGS_INPUT_TEMPLATE = "<input data-key=\"{key}\" value='{value}'>"


def _esc_source() -> str:
    """把 index.html 里 `const esc = ...` 那一行原样抠出来。"""
    for line in HTML.splitlines():
        if line.startswith("const esc ="):
            return line
    raise AssertionError("index.html 里找不到 `const esc =` 定义——它被改名或移动了")


def _run_esc(values: list[str]) -> list[str]:
    """用 node 执行 index.html 里真正的 esc()，返回逐个转义后的结果。"""
    script = (
        f"{_esc_source()}\n"
        "const inputs = JSON.parse(process.argv[1]);\n"
        "process.stdout.write(JSON.stringify(inputs.map(esc)));\n"
    )
    out = subprocess.run(
        ["node", "-e", script, json.dumps(values)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


class _AttrCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _parse_one_tag(markup: str) -> tuple[str, dict[str, str | None]]:
    parser = _AttrCollector()
    parser.feed(markup)
    assert len(parser.tags) == 1, f"期望只解析出一个标签，实际 {parser.tags}"
    return parser.tags[0]


requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 才能执行 index.html 里真正的 esc()"
)


# 这条错误文案是采集器自己产出的：fetch_list 解析出 0 条卡片时，把 CSS
# 选择器用 Python 的 repr() 嵌进消息里（collector/boss.py），repr 恒用单
# 引号包裹。这条消息随后被 cli.py 写进 last_collect_error 设置项，而
# /api/settings 返回 SETTING_DEFAULTS 的全部键——包括它。也就是说：攻击
# 面不需要任何外部输入，系统第一次采集失败就会自己踩中。
SELF_INFLICTED_ERROR = (
    f"第 1 页轮询 {SELECTORS['card']!r} 后解析出 0 条卡片，既不是已知的登录墙特征"
)

# 真正带攻击意图的值：如果 esc() 漏掉单引号，这串会闭合 value 属性并挂上
# 一个 onfocus 处理器。
BREAKOUT_PAYLOAD = "' onfocus='alert(document.cookie)"


@requires_node
@pytest.mark.parametrize(
    "value",
    [
        SELF_INFLICTED_ERROR,
        BREAKOUT_PAYLOAD,
        '" onmouseover="alert(1)',
        "<script>alert(1)</script>",
        "正常文案，没有元字符",
    ],
)
def test_escaped_value_cannot_break_out_of_single_quoted_attribute(value):
    (escaped,) = _run_esc([value])
    tag, attrs = _parse_one_tag(
        SETTINGS_INPUT_TEMPLATE.format(key="last_collect_error", value=escaped)
    )
    assert tag == "input"
    assert set(attrs) == {"data-key", "value"}, (
        f"属性被闭合了，多出来的属性：{set(attrs) - {'data-key', 'value'}}"
    )
    assert attrs["value"] == value, "转义后解析回来应当和原值逐字相同"


@requires_node
def test_esc_covers_all_five_html_metacharacters():
    (escaped,) = _run_esc(["&<>\"'"])
    assert escaped == "&amp;&lt;&gt;&quot;&#39;"


@requires_node
def test_esc_renders_null_and_undefined_as_empty_string():
    """设置项的默认值有 null（score_threshold、last_collect_*）。`String(null)`
    会渲染成字面量 "null"——`?? ""` 这层不能被顺手删掉。"""
    out = subprocess.run(
        ["node", "-e", f"{_esc_source()}\nprocess.stdout.write(esc(null) + '|' + esc(undefined));"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "|"


def test_every_single_quoted_attribute_in_the_panel_goes_through_esc():
    """结构检查（不需要 node）：单引号属性是这个文件里唯一需要单引号转义的
    地方。将来谁再写一个 `attr='${...}'`，插值必须经过 esc()，否则 Important 1
    会以另一种形式回来。

    这是个文本扫描器而不是 JS 解析器：只跳过整行注释（本文件里的注释都是
    行注释），不理解块注释或字符串字面量里的伪代码。误报时把那行改成不长得
    像属性即可，不要为了让它闭嘴而放宽判断条件。"""
    offenders = []
    for lineno, line in enumerate(HTML.splitlines(), start=1):
        if line.lstrip().startswith("//"):
            continue
        for idx, char in enumerate(line):
            if char != "'" or not line.startswith("='", max(0, idx - 1)):
                continue
            rest = line[idx + 1 :]
            end = rest.find("'")
            if end == -1:
                continue
            body = rest[:end]
            if "${" in body and "esc(" not in body:
                offenders.append(f"{INDEX.name}:{lineno}: {line.strip()}")
    assert not offenders, "单引号属性里有未经 esc() 的插值：\n" + "\n".join(offenders)


def _ternary_branch(count_field: str) -> str:
    """把 `(h.xxx ? ... : "")` 这一整个三元分支的文本取出来。

    用固定字符数开窗是不行的：两条失败横幅紧挨着，`h.scoring_failed` 的窗口
    会滑进 `h.pitch_failed` 的分支里，于是删掉前者的命令文案、断言照样能被
    后者的文案满足。这里从分支开头一直取到它自己的 `: "")`。
    """
    start = HTML.find(f"({count_field} ?")
    assert start != -1, f"横幅里找不到 {count_field} 的分支"
    end = HTML.find(': "")', start)
    assert end != -1, f"{count_field} 的三元分支没有找到结尾"
    return HTML[start:end]


@pytest.mark.parametrize("count_field", ["h.scoring_failed", "h.pitch_failed"])
def test_failure_banners_name_the_command_that_reopens_the_jobs(count_field):
    """Important 4：横幅上的「打分失败 N 条」以前是条死路——run_score 永远
    不会再看这些岗位，面板上也没有入口。计数和重新入场的办法必须一起出现，
    否则这条提示只是在通知一个人工无法处理的事实。"""
    branch = _ternary_branch(count_field)
    assert count_field in branch
    assert "jobstar score --rescore" in branch, (
        f"{count_field} 的横幅没有给出重新入场的命令"
    )


def test_ternary_branch_helper_does_not_bleed_into_its_neighbour():
    """上面那条断言靠 _ternary_branch 精确切分。如果切分又滑进隔壁分支，
    两条断言会同时被同一段文案满足，删掉其中一条的命令也测不出来。"""
    scoring = _ternary_branch("h.scoring_failed")
    assert "h.pitch_failed" not in scoring
    assert scoring.count("jobstar score --rescore") == 1


# 允许不经过 esc()/safeHref() 的插值。每一条都要能说清「为什么这个值不可能
# 是攻击者控制的字符串」——不是「看起来像数字」，而是产品代码保证它是数字。
# 加新条目意味着你要为它给出同样强度的理由。
UNESCAPED_ALLOWLIST = {
    # /api/health 的计数：COUNT(*) 或 len()，见 panel/app.py:health
    "h.sent_today", "h.remaining_quota", "h.pending", "h.scoring_failed",
    "h.pitch_failed", "h.uncertain", "h.interrupted",
    # daily_greeting_limit 是配置项，写入时 validate_setting_value 强制
    # 「非负整数」（config.py），不可能是字符串
    "h.daily_limit",
    # actions.id：SQLite INTEGER PRIMARY KEY
    "row.action_id", "i.action_id",
    # scores.total / DimensionScore.score：打分器算出来的数字
    'i.total ?? "-"', "j.total", "dim.score",
    # /api/threshold 返回的统计量，全部由 _stats() 用 min/max/sum 算出
    "t.labeled_total", "g.count", "g.min", "g.max", "g.mean",
    "f(t.would_apply)", "f(t.would_not_apply)",
    't.scores_yes.join(", ") || "无"', 't.scores_no.join(", ") || "无"',
    # 本地拼好的 HTML 片段，内部的值已经各自转义过
    "noteHtml",
}

# 这些插值不产生 HTML：URL 路径片段和 alert() 的纯文本参数。
NON_HTML_SINKS = ("await api(`/api/", "alert(`")


def _leaf_interpolations(line: str) -> list[str]:
    """这一行里所有**最内层**的 `${...}`。

    只看最内层是有意的。像 `${dim.cards.map(c => `…${esc(c.id)}…`).join("")}`
    这种嵌套，外层本身不产出任何未经处理的值——真正落到 innerHTML 上的值
    全部来自内层的那几个 `${}`，而它们各自会被单独扫到。反过来把外层整体
    放进白名单才是危险的：那会连带豁免它内部所有插值。
    """
    leaves = []
    for start in (i for i in range(len(line) - 1) if line[i : i + 2] == "${"):
        depth, pos = 0, start + 1
        while pos < len(line):
            if line[pos] == "{":
                depth += 1
            elif line[pos] == "}":
                depth -= 1
                if depth == 0:
                    break
            pos += 1
        else:
            continue  # 这一行里没闭合（跨行模板），交给它闭合的那一行去看
        body = line[start + 2 : pos]
        if "${" not in body:
            leaves.append(body.strip())
    return leaves


def test_every_interpolation_that_reaches_innerhtml_is_escaped():
    """Important 1 的另一半：只钉住 esc() 函数本身是不够的——把 esc() 从
    某个渲染点删掉，函数级的测试照样全绿。这里逐个检查调用点。

    面板全部是 innerHTML 字符串拼接，没有任何自动转义的模板引擎，所以
    「插值必须经过 esc()/safeHref()」是唯一的防线，必须逐点成立。
    """
    offenders = []
    for lineno, line in enumerate(HTML.splitlines(), start=1):
        if line.strip().startswith("//") or any(s in line for s in NON_HTML_SINKS):
            continue
        for expr in _leaf_interpolations(line):
            if expr.startswith(("esc(", "safeHref(")) or expr in UNESCAPED_ALLOWLIST:
                continue
            offenders.append(f"{INDEX.name}:{lineno}: ${{{expr}}}")
    assert not offenders, (
        "这些插值既没经过 esc()/safeHref()，也不在白名单里：\n"
        + "\n".join(offenders)
        + "\n\n如果确实安全，把它加进 UNESCAPED_ALLOWLIST 并写清理由。"
    )


def test_render_clears_the_error_banner_first():
    """下面那条测试的前提：render() 一进来就 clearError()。如果哪天不再是
    这样，那条测试锁的顺序就失去意义，应该一起重新考虑。"""
    body = HTML[HTML.index("async function render()") :]
    body = body[: body.index("\n}")]
    assert "clearError();" in body


def test_action_handlers_report_errors_after_rerendering_not_before():
    """`showError(err)` 必须排在 `render()` 之后。

    render() 开头就 clearError()，所以「先报错、再重渲染」会让刚写上去的
    错误横幅在同一帧里被抹掉——人工看到的是「点了确认，什么都没发生」。
    这两个处理器上的错误恰恰是最不能丢的：话术为空被拒（否则人工会以为
    是按库里那版 LLM 原稿发出去了），以及 409 状态冲突（这一行在别处已经
    被跳过或被执行器认领）。
    """
    offenders = []
    for marker in ("/api/actions/${id}/${act}", "/api/actions/${id}/resolve"):
        start = HTML.index(marker)
        block = HTML[start : HTML.index("\n  }", start)]
        assert "showError(err)" in block, f"{marker} 的处理器没有报错分支"
        if block.index("await render()") > block.index("showError(err)"):
            offenders.append(marker)
    assert not offenders, (
        "这些处理器先报错再 render()，错误横幅会被 clearError() 抹掉：\n"
        + "\n".join(offenders)
    )
