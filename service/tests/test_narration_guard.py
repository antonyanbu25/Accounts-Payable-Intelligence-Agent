import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from narration_guard import check_narration, extract_numbers  # noqa: E402


def test_correct_narration_passes_with_no_false_positives():
    text = ("Vendor X has an outstanding balance of 84,000.00, due 15-Sep-2026. "
            "This includes 12% GST (24,000.00) and TDS of 20,000.00 under Section 194J, "
            "net of a payment of 120,000.00 already made on the 200,000.00 base amount.")
    result = check_narration(text, structured_values=[200000, 24000, 20000, 120000, 84000, 12])
    assert result["passed"] is True, result
    assert result["numbers_not_in_structured_result"] == []


def test_invented_number_is_caught():
    text = "Vendor X owes 84000 rupees, due 15-Sep-2026, per Section 194J."
    result = check_narration(text, structured_values=[200000, 24000, 20000, 120000])
    assert result["passed"] is False
    assert 84000.0 in result["numbers_not_in_structured_result"]


def test_dates_are_not_flagged():
    text = "Invoice dated 22 September 2025, due 2026-03-15, also written 15/03/2026."
    numbers = extract_numbers(text)
    assert numbers == [], f"dates leaked through as numbers: {numbers}"


def test_section_reference_not_flagged():
    text = "Withheld under Section 194J and Section 194C."
    numbers = extract_numbers(text)
    assert numbers == [], f"section codes leaked through as numbers: {numbers}"


def test_us_style_month_day_year_date_not_flagged():
    """Found by live recruiter-mindset testing: 'June 15, 2030' (day AFTER
    the month name) left the bare day '15' unstripped and wrongly flagged,
    even though the DD-Month-YYYY order was already handled."""
    text = "As of June 15, 2030, Furniture attracts GST at an aggregate rate of 18%."
    numbers = extract_numbers(text)
    assert numbers == [18.0], numbers


def test_bare_year_and_classification_code_not_flagged():
    """Found by live Day-5 testing: 'Income Tax Act, 1961' and 'SAC 998313'
    were both being flagged as invented figures -- neither is a monetary
    amount, both are legitimate verbatim quotes from a source document."""
    text = ("Software services under SAC 998313 attract GST at 18%. TDS applies "
            "at 10% under Section 194J of the Income Tax Act, 1961.")
    result = check_narration(text, structured_values=[18, 10])
    assert result["passed"] is True, result


def test_comma_grouped_number_not_fragmented():
    numbers = extract_numbers("The total is 1,23,456.78 for this quarter.")
    assert numbers == [123456.78], numbers


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
