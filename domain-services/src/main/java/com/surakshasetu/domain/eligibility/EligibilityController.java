package com.surakshasetu.domain.eligibility;

import com.surakshasetu.domain.catalog.CatalogRepository;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import com.surakshasetu.domain.common.RawJson;
import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.common.Uuid7;
import com.surakshasetu.domain.contract.EligibilityApi;
import com.surakshasetu.domain.contract.model.EligibilityRequest;
import com.surakshasetu.domain.contract.model.EligibilityResult;
import com.surakshasetu.domain.contract.model.EligibilityResult.OutcomeEnum;
import com.surakshasetu.domain.contract.model.EligibilityResult.UwPathEnum;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.Proposer;
import com.surakshasetu.domain.contract.model.RequiredAttribute;
import com.surakshasetu.domain.quote.RatingEngine;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.RestController;

/**
 * S1 eligibility (TDD §3.6, §7.3). The DMN table decides E-01..E-06 under the pinned rules; this
 * class adds what the prompt keeps out of DMN: proposer complexity, pincode serviceability, the
 * underwriting flags and the eligible products. Precedence follows TDD §3.1: data erasure, then
 * escalation, then not eligible, then re-ask, then eligible.
 */
@RestController
class EligibilityController implements EligibilityApi {

  private static final Logger log = LoggerFactory.getLogger(EligibilityController.class);
  // TDD §3.6 complexity: a proposer who isn't the life assured is complex outside these.
  private static final Set<String> SIMPLE_RELATIONSHIPS =
      Set.of("self", "spouse", "child", "parent");

  private final Rules rules;
  private final CatalogRepository catalog;
  private final RatingEngine rating;
  private final JdbcClient jdbc;

  EligibilityController(
      Rules rules, CatalogRepository catalog, RatingEngine rating, JdbcClient jdbc) {
    this.rules = rules;
    this.catalog = catalog;
    this.rating = rating;
    this.jdbc = jdbc;
  }

  /** One row of the Eligibility table. */
  private record Row(
      String ruleId, String outcome, @Nullable String escalationReason, @Nullable String flag) {}

  @Override
  public ResponseEntity<EligibilityResult> evaluateEligibility(EligibilityRequest request) {
    long started = System.nanoTime();
    Rules.Release release = rules.release(request.getPins().getRules());
    String inputsSha256 = Jcs.sha256Hex(RawJson.current());
    String occupation = occupationClass(request.getOccupationCode());
    Set<String> ruleIds = new LinkedHashSet<>();
    UUID decisionId = Uuid7.next();

    Row row = table(release, request.getAgeYears(), request, occupation);
    ruleIds.add(row.ruleId());
    if (row.outcome().equals("DATA_ERASURE_EXIT")) { // V2: the customer is a minor; nothing else
      log.info("eligibility {} under {}: DATA_ERASURE_EXIT", decisionId, release.rulesVersion());
      return ResponseEntity.ok(
          result(decisionId, OutcomeEnum.DATA_ERASURE_EXIT, List.of(), List.of(), ruleIds, release)
              .inputsSha256(inputsSha256));
    }

    // Proposer ≠ life assured: the life assured's age drives the age rules and the entry-age
    // filter; a minor life assured (E-03 on la_age) is complex, so no age threshold lives here.
    int insuredAge = request.getAgeYears();
    Proposer proposer = request.getProposer();
    boolean complex = false;
    if (!proposer.getIsLifeAssured()) {
      Integer laAge = proposer.getLaAge();
      String relationship =
          proposer.getRelationship() == null
              ? ""
              : proposer.getRelationship().strip().toLowerCase(Locale.ROOT);
      complex =
          laAge == null
              || !SIMPLE_RELATIONSHIPS.contains(relationship)
              || Boolean.TRUE.equals(proposer.getBusinessCover());
      if (!complex) {
        Row insured = table(release, laAge, request, occupation);
        ruleIds.add(insured.ruleId());
        if (insured.outcome().equals("DATA_ERASURE_EXIT")) {
          complex = true;
        } else if (!row.outcome().equals("HUMAN_ESCALATION")) {
          row = insured;
          insuredAge = laAge;
        }
      }
    }

    String outcome = row.outcome();
    String escalation = row.escalationReason();
    Set<String> reasons = new LinkedHashSet<>();
    if (complex && !outcome.equals("HUMAN_ESCALATION")) {
      outcome = "HUMAN_ESCALATION";
      escalation = "HE_COMPLEX_PROPOSER";
    }
    if (escalation != null) {
      reasons.add(escalation);
    }
    if (complex) {
      reasons.add("HE_COMPLEX_PROPOSER");
    }
    if (!serviceable(request.getPincode())) {
      reasons.add("REASON_PIN_UNSERVICEABLE");
      if (outcome.equals("ELIGIBLE") || outcome.equals("RE_ASK")) {
        outcome = "NOT_ELIGIBLE";
      }
    }

    Set<String> flags = new LinkedHashSet<>();
    if (row.flag() != null) {
      flags.add(row.flag());
    }
    var health = request.getHealthFlags().values();
    if (health.contains(Boolean.TRUE)) {
      flags.add("MEDICAL_UW");
    }
    if (request.getTobacco12m() == null || health.contains(null)) {
      flags.add("PREMIUM_WITHHELD"); // eligible, but S3 shows no premium
    }

    List<String> eligibleUins =
        outcome.equals("ELIGIBLE") ? eligibleUins(insuredAge) : List.<String>of();
    EligibilityResult result =
        result(
                decisionId,
                OutcomeEnum.fromValue(outcome),
                eligibleUins,
                List.copyOf(flags),
                ruleIds,
                release)
            .escalationReason(escalation)
            .reasonCodes(List.copyOf(reasons))
            .inputsSha256(inputsSha256);
    log.info(
        "eligibility {} under {}: {} {} in {} ms",
        decisionId,
        release.rulesVersion(),
        outcome,
        ruleIds,
        (System.nanoTime() - started) / 1_000_000);
    return ResponseEntity.ok(result);
  }

  /** The S1 questions under the pinned rules; gender only if a launched product rates by it. */
  @Override
  public ResponseEntity<List<RequiredAttribute>> getRequiredAttributes(
      String pinsRules, @Nullable OffsetDateTime asOf) {
    Rules.Release release = rules.release(pinsRules);
    boolean genderRated =
        catalog.products(ProductStatus.IN_FORCE, null, true, BusinessDates.on(asOf)).stream()
            .anyMatch(p -> rating.ratesByGender(p.getUin()));
    List<RequiredAttribute> attributes = new ArrayList<>();
    for (Map<String, @Nullable Object> r :
        release.rows(
            release.eligibility(),
            "RequiredAttributes",
            Rules.inputs("gender_rated", genderRated))) {
      attributes.add(
          new RequiredAttribute((String) r.get("attribute"), (String) r.get("reason_line_id"))
              .askedIf((String) r.get("asked_if")));
    }
    return ResponseEntity.ok(attributes);
  }

  private static Row table(
      Rules.Release release, int age, EligibilityRequest request, String occupation) {
    List<Map<String, @Nullable Object>> rows =
        release.rows(
            release.eligibility(),
            "Eligibility",
            Rules.inputs(
                "age",
                age,
                "residency",
                request.getResidency().getValue(),
                "occupation_risk_class",
                occupation));
    if (rows.isEmpty()) {
      throw new IllegalStateException("no eligibility rule matched");
    }
    Map<String, @Nullable Object> r = rows.getFirst();
    return new Row(
        (String) r.get("rule_id"),
        (String) r.get("outcome"),
        (String) r.get("escalation_reason"),
        (String) r.get("flag"));
  }

  /** The occupation master's risk class; declined without a code, unknown for a code not in it. */
  private String occupationClass(@Nullable String code) {
    if (code == null) {
      return "declined";
    }
    return jdbc.sql("SELECT risk_class FROM catalog.occupation WHERE code = ?")
        .param(code)
        .query(Integer.class)
        .optional()
        .map(String::valueOf)
        .orElse("unknown");
  }

  /** An unknown pincode is not serviceable. */
  private boolean serviceable(String pincode) {
    return jdbc.sql("SELECT serviceable FROM catalog.pincode WHERE pincode = ?")
        .param(pincode)
        .query(Boolean.class)
        .optional()
        .orElse(false);
  }

  /** In force and launched today, the age inside the entry band, and the minimum term fits. */
  private List<String> eligibleUins(int age) {
    return catalog.products(ProductStatus.IN_FORCE, null, true, BusinessDates.on(null)).stream()
        .filter(p -> p.getEntryAgeMin() <= age && age <= p.getEntryAgeMax())
        .filter(p -> age + p.getTermYearsMin() <= p.getMaturityAgeMax())
        .map(Product::getUin)
        .toList();
  }

  private static EligibilityResult result(
      UUID decisionId,
      OutcomeEnum outcome,
      List<String> eligibleUins,
      List<String> flags,
      Set<String> ruleIds,
      Rules.Release release) {
    boolean manual = flags.contains("MANUAL_UW") || flags.contains("MEDICAL_UW");
    return new EligibilityResult(
        decisionId,
        outcome,
        eligibleUins,
        manual ? UwPathEnum.MANUAL : UwPathEnum.STANDARD,
        flags,
        List.copyOf(ruleIds),
        List.of(),
        release.rulesVersion(),
        release.paramsVersion(),
        "");
  }
}
