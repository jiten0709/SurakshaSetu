package com.surakshasetu.domain.quote;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import com.surakshasetu.domain.DomainApiTestSupport;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Every way a quote request can break the product's limits (seed catalog, cover step ₹5 lakh from
 * the pinned params): 422 QUOTE_OUT_OF_BOUNDS names the field and its bounds.
 */
class QuoteValidationTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final String TERM = "999N001V02"; // 18–65, ₹25 lakh–₹10 crore, 10–40, maturity 85
  private static final String ROP = "999N002V01"; // 18–55, ₹25 lakh–₹2 crore, 15–35, maturity 75

  @Test
  void undisclosedTobaccoGetsNoQuote() throws Exception {
    JsonNode p = problem(with("tobacco_12m", null), 422);
    assertThat(p.get("code").asString()).isEqualTo("TOBACCO_UNDISCLOSED");
    assertThat(p.has("field")).isFalse();
  }

  @Test
  void entryAge() throws Exception {
    // The oldest entry age also leaves the minimum term: min(65, 85 − 10) = 65; ROP min(55, 60).
    outOfBounds(with("age_years", 17), "age_years", "18", "65", null);
    outOfBounds(with("age_years", 66), "age_years", "18", "65", null);
    Map<String, Object> rop = with("uin", ROP);
    rop.put("age_years", 56);
    rop.put("term_years", 15);
    outOfBounds(rop, "age_years", "18", "55", null);
  }

  @Test
  void coverRangeAndStep() throws Exception {
    for (String sa : List.of("2000000", "100500000", "2700000", "10000000.50")) {
      outOfBounds(with("sum_assured_inr", sa), "sum_assured_inr", "2500000", "100000000", "500000");
    }
    Map<String, Object> rop = with("uin", ROP);
    rop.put("sum_assured_inr", "25000000");
    outOfBounds(rop, "sum_assured_inr", "2500000", "20000000", "500000");
  }

  @Test
  void termRangeAndMaturity() throws Exception {
    outOfBounds(with("term_years", 9), "term_years", "10", "40", null);
    outOfBounds(with("term_years", 41), "term_years", "10", "40", null);
    // At 60 the maturity age 85 leaves 25 years.
    Map<String, Object> older = with("age_years", 60);
    outOfBounds(older, "term_years", "10", "25", null);
  }

  @Test
  void premiumPayingTermAndFrequency() throws Exception {
    Map<String, Object> ropSingle = with("uin", ROP);
    ropSingle.put("ppt", "single");
    ropSingle.put("frequency", "single");
    outOfBounds(ropSingle, "ppt", null, null, null);
    outOfBounds(with("ppt", "single"), "frequency", null, null, null); // single paid annually
    outOfBounds(with("frequency", "single"), "frequency", null, null, null); // regular, single
  }

  @Test
  void ridersMustAttachOnceEach() throws Exception {
    Map<String, Object> ropCi = with("uin", ROP);
    ropCi.put("rider_uins", List.of("999A009V01")); // critical illness attaches to 001 only
    outOfBounds(ropCi, "rider_uins", null, null, null);
    outOfBounds(
        with("rider_uins", List.of("999A007V01", "999A007V01")), "rider_uins", null, null, null);
    outOfBounds(with("rider_uins", List.of("999A099V01")), "rider_uins", null, null, null);
  }

  @Test
  void riderCoverLimit() throws Exception {
    // No seed rider has a limit, so give accidental death one for the test, then restore it.
    ADMIN.sql("UPDATE catalog.rider SET sa_max_inr = 5000000 WHERE uin = '999A007V01'").update();
    try {
      outOfBounds(with("rider_uins", List.of("999A007V01")), "rider_uins", null, "5000000", null);
    } finally {
      ADMIN.sql("UPDATE catalog.rider SET sa_max_inr = NULL WHERE uin = '999A007V01'").update();
    }
  }

  @Test
  void aProductNotOnSaleOrUnknown() throws Exception {
    // 999N010V01 is seeded but not launched (A5).
    Map<String, Object> savings = with("uin", "999N010V01");
    savings.put("ppt", "limited_10");
    savings.put("sum_assured_inr", "1000000");
    savings.put("term_years", 15);
    assertThat(problem(savings, 409).get("code").asString()).isEqualTo("PRODUCT_WITHDRAWN");
    assertThat(problem(with("uin", "999N099V99"), 404).get("code").asString())
        .isEqualTo("NOT_FOUND");
  }

  // --- helpers -----------------------------------------------------------------------------

  /** Quote-Only for the term plan at its quote_defaults: 34, no tobacco, ₹1 crore, 30 years. */
  static Map<String, Object> quote() {
    Map<String, Object> request = new LinkedHashMap<>();
    request.put("pins", Map.of("rules", "rules-2026.09.1"));
    request.put("uin", TERM);
    request.put("sum_assured_inr", "10000000");
    request.put("term_years", 30);
    request.put("ppt", "regular");
    request.put("rider_uins", new ArrayList<>());
    request.put("age_years", 34);
    request.put("tobacco_12m", false);
    request.put("frequency", "annual");
    return request;
  }

  private static Map<String, Object> with(String key, @Nullable Object value) {
    Map<String, Object> request = quote();
    request.put(key, value);
    return request;
  }

  private void outOfBounds(
      Map<String, Object> request,
      String field,
      @Nullable String min,
      @Nullable String max,
      @Nullable String step)
      throws Exception {
    JsonNode p = problem(request, 422);
    assertThat(p.get("code").asString()).isEqualTo("QUOTE_OUT_OF_BOUNDS");
    assertThat(p.get("field").asString()).isEqualTo(field);
    assertThat(text(p, "allowed_min")).as("allowed_min").isEqualTo(min);
    assertThat(text(p, "allowed_max")).as("allowed_max").isEqualTo(max);
    assertThat(text(p, "allowed_step")).as("allowed_step").isEqualTo(step);
  }

  private JsonNode problem(Map<String, Object> request, int status) throws Exception {
    var response =
        api(post("/v1/quotes")
                .contentType(MediaType.APPLICATION_JSON)
                .content(JSON.writeValueAsString(request)))
            .andReturn()
            .getResponse();
    assertThat(response.getStatus()).isEqualTo(status);
    return JSON.readTree(response.getContentAsString());
  }

  private static @Nullable String text(JsonNode node, String field) {
    return node.has(field) ? node.get(field).asString() : null;
  }
}
