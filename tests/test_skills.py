import itertools
import re
from pathlib import Path

from market_research.research import select_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "research-skills"


def test_every_selectable_skill_exists_and_protocol_always_loads():
    names = set()
    for flags in itertools.product([False, True], repeat=6):
        stock, industries, history, risk, valuation, events = flags
        for task, question in (("market_overview", "市场状态"), ("named_comparison", "对比"), ("sector_ranking", "行业")):
            chosen = select_skills(task, question, stock=stock, industries=industries, history=history,
                                   risk=risk, valuation=valuation, events=events)
            assert chosen[0] == "research-protocol"
            assert ("security-context" in chosen) == stock and ("market-state" in chosen) != stock
            names.update(chosen)
    assert names == {path.stem for path in SKILLS.glob("*.md")}


def test_skills_only_name_registered_tools_and_existing_ids():
    research = (ROOT / "apps/api/market_research/research.py").read_text()
    insights = (ROOT / "apps/api/market_research/insights.py").read_text()
    tools = set(re.findall(r'"name": "([a-z_]+)"', research))
    for path in SKILLS.glob("*.md"):
        text = path.read_text()
        assert "改编来源" not in text, path.name
        for tool in re.findall(r"\b(?:get|compare)_[a-z_]+", text):
            assert tool in tools, (path.name, tool)
        for identifier in re.findall(r"\b([a-z]+(?:_[a-z]+)*)_\*", text):
            assert f'"{identifier}_"' in insights, (path.name, identifier)
        for identifier in ("participation", "coverage", "comparison", "turnover_trend", "sector_leaders", "sector_coverage",
                           "different_windows", "activity_vs_price", "evidence_gaps", "sector_ranking_scope", "stock_vs_market"):
            if re.search(rf"\b{identifier}\b", text):
                assert f'"{identifier}"' in insights, (path.name, identifier)
