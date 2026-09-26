package com.surakshasetu.domain.quote;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.BusinessDates;
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
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** POST /v1/quotes and /v1/quotes/alternatives over the DUMMY rate table (GST 0.00). */
@ExtendWith(OutputCaptureExtension.class)
class QuotesApiTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();

  @Test
  void anIndicativeQuoteForTheTermPlan() throws Exception {
    Map<String, Object> request = QuoteValidationTest.quote();
    request.put("rider_uins", List.of("999A007V01"));
    String body = JSON.writeValueAsString(request);
    JsonNode q = ok("/v1/quotes", body);

    // 34 × 30 years: 1.60 per ₹1,000. Base 10,000 × 1.60 = 16,000; rider 10,000 × 0.30 = 3,000.
    assertThat(q.get("annual_premium_inr").asString()).isEqualTo("19000");
    assertThat(q.get("rider_premiums").get("999A007V01").asString()).isEqualTo("3000");
    assertThat(q.get("uin").asString()).isEqualTo("999N001V02");
    assertThat(q.get("sum_assured_inr").asString()).isEqualTo("10000000");
    assertThat(q.get("term_years").asInt()).isEqualTo(30);
    assertThat(q.get("ppt").asString()).isEqualTo("regular");
    assertThat(q.get("frequency").asString()).isEqualTo("annual");
    String today = BusinessDates.on(null).toString();
    assertThat(q.get("quote_id").asString()).matches("Q-" + today + "-\\d{4}");
    assertThat(q.get("valid_until").asString())
        .isEqualTo(BusinessDates.on(null).plusDays(30).toString());
    assertThat(q.get("indicative").asBoolean()).isTrue();
    assertThat(q.get("gst_included").asBoolean()).isTrue();
    assertThat(q.get("rating_version").asString()).isEqualTo("rating-dummy-2026.09.1");
    assertThat(q.get("reason_codes").isEmpty()).isTrue();
    assertThat(q.get("inputs_sha256").asString()).isEqualTo(Jcs.sha256Hex(body));
  }

  @Test
  void identicalRequestsGetIdenticalPremiumsAndNewIds() throws Exception {
    String body = JSON.writeValueAsString(QuoteValidationTest.quote());
    ObjectNode first = (ObjectNode) ok("/v1/quotes", body);
    ObjectNode second = (ObjectNode) ok("/v1/quotes", body);
    assertThat(first.get("quote_id")).isNotEqualTo(second.get("quote_id"));
    assertThat(first.get("decision_id")).isNotEqualTo(second.get("decision_id"));
    for (ObjectNode q : List.of(first, second)) {
      q.remove("quote_id");
      q.remove("decision_id");
    }
    assertThat(first).isEqualTo(second);
  }

  @Test
  void cheaperAlternativesCarryTheirProtectionGap() throws Exception {
    Map<String, Object> request = QuoteValidationTest.quote();
    request.put("rider_uins", List.of("999A007V01", "999A008V01"));
    request.put("recommended_cover_inr", "12000000");
    JsonNode alternatives = ok("/v1/quotes/alternatives", JSON.writeValueAsString(request));

    // Per ₹1,000: base 1.60, accidental death 0.30, waiver 0.15; limited_10 ×1.70, single ×9.
    List<String> rows = new ArrayList<>();
    for (JsonNode a : alternatives) {
      JsonNode q = a.get("quote");
      rows.add(
          String.join(
              " ",
              a.get("change").asString(),
              q.get("sum_assured_inr").asString(),
              q.get("ppt").asString(),
              q.get("frequency").asString(),
              String.join("+", q.get("rider_premiums").propertyNames()),
              q.get("annual_premium_inr").asString(),
              a.get("protection_gap_inr").asString()));
    }
    assertThat(rows)
        .containsExactly(
            // 7,500 × 2.05 = 15,375; gap 1.20 crore − 75 lakh.
            "LOWER_COVER 7500000 regular annual 999A007V01+999A008V01 15375 4500000",
            // 5,000 × 2.05 = 10,250.
            "LOWER_COVER 5000000 regular annual 999A007V01+999A008V01 10250 7000000",
            // 10,000 × 1.75 = 17,500, then 10,000 × 1.90, then the base alone.
            "FEWER_RIDERS 10000000 regular annual 999A008V01 17500 2000000",
            "FEWER_RIDERS 10000000 regular annual 999A007V01 19000 2000000",
            "FEWER_RIDERS 10000000 regular annual  16000 2000000",
            // 10,000 × 2.05 × 1.70 = 34,850; 10,000 × 2.05 × 9 = 1,84,500.
            "OTHER_PPT 10000000 limited_10 annual 999A007V01+999A008V01 34850 2000000",
            "OTHER_PPT 10000000 single single 999A007V01+999A008V01 184500 2000000");

    // Each quote hashes the request the service built: here, the first lower cover.
    Map<String, Object> built = QuoteValidationTest.quote();
    built.put("rider_uins", List.of("999A007V01", "999A008V01"));
    built.put("sum_assured_inr", "7500000");
    assertThat(alternatives.get(0).get("quote").get("inputs_sha256").asString())
        .isEqualTo(Jcs.sha256Hex(JSON.writeValueAsString(built)));
  }

  @Test
  void theGapNeverGoesNegativeAndCoverStopsAtTheMinimum() throws Exception {
    Map<String, Object> request = QuoteValidationTest.quote();
    request.put("sum_assured_inr", "4000000");
    request.put("recommended_cover_inr", "2500000");
    JsonNode alternatives = ok("/v1/quotes/alternatives", JSON.writeValueAsString(request));
    // −25% is ₹30 lakh; −50% would be ₹20 lakh, below the ₹25 lakh minimum, so ₹25 lakh.
    List<String> lower = new ArrayList<>();
    for (JsonNode a : alternatives) {
      assertThat(a.get("protection_gap_inr").asString()).isEqualTo("0");
      if (a.get("change").asString().equals("LOWER_COVER")) {
        lower.add(a.get("quote").get("sum_assured_inr").asString());
      }
    }
    assertThat(lower).containsExactly("3000000", "2500000");
  }

  @Test
  void alternativesCheckTheRequestFirst() throws Exception {
    Map<String, Object> request = QuoteValidationTest.quote();
    request.put("tobacco_12m", null);
    request.put("recommended_cover_inr", "10000000");
    var response =
        api(MockMvcRequestBuilders.post("/v1/quotes/alternatives")
                .contentType(MediaType.APPLICATION_JSON)
                .content(JSON.writeValueAsString(request)))
            .andExpect(status().isUnprocessableContent())
            .andReturn()
            .getResponse();
    assertThat(JSON.readTree(response.getContentAsString()).get("code").asString())
        .isEqualTo("TOBACCO_UNDISCLOSED");
  }

  @Test
  void quotesLogNoRequestValue(CapturedOutput output) throws Exception {
    String cover = "73500000"; // 47 × 30 years: 3.10 per ₹1,000 → 2,27,850.
    Map<String, Object> request = QuoteValidationTest.quote();
    request.put("sum_assured_inr", cover);
    request.put("age_years", 47);
    JsonNode q = ok("/v1/quotes", JSON.writeValueAsString(request));
    assertThat(q.get("annual_premium_inr").asString()).isEqualTo("227850");
    Map<String, Object> alternatives = new LinkedHashMap<>(request);
    alternatives.put("recommended_cover_inr", "81500000");
    ok("/v1/quotes/alternatives", JSON.writeValueAsString(alternatives));

    assertThat(output.getAll()).contains("quote");
    assertThat(output.getAll()).doesNotContain(cover, "227850", "81500000");
  }

  private JsonNode ok(String path, String body) throws Exception {
    return JSON.readTree(
        api(MockMvcRequestBuilders.post(path).contentType(MediaType.APPLICATION_JSON).content(body))
            .andExpect(status().isOk())
            .andReturn()
            .getResponse()
            .getContentAsString());
  }
}
