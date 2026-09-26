package com.surakshasetu.domain.suitability;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import java.math.BigDecimal;
import java.util.List;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/**
 * The Affordability, AffordabilityLimits and Vulnerability tables of rules-2026.09.1: green up to
 * 10% of income, amber to 20%, red above; a vulnerable customer's bands tighten to 5% and 10%.
 */
class AffordabilityTest {

  private static final Rules.Release RULES = SuitabilityCalculatorTest.release();

  @ParameterizedTest(name = "{0} vulnerable={1} -> {2}")
  @CsvSource(
      nullValues = "null",
      value = {
        "0, false, green",
        "0.10, false, green",
        "0.1000001, false, amber",
        "0.20, false, amber",
        "0.2000001, false, red",
        "null, false, unknown",
        "0.05, true, green",
        "0.0500001, true, amber",
        "0.10, true, amber",
        "0.1000001, true, red",
        "null, true, unknown",
      })
  void bandEdges(@Nullable BigDecimal share, boolean vulnerable, String band) {
    var result =
        RULES.decide(
            RULES.suitability(),
            "Affordability",
            Rules.inputs("premium_to_income", share, "vulnerable", vulnerable));
    assertThat(((java.util.Map<?, ?>) result.get("Affordability")).get("band")).isEqualTo(band);
    assertThat(((java.util.Map<?, ?>) result.get("AffordabilityLimits")).get("rule_id"))
        .isEqualTo(vulnerable ? "AFL-02" : "AFL-01");
  }

  @ParameterizedTest(name = "age {0}, {1}, income {2} -> {5}")
  @CsvSource(
      nullValues = "null",
      delimiter = '|',
      value = {
        "59 | salaried | 1200000 | false | 1 | ",
        "60 | salaried | 1200000 | false | 1 | VULN_AGE_60_PLUS",
        "34 | homemaker | null | false | 0 | VULN_NO_PERSONAL_INCOME",
        "34 | student | 0 | false | 0 | VULN_NO_PERSONAL_INCOME,VULN_NO_PERSONAL_INCOME",
        "34 | salaried | 0 | false | 0 | VULN_NO_PERSONAL_INCOME",
        "34 | salaried | null | true | 0 | VULN_FINANCIAL_DISTRESS",
        "34 | salaried | 1200000 | false | 2 | VULN_COMPREHENSION",
        "62 | retired | 300000 | true | 3 | VULN_AGE_60_PLUS,VULN_NO_PERSONAL_INCOME,VULN_FINANCIAL_DISTRESS,VULN_COMPREHENSION",
      })
  void vulnerabilityFlags(
      int age,
      String incomeType,
      @Nullable BigDecimal income,
      boolean distress,
      int comprehension,
      @Nullable String flags) {
    List<Object> fired =
        RULES
            .rows(
                RULES.suitability(),
                "Vulnerability",
                Rules.inputs(
                    "age",
                    age,
                    "income_type",
                    incomeType,
                    "annual_income",
                    income,
                    "financial_distress",
                    distress,
                    "comprehension_difficulty_count",
                    comprehension))
            .stream()
            .map(r -> (Object) r.get("flag"))
            .toList();
    assertThat(fired)
        .containsExactly((Object[]) (flags == null ? new String[0] : flags.split(",")));
  }
}
