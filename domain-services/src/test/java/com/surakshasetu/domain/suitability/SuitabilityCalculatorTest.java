package com.surakshasetu.domain.suitability;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.common.Rules.ActuarialParams;
import com.surakshasetu.domain.contract.model.Dependant;
import com.surakshasetu.domain.contract.model.Dependant.RelationEnum;
import com.surakshasetu.domain.contract.model.Goal;
import com.surakshasetu.domain.contract.model.IncomeType;
import com.surakshasetu.domain.contract.model.Liability;
import com.surakshasetu.domain.contract.model.Liability.KindEnum;
import com.surakshasetu.domain.contract.model.NeedsPayload;
import com.surakshasetu.domain.suitability.SuitabilityCalculator.Sizing;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.Test;

/**
 * Golden cover-sizing cases under actuarial-2026.09.1 (DUMMY): s = 0.30, i = 0.07, g = 0.05,
 * children independent at 25, F = ₹2,00,000, half of employer cover counted, child education
 * ₹20,00,000 per child due at 18, SA step ₹5,00,000. The multiples are the IncomeMultiple table's
 * (18–35: 25, 36–45: 20, 46–55: 15). Every value was computed independently with Python's decimal
 * module at 34 digits.
 *
 * <p>r = 1.07 / 1.05 − 1 = 0.0190476190…; a(n) = (1 − (1+r)^−n) / r: a(10) = 9.0275099, a(20) =
 * 16.5027163, a(24) = 19.1195785, a(26) = 20.3557824.
 */
class SuitabilityCalculatorTest {

  private static final ActuarialParams PARAMS = release().params();
  private static final BigDecimal SA_MAX = new BigDecimal("100000000"); // 999N001V02: ₹10 crore

  @Test
  void noDependants() {
    // n = 0, so no income term. N = F = 2,00,000. uw_cap = 25 × 12,00,000 = 3,00,00,000.
    // SA_rec = 2,00,000 up to the step = 5,00,000. term = 60 − 30 = 30.
    Sizing s = size(30, "1200000", List.of(), List.of(), "0", "0", null, 25, SA_MAX);
    assertSizing(s, "200000.00", "500000", "30000000", 30, 0);
    assertThat(s.reasonCodes()).isEmpty();
  }

  @Test
  void twoChildren() {
    // Age 35, I = 24,00,000; children 5 and 8 depend until 25: 20 and 17 years; n = min(20, 25).
    // Income term = 24,00,000 × 0.7 × a(20) = 2,77,24,563.34.
    // G (child education, due at 18): 20,00,000 / (1+r)^13 = 15,64,954.71
    //                                + 20,00,000 / (1+r)^10 = 16,56,094.86 → 32,21,049.57.
    // E = 10,00,000 + 0.5 × 20,00,000 = 20,00,000; A = 5,00,000; L = 30,00,000.
    // N = 2,77,24,563.34 + 30,00,000 + 32,21,049.57 + 2,00,000 − 20,00,000 − 5,00,000
    //   = 3,16,45,612.91. Cap 25 × 24,00,000 = 6 crore doesn't bind; up to the step: 3,20,00,000.
    Sizing s =
        size(
            35,
            "2400000",
            List.of(child(5), child(8)),
            List.of(home("3000000")),
            "1000000",
            "2000000",
            "500000",
            25,
            SA_MAX,
            Goal.INCOME_PROTECTION,
            Goal.CHILD_EDUCATION);
    assertSizing(s, "31645612.91", "32000000", "60000000", 25, 20);
    assertThat(s.existingCoverCounted()).isEqualByComparingTo("2000000");
  }

  @Test
  void aSpouseDependsUntilTheCoverToAge() {
    // Age 34, spouse 32: no independence age for a spouse, so 60 − 34 = 26 years; n = 26.
    // N = 24,00,000 × 0.7 × a(26) + F = 3,41,97,714.43 + 2,00,000 = 3,43,97,714.43 → 3,45,00,000.
    Sizing s = size(34, "2400000", List.of(spouse(32)), List.of(), "0", "0", null, 25, SA_MAX);
    assertSizing(s, "34397714.43", "34500000", "60000000", 26, 26);
  }

  @Test
  void incomeDeclined() {
    // Age 40, income declined: no income term and no uw_cap. N = L + F = 50,00,000 + 2,00,000
    // = 52,00,000 → 55,00,000 (SA_max ₹2 crore doesn't bind). n is still 25 − 10 = 15.
    Sizing s =
        size(
            40,
            null,
            List.of(child(10)),
            List.of(home("5000000")),
            "0",
            "0",
            null,
            20,
            new BigDecimal("20000000"),
            Goal.LOAN_COVER);
    assertSizing(s, "5200000.00", "5500000", null, 20, 15);
    assertThat(s.reasonCodes()).containsExactly("SUIT-INCOME-DECLINED");
  }

  @Test
  void existingCoverLeavesNoGap() {
    // N = F − E = 2,00,000 − 50,00,000 = −48,00,000: no cover to recommend.
    Sizing s = size(45, "1000000", List.of(), List.of(), "5000000", "0", null, 20, SA_MAX);
    assertSizing(s, "-4800000.00", "0", "20000000", 15, 0);
  }

  @Test
  void cappedByTheIncomeMultiple() {
    // Age 50, I = 5,10,000, child 10: n = min(15, 60 − 50) = 10.
    // N = 5,10,000 × 0.7 × a(10) + 80,00,000 + 2,00,000 = 32,22,821.03 + 82,00,000
    //   = 1,14,22,821.03. uw_cap = 15 × 5,10,000 = 76,50,000 binds; up to the step would be
    //   80,00,000, above the cap, so down to 75,00,000.
    Sizing s =
        size(
            50, "510000", List.of(child(10)), List.of(home("8000000")), "0", "0", null, 15, SA_MAX);
    assertSizing(s, "11422821.03", "7500000", "7650000", 10, 10);
    assertThat(s.reasonCodes()).containsExactly("SUIT-CAP-UW");
  }

  @Test
  void cappedByTheProductMaximum() {
    // Age 30, I = 1 crore, child 1: n = min(24, 30) = 24.
    // N = 1,00,00,000 × 0.7 × a(24) + F = 13,38,37,049.31 + 2,00,000 = 13,40,37,049.31.
    // uw_cap = 25 crore; SA_max = 10 crore binds.
    Sizing s = size(30, "10000000", List.of(child(1)), List.of(), "0", "0", null, 25, SA_MAX);
    assertSizing(s, "134037049.31", "100000000", "250000000", 30, 24);
    assertThat(s.reasonCodes()).containsExactly("SUIT-CAP-SA-MAX");
  }

  @Test
  void aZeroRealRateUsesNYears() {
    // i = g, so r = 0 and a(n) = n. Age 40, child 15: n = 10.
    // N = 10,00,000 × 0.7 × 10 + F = 70,00,000 + 2,00,000 = 72,00,000 → 75,00,000.
    ActuarialParams flat =
        new ActuarialParams(
            PARAMS.paramsVersion(),
            PARAMS.rulesVersion(),
            true,
            PARAMS.consumptionShare(),
            new BigDecimal("0.05"),
            new BigDecimal("0.05"),
            PARAMS.independenceAge(),
            PARAMS.coverToAgeDefault(),
            PARAMS.finalExpensesInr(),
            PARAMS.employerCoverShare(),
            PARAMS.saRoundingStepInr(),
            PARAMS.goalCorpora());
    Sizing s =
        SuitabilityCalculator.size(
            needs("1000000", List.of(child(15)), List.of(), "0", "0", null, Goal.INCOME_PROTECTION),
            40,
            60,
            flat,
            new BigDecimal("20"),
            SA_MAX);
    assertSizing(s, "7200000.00", "7500000", "20000000", 20, 10);
  }

  @Test
  void bandRoundsUpButNeverAboveTheCap() {
    BigDecimal step = new BigDecimal("500000");
    assertThat(SuitabilityCalculator.band(new BigDecimal("500000"), null, step))
        .isEqualByComparingTo("500000");
    assertThat(SuitabilityCalculator.band(new BigDecimal("500000.01"), null, step))
        .isEqualByComparingTo("1000000");
    assertThat(SuitabilityCalculator.band(BigDecimal.ZERO, null, step)).isZero();
    assertThat(SuitabilityCalculator.band(new BigDecimal("400000"), new BigDecimal("400000"), step))
        .isZero();
  }

  // --- helpers -----------------------------------------------------------------------------

  private static Sizing size(
      int age,
      @Nullable String income,
      List<Dependant> dependants,
      List<Liability> liabilities,
      String existing,
      String employer,
      @Nullable String assets,
      int multiple,
      @Nullable BigDecimal saMax,
      Goal... goals) {
    Goal[] g = goals.length == 0 ? new Goal[] {Goal.INCOME_PROTECTION} : goals;
    return SuitabilityCalculator.size(
        needs(income, dependants, liabilities, existing, employer, assets, g),
        age,
        60,
        PARAMS,
        BigDecimal.valueOf(multiple),
        saMax);
  }

  private static NeedsPayload needs(
      @Nullable String income,
      List<Dependant> dependants,
      List<Liability> liabilities,
      String existing,
      String employer,
      @Nullable String assets,
      Goal... goals) {
    return new NeedsPayload(List.of(goals), income, IncomeType.SALARIED, "0", false, 0)
        .dependants(dependants)
        .liabilities(liabilities)
        .existingCoverInr(existing)
        .employerCoverInr(employer)
        .earmarkedAssetsInr(assets);
  }

  private static void assertSizing(
      Sizing s,
      String need,
      String recommended,
      @Nullable String uwCap,
      int term,
      int dependencyYears) {
    assertThat(s.need().setScale(2, RoundingMode.HALF_UP).toPlainString()).isEqualTo(need);
    assertThat(s.recommended()).isEqualByComparingTo(recommended);
    if (uwCap == null) {
      assertThat(s.uwCap()).isNull();
    } else {
      assertThat(s.uwCap()).isEqualByComparingTo(uwCap);
    }
    assertThat(s.termYears()).isEqualTo(term);
    assertThat(s.dependencyYears()).isEqualTo(dependencyYears);
  }

  private static Dependant child(int age) {
    return new Dependant(RelationEnum.CHILD, age);
  }

  private static Dependant spouse(int age) {
    return new Dependant(RelationEnum.SPOUSE, age);
  }

  private static Liability home(String outstanding) {
    return new Liability(KindEnum.HOME, outstanding, 15);
  }

  static Rules.Release release() {
    try {
      return Rules.load("classpath*:dmn/*.dmn", "classpath*:params/*.yaml")
          .release("rules-2026.09.1");
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
