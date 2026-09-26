package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.atlassian.oai.validator.model.Request;
import com.atlassian.oai.validator.model.SimpleResponse;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Meta, reference and framework errors against the contract. Every {@code api(...)} call in the
 * suite validates the response against the spec; the last test proves that check can fail.
 */
class ContractValidationTest extends DomainApiTestSupport {

  private static final JsonMapper JSON = JsonMapper.builder().build();

  @Test
  void versions() throws Exception {
    JsonNode versions = json(api(get("/v1/meta/versions")).andExpect(status().isOk()));
    assertThat(versions.get("registry_version").asString()).isEqualTo("2026.09.1");
    assertThat(versions.get("rules_version").asString()).isEqualTo("rules-2026.09.1");
    assertThat(versions.get("params_version").asString()).isEqualTo("actuarial-2026.09.1");
    assertThat(versions.get("ranker_version").asString()).isEqualTo("ranker-2026.09.1");
    assertThat(versions.get("rating_version").asString()).isEqualTo("rating-dummy-2026.09.1");
    assertThat(versions.get("active_rules_versions").get(0))
        .isEqualTo(versions.get("rules_version"));
  }

  @Test
  void pincodes() throws Exception {
    api(get("/v1/reference/pincodes/411001"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.district").value("Pune"))
        .andExpect(jsonPath("$.serviceable").value(true))
        .andExpect(jsonPath("$.is_dummy").value(true));
    api(get("/v1/reference/pincodes/744101")).andExpect(jsonPath("$.serviceable").value(false));
    api(get("/v1/reference/pincodes/999999"))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
    apiRejecting(get("/v1/reference/pincodes/01234"))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
  }

  @Test
  void occupations() throws Exception {
    assertThat(codes(api(get("/v1/reference/occupations")))).hasSize(10).isSorted();
    assertThat(codes(api(get("/v1/reference/occupations?q=DRIVER"))))
        .containsExactly("OCC-DRIVER-06");
    assertThat(codes(api(get("/v1/reference/occupations?q=occ-sw")))).containsExactly("OCC-SWE-03");
    assertThat(codes(api(get("/v1/reference/occupations?q=%25")))).isEmpty();
    api(get("/v1/reference/occupations/OCC-MINING-09"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.risk_class").value(4));
    api(get("/v1/reference/occupations/OCC-NOPE-99")).andExpect(status().isNotFound());
  }

  @Test
  void frameworkErrorsCarryTheStatusNameAsCode() throws Exception {
    mvc.perform(get("/v1/no-such-route"))
        .andExpect(status().isNotFound())
        .andExpect(content().contentType(MediaType.APPLICATION_PROBLEM_JSON))
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
    mvc.perform(delete("/v1/meta/versions"))
        .andExpect(status().isMethodNotAllowed())
        .andExpect(jsonPath("$.code").value("METHOD_NOT_ALLOWED"));
    // Found by Schemathesis as 500s: a NUL the database refuses, a null list element.
    api(get("/v1/reference/occupations").param("q", "a\u0000b"))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
    apiRejecting(
            post("/v1/consent/records")
                .header("Idempotency-Key", "null-element")
                .contentType(MediaType.APPLICATION_JSON)
                .content(
                    """
                    {"session_id":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
                     "subject_ref":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c",
                     "notice_version":"2026.09.1-en","notice_sha256":"%s","language":"en-IN",
                     "ai_disclosure_version":"ai-1","purposes":[null],
                     "age_18_plus_declared":true,"method":"structured_action"}
                    """
                        .formatted("a".repeat(64))))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
  }

  @Test
  void theValidatorRejectsAResponseThatBreaksTheContract() {
    var report =
        CONTRACT.validateResponse(
            "/v1/meta/versions",
            Request.Method.GET,
            SimpleResponse.Builder.ok()
                .withContentType("application/json")
                .withBody("{\"rules_version\": 1}")
                .build());
    assertThat(report.hasErrors()).isTrue();
  }

  private static List<String> codes(ResultActions result) throws Exception {
    List<String> codes = new ArrayList<>();
    json(result.andExpect(status().isOk())).forEach(o -> codes.add(o.get("code").asString()));
    return codes;
  }

  private static JsonNode json(ResultActions result) throws Exception {
    return JSON.readTree(result.andReturn().getResponse().getContentAsString());
  }
}
