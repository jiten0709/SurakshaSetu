from surakshasetu.rails.redact import redact

VALID_AADHAAR = "234123412346"
INVALID_AADHAAR = "234123412340"
VALID_CARD = "4000000000000002"
INVALID_CARD = "4532015112830000"


def test_a_valid_aadhaar_is_masked_and_triggers_the_reminder() -> None:
    result = redact(f"my aadhaar is {VALID_AADHAAR} please note it")
    assert VALID_AADHAAR not in result.stored_raw
    assert "[REDACTED]" in result.stored_raw
    assert result.reminder is True
    assert "<AADHAAR_1>" in result.redacted


def test_an_invalid_aadhaar_checksum_is_not_flagged() -> None:
    result = redact(f"my number is {INVALID_AADHAAR} maybe")
    assert INVALID_AADHAAR in result.stored_raw
    assert result.reminder is False


def test_a_valid_card_number_is_masked_and_triggers_the_reminder() -> None:
    result = redact(f"card number {VALID_CARD} for payment")
    assert VALID_CARD not in result.stored_raw
    assert "[REDACTED]" in result.stored_raw
    assert result.reminder is True
    assert "<CARD_1>" in result.redacted


def test_an_invalid_card_checksum_is_not_flagged() -> None:
    result = redact(f"card number {INVALID_CARD} for payment")
    assert INVALID_CARD in result.stored_raw
    assert result.reminder is False


def test_bank_account_near_context_is_masked() -> None:
    result = redact("please debit my bank account number 123456789012 for the premium")
    assert "123456789012" not in result.stored_raw
    assert result.reminder is True
    assert "<BANK_ACCOUNT_1>" in result.redacted


def test_a_bare_long_number_with_no_account_context_is_not_flagged() -> None:
    result = redact("the reference number is 123456789012 on the form")
    assert "123456789012" in result.stored_raw
    assert result.reminder is False


def test_pan_and_ifsc_are_kept_in_stored_raw_but_tokenised_in_redacted() -> None:
    result = redact("my PAN is ABCDE1234F and IFSC is HDFC0001234")
    assert "ABCDE1234F" in result.stored_raw
    assert "HDFC0001234" in result.stored_raw
    assert result.reminder is False
    assert "<PAN_1>" in result.redacted
    assert "<IFSC_1>" in result.redacted
    assert "ABCDE1234F" not in result.redacted


def test_mobile_number_is_kept_in_stored_raw_but_tokenised_in_redacted() -> None:
    result = redact("call me on 9876543210 please")
    assert "9876543210" in result.stored_raw
    assert "<IN_MOBILE_1>" in result.redacted


def test_email_is_kept_in_stored_raw_but_tokenised_in_redacted() -> None:
    result = redact("reach me at customer@example.com")
    assert "customer@example.com" in result.stored_raw
    assert "customer@example.com" not in result.redacted


def test_a_mobile_number_inside_hindi_text_is_still_caught() -> None:
    result = redact("मेरा नंबर 9876543210 है, कृपया कॉल करें")
    assert "9876543210" in result.stored_raw
    assert "<IN_MOBILE_1>" in result.redacted


def test_clean_text_has_no_reminder_and_is_unchanged() -> None:
    result = redact("I want a term plan for 25 lakh cover")
    assert result.reminder is False
    assert result.stored_raw == result.redacted == "I want a term plan for 25 lakh cover"
