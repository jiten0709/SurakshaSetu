package com.surakshasetu.domain.ranking;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.http.MediaType;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * POST /v1/ranking/rank on the seeded catalog, fed by a real suitability decision for the TDD
 * example (34, ₹24 lakh income, spouse and two children, home loan: ₹3.75 crore for 26 years).
 */
@ExtendWith(OutputCaptureExtension.class)
class RankingApiTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final Path VECTORS = Path.of("..", "content", "testvectors", "needs");
  private static final String FIXTURE = "999N098V01";

  @Test
  void theTddExampleRanksBothTermPlans() throws Exception {
    ObjectNode request = request();
    String body = JSON.writeValueAsString(request);
    JsonNode r = rank(body);

    assertThat(r.get("ranker_version").asString()).isEqualTo("ranker-2026.09.1");
    assertThat(r.get("reason_codes").isEmpty()).isTrue();
    assertThat(r.get("inputs_sha256").asString()).isEqualTo(Jcs.sha256Hex(body));
    assertThat(r.get("suitability_inputs_sha256"))
        .isEqualTo(request.get("suitability").get("inputs_sha256"));
    assertThat(uins(r)).containsExactly("999N001V02", "999N002V01");

    // 999N001V02: ₹3.75 crore, 26 years, all three riders (dependants, age 34, green).
    // 37,500 × (1.60 + 0.30 + 0.15 + 0.80) = 60,000 + 11,250 + 5,625 + 30,000 = 1,06,875.
    JsonNode first = r.get("options").get(0);
    assertThat(first.get("rank").asInt()).isEqualTo(1);
    assertThat(first.get("sum_assured_inr").asString()).isEqualTo("37500000");
    assertThat(first.get("term_years").asInt()).isEqualTo(26);
    assertThat(first.get("ppt_years").asInt()).isEqualTo(26);
    assertThat(strings(first, "rider_uins"))
        .containsExactly("999A007V01", "999A008V01", "999A009V01");
    assertThat(first.get("protection_gap_inr").asString()).isEqualTo("0");
    JsonNode quote = first.get("quote");
    assertThat(quote.get("annual_premium_inr").asString()).isEqualTo("106875");
    assertThat(quote.get("rider_premiums").get("999A009V01").asString()).isEqualTo("30000");
    assertThat(quote.get("ppt").asString()).isEqualTo("regular");
    // The quote hashes the request the ranker built.
    ObjectNode built = JSON.createObjectNode();
    built.set("pins", request.get("pins"));
    built.put("uin", "999N001V02").put("sum_assured_inr", "37500000").put("term_years", 26);
    built.put("ppt", "regular").put("age_years", 34).put("tobacco_12m", false);
    built.put("frequency", "annual");
    built.putArray("rider_uins").add("999A007V01").add("999A008V01").add("999A009V01");
    assertThat(quote.get("inputs_sha256").asString())
        .isEqualTo(Jcs.sha256Hex(JSON.writeValueAsString(built)));

    // 999N002V01: capped at ₹2 crore; 20,000 × (4.20 + 0.30) = 84,000 + 6,000 = 90,000.
    JsonNode second = r.get("options").get(1);
    assertThat(second.get("sum_assured_inr").asString()).isEqualTo("20000000");
    assertThat(second.get("protection_gap_inr").asString()).isEqualTo("17500000");
    assertThat(second.get("quote").get("annual_premium_inr").asString()).isEqualTo("90000");
    assertThat(strings(second, "reason_codes"))
        .containsExactly("RANK-FIT-TERM_ROP", "RANK-SA-CAPPED", "RANK-RIDER-ADB");
  }

  @Test
  void savingsIsNeverRankedInPhaseOne() throws Exception {
    ObjectNode request = request();
    ((ArrayNode) request.get("eligible_uins")).add("999N010V01");
    ((ArrayNode) request.get("suitability").get("fit_types")).add("NON_PAR_SAVINGS");
    assertThat(uins(rank(JSON.writeValueAsString(request))))
        .containsExactly("999N001V02", "999N002V01");
  }

  @Test
  void excludedAndWithheld() throws Exception {
    ObjectNode request = request();
    request.putArray("excluded_uins").add("999N001V02");
    request.putArray("flags").add("PREMIUM_WITHHELD");
    JsonNode r = rank(JSON.writeValueAsString(request));
    assertThat(uins(r)).containsExactly("999N002V01");
    JsonNode option = r.get("options").get(0);
    assertThat(option.get("quote").isNull()).isTrue();
    assertThat(strings(option, "reason_codes")).contains("PREMIUM_WITHHELD");
  }

  @Test
  void nothingEligibleIsNoEligibleOption() throws Exception {
    ObjectNode request = request();
    request.putArray("eligible_uins");
    JsonNode r = rank(JSON.writeValueAsString(request));
    assertThat(r.get("options").isEmpty()).isTrue();
    assertThat(strings(r, "reason_codes")).containsExactly("NO_ELIGIBLE_OPTION");
  }

  @Test
  void aKillSwitchMidSessionDropsTheProductFromTheNextRank() throws Exception {
    // A launched term plan the rate table doesn't know: ranked, but without a quote.
    ADMIN
        .sql(
            """
            INSERT INTO catalog.product (uin, name, category, status, entry_age_min, entry_age_max,
              maturity_age_max, sa_min_inr, term_years, ppt_options, payout_options,
              effective_from, launch_enabled, is_dummy)
            VALUES (?, 'Ranking fixture', 'TERM', 'in_force', 18, 65, 85, 2500000,
              int4range(10, 40, '[]'), '{regular}', '{lumpsum}', ?, true, true)
            """)
        .param(FIXTURE)
        .param(BusinessDates.on(null)) // from today only, so no as_of listing ever shows it
        .update();
    try {
      ObjectNode request = request();
      ((ArrayNode) request.get("eligible_uins")).add(FIXTURE);
      String body = JSON.writeValueAsString(request);

      JsonNode before = rank(body);
      assertThat(uins(before)).containsExactly("999N001V02", "999N002V01", FIXTURE);
      JsonNode fixture = before.get("options").get(2);
      assertThat(fixture.get("quote").isNull()).isTrue();
      assertThat(strings(fixture, "reason_codes")).contains("RATING_UNAVAILABLE");

      api(post("/v1/catalog/products/" + FIXTURE + "/kill-switch")
              .contentType(MediaType.APPLICATION_JSON)
              .content("{\"reason\":\"test\",\"actor\":\"ops-test\"}"))
          .andExpect(status().isOk());
      assertThat(uins(rank(body))).containsExactly("999N001V02", "999N002V01");
    } finally {
      ADMIN
          .sql("UPDATE catalog.product SET status = 'withdrawn', effective_to = ? WHERE uin = ?")
          .param(BusinessDates.on(null))
          .param(FIXTURE)
          .update();
    }
  }

  @Test
  void identicalInputsRankIdentically() throws Exception {
    String body = JSON.writeValueAsString(request());
    JsonNode first = withoutIds(rank(body));
    assertThat(withoutIds(rank(body))).isEqualTo(first);
  }

  @Test
  void rankingLogsNoRequestValue(CapturedOutput output) throws Exception {
    String cover = "73500000";
    ObjectNode request = request();
    ((ObjectNode) request.get("suitability")).put("recommended_cover_inr", cover);
    JsonNode r = rank(JSON.writeValueAsString(request));
    String premium = r.get("options").get(0).get("quote").get("annual_premium_inr").asString();

    assertThat(output.getAll()).contains("ranking");
    assertThat(output.getAll()).doesNotContain(cover, premium);
  }

  // --- helpers -----------------------------------------------------------------------------

  /** A ranking request around the live suitability decision for the TDD needs vector. */
  private ObjectNode request() throws Exception {
    ObjectNode suitability = JSON.createObjectNode();
    suitability.putObject("pins").put("rules", "rules-2026.09.1");
    ObjectNode eligibility = suitability.putObject("eligibility");
    eligibility.put("age_years", 34).put("tobacco_12m", false);
    eligibility.putArray("eligible_uins").add("999N001V02").add("999N002V01");
    eligibility.putArray("flags");
    suitability.set(
        "needs",
        JSON.readTree(Files.readString(VECTORS.resolve("01-tdd-example.json"))).get("needs"));
    JsonNode result =
        JSON.readTree(
            api(post("/v1/suitability/evaluate")
                    .contentType(MediaType.APPLICATION_JSON)
                    .content(JSON.writeValueAsString(suitability)))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString());

    ObjectNode request = JSON.createObjectNode();
    request.putObject("pins").put("rules", "rules-2026.09.1");
    request.putArray("eligible_uins").add("999N001V02").add("999N002V01");
    request.set("suitability", result);
    request.putArray("excluded_uins");
    request.put("channel", "web").put("language", "en-IN");
    request.put("as_of", OffsetDateTime.now(ZoneOffset.UTC).toString());
    request.put("tobacco_12m", false).put("age_years", 34);
    request.putArray("flags");
    return request;
  }

  private JsonNode rank(String body) throws Exception {
    return JSON.readTree(
        api(post("/v1/ranking/rank").contentType(MediaType.APPLICATION_JSON).content(body))
            .andExpect(status().isOk())
            .andReturn()
            .getResponse()
            .getContentAsString());
  }

  private static JsonNode withoutIds(JsonNode result) {
    ObjectNode copy = (ObjectNode) result.deepCopy();
    copy.remove("decision_id");
    for (JsonNode option : copy.get("options")) {
      ObjectNode quote = (ObjectNode) option.get("quote");
      quote.remove("decision_id");
      quote.remove("quote_id");
    }
    return copy;
  }

  private static List<String> uins(JsonNode result) {
    List<String> uins = new ArrayList<>();
    result.get("options").forEach(o -> uins.add(o.get("uin").asString()));
    return uins;
  }

  private static List<String> strings(JsonNode node, String field) {
    List<String> values = new ArrayList<>();
    node.get(field).forEach(v -> values.add(v.asString()));
    return values;
  }
}
