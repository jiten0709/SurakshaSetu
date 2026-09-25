package com.surakshasetu.domain.consent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

@ExtendWith(OutputCaptureExtension.class)
class ConsentServiceTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();
  private static final String EN = "2026.09.1-en";

  // --- create and idempotency ------------------------------------------------------------

  @Test
  void createsARecordAndReplaysTheSameKey() throws Exception {
    Map<String, Object> request = consent(EN, enSha(), "en-IN", grants(true, false, true), true);
    String key = UUID.randomUUID().toString();

    JsonNode created = json(create(key, request).andExpect(status().isCreated()));
    assertThat(created.get("valid_p1").asBoolean()).isTrue();
    assertThat(created.get("valid_reasons")).isEmpty();
    assertThat(created.get("notice_language").asString()).isEqualTo("en-IN");
    assertThat(created.get("method").asString()).isEqualTo("structured_action");
    assertThat(purposes(created))
        .isEqualTo(
            Map.of("P1_NEEDS_RECO", true, "P2_ADVISOR_CONTACT", false, "P3_MARKETING", true));
    UUID id = UUID.fromString(created.get("consent_id").asString());
    assertThat(id.version()).isEqualTo(7);

    JsonNode replayed = json(create(key, request).andExpect(status().isOk()));
    assertThat(replayed.get("consent_id")).isEqualTo(created.get("consent_id"));
    assertThat(replayed.get("captured_at")).isEqualTo(created.get("captured_at"));
    assertThat(count("SELECT count(*) FROM consent.record WHERE idempotency_key = ?", key))
        .isEqualTo(1);
    assertThat(count("SELECT count(*) FROM consent.purpose_grant WHERE consent_id = ?", id))
        .isEqualTo(3);
  }

  @Test
  void theSameKeyWithAnotherBodyIsRefused() throws Exception {
    String key = UUID.randomUUID().toString();
    create(key, consent(EN, enSha(), "en-IN", grants(true, false, false), true))
        .andExpect(status().isCreated());

    create(key, consent(EN, enSha(), "en-IN", grants(true, true, false), true))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("IDEMPOTENCY_KEY_REUSED"));
  }

  @Test
  void p1MustBePresentAndGranted() throws Exception {
    Map<String, Object> declined = consent(EN, enSha(), "en-IN", grants(false, true, true), true);
    Map<String, Object> missing = consent(EN, enSha(), "en-IN", List.of(grant("P2", true)), true);
    for (Map<String, Object> request : List.of(declined, missing)) {
      String key = UUID.randomUUID().toString();
      create(key, request)
          .andExpect(status().isUnprocessableContent())
          .andExpect(jsonPath("$.code").value("P1_REQUIRED"));
      assertThat(count("SELECT count(*) FROM consent.record WHERE idempotency_key = ?", key))
          .isZero();
    }
  }

  @Test
  void theNoticeMustBeInForceForTheLanguageAndMatchItsHash() throws Exception {
    // Two past notices in a test language: the older one is superseded today.
    notice("xs-TEST-1", "xs-TEST", LocalDate.of(2026, 1, 1));
    notice("xs-TEST-2", "xs-TEST", LocalDate.of(2026, 2, 1));

    for (Map<String, Object> request :
        List.of(
            consent(EN, "0".repeat(64), "en-IN", grants(true, false, false), true),
            consent(EN, enSha(), "hi-IN", grants(true, false, false), true),
            consent("2026.01.9-en", enSha(), "en-IN", grants(true, false, false), true),
            consent("xs-TEST-1", sha("xs-TEST-1"), "xs-TEST", grants(true, false, false), true))) {
      create(UUID.randomUUID().toString(), request)
          .andExpect(status().isUnprocessableContent())
          .andExpect(jsonPath("$.code").value("NOTICE_MISMATCH"));
    }
    create(
            UUID.randomUUID().toString(),
            consent("xs-TEST-2", sha("xs-TEST-2"), "xs-TEST", grants(true, false, false), true))
        .andExpect(status().isCreated());
  }

  @Test
  void duplicatePurposesAndAnUnknownMethodAreBadRequests() throws Exception {
    Map<String, Object> duplicate =
        consent(EN, enSha(), "en-IN", List.of(grant("P1", true), grant("P1", false)), true);
    create(UUID.randomUUID().toString(), duplicate)
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));

    Map<String, Object> typed = consent(EN, enSha(), "en-IN", grants(true, false, false), true);
    typed.put("method", "typed_affirmation");
    apiRejecting(createRequest(UUID.randomUUID().toString(), typed))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));

    apiRejecting(
            post("/v1/consent/records")
                .contentType(MediaType.APPLICATION_JSON)
                .content(JSON.writeValueAsString(duplicate)))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
  }

  // --- valid_p1 ----------------------------------------------------------------------------

  @Test
  void validP1LapsesWithTheTtl() throws Exception {
    JsonNode record = createValid(grants(true, false, false));
    OffsetDateTime captured = OffsetDateTime.parse(record.get("captured_at").asString());

    assertThat(asOf(record, captured.plusDays(30).minusSeconds(1)).get("valid_p1").asBoolean())
        .isTrue();
    JsonNode expired = asOf(record, captured.plusDays(30));
    assertThat(expired.get("valid_p1").asBoolean()).isFalse();
    assertThat(reasons(expired)).containsExactly("CONSENT_EXPIRED");
  }

  @Test
  void validP1LapsesWhenANewerNoticeTakesEffect() throws Exception {
    LocalDate today = BusinessDates.on(null);
    notice("xn-TEST-1", "xn-TEST", today.minusDays(1));
    notice("xn-TEST-2", "xn-TEST", today.plusDays(10));
    JsonNode record =
        json(
            create(
                    UUID.randomUUID().toString(),
                    consent(
                        "xn-TEST-1", sha("xn-TEST-1"), "xn-TEST", grants(true, false, false), true))
                .andExpect(status().isCreated()));
    OffsetDateTime captured = OffsetDateTime.parse(record.get("captured_at").asString());

    assertThat(asOf(record, captured.plusDays(5)).get("valid_p1").asBoolean()).isTrue();
    JsonNode superseded = asOf(record, captured.plusDays(12));
    assertThat(reasons(superseded)).containsExactly("NOTICE_SUPERSEDED");
  }

  @Test
  void anUnder18DeclarationNeverMakesP1Valid() throws Exception {
    JsonNode record =
        json(
            create(
                    UUID.randomUUID().toString(),
                    consent(EN, enSha(), "en-IN", grants(true, false, false), false))
                .andExpect(status().isCreated()));
    assertThat(reasons(record)).containsExactly("AGE_NOT_DECLARED");
  }

  // --- purposes and withdrawal -------------------------------------------------------------

  @Test
  void purposesAreAnAppendOnlyHistoryWhoseNewestRowWins() throws Exception {
    JsonNode record = createValid(grants(true, false, false));
    String id = record.get("consent_id").asString();

    assertThat(purposes(json(change(id, "P2_ADVISOR_CONTACT", true))))
        .containsEntry("P2_ADVISOR_CONTACT", true);
    JsonNode revoked = json(change(id, "P1_NEEDS_RECO", false));
    assertThat(revoked.get("valid_p1").asBoolean()).isFalse();
    assertThat(reasons(revoked)).containsExactly("P1_NOT_GRANTED");
    JsonNode regranted = json(change(id, "P1_NEEDS_RECO", true));
    assertThat(regranted.get("valid_p1").asBoolean()).isTrue();

    assertThat(
            count(
                "SELECT count(*) FROM consent.purpose_grant WHERE consent_id = ?",
                UUID.fromString(id)))
        .isEqualTo(3 + 3);
  }

  @Test
  void withdrawalIsIdempotentAndFinal() throws Exception {
    JsonNode record = createValid(grants(true, true, true));
    String id = record.get("consent_id").asString();

    JsonNode withdrawn = json(withdraw(id));
    assertThat(withdrawn.get("valid_p1").asBoolean()).isFalse();
    assertThat(reasons(withdrawn)).containsExactly("P1_NOT_GRANTED", "WITHDRAWN");
    assertThat(purposes(withdrawn)).doesNotContainValue(true);
    long rows =
        count(
            "SELECT count(*) FROM consent.purpose_grant WHERE consent_id = ?", UUID.fromString(id));
    assertThat(rows).isEqualTo(3 + 3);

    JsonNode again = json(withdraw(id));
    assertThat(again.get("withdrawn_at")).isEqualTo(withdrawn.get("withdrawn_at"));
    assertThat(
            count(
                "SELECT count(*) FROM consent.purpose_grant WHERE consent_id = ?",
                UUID.fromString(id)))
        .isEqualTo(rows);

    api(purposeRequest(id, "P3_MARKETING", true))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("CONSENT_WITHDRAWN"));
    // An earlier as_of cannot revive it.
    OffsetDateTime captured = OffsetDateTime.parse(record.get("captured_at").asString());
    assertThat(asOf(record, captured).get("valid_p1").asBoolean()).isFalse();
  }

  @Test
  void anUnknownRecordIs404() throws Exception {
    String id = UUID.randomUUID().toString();
    for (MockHttpServletRequestBuilder request :
        List.of(
            get("/v1/consent/records/" + id),
            purposeRequest(id, "P2_ADVISOR_CONTACT", true),
            withdrawRequest(id))) {
      api(request)
          .andExpect(status().isNotFound())
          .andExpect(jsonPath("$.code").value("NOT_FOUND"));
    }
  }

  // --- notices -----------------------------------------------------------------------------

  @Test
  void servesTheNoticeInForcePerLanguage() throws Exception {
    JsonNode en = json(api(get("/v1/consent/notices/current?language=en-IN")));
    assertThat(en.get("notice_version").asString()).isEqualTo(EN);
    assertThat(en.get("is_dummy").asBoolean()).isTrue();
    assertThat(en.get("body").asString())
        .startsWith("DUMMY:")
        .contains("[P1 required]", "18 or older", "हिंदी");
    assertThat(en.get("body_sha256").asString())
        .isEqualTo(Jcs.bodySha256(en.get("body").asString()));

    JsonNode hi = json(api(get("/v1/consent/notices/2026.09.1-hi")));
    assertThat(hi.get("language").asString()).isEqualTo("hi-IN");

    api(get("/v1/consent/notices/current?language=ta-IN")).andExpect(status().isNotFound());
    api(get("/v1/consent/notices/2020.01.1-en")).andExpect(status().isNotFound());
  }

  // --- logging -----------------------------------------------------------------------------

  @Test
  void consentPathsLogNoIdentifierOrRequestValue(CapturedOutput output) throws Exception {
    UUID subject = UUID.fromString("5e471e11-0000-7000-8000-00000000c0de");
    UUID session = UUID.fromString("5e551011-0000-7000-8000-00000000c0de");
    String key = "sentinel-idempotency-key-0c0de";
    String aiVersion = "sentinel-ai-disclosure-0c0de";
    Map<String, Object> request = consent(EN, enSha(), "en-IN", grants(true, false, false), true);
    request.put("subject_ref", subject.toString());
    request.put("session_id", session.toString());
    request.put("ai_disclosure_version", aiVersion);

    String id =
        json(create(key, request).andExpect(status().isCreated())).get("consent_id").asString();
    create(key, request).andExpect(status().isOk());
    Map<String, Object> other = new LinkedHashMap<>(request);
    other.put("age_18_plus_declared", false);
    create(key, other).andExpect(status().isConflict());
    Map<String, Object> noP1 = new LinkedHashMap<>(request);
    noP1.put("purposes", grants(false, false, false));
    create(key + "-2", noP1).andExpect(status().isUnprocessableContent());
    Map<String, Object> badMethod = new LinkedHashMap<>(request);
    badMethod.put("method", "sentinel-method-0c0de");
    apiRejecting(createRequest(key + "-3", badMethod)).andExpect(status().isBadRequest());
    change(id, "P2_ADVISOR_CONTACT", true);
    withdraw(id);

    assertThat(output.getAll()).contains("consent recorded", "consent withdrawn");
    assertThat(output.getAll())
        .doesNotContain(subject.toString(), session.toString(), key, aiVersion, id);
  }

  // --- helpers -----------------------------------------------------------------------------

  private JsonNode createValid(List<Map<String, Object>> grants) throws Exception {
    return json(
        create(UUID.randomUUID().toString(), consent(EN, enSha(), "en-IN", grants, true))
            .andExpect(status().isCreated()));
  }

  private ResultActions create(String key, Map<String, Object> request) throws Exception {
    return api(createRequest(key, request));
  }

  private static MockHttpServletRequestBuilder createRequest(
      String key, Map<String, Object> request) {
    return post("/v1/consent/records")
        .header("Idempotency-Key", key)
        .contentType(MediaType.APPLICATION_JSON)
        .content(JSON.writeValueAsString(request));
  }

  private ResultActions change(String id, String purpose, boolean granted) throws Exception {
    return api(purposeRequest(id, purpose, granted)).andExpect(status().isOk());
  }

  private static MockHttpServletRequestBuilder purposeRequest(
      String id, String purpose, boolean granted) {
    return post("/v1/consent/records/" + id + "/purposes")
        .contentType(MediaType.APPLICATION_JSON)
        .content(JSON.writeValueAsString(Map.of("purpose_id", purpose, "granted", granted)));
  }

  private ResultActions withdraw(String id) throws Exception {
    return api(withdrawRequest(id)).andExpect(status().isOk());
  }

  private static MockHttpServletRequestBuilder withdrawRequest(String id) {
    return post("/v1/consent/records/" + id + "/withdraw")
        .contentType(MediaType.APPLICATION_JSON)
        .content("{\"reason\":\"customer asked\"}");
  }

  private JsonNode asOf(JsonNode record, OffsetDateTime asOf) throws Exception {
    return json(
        api(get("/v1/consent/records/" + record.get("consent_id").asString())
                .param("as_of", asOf.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)))
            .andExpect(status().isOk()));
  }

  private static Map<String, Object> consent(
      String notice, String sha, String language, List<Map<String, Object>> grants, boolean adult) {
    Map<String, Object> request = new LinkedHashMap<>();
    request.put("session_id", UUID.randomUUID().toString());
    request.put("subject_ref", UUID.randomUUID().toString());
    request.put("notice_version", notice);
    request.put("notice_sha256", sha);
    request.put("language", language);
    request.put("ai_disclosure_version", "ai-2026.09.1");
    request.put("purposes", grants);
    request.put("age_18_plus_declared", adult);
    request.put("method", "structured_action");
    return request;
  }

  private static List<Map<String, Object>> grants(boolean p1, boolean p2, boolean p3) {
    return List.of(grant("P1", p1), grant("P2", p2), grant("P3", p3));
  }

  private static Map<String, Object> grant(String purpose, boolean granted) {
    String id =
        switch (purpose) {
          case "P1" -> "P1_NEEDS_RECO";
          case "P2" -> "P2_ADVISOR_CONTACT";
          default -> "P3_MARKETING";
        };
    return Map.of("purpose_id", id, "granted", granted);
  }

  private static String enSha() {
    return sha(EN);
  }

  private static String sha(String noticeVersion) {
    return HexFormat.of()
        .formatHex(
            ADMIN
                .sql("SELECT body_sha256 FROM consent.notice_version WHERE notice_version = ?")
                .param(noticeVersion)
                .query(byte[].class)
                .single());
  }

  private static void notice(String version, String language, LocalDate effectiveFrom) {
    String body = "DUMMY: test notice " + version;
    ADMIN
        .sql(
            """
            INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,
              approved_by, effective_from, is_dummy)
            VALUES (?, ?, ?, ?, 'test', ?, true)
            """)
        .params(
            version, language, body, HexFormat.of().parseHex(Jcs.bodySha256(body)), effectiveFrom)
        .update();
  }

  private static long count(String sql, Object param) {
    return ADMIN.sql(sql).param(param).query(Long.class).single();
  }

  private static JsonNode json(ResultActions result) throws Exception {
    return JSON.readTree(result.andReturn().getResponse().getContentAsString());
  }

  private static Map<String, Boolean> purposes(JsonNode record) {
    Map<String, Boolean> purposes = new LinkedHashMap<>();
    record
        .get("purposes")
        .forEach(p -> purposes.put(p.get("purpose_id").asString(), p.get("granted").asBoolean()));
    return purposes;
  }

  private static List<String> reasons(JsonNode record) {
    List<String> reasons = new java.util.ArrayList<>();
    record.get("valid_reasons").forEach(r -> reasons.add(r.asString()));
    return reasons;
  }
}
