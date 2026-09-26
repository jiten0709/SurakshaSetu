package com.surakshasetu.domain.suitability;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.http.MediaType;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * POST /v1/suitability/evaluate end to end under rules-2026.09.1, with premiums from the DUMMY rate
 * table (rating/dummy-rates-2026.09.1.yaml). The base case is the TDD needs vector: age 34, income
 * ₹24 lakh, spouse 32 and child 4, home loan ₹35 lakh, employer cover ₹15 lakh, goals income
 * protection and loan cover.
 */
@ExtendWith(OutputCaptureExtension.class)
class SuitabilityApiTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final Path VECTORS = Path.of("..", "content", "testvectors", "needs");

  @Test
  void theTddExampleFitsTermPlans() throws Exception {
    JsonNode r = evaluate(request("01-tdd-example.json"));

    assertThat(r.get("outcome").asString()).isEqualTo("FIT");
    assertThat(strings(r, "fit_types")).containsExactly("TERM", "TERM_ROP");
    assertThat(r.get("excluded").isEmpty()).isTrue();
    // n = min(max(26 spouse, 21 child), 60 − 34) = 26.
    // N = 24,00,000 × 0.7 × a(26) + 35,00,000 + 2,00,000 − 0.5 × 15,00,000 = 3,71,47,714.43.
    assertThat(r.get("need_inr").asString()).isEqualTo("37147714.43");
    assertThat(r.get("recommended_cover_inr").asString()).isEqualTo("37500000");
    assertThat(r.get("uw_cap_inr").asString()).isEqualTo("60000000"); // 25 × 24,00,000
    assertThat(r.get("term_years").asInt()).isEqualTo(26);
    // The cheaper of the two term plans at the ranker's sizing: 999N001V02 at ₹3.75 crore for 26
    // years is 37,500 × 1.60 = 60,000 (999N002V01, capped at ₹2 crore, is 84,000). No existing
    // premiums: 2.5% of income.
    assertThat(r.get("affordability_premium_estimate_inr").asString()).isEqualTo("60000");
    assertThat(r.get("affordability").asString()).isEqualTo("green");
    assertThat(strings(r, "vulnerability_flags")).isEmpty();
    assertThat(r.get("profile_sufficiency").decimalValue()).isEqualByComparingTo("0.975");
    assertThat(strings(r, "rule_ids"))
        .containsExactly("FIT-01", "FIT-02", "FIT-03", "MA-01", "AFL-01", "AFF-01");
    assertThat(strings(r, "reason_codes")).isEmpty();
    assertThat(r.has("escalation_reason")).isFalse();
    JsonNode a = r.get("assumptions");
    assertThat(a.get("cover_to_age").asInt()).isEqualTo(60);
    assertThat(a.get("dependency_years").asInt()).isEqualTo(26);
    assertThat(a.get("discount_rate").asString()).isEqualTo("0.07");
    assertThat(a.get("income_growth").asString()).isEqualTo("0.05");
    assertThat(a.get("consumption_share").asString()).isEqualTo("0.30");
    assertThat(a.get("final_expenses_inr").asString()).isEqualTo("200000");
    assertThat(a.get("existing_cover_counted_inr").asString()).isEqualTo("750000");
    assertThat(r.get("rules_version").asString()).isEqualTo("rules-2026.09.1");
    assertThat(r.get("params_version").asString()).isEqualTo("actuarial-2026.09.1");
  }

  static Stream<String> vectors() throws Exception {
    try (Stream<Path> files = Files.list(VECTORS)) {
      return files
          .map(p -> p.getFileName().toString())
          .filter(n -> n.endsWith(".json"))
          .sorted()
          .toList()
          .stream();
    }
  }

  @ParameterizedTest
  @MethodSource("vectors")
  void inputsSha256IsTheNeedsVectorHash(String vector) throws Exception {
    JsonNode expected = JSON.readTree(Files.readString(VECTORS.resolve(vector)));
    assertThat(evaluate(request(vector)).get("inputs_sha256").asString())
        .isEqualTo(expected.get("inputs_sha256").asString());
  }

  @Test
  void aSavingsGoalIsExcludedInPhaseOne() throws Exception {
    ObjectNode request = request("01-tdd-example.json");
    needs(request).putArray("goals").add("income_protection").add("savings");
    JsonNode r = evaluate(request);
    assertThat(r.get("outcome").asString()).isEqualTo("FIT");
    assertThat(strings(r, "fit_types")).containsExactly("TERM", "TERM_ROP");
    assertThat(strings(r.get("excluded"), "NON_PAR_SAVINGS"))
        .containsExactly("SUIT-SAV-LAUNCH-DISABLED");
    assertThat(strings(r.get("excluded"), "PAR")).containsExactly("SUIT-PAR-LAUNCH-DISABLED");
    assertThat(strings(r.get("excluded"), "ULIP")).containsExactly("SUIT-ULIP-LAUNCH-DISABLED");
  }

  @Test
  void aSavingsOnlyNeedIsOutOfScope() throws Exception {
    ObjectNode request = request("01-tdd-example.json");
    needs(request).putArray("goals").add("savings");
    JsonNode r = evaluate(request);
    assertThat(r.get("outcome").asString()).isEqualTo("ESCALATE");
    assertThat(r.get("escalation_reason").asString()).isEqualTo("HE_OUT_OF_SCOPE");
    assertThat(strings(r, "fit_types")).isEmpty();
    assertThat(strings(r, "reason_codes")).contains("HE_OUT_OF_SCOPE", "SUIT-NO-FIT-TYPE");
    assertThat(r.get("affordability").asString()).isEqualTo("unknown"); // nothing to price
  }

  @Test
  void vulnerabilityWithAComplexGoalEscalatesFirst() throws Exception {
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("financial_distress", true);
    needs(request).putArray("goals").add("income_protection").add("retirement");
    JsonNode r = evaluate(request);
    assertThat(r.get("outcome").asString()).isEqualTo("ESCALATE");
    assertThat(r.get("escalation_reason").asString()).isEqualTo("HE_VULNERABLE_COMPLEX");
    assertThat(strings(r, "vulnerability_flags")).containsExactly("VULN_FINANCIAL_DISTRESS");
    assertThat(strings(r.get("excluded"), "ULIP"))
        .containsExactly("SUIT-ULIP-LAUNCH-DISABLED", "SUIT-VULN-COMPLEX");
    assertThat(strings(r, "rule_ids")).contains("VUL-04", "CG-01", "EXC-04", "OUT-01", "AFL-02");
  }

  @Test
  void existingCoverBeyondTheNeedIsNoGap() throws Exception {
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("existing_cover_inr", "100000000");
    JsonNode r = evaluate(request);
    assertThat(r.get("outcome").asString()).isEqualTo("NO_GAP");
    assertThat(r.get("need_inr").asString()).startsWith("-");
    assertThat(r.get("recommended_cover_inr").asString()).isEqualTo("0");
    assertThat(strings(r, "reason_codes")).containsExactly("SUIT-NO-GAP");
  }

  @Test
  void redAffordabilityEscalates() throws Exception {
    // (60,000 + 5,00,000) / 24,00,000 = 23.3%: red.
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("existing_annual_premium_inr", "500000");
    JsonNode r = evaluate(request);
    assertThat(r.get("affordability").asString()).isEqualTo("red");
    assertThat(r.get("affordability_premium_estimate_inr").asString()).isEqualTo("560000");
    assertThat(r.get("outcome").asString()).isEqualTo("ESCALATE");
    assertThat(r.get("escalation_reason").asString()).isEqualTo("HE_AFFORDABILITY_RED");
  }

  @Test
  void noPersonalIncomeLeavesNoInsurableCover() throws Exception {
    // m(a) × 0 = 0 caps the cover at nothing while a need remains: the non-earning basis is the
    // underwriting policy's, so an advisor takes it.
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("income_type", "homemaker").put("annual_income_inr", "0");
    JsonNode r = evaluate(request);
    assertThat(r.get("outcome").asString()).isEqualTo("ESCALATE");
    assertThat(r.get("escalation_reason").asString()).isEqualTo("HE_OUT_OF_SCOPE");
    assertThat(strings(r, "reason_codes")).contains("SUIT-NO-INSURABLE-COVER");
    assertThat(strings(r, "vulnerability_flags")).containsExactly("VULN_NO_PERSONAL_INCOME");
    assertThat(r.get("uw_cap_inr").asString()).isEqualTo("0");
    assertThat(r.get("affordability").asString()).isEqualTo("unknown");
  }

  @Test
  void aDeclinedIncomeHasNoCapAndUnknownAffordability() throws Exception {
    JsonNode r = evaluate(request("03-income-declined.json"));
    assertThat(r.get("uw_cap_inr").isNull()).isTrue();
    assertThat(r.get("affordability").asString()).isEqualTo("unknown");
    assertThat(strings(r, "reason_codes")).contains("SUIT-INCOME-DECLINED");
    assertThat(r.get("assumptions").get("cover_to_age").asInt()).isEqualTo(65);
  }

  @Test
  void implausibleValuesAreFlaggedNotRejected() throws Exception {
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("annual_income_inr", "20000"); // below the floor; loans 175× income
    JsonNode r = evaluate(request);
    assertThat(strings(r, "reason_codes")).contains("IMPLAUSIBLE_INPUT");
    assertThat(strings(r, "rule_ids")).contains("PLB-01", "PLB-02");
  }

  @Test
  void anOmittedCoverToAgeTakesTheParameterDefault() throws Exception {
    ObjectNode request = request("02-short-path.json");
    JsonNode r = evaluate(request);
    assertThat(r.get("assumptions").get("cover_to_age").asInt()).isEqualTo(60);
    assertThat(r.get("profile_sufficiency").decimalValue()).isEqualByComparingTo("0.85");
  }

  @Test
  void requiredSlotsFollowThePinnedRules() throws Exception {
    JsonNode r =
        JSON.readTree(
            api(get("/v1/suitability/required-slots?pins.rules=rules-2026.09.1"))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString());
    assertThat(r).hasSize(10);
    assertThat(r.get(0).get("slot").asString()).isEqualTo("goals");
    assertThat(r.get(1).get("weight").decimalValue()).isEqualByComparingTo("0.25");
    assertThat(r.get(1).get("reason_line_id").asString()).isEqualTo("RL-S2-INCOME");
  }

  @Test
  void anExplicitNullOnADefaultedMemberReadsAsAbsent() throws Exception {
    // employer_cover_inr isn't nullable; a client that sends null gets the contract default "0"
    // rather than a 500 (found by Schemathesis).
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("employer_cover_inr", "0");
    JsonNode zero = evaluate(request);
    needs(request).putNull("employer_cover_inr");
    JsonNode nulled =
        JSON.readTree(
            apiRejecting(
                    post("/v1/suitability/evaluate")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(JSON.writeValueAsString(request)))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString());
    assertThat(nulled.get("need_inr")).isEqualTo(zero.get("need_inr"));
  }

  @Test
  void suitabilityLogsNoRequestValue(CapturedOutput output) throws Exception {
    String income = "7777777.77";
    String loan = "3141592.65";
    ObjectNode request = request("01-tdd-example.json");
    needs(request).put("annual_income_inr", income);
    needs(request)
        .putArray("liabilities")
        .addObject()
        .put("kind", "home")
        .put("outstanding_inr", loan)
        .put("years_left", 11);
    evaluate(request);

    assertThat(output.getAll()).contains("suitability");
    assertThat(output.getAll()).doesNotContain(income, loan, "7777777", "3141592");
  }

  // --- helpers -----------------------------------------------------------------------------

  /** A suitability request around a needs vector, for the TDD example's eligibility. */
  private static ObjectNode request(String vector) throws Exception {
    ObjectNode request = JSON.createObjectNode();
    request.putObject("pins").put("rules", "rules-2026.09.1");
    ObjectNode eligibility = request.putObject("eligibility");
    eligibility.put("age_years", 34).put("tobacco_12m", false);
    eligibility.putArray("eligible_uins").add("999N001V02").add("999N002V01");
    eligibility.putArray("flags");
    request.set("needs", JSON.readTree(Files.readString(VECTORS.resolve(vector))).get("needs"));
    return request;
  }

  private static ObjectNode needs(ObjectNode request) {
    return (ObjectNode) request.get("needs");
  }

  private JsonNode evaluate(ObjectNode request) throws Exception {
    return JSON.readTree(
        api(post("/v1/suitability/evaluate")
                .contentType(MediaType.APPLICATION_JSON)
                .content(JSON.writeValueAsString(request)))
            .andExpect(status().isOk())
            .andReturn()
            .getResponse()
            .getContentAsString());
  }

  private static List<String> strings(JsonNode node, String field) {
    List<String> out = new ArrayList<>();
    node.get(field).forEach(v -> out.add(v.asString()));
    return out;
  }
}
