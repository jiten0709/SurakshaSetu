package com.surakshasetu.domain.suitability;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * profile_sufficiency: the weighted share of S2 slots answered, with the RequiredSlots weights of
 * rules-2026.09.1 (goals 0.15, income 0.25, income type 0.05, dependants 0.20, liabilities 0.15,
 * existing cover, employer cover and existing premiums 0.05 each, assets and budget 0.025 each).
 */
class SufficiencyTest {

  private static final Rules.Release RULES = SuitabilityCalculatorTest.release();
  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final Path VECTORS = Path.of("..", "content", "testvectors", "needs");

  @Test
  void weightsSumToOneInAskingOrder() {
    Map<String, BigDecimal> w = weights();
    assertThat(w.keySet())
        .containsExactly(
            "goals",
            "annual_income_inr",
            "income_type",
            "dependants",
            "liabilities",
            "existing_cover_inr",
            "employer_cover_inr",
            "existing_annual_premium_inr",
            "earmarked_assets_inr",
            "premium_budget_inr_pa");
    assertThat(w.values().stream().reduce(BigDecimal.ZERO, BigDecimal::add))
        .isEqualByComparingTo("1");
  }

  @Test
  void everySlotAnsweredIsOne() throws Exception {
    ObjectNode needs = needs("01-tdd-example.json");
    needs.put("earmarked_assets_inr", "100000");
    assertThat(sufficiency(needs)).isEqualByComparingTo("1");
  }

  @Test
  void aNullSlotIsDeclinedNotAnswered() throws Exception {
    // The TDD example declines earmarked assets: 1 − 0.025.
    assertThat(sufficiency(needs("01-tdd-example.json"))).isEqualByComparingTo("0.975");
  }

  @Test
  void theShortPathOmitsWhatItDidNotAsk() throws Exception {
    // goals, income, income type, dependants, liabilities, existing premiums:
    // 0.15 + 0.25 + 0.05 + 0.20 + 0.15 + 0.05 = 0.85
    assertThat(sufficiency(needs("02-short-path.json"))).isEqualByComparingTo("0.85");
  }

  @Test
  void aDeclinedIncomeAndEmptyGoalsCountForNothing() throws Exception {
    // Income, assets and budget declined: 1 − 0.25 − 0.025 − 0.025 = 0.70.
    assertThat(sufficiency(needs("03-income-declined.json"))).isEqualByComparingTo("0.70");
    ObjectNode noGoals = needs("01-tdd-example.json");
    noGoals.putArray("goals");
    assertThat(sufficiency(noGoals)).isEqualByComparingTo("0.825");
  }

  private static BigDecimal sufficiency(JsonNode needs) {
    return SuitabilityCalculator.profileSufficiency(needs, weights());
  }

  private static Map<String, BigDecimal> weights() {
    Map<String, BigDecimal> w = new LinkedHashMap<>();
    RULES
        .rows(RULES.suitability(), "RequiredSlots", Rules.inputs())
        .forEach(r -> w.put((String) r.get("slot"), (BigDecimal) r.get("weight")));
    return w;
  }

  static ObjectNode needs(String vector) throws Exception {
    return (ObjectNode) JSON.readTree(Files.readString(VECTORS.resolve(vector))).get("needs");
  }
}
