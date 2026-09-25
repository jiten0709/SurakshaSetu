package com.surakshasetu.domain.disclosure;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.Jcs;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

class DisclosureRegistryTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final HexFormat HEX = HexFormat.of();

  /**
   * set_sha256 of 999N001V02 / web / en-IN / 2026.09.1 from content/seed. Computed once and
   * reviewed: the orchestrator's surakshasetu.crypto.jcs gives the same value (step-5 audit).
   * Changing a seed body or the order must change it, and needs a new registry_version.
   */
  static final String SEED_SET_SHA256 =
      "fdcc70b9a658fda8ce69daf5fec4c0420de4a784911541b292a0e53e5450eee8";

  private static final List<String> SEED_ORDER =
      List.of(
          "DISC-GLOBAL-SOLICIT-01",
          "DISC-GLOBAL-QUOTE-02",
          "DISC-UIN-999N001V02-21",
          "DISC-GLOBAL-S45-03",
          "DISC-GLOBAL-FREELOOK-04",
          "DISC-GLOBAL-TAX-05");

  @Test
  void theSeedSetIsServedInOrderWithVerifiedHashes() throws Exception {
    JsonNode set = json(api(setRequest("999N001V02", "web", "en-IN")).andExpect(status().isOk()));

    assertThat(ids(set)).isEqualTo(SEED_ORDER);
    assertThat(set.get("registry_version").asString()).isEqualTo("2026.09.1");
    assertThat(set.get("is_dummy").asBoolean()).isTrue();
    List<Jcs.SetItem> items = new ArrayList<>();
    for (JsonNode item : set.get("items")) {
      String body = item.get("body").asString();
      assertThat(body).startsWith("DUMMY:");
      assertThat(item.get("body_sha256").asString()).isEqualTo(Jcs.bodySha256(body));
      items.add(new Jcs.SetItem(item.get("disclosure_id").asString(), Jcs.bodySha256(body)));
    }
    assertThat(set.get("set_sha256").asString())
        .isEqualTo(Jcs.setSha256("2026.09.1", "999N001V02", "web", "en-IN", items))
        .isEqualTo(SEED_SET_SHA256);
  }

  @Test
  void everyChannelAndLanguageHasItsOwnSet() throws Exception {
    JsonNode web = json(api(setRequest("999N001V02", "web", "en-IN")));
    JsonNode app = json(api(setRequest("999N001V02", "app", "en-IN")));
    JsonNode hindi = json(api(setRequest("999N001V02", "web", "hi-IN")).andExpect(status().isOk()));

    assertThat(ids(app)).isEqualTo(SEED_ORDER);
    assertThat(ids(hindi)).isEqualTo(SEED_ORDER);
    assertThat(hindi.get("items").get(0).get("body").asString())
        .startsWith("DUMMY: [hi translation pending approval]");
    assertThat(List.of(web, app, hindi))
        .extracting(s -> s.get("set_sha256").asString())
        .doesNotHaveDuplicates();

    JsonNode rop = json(api(setRequest("999N002V01", "app", "en-IN")).andExpect(status().isOk()));
    assertThat(ids(rop).get(2)).isEqualTo("DISC-UIN-999N002V01-22");
  }

  @Test
  void asOfSelectsTheNewestRegistryVersionWhoseMembersAreInForce() throws Exception {
    disclosure(
        "XA-TEST", "xa-TEST", "DUMMY: old", LocalDate.of(2026, 1, 1), LocalDate.of(2026, 10, 1));
    disclosure("XB-TEST", "xa-TEST", "DUMMY: new", LocalDate.of(2026, 10, 1), null);
    set("web", "xa-TEST", "2026.09.1", List.of("XA-TEST"), null);
    set("web", "xa-TEST", "2026.10.1", List.of("XB-TEST"), null);

    assertThat(
            json(api(
                    setRequest("999N001V02", "web", "xa-TEST")
                        .param("as_of", "2026-09-15T00:00:00Z")))
                .get("registry_version")
                .asString())
        .isEqualTo("2026.09.1");
    assertThat(
            json(api(
                    setRequest("999N001V02", "web", "xa-TEST")
                        .param("as_of", "2026-10-15T00:00:00Z")))
                .get("registry_version")
                .asString())
        .isEqualTo("2026.10.1");
    api(setRequest("999N001V02", "web", "xa-TEST").param("as_of", "2025-12-01T00:00:00Z"))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  @Test
  void aSetWhoseStoredHashDoesNotMatchIsNeverServed() throws Exception {
    set("tamper-test", "en-IN", "2026.09.1", SEED_ORDER, new byte[32]);

    api(setRequest("999N001V02", "tamper-test", "en-IN"))
        .andExpect(status().isInternalServerError())
        .andExpect(jsonPath("$.code").value("REGISTRY_INTEGRITY"))
        .andExpect(jsonPath("$.items").doesNotExist());
  }

  @Test
  void aBodyWhoseStoredHashDoesNotMatchIsNeverServed() throws Exception {
    // The stored hash is of other text, as if the body were edited in place.
    ADMIN
        .sql(
            """
            INSERT INTO catalog.disclosure (disclosure_id, scope, language, body, body_sha256,
              approved_by, effective_from, is_dummy)
            VALUES ('XT-TEST', 'GLOBAL', 'xt-TEST', 'DUMMY: edited in place', ?, 'test',
              '2026-01-01', true)
            """)
        .param(HEX.parseHex(Jcs.bodySha256("DUMMY: as approved")))
        .update();
    set("web", "xt-TEST", "2026.09.1", List.of("XT-TEST"), null);

    api(setRequest("999N001V02", "web", "xt-TEST"))
        .andExpect(status().isInternalServerError())
        .andExpect(jsonPath("$.code").value("REGISTRY_INTEGRITY"));
    api(get("/v1/disclosures/XT-TEST").param("language", "xt-TEST"))
        .andExpect(status().isInternalServerError())
        .andExpect(jsonPath("$.code").value("REGISTRY_INTEGRITY"));
  }

  @Test
  void unknownProductChannelOrLanguageIs404() throws Exception {
    api(setRequest("999N099V99", "web", "en-IN")).andExpect(status().isNotFound());
    api(setRequest("999N001V02", "whatsapp", "en-IN")).andExpect(status().isNotFound());
    api(setRequest("999N010V01", "web", "en-IN")).andExpect(status().isNotFound());
    api(setRequest("999N001V02", "web", "ta-IN"))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  @Test
  void servesASingleDisclosurePerLanguage() throws Exception {
    JsonNode ai =
        json(
            api(get("/v1/disclosures/DISC-GLOBAL-AI-06").param("language", "en-IN"))
                .andExpect(status().isOk()));
    assertThat(ai.get("body").asString()).startsWith("DUMMY:");
    assertThat(ai.get("body_sha256").asString())
        .isEqualTo(Jcs.bodySha256(ai.get("body").asString()));

    JsonNode hindi = json(api(get("/v1/disclosures/DISC-GLOBAL-AI-06").param("language", "hi-IN")));
    assertThat(hindi.get("language").asString()).isEqualTo("hi-IN");
    assertThat(hindi.get("body_sha256")).isNotEqualTo(ai.get("body_sha256"));

    api(get("/v1/disclosures/DISC-NOPE-99").param("language", "en-IN"))
        .andExpect(status().isNotFound());
    api(get("/v1/disclosures/DISC-GLOBAL-AI-06")
            .param("language", "en-IN")
            .param("as_of", "2026-08-01T00:00:00Z"))
        .andExpect(status().isNotFound());
  }

  private static org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder
      setRequest(String uin, String channel, String language) {
    return get("/v1/disclosures/sets/" + uin).param("channel", channel).param("language", language);
  }

  private static void disclosure(
      String id, String language, String body, LocalDate from, LocalDate to) {
    ADMIN
        .sql(
            """
            INSERT INTO catalog.disclosure (disclosure_id, scope, language, body, body_sha256,
              approved_by, effective_from, effective_to, is_dummy)
            VALUES (?, 'GLOBAL', ?, ?, ?, 'test', ?, ?, true)
            """)
        .params(id, language, body, HEX.parseHex(Jcs.bodySha256(body)), from, to)
        .update();
  }

  /** A set on 999N001V02; its hash is computed from the stored bodies unless given. */
  private static void set(
      String channel, String language, String version, List<String> ids, byte[] setSha256) {
    List<Jcs.SetItem> items = new ArrayList<>();
    for (String id : ids) {
      byte[] bodySha =
          ADMIN
              .sql(
                  "SELECT body_sha256 FROM catalog.disclosure"
                      + " WHERE disclosure_id = ? AND language = ?")
              .params(id, language)
              .query(byte[].class)
              .single();
      items.add(new Jcs.SetItem(id, HEX.formatHex(bodySha)));
    }
    byte[] sha =
        setSha256 != null
            ? setSha256
            : HEX.parseHex(Jcs.setSha256(version, "999N001V02", channel, language, items));
    ADMIN
        .sql(
            """
            INSERT INTO catalog.disclosure_set (uin, channel, language, registry_version,
              disclosure_ids, set_sha256)
            VALUES ('999N001V02', ?, ?, ?, ?, ?)
            """)
        .params(channel, language, version, ids.toArray(String[]::new), sha)
        .update();
  }

  private static List<String> ids(JsonNode set) {
    List<String> ids = new ArrayList<>();
    set.get("items").forEach(i -> ids.add(i.get("disclosure_id").asString()));
    return ids;
  }

  private static JsonNode json(ResultActions result) throws Exception {
    return JSON.readTree(result.andReturn().getResponse().getContentAsString());
  }
}
