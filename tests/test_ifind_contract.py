import json
from decimal import Decimal

import pytest

from market_research.connections import ProbeError
from market_research.ifind_contract import ExpectedField, decode_result, inspect_result, parse_quantity, table_rows


def fixture(header="上涨股票数量（单位：家）", value="100", params=None, code=1, date="20260930"):
    """Synthetic data only; not an actual provider response."""
    answer = f"|证券代码|日期|{header}|\n|---|---|---|\n|TEST|{date}|{value}|"
    return {"isError": False, "content": [{"type": "text", "text": json.dumps({
        "code": code, "data": {"answer": answer, "indicators_params": params if params is not None else {
            "上涨股票数量": {"交易日期": "20260930"}}}})}]}


def inspect(result, fields=None):
    return inspect_result(result, instrument="TEST", dates={"20260930"},
                          fields=fields or [ExpectedField("上涨股票数量", "家")])


def test_complete_explicit_contract_passes():
    result = inspect(fixture())
    assert result["status"] == "validated"
    assert result["observations"][0]["source_row"] == 1


def test_fuyao_success_code_not_accepted_for_ifind():
    with pytest.raises(ProbeError, match="ifind_business_error"):
        decode_result(fixture(code=0))


def test_percent_not_substituted_for_counts():
    result = inspect(fixture(header="上涨股票数量占比（单位：%）"))
    assert result["metric_issues"]["上涨股票数量"] == ["requested_field_missing"]
    assert result["eligible_observation_count"] == 0


def test_latest_metadata_invalidates_historical_claim():
    result = inspect(fixture(params={"上涨股票数量": {"截止日期": "最新"}}))
    assert "dynamic_date_parameter" in result["metric_issues"]["上涨股票数量"]
    assert result["observations"][0]["eligible_for_claim"] is False


def test_index_currency_is_not_silently_corrected_to_points():
    result = inspect(fixture(header="收盘价（单位：元）", params={"收盘价": {"交易日期": "20260930"}}), [ExpectedField("收盘价", "点")])
    row = result["observations"][0]
    assert row["reported_unit"] == "元" and row["expected_unit"] == "点"
    assert row["issues"] == ["unit_mismatch"]


def test_missing_unit_remains_unknown():
    result = inspect(fixture(header="上涨股票数量"))
    assert "unit_not_reported" in result["metric_issues"]["上涨股票数量"]


def test_mismatched_metadata_unit_not_overwritten():
    result = inspect(fixture(params={"上涨股票数量": {"交易日期": "20260930", "单位": "%"}}))
    assert "unit_metadata_conflict" in result["metric_issues"]["上涨股票数量"]


def test_missing_value_not_filled_with_zero():
    result = inspect(fixture(value="--"))
    assert result["observations"][0]["parsed_value"] is None
    assert "missing_value" in result["observations"][0]["issues"]


def test_display_scaled_value_keeps_precision_warning():
    result = inspect(fixture(header="成交金额(合计)（单位：元）", value="1.4501万亿", params={"成交金额(合计)": {"交易日期": "20260930"}}), [ExpectedField("成交金额(合计)", "元")])
    row = result["observations"][0]
    assert Decimal(row["parsed_value"]) == Decimal("1450100000000")
    assert row["raw_value"] == "1.4501万亿"
    assert row["eligible_for_claim"] is False


@pytest.mark.parametrize("raw", ["1,2", "NaN", "inf", "约100", "100元", "1e4"])
def test_ambiguous_numeric_text_is_rejected(raw):
    with pytest.raises(ProbeError, match="unsupported_quantity_format"):
        parse_quantity(raw)


def test_wrong_date_not_admitted():
    result = inspect(fixture(date="20261007"))
    assert result["identity_issues"] == ["date_coverage_mismatch"]
    assert not result["observations"][0]["eligible_for_claim"]


@pytest.mark.parametrize("value", ["-1", "2.5"])
def test_counts_must_be_nonnegative_integers(value):
    result = inspect(fixture(value=value))
    assert "invalid_count" in result["observations"][0]["issues"]


def test_fixed_date_parameter_cannot_validate_other_rows():
    raw = fixture()
    body = json.loads(raw['content'][0]['text'])
    body['data']['answer'] += '\n|TEST|20260929|200|'
    raw['content'][0]['text'] = json.dumps(body)
    result = inspect_result(raw, instrument="TEST", dates={"20260929", "20260930"}, fields=[ExpectedField("上涨股票数量", "家")])
    assert result["observations"][0]["eligible_for_claim"] is True
    assert "row_parameter_date_conflict" in result["observations"][1]["issues"]
    assert result['status'] == 'needs_review'


def test_empty_requirements_cannot_approve_arbitrary_response():
    with pytest.raises(ProbeError, match="invalid_validation_contract"):
        inspect_result(fixture(), instrument="TEST", dates={"20260930"}, fields=[])


@pytest.mark.parametrize("answer", ["No data", "|A|A|\n|---|---|\n|1|2|", "|A|B|\n|---|---|\n|1|"])
def test_non_table_and_malformed_tables_rejected(answer):
    with pytest.raises(ProbeError):
        table_rows(answer)
