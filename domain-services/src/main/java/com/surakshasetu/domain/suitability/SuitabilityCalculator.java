package com.surakshasetu.domain.suitability;

import com.surakshasetu.domain.common.Rules.ActuarialParams;
import com.surakshasetu.domain.common.Rules.GoalCorpus;
import com.surakshasetu.domain.contract.model.Dependant;
import com.surakshasetu.domain.contract.model.Goal;
import com.surakshasetu.domain.contract.model.Liability;
import com.surakshasetu.domain.contract.model.NeedsPayload;
import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import tools.jackson.databind.JsonNode;

/**
 * Needs-based cover sizing (TDD §3.7), in BigDecimal at DECIMAL128:
 *
 * <pre>
 * N = I(1−s)·(1−(1+r)^−n)/r + L + G + F − E − A,   r = (1+i)/(1+g) − 1
 * SA_rec = band(min(max(N, 0), m(a)·I, SA_max))
 * </pre>
 *
 * The parameters come from the pinned release's params file and m(a) from its DMN, so no rate, band
 * or multiple lives here. n: each dependant depends until the independence age for its relation
 * (children), or else until the customer's cover-to age; n is the longest of those, capped at
 * cover_to_age − age, and 0 with no dependants.
 */
final class SuitabilityCalculator {

  private static final MathContext MC = MathContext.DECIMAL128;

  private SuitabilityCalculator() {}

  /** The sized need; amounts are unrounded except {@code recommended}, a multiple of the step. */
  record Sizing(
      BigDecimal need,
      BigDecimal recommended,
      @Nullable BigDecimal uwCap,
      int termYears,
      int dependencyYears,
      BigDecimal existingCoverCounted,
      List<String> reasonCodes) {}

  static Sizing size(
      NeedsPayload needs,
      int age,
      int coverToAge,
      ActuarialParams params,
      BigDecimal incomeMultiple,
      @Nullable BigDecimal saMax) {
    BigDecimal r =
        BigDecimal.ONE
            .add(params.discountRate())
            .divide(BigDecimal.ONE.add(params.incomeGrowth()), MC)
            .subtract(BigDecimal.ONE);
    int horizon = Math.max(coverToAge - age, 0);
    int n = 0;
    for (Dependant d : needs.getDependants()) {
      Integer independence = params.independenceAge().get(d.getRelation().getValue());
      n = Math.max(n, independence == null ? horizon : Math.max(independence - d.getAge(), 0));
    }
    n = Math.min(n, horizon);

    BigDecimal income = money(needs.getAnnualIncomeInr());
    BigDecimal incomeTerm =
        income == null
            ? BigDecimal.ZERO
            : income
                .multiply(BigDecimal.ONE.subtract(params.consumptionShare()), MC)
                .multiply(annuity(r, n), MC);
    BigDecimal loans = BigDecimal.ZERO;
    for (Liability l : needs.getLiabilities()) {
      loans = loans.add(new BigDecimal(l.getOutstandingInr()));
    }
    BigDecimal goals = BigDecimal.ZERO;
    for (GoalCorpus c : params.goalCorpora()) {
      if (!needs.getGoals().contains(Goal.fromValue(c.goal()))) {
        continue;
      }
      for (Dependant d : needs.getDependants()) {
        if (d.getRelation().getValue().equals(c.relation()) && d.getAge() <= c.dueAtAge()) {
          goals =
              goals.add(
                  c.amountInr()
                      .divide(BigDecimal.ONE.add(r).pow(c.dueAtAge() - d.getAge(), MC), MC));
        }
      }
    }
    BigDecimal existing =
        new BigDecimal(needs.getExistingCoverInr())
            .add(new BigDecimal(needs.getEmployerCoverInr()).multiply(params.employerCoverShare()));
    BigDecimal assets = money(needs.getEarmarkedAssetsInr());
    BigDecimal need =
        incomeTerm
            .add(loans)
            .add(goals)
            .add(params.finalExpensesInr())
            .subtract(existing)
            .subtract(assets == null ? BigDecimal.ZERO : assets);

    List<String> reasons = new ArrayList<>();
    BigDecimal uwCap = income == null ? null : incomeMultiple.multiply(income);
    if (income == null) {
      reasons.add("SUIT-INCOME-DECLINED");
    }
    BigDecimal cap = uwCap;
    String capCode = "SUIT-CAP-UW";
    if (saMax != null && (cap == null || saMax.compareTo(cap) < 0)) {
      cap = saMax;
      capCode = "SUIT-CAP-SA-MAX";
    }
    BigDecimal wanted = need.max(BigDecimal.ZERO);
    if (cap != null && wanted.compareTo(cap) > 0) {
      reasons.add(capCode);
    }
    BigDecimal recommended =
        band(cap == null ? wanted : wanted.min(cap), cap, params.saRoundingStepInr());
    return new Sizing(need, recommended, uwCap, horizon, n, existing, reasons);
  }

  /** (1 − (1+r)^−n) / r, the present value of 1 a year for n years; n when r is 0. */
  static BigDecimal annuity(BigDecimal r, int n) {
    if (r.signum() == 0) {
      return BigDecimal.valueOf(n);
    }
    BigDecimal discount = BigDecimal.ONE.divide(BigDecimal.ONE.add(r).pow(n, MC), MC);
    return BigDecimal.ONE.subtract(discount).divide(r, MC);
  }

  /** Up to the next multiple of the step, but never above the cap (then down to a multiple). */
  static BigDecimal band(BigDecimal amount, @Nullable BigDecimal cap, BigDecimal step) {
    if (amount.signum() == 0) {
      return BigDecimal.ZERO;
    }
    BigDecimal up = amount.divide(step, 0, RoundingMode.CEILING).multiply(step);
    return cap == null || up.compareTo(cap) <= 0
        ? up
        : cap.divide(step, 0, RoundingMode.FLOOR).multiply(step);
  }

  /**
   * The weighted share of slots answered. A slot counts when the needs JSON as sent has it and it
   * isn't null (declined); an empty goals list is unanswered.
   */
  static BigDecimal profileSufficiency(JsonNode needs, Map<String, BigDecimal> weights) {
    BigDecimal total = BigDecimal.ZERO;
    BigDecimal answered = BigDecimal.ZERO;
    for (var w : weights.entrySet()) {
      total = total.add(w.getValue());
      JsonNode value = needs.get(w.getKey());
      if (value != null && !value.isNull() && !(w.getKey().equals("goals") && value.isEmpty())) {
        answered = answered.add(w.getValue());
      }
    }
    return total.signum() == 0
        ? BigDecimal.ZERO
        : answered.divide(total, 4, RoundingMode.HALF_UP).stripTrailingZeros();
  }

  private static @Nullable BigDecimal money(@Nullable String amount) {
    return amount == null ? null : new BigDecimal(amount);
  }
}
