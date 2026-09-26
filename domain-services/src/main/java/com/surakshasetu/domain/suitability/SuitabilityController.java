package com.surakshasetu.domain.suitability;

import static com.surakshasetu.domain.common.Rules.inputs;

import com.surakshasetu.domain.catalog.CatalogRepository;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import com.surakshasetu.domain.common.RawJson;
import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.common.Rules.ActuarialParams;
import com.surakshasetu.domain.common.Uuid7;
import com.surakshasetu.domain.contract.SuitabilityApi;
import com.surakshasetu.domain.contract.model.EligibilitySnapshot;
import com.surakshasetu.domain.contract.model.Goal;
import com.surakshasetu.domain.contract.model.Liability;
import com.surakshasetu.domain.contract.model.NeedsPayload;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.ProductType;
import com.surakshasetu.domain.contract.model.RequiredSlot;
import com.surakshasetu.domain.contract.model.SuitabilityAssumptions;
import com.surakshasetu.domain.contract.model.SuitabilityRequest;
import com.surakshasetu.domain.contract.model.SuitabilityResult;
import com.surakshasetu.domain.contract.model.SuitabilityResult.AffordabilityEnum;
import com.surakshasetu.domain.contract.model.SuitabilityResult.OutcomeEnum;
import com.surakshasetu.domain.quote.QuoteAdapter;
import com.surakshasetu.domain.quote.RatingEngine;
import com.surakshasetu.domain.suitability.SuitabilityCalculator.Sizing;
import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.jspecify.annotations.Nullable;
import org.kie.dmn.api.core.DMNModel;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * S2 suitability (TDD §3.7) under the pinned rules: vulnerability, fit types and exclusions, m(a),
 * affordability, plausibility, sufficiency and the outcome come from the DMN tables; cover sizing
 * from {@link SuitabilityCalculator} over the release's params. inputs_sha256 is taken over the
 * needs as sent, minus slots_sha256 (I2).
 */
@RestController
class SuitabilityController implements SuitabilityApi {

  private static final Logger log = LoggerFactory.getLogger(SuitabilityController.class);
  private static final JsonMapper JSON = JsonMapper.builder().build();

  private final Rules rules;
  private final CatalogRepository catalog;
  private final RatingEngine rating;

  SuitabilityController(Rules rules, CatalogRepository catalog, RatingEngine rating) {
    this.rules = rules;
    this.catalog = catalog;
    this.rating = rating;
  }

  @Override
  public ResponseEntity<SuitabilityResult> evaluateSuitability(SuitabilityRequest request) {
    long started = System.nanoTime();
    Rules.Release release = rules.release(request.getPins().getRules());
    ActuarialParams params = release.params();
    DMNModel model = release.suitability();
    JsonNode sent = JSON.readTree(RawJson.current()).get("needs");
    String inputsSha256 = Jcs.needsSha256(sent);
    NeedsPayload needs = request.getNeeds();
    EligibilitySnapshot eligibility = request.getEligibility();
    int age = eligibility.getAgeYears();
    BigDecimal income =
        needs.getAnnualIncomeInr() == null ? null : new BigDecimal(needs.getAnnualIncomeInr());
    Set<String> ruleIds = new LinkedHashSet<>();
    Set<String> reasons = new LinkedHashSet<>();
    UUID decisionId = Uuid7.next();

    Set<String> vulnerability = new LinkedHashSet<>();
    for (var row :
        release.rows(
            model,
            "Vulnerability",
            inputs(
                "age",
                age,
                "income_type",
                needs.getIncomeType().getValue(),
                "annual_income",
                income,
                "financial_distress",
                needs.getFinancialDistress(),
                "comprehension_difficulty_count",
                needs.getComprehensionDifficultyCount()))) {
      addRule(ruleIds, row);
      vulnerability.add((String) row.get("flag"));
    }
    boolean vulnerable = !vulnerability.isEmpty();

    // Product types in goal-rank order; a type with any exclusion reason is excluded.
    Set<String> candidates = new LinkedHashSet<>();
    boolean complexGoal = false;
    for (Goal goal : needs.getGoals()) {
      for (var row : release.rows(model, "FitTypes", inputs("goal", goal.getValue()))) {
        addRule(ruleIds, row);
        candidates.add((String) row.get("product_type"));
      }
      var row = release.rows(model, "ComplexGoals", inputs("goal", goal.getValue())).getFirst();
      addRule(ruleIds, row);
      complexGoal |= Boolean.TRUE.equals(row.get("complex"));
    }
    List<ProductType> fit = new ArrayList<>();
    Map<String, List<String>> excluded = new LinkedHashMap<>();
    for (String type : candidates) {
      List<String> codes = new ArrayList<>();
      for (var row :
          release.rows(
              model, "Exclusions", inputs("product_type", type, "vulnerable", vulnerable))) {
        addRule(ruleIds, row);
        codes.add((String) row.get("reason_code"));
      }
      if (codes.isEmpty()) {
        fit.add(ProductType.fromValue(type));
      } else {
        excluded.put(type, codes);
      }
    }

    var multipleRow = release.rows(model, "IncomeMultiple", inputs("age", age)).getFirst();
    addRule(ruleIds, multipleRow);
    // The customer's eligible, launched products of a fitting type: they bound the cover and
    // price the affordability estimate.
    List<Product> products =
        catalog.products(ProductStatus.IN_FORCE, null, true, BusinessDates.on(null)).stream()
            .filter(p -> eligibility.getEligibleUins().contains(p.getUin()))
            .filter(p -> fit.contains(p.getCategory()))
            .toList();
    BigDecimal saMax =
        products.isEmpty() || products.stream().anyMatch(p -> p.getSaMaxInr() == null)
            ? null
            : products.stream()
                .map(p -> new BigDecimal(p.getSaMaxInr()))
                .max(Comparator.naturalOrder())
                .orElseThrow();
    int coverToAge =
        sent.hasNonNull("cover_to_age") ? needs.getCoverToAge() : params.coverToAgeDefault();
    Sizing sizing =
        SuitabilityCalculator.size(
            needs, age, coverToAge, params, (BigDecimal) multipleRow.get("multiple"), saMax);
    reasons.addAll(sizing.reasonCodes());

    BigDecimal loans = BigDecimal.ZERO;
    for (Liability l : needs.getLiabilities()) {
      loans = loans.add(new BigDecimal(l.getOutstandingInr()));
    }
    for (var row :
        release.rows(
            model, "Plausibility", inputs("annual_income", income, "loans_total", loans))) {
      addRule(ruleIds, row);
      reasons.add((String) row.get("flag"));
    }

    BigDecimal estimate =
        premiumEstimate(products, eligibility, needs, sizing, params.saRoundingStepInr());
    BigDecimal share =
        income == null || income.signum() == 0 || estimate == null
            ? null
            : estimate.divide(income, MathContext.DECIMAL128);
    var affordability =
        release.decide(
            model, "Affordability", inputs("premium_to_income", share, "vulnerable", vulnerable));
    addRule(ruleIds, row(affordability.get("AffordabilityLimits")));
    addRule(ruleIds, row(affordability.get("Affordability")));
    String band = (String) row(affordability.get("Affordability")).get("band");

    Map<String, BigDecimal> weights = new LinkedHashMap<>();
    for (var row : release.rows(model, "RequiredSlots", inputs())) {
      weights.put((String) row.get("slot"), (BigDecimal) row.get("weight"));
    }
    BigDecimal sufficiency = SuitabilityCalculator.profileSufficiency(sent, weights);

    boolean needPositive = sizing.need().signum() > 0;
    List<Map<String, @Nullable Object>> outcomes =
        release.rows(
            model,
            "Outcome",
            inputs(
                "vulnerable_complex",
                vulnerable && complexGoal,
                "out_of_scope",
                fit.isEmpty(),
                "uninsurable",
                needPositive && sizing.recommended().signum() == 0,
                "affordability",
                band,
                "need_positive",
                needPositive));
    for (var row : outcomes) {
      addRule(ruleIds, row);
      addIfPresent(reasons, row.get("escalation_reason"));
      addIfPresent(reasons, row.get("reason_code"));
    }
    String outcome = (String) outcomes.getFirst().get("outcome");

    SuitabilityResult result =
        new SuitabilityResult(
                decisionId,
                OutcomeEnum.fromValue(outcome),
                sufficiency,
                fit,
                excluded,
                money(sizing.need()),
                money(sizing.recommended()),
                sizing.uwCap() == null ? null : money(sizing.uwCap()),
                sizing.termYears(),
                AffordabilityEnum.fromValue(band),
                List.copyOf(vulnerability),
                new SuitabilityAssumptions(
                    coverToAge,
                    sizing.dependencyYears(),
                    params.discountRate().toPlainString(),
                    params.incomeGrowth().toPlainString(),
                    params.consumptionShare().toPlainString(),
                    money(params.finalExpensesInr()),
                    money(sizing.existingCoverCounted())),
                List.copyOf(ruleIds),
                List.copyOf(reasons),
                release.paramsVersion(),
                release.rulesVersion(),
                inputsSha256)
            .escalationReason((String) outcomes.getFirst().get("escalation_reason"))
            .affordabilityPremiumEstimateInr(estimate == null ? null : money(estimate));
    log.info(
        "suitability {} under {}: {} {} in {} ms",
        decisionId,
        release.rulesVersion(),
        outcome,
        ruleIds,
        (System.nanoTime() - started) / 1_000_000);
    return ResponseEntity.ok(result);
  }

  /** The S2 questions and their sufficiency weights under the pinned rules. */
  @Override
  public ResponseEntity<List<RequiredSlot>> getRequiredSlots(String pinsRules) {
    Rules.Release release = rules.release(pinsRules);
    return ResponseEntity.ok(
        release.rows(release.suitability(), "RequiredSlots", inputs()).stream()
            .map(
                r ->
                    new RequiredSlot(
                        (String) r.get("slot"),
                        (BigDecimal) r.get("weight"),
                        (String) r.get("reason_line_id")))
            .toList());
  }

  /**
   * The cheapest indicative premium among the fitting products, each sized as the ranker sizes it
   * (cover and term within its limits, its default PPT, no riders), plus the premiums already paid;
   * null when nothing can be rated. Undisclosed tobacco is priced as tobacco, so the estimate errs
   * high. No new cover prices at zero.
   */
  private @Nullable BigDecimal premiumEstimate(
      List<Product> products,
      EligibilitySnapshot eligibility,
      NeedsPayload needs,
      Sizing sizing,
      BigDecimal step) {
    BigDecimal existing = new BigDecimal(needs.getExistingAnnualPremiumInr());
    if (sizing.recommended().signum() == 0) {
      return existing;
    }
    int age = eligibility.getAgeYears();
    boolean tobacco = !Boolean.FALSE.equals(eligibility.getTobacco12m());
    return products.stream()
        .flatMap(
            p ->
                QuoteAdapter.size(p, age, sizing.recommended(), sizing.termYears(), step).stream()
                    .flatMap(
                        sized ->
                            rating
                                .rate(
                                    new RatingEngine.Basis(
                                        p.getUin(),
                                        age,
                                        tobacco,
                                        eligibility.getGender(),
                                        sized.sumAssured(),
                                        sized.termYears(),
                                        catalog.quoteDefaults(p).ppt(),
                                        List.of()))
                                .stream()))
        .map(RatingEngine.Premium::total)
        .min(Comparator.naturalOrder())
        .map(existing::add)
        .orElse(null);
  }

  @SuppressWarnings("unchecked")
  private static Map<String, @Nullable Object> row(@Nullable Object decisionResult) {
    return (Map<String, @Nullable Object>) decisionResult;
  }

  /** Default rows carry no rule_id: they aren't rules. */
  private static void addRule(Set<String> ruleIds, Map<String, @Nullable Object> row) {
    addIfPresent(ruleIds, row.get("rule_id"));
  }

  private static void addIfPresent(Set<String> codes, @Nullable Object code) {
    if (code != null) {
      codes.add((String) code);
    }
  }

  /** Rupees to at most 2 decimals, without trailing zeros. */
  private static String money(BigDecimal amount) {
    return amount.setScale(2, RoundingMode.HALF_UP).stripTrailingZeros().toPlainString();
  }
}
