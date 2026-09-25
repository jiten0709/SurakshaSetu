package com.surakshasetu.domain.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import java.nio.file.Files;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

class CatalogTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  // Kill-switch fixture; excluded from listings so test order doesn't matter.
  private static final String FIXTURE = "999N099V01";

  @Test
  void filtersProducts() throws Exception {
    assertThat(uins("/v1/catalog/products"))
        .containsExactly("999N001V02", "999N002V01", "999N010V01");
    assertThat(uins("/v1/catalog/products?launch_enabled=true"))
        .containsExactly("999N001V02", "999N002V01");
    assertThat(uins("/v1/catalog/products?launch_enabled=false")).containsExactly("999N010V01");
    assertThat(uins("/v1/catalog/products?category=TERM")).containsExactly("999N001V02");
    assertThat(uins("/v1/catalog/products?status=in_force&category=NON_PAR_SAVINGS"))
        .containsExactly("999N010V01");
    assertThat(uins("/v1/catalog/products?as_of=2026-08-15T00:00:00Z")).isEmpty();
  }

  @Test
  void servesAProductWithItsRidersAndDocuments() throws Exception {
    JsonNode p = json(api(get("/v1/catalog/products/999N001V02")).andExpect(status().isOk()));

    assertThat(p.get("category").asString()).isEqualTo("TERM");
    assertThat(p.get("status").asString()).isEqualTo("in_force");
    assertThat(p.get("entry_age_min").asInt()).isEqualTo(18);
    assertThat(p.get("entry_age_max").asInt()).isEqualTo(65);
    assertThat(p.get("sa_min_inr").asString()).isEqualTo("2500000.00");
    assertThat(p.get("sa_max_inr").asString()).isEqualTo("100000000.00");
    assertThat(p.get("term_years_min").asInt()).isEqualTo(10);
    assertThat(p.get("term_years_max").asInt()).isEqualTo(40);
    assertThat(strings(p.get("ppt_options"))).containsExactly("regular", "limited_10", "single");
    assertThat(strings(p.get("benefit_payment_options")))
        .containsExactly("lumpsum", "monthly_income");
    assertThat(strings(p.get("rider_uins")))
        .containsExactly("999A007V01", "999A008V01", "999A009V01");
    assertThat(p.get("is_dummy").asBoolean()).isTrue();
    assertThat(p.get("effective_to").isNull()).isTrue();

    List<String> riders = new ArrayList<>();
    p.get("riders").forEach(r -> riders.add(r.get("uin").asString()));
    assertThat(riders).containsExactly("999A007V01", "999A008V01", "999A009V01");

    assertThat(p.get("documents")).hasSize(2);
    for (JsonNode doc : p.get("documents")) {
      String uri = doc.get("uri").asString();
      assertThat(uri).startsWith("content/seed/kb/product/999N001V02-");
      byte[] file = Files.readAllBytes(SEED.resolve(uri.substring("content/seed/".length())));
      assertThat(doc.get("sha256").asString()).isEqualTo(Jcs.sha256Hex(file));
    }
  }

  @Test
  void theSavingsProductIsSeededButNotLaunched() throws Exception {
    JsonNode p = json(api(get("/v1/catalog/products/999N010V01")).andExpect(status().isOk()));
    assertThat(p.get("launch_enabled").asBoolean()).isFalse();
    assertThat(p.get("category").asString()).isEqualTo("NON_PAR_SAVINGS");
  }

  @Test
  void servesRiders() throws Exception {
    JsonNode ci = json(api(get("/v1/catalog/riders/999A009V01")).andExpect(status().isOk()));
    assertThat(strings(ci.get("attaches_to"))).containsExactly("999N001V02");
    assertThat(ci.get("sa_max_inr").isNull()).isTrue();

    api(get("/v1/catalog/riders/999A099V01"))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  @Test
  void unknownOrMalformedUins() throws Exception {
    api(get("/v1/catalog/products/999N099V99"))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
    apiRejecting(get("/v1/catalog/products/not-a-uin"))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
  }

  @Test
  void theKillSwitchWithdrawsAProductFromToday() throws Exception {
    ADMIN
        .sql(
            """
            INSERT INTO catalog.product (uin, name, category, status, entry_age_min, entry_age_max,
              maturity_age_max, sa_min_inr, term_years, ppt_options, payout_options,
              effective_from, launch_enabled, is_dummy)
            VALUES (?, 'Kill-switch fixture', 'TERM', 'in_force', 18, 65, 85, 2500000,
              int4range(10, 40, '[]'), '{regular}', '{lumpsum}', '2026-01-01', true, true)
            """)
        .param(FIXTURE)
        .update();
    LocalDate today = BusinessDates.on(null);
    String yesterday = OffsetDateTime.now(ZoneOffset.UTC).minusDays(1).toString();

    for (int attempt = 0; attempt < 2; attempt++) { // the second call is a no-op
      api(killSwitch(FIXTURE))
          .andExpect(status().isOk())
          .andExpect(jsonPath("$.uin").value(FIXTURE))
          .andExpect(jsonPath("$.status").value("withdrawn"));
      assertThat(
              ADMIN
                  .sql("SELECT effective_to FROM catalog.product WHERE uin = ?")
                  .param(FIXTURE)
                  .query(LocalDate.class)
                  .single())
          .isEqualTo(today);
    }

    api(get("/v1/catalog/products/" + FIXTURE)).andExpect(status().isNotFound());
    JsonNode before =
        json(
            api(get("/v1/catalog/products/" + FIXTURE).param("as_of", yesterday))
                .andExpect(status().isOk()));
    assertThat(before.get("status").asString()).isEqualTo("withdrawn");
    assertThat(
            json(api(
                    get("/v1/catalog/products")
                        .param("status", "withdrawn")
                        .param("as_of", yesterday)))
                .findValuesAsString("uin"))
        .contains(FIXTURE);

    api(killSwitch("999N099V98")).andExpect(status().isNotFound());
  }

  private static org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder
      killSwitch(String uin) {
    return post("/v1/catalog/products/" + uin + "/kill-switch")
        .contentType(MediaType.APPLICATION_JSON)
        .content("{\"reason\":\"test\",\"actor\":\"ops-test\"}");
  }

  private List<String> uins(String path) throws Exception {
    List<String> uins = new ArrayList<>();
    json(api(get(path)).andExpect(status().isOk())).forEach(p -> uins.add(p.get("uin").asString()));
    uins.remove(FIXTURE);
    return uins;
  }

  private static List<String> strings(JsonNode array) {
    List<String> values = new ArrayList<>();
    array.forEach(v -> values.add(v.asString()));
    return values;
  }

  private static JsonNode json(ResultActions result) throws Exception {
    return JSON.readTree(result.andReturn().getResponse().getContentAsString());
  }
}
