package com.surakshasetu.domain.eligibility;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/** The Eligibility table of rules-2026.09.1 (TDD §7.3 E-01..E-06, V2), without Spring. */
class EligibilityDmnTest {

  private static final Rules.Release RULES = load();

  @ParameterizedTest(name = "{1} {2} class {3} -> {0}")
  @CsvSource(
      nullValues = "",
      value = {
        // rule, age, residency, occupation class, outcome, escalation reason, flag
        "E-03, 17, resident, 1, DATA_ERASURE_EXIT, , ",
        "E-03, 0, resident, 1, DATA_ERASURE_EXIT, , ",
        "E-05, 34, nri, 1, HUMAN_ESCALATION, HE_NRI, ",
        "E-05, 34, oci_pio, 1, HUMAN_ESCALATION, HE_NRI, ",
        "E-04, 66, resident, 1, HUMAN_ESCALATION, HE_AGE_BAND, ",
        "E-04, 75, resident, 4, HUMAN_ESCALATION, HE_AGE_BAND, ",
        "E-06, 34, resident, declined, RE_ASK, , ",
        "E-06, 34, resident, unknown, RE_ASK, , ",
        "E-02, 18, resident, 4, ELIGIBLE, , MANUAL_UW",
        "E-02, 65, resident, 4, ELIGIBLE, , MANUAL_UW",
        "E-01, 18, resident, 1, ELIGIBLE, , ",
        "E-01, 34, resident, 2, ELIGIBLE, , ",
        "E-01, 65, resident, 3, ELIGIBLE, , ",
        // Precedence: the minor path beats everything, residency beats the age band, and an
        // escalation beats the occupation re-ask.
        "E-03, 17, nri, declined, DATA_ERASURE_EXIT, , ",
        "E-05, 70, nri, 4, HUMAN_ESCALATION, HE_NRI, ",
        "E-04, 70, resident, declined, HUMAN_ESCALATION, HE_AGE_BAND, ",
      })
  void eligibilityRules(
      String rule,
      int age,
      String residency,
      String occupation,
      String outcome,
      @Nullable String escalation,
      @Nullable String flag) {
    Map<String, @Nullable Object> row =
        RULES
            .rows(
                RULES.eligibility(),
                "Eligibility",
                Rules.inputs(
                    "age", age, "residency", residency, "occupation_risk_class", occupation))
            .getFirst();

    assertThat(row.get("rule_id")).isEqualTo(rule);
    assertThat(row.get("outcome")).isEqualTo(outcome);
    assertThat(row.get("escalation_reason")).isEqualTo(escalation);
    assertThat(row.get("flag")).isEqualTo(flag);
  }

  @Test
  void requiredAttributesFollowTheRulesAndAskGenderOnlyWhenRated() {
    assertThat(attributes(false))
        .containsExactly(
            "age_years",
            "residency",
            "pincode",
            "tobacco_12m",
            "occupation_code",
            "health_flags",
            "proposer.is_life_assured",
            "proposer.relationship",
            "proposer.la_age",
            "proposer.business_cover");
    assertThat(attributes(true)).element(1).isEqualTo("gender");
    assertThat(attributes(true)).hasSize(11);

    // Every Eligibility input is asked for (occupation_risk_class comes from occupation_code).
    assertThat(attributes(false)).contains("age_years", "residency", "occupation_code");
    for (var row :
        RULES.rows(RULES.eligibility(), "RequiredAttributes", Rules.inputs("gender_rated", true))) {
      assertThat((String) row.get("reason_line_id")).startsWith("RL-S1-");
      boolean laDetail =
          ((String) row.get("attribute"))
              .matches("proposer\\.(relationship|la_age|business_cover)");
      assertThat(row.get("asked_if"))
          .isEqualTo(laDetail ? "proposer.is_life_assured = false" : null);
    }
  }

  private static List<String> attributes(boolean genderRated) {
    return RULES
        .rows(RULES.eligibility(), "RequiredAttributes", Rules.inputs("gender_rated", genderRated))
        .stream()
        .map(r -> (String) r.get("attribute"))
        .toList();
  }

  static Rules.Release load() {
    try {
      return Rules.load("classpath*:dmn/*.dmn", "classpath*:params/*.yaml")
          .release("rules-2026.09.1");
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
