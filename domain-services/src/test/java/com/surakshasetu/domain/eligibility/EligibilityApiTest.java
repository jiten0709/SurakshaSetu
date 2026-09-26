package com.surakshasetu.domain.eligibility;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.Jcs;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.http.MediaType;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** POST /v1/eligibility/evaluate and GET required-attributes against the seeded catalog. */
@ExtendWith(OutputCaptureExtension.class)
class EligibilityApiTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();

  @Test
  void theTddExampleIsEligibleForBothLaunchedTermPlans() throws Exception {
    Map<String, Object> request = tddExample();
    String body = JSON.writeValueAsString(request);
    JsonNode r = json(body);

    assertThat(r.get("outcome").asString()).isEqualTo("ELIGIBLE");
    assertThat(strings(r, "eligible_uins")).containsExactly("999N001V02", "999N002V01");
    assertThat(r.get("uw_path").asString()).isEqualTo("standard");
    assertThat(strings(r, "flags")).isEmpty();
    assertThat(strings(r, "rule_ids")).containsExactly("E-01");
    assertThat(strings(r, "reason_codes")).isEmpty();
    assertThat(r.has("escalation_reason")).isFalse();
    assertThat(r.get("rules_version").asString()).isEqualTo("rules-2026.09.1");
    assertThat(r.get("params_version").asString()).isEqualTo("actuarial-2026.09.1");
    // Hashed over the body exactly as sent.
    assertThat(r.get("inputs_sha256").asString()).isEqualTo(Jcs.sha256Hex(body));
  }

  @Test
  void entryAgeAndMaturityPickTheProducts() throws Exception {
    // 999N002V01 takes entry ages 18–55; 999N001V02 18–65 with maturity 85 (65 + 10 ≤ 85).
    assertThat(strings(json(with("age_years", 58)), "eligible_uins")).containsExactly("999N001V02");
    assertThat(strings(json(with("age_years", 65)), "eligible_uins")).containsExactly("999N001V02");
  }

  @Test
  void anUnserviceableOrUnknownPincodeIsNotEligible() throws Exception {
    for (String pincode : List.of("744101", "999999")) {
      JsonNode r = json(with("pincode", pincode));
      assertThat(r.get("outcome").asString()).isEqualTo("NOT_ELIGIBLE");
      assertThat(strings(r, "reason_codes")).containsExactly("REASON_PIN_UNSERVICEABLE");
      assertThat(strings(r, "eligible_uins")).isEmpty();
    }
    // Not eligible beats a re-ask; an escalation beats not eligible.
    Map<String, Object> reAsk = with("pincode", "744101");
    reAsk.put("occupation_code", null);
    assertThat(json(reAsk).get("outcome").asString()).isEqualTo("NOT_ELIGIBLE");
    Map<String, Object> nri = with("pincode", "744101");
    nri.put("residency", "nri");
    JsonNode r = json(nri);
    assertThat(r.get("outcome").asString()).isEqualTo("HUMAN_ESCALATION");
    assertThat(strings(r, "reason_codes")).containsExactly("HE_NRI", "REASON_PIN_UNSERVICEABLE");
  }

  @Test
  void occupationClassFourIsManualUnderwriting() throws Exception {
    JsonNode r = json(with("occupation_code", "OCC-MINING-09"));
    assertThat(r.get("outcome").asString()).isEqualTo("ELIGIBLE");
    assertThat(strings(r, "rule_ids")).containsExactly("E-02");
    assertThat(strings(r, "flags")).containsExactly("MANUAL_UW");
    assertThat(r.get("uw_path").asString()).isEqualTo("manual");
  }

  @Test
  void aDeclinedOrUnknownOccupationIsReAsked() throws Exception {
    for (Object code : new Object[] {null, "OCC-NOT-IN-MASTER"}) {
      JsonNode r = json(with("occupation_code", code));
      assertThat(r.get("outcome").asString()).isEqualTo("RE_ASK");
      assertThat(strings(r, "rule_ids")).containsExactly("E-06");
      assertThat(strings(r, "eligible_uins")).isEmpty();
    }
  }

  @Test
  void declinedAnswersWithholdThePremiumAndAYesIsMedicalUnderwriting() throws Exception {
    JsonNode tobacco = json(with("tobacco_12m", null));
    assertThat(tobacco.get("outcome").asString()).isEqualTo("ELIGIBLE");
    assertThat(strings(tobacco, "flags")).containsExactly("PREMIUM_WITHHELD");
    assertThat(tobacco.get("uw_path").asString()).isEqualTo("standard");

    Map<String, Object> health = new LinkedHashMap<>();
    health.put("HS-01", true);
    health.put("HS-02", null);
    JsonNode r = json(with("health_flags", health));
    assertThat(r.get("outcome").asString()).isEqualTo("ELIGIBLE");
    assertThat(strings(r, "flags")).containsExactly("MEDICAL_UW", "PREMIUM_WITHHELD");
    assertThat(r.get("uw_path").asString()).isEqualTo("manual");
  }

  @Test
  void aMinorGetsDataErasureAndNothingElse() throws Exception {
    Map<String, Object> request = with("age_years", 16);
    request.put("residency", "nri");
    request.put("tobacco_12m", null);
    JsonNode r = json(request);
    assertThat(r.get("outcome").asString()).isEqualTo("DATA_ERASURE_EXIT");
    assertThat(strings(r, "rule_ids")).containsExactly("E-03");
    assertThat(strings(r, "eligible_uins")).isEmpty();
    assertThat(strings(r, "flags")).isEmpty();
    assertThat(strings(r, "reason_codes")).isEmpty();
  }

  @Test
  void aComplexProposerEscalates() throws Exception {
    List<Map<String, Object>> complex =
        List.of(
            proposer("sibling", 30, false), // relationship outside self/spouse/child/parent
            proposer("spouse", 30, true), // business cover
            proposer("spouse", null, false), // the life assured's age is unknown
            proposer("child", 10, false)); // a minor life assured
    for (Map<String, Object> p : complex) {
      JsonNode r = json(with("proposer", p));
      assertThat(r.get("outcome").asString()).isEqualTo("HUMAN_ESCALATION");
      assertThat(r.get("escalation_reason").asString()).isEqualTo("HE_COMPLEX_PROPOSER");
      assertThat(strings(r, "reason_codes")).containsExactly("HE_COMPLEX_PROPOSER");
    }
    // The minor life assured is found by the table's own E-03, on la_age.
    assertThat(strings(json(with("proposer", proposer("child", 10, false))), "rule_ids"))
        .containsExactly("E-01", "E-03");
    // An escalation the proposer already has keeps its reason first.
    Map<String, Object> nri = with("proposer", proposer("sibling", 30, false));
    nri.put("residency", "nri");
    assertThat(strings(json(nri), "reason_codes")).containsExactly("HE_NRI", "HE_COMPLEX_PROPOSER");
  }

  @Test
  void theLifeAssuredsAgeDrivesTheAgeRules() throws Exception {
    // A 34-year-old insuring a 70-year-old parent: E-04 on the life assured.
    JsonNode parent = json(with("proposer", proposer("Parent", 70, false)));
    assertThat(parent.get("outcome").asString()).isEqualTo("HUMAN_ESCALATION");
    assertThat(parent.get("escalation_reason").asString()).isEqualTo("HE_AGE_BAND");
    assertThat(strings(parent, "rule_ids")).containsExactly("E-01", "E-04");

    // A 58-year-old spouse: the entry-age filter uses 58, not the proposer's 34.
    JsonNode spouse = json(with("proposer", proposer("spouse", 58, false)));
    assertThat(spouse.get("outcome").asString()).isEqualTo("ELIGIBLE");
    assertThat(strings(spouse, "eligible_uins")).containsExactly("999N001V02");
  }

  @Test
  void requiredAttributesFollowThePinnedRules() throws Exception {
    JsonNode r =
        JSON.readTree(
            api(get("/v1/eligibility/required-attributes?pins.rules=rules-2026.09.1"))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString());
    List<String> attributes = new ArrayList<>();
    r.forEach(a -> attributes.add(a.get("attribute").asString()));
    // The DUMMY rate table rates no product by gender, so gender isn't asked.
    assertThat(attributes).doesNotContain("gender").hasSize(10).startsWith("age_years");
    assertThat(r.get(0).get("reason_line_id").asString()).isEqualTo("RL-S1-AGE");
    assertThat(r.get(9).get("asked_if").asString()).isEqualTo("proposer.is_life_assured = false");
  }

  @Test
  void eligibilityLogsNoRequestValue(CapturedOutput output) throws Exception {
    String pincode = "560037"; // not in the master
    String occupation = "OCC-SENTINEL-0C0DE";
    String question = "HS-SENTINEL-0C0DE";
    String relationship = "sentinel-sibling-0c0de";
    Map<String, Object> request = with("pincode", pincode);
    request.put("occupation_code", occupation);
    request.put("health_flags", Map.of(question, true));
    json(request);
    json(with("proposer", proposer(relationship, 30, false)));

    assertThat(output.getAll()).contains("eligibility");
    assertThat(output.getAll()).doesNotContain(pincode, occupation, question, relationship);
  }

  // --- helpers -----------------------------------------------------------------------------

  /** TDD §3.6: 34, Pune 411001, salaried class 1, no tobacco in the last 12 months. */
  static Map<String, Object> tddExample() {
    Map<String, Object> request = new LinkedHashMap<>();
    request.put("pins", Map.of("rules", "rules-2026.09.1"));
    request.put("age_years", 34);
    request.put("residency", "resident");
    request.put("pincode", "411001");
    request.put("tobacco_12m", false);
    request.put("occupation_code", "OCC-OFFICE-01");
    request.put("health_flags", Map.of("HS-01", false, "HS-02", false));
    request.put("proposer", Map.of("is_life_assured", true));
    return request;
  }

  private static Map<String, Object> with(String key, Object value) {
    Map<String, Object> request = tddExample();
    request.put(key, value);
    return request;
  }

  private static Map<String, Object> proposer(
      String relationship, Integer laAge, boolean business) {
    Map<String, Object> p = new LinkedHashMap<>();
    p.put("is_life_assured", false);
    p.put("relationship", relationship);
    if (laAge != null) {
      p.put("la_age", laAge);
    }
    p.put("business_cover", business);
    return p;
  }

  private JsonNode json(Map<String, Object> request) throws Exception {
    return json(JSON.writeValueAsString(request));
  }

  private JsonNode json(String body) throws Exception {
    return JSON.readTree(
        api(post("/v1/eligibility/evaluate").contentType(MediaType.APPLICATION_JSON).content(body))
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
