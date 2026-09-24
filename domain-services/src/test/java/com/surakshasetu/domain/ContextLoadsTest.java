package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.contract.model.ConsentRecord;
import com.surakshasetu.domain.contract.model.SuitabilityResult;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.testcontainers.service.connection.ServiceConnection;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;
import tools.jackson.databind.json.JsonMapper;

/** The generated contract wires up under Boot 4 and Jackson 3; every stub answers 501. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Testcontainers
class ContextLoadsTest {

  // Same image as infra/compose.yaml.
  @Container @ServiceConnection
  static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:16.15-trixie");

  @Value("${local.server.port}")
  int port;

  @Autowired JsonMapper mapper;

  @ParameterizedTest
  @CsvSource({
    "GET, /v1/meta/versions",
    "GET, /v1/consent/notices/current?language=en-IN",
    "GET, /v1/catalog/products",
    "GET, /v1/disclosures/DISC-GLOBAL-AI-06?language=en-IN",
    "GET, /v1/reference/occupations",
    "GET, /v1/eligibility/required-attributes",
    "GET, /v1/suitability/required-slots?pins.rules=r1",
    "POST, /v1/ranking/rank",
    "POST, /v1/quotes",
  })
  void stubAnswers501ProblemJson(String method, String path) throws Exception {
    HttpRequest request =
        HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
            .method(method, HttpRequest.BodyPublishers.ofString("{}"))
            .header("Content-Type", "application/json")
            .build();

    HttpResponse<String> response;
    try (HttpClient client = HttpClient.newHttpClient()) {
      response = client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    assertThat(response.statusCode()).isEqualTo(501);
    assertThat(response.headers().firstValue("Content-Type")).hasValue("application/problem+json");
    assertThat(response.body()).contains("\"code\":\"NOT_IMPLEMENTED\"", "\"status\":501");
  }

  @Test
  void generatedModelsRoundTripThroughJackson3() {
    String consent =
        """
        {"consent_id":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b","notice_version":"2026.09.1-en",
         "notice_sha256":"%s","notice_language":"en-IN","ai_disclosure_version":"ai-2026.09.1",
         "purposes":[{"purpose_id":"P1_NEEDS_RECO","granted":true}],
         "age_18_plus_declared":true,"method":"parsed_affirmation",
         "captured_at":"2026-09-23T10:15:00Z","withdrawn_at":null,
         "valid_p1":true,"valid_reasons":[]}
        """
            .formatted("a".repeat(64));
    String suitability =
        """
        {"decision_id":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c","outcome":"FIT",
         "profile_sufficiency":0.85,"fit_types":["TERM"],
         "excluded":{"NON_PAR_SAVINGS":["SUIT-OFF-A5"]},"need_inr":"-150000.50",
         "recommended_cover_inr":"10000000","uw_cap_inr":null,"term_years":26,
         "affordability":"green","vulnerability_flags":[],"rule_ids":["FIT-01"],
         "reason_codes":["SUIT-TERM-04"],
         "params_version":"p1","rules_version":"r1","inputs_sha256":"%s"}
        """
            .formatted("b".repeat(64));

    for (var sample :
        new Object[][] {{consent, ConsentRecord.class}, {suitability, SuitabilityResult.class}}) {
      Object model = mapper.readValue((String) sample[0], (Class<?>) sample[1]);
      assertThat(mapper.readTree(mapper.writeValueAsString(model)))
          .isEqualTo(mapper.readTree((String) sample[0]));
    }
  }
}
