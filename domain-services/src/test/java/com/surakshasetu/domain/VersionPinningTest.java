package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.common.Rules;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

/**
 * A decision evaluates exactly its pins.rules: an unknown version is 409, and two loaded versions
 * answer by their own tables (I7).
 */
class VersionPinningTest extends DomainApiTestSupport {

  private static final Path MAIN = Path.of("src", "main", "resources");
  private static final String UNKNOWN = "rules-1999.01.1";

  @Test
  void anUnknownRulesVersionIs409OnEveryPinnedOperation() throws Exception {
    String eligibility =
        """
        {"pins":{"rules":"%s"},"age_years":34,"residency":"resident","pincode":"411001",
         "tobacco_12m":false,"occupation_code":"OCC-OFFICE-01","health_flags":{},
         "proposer":{"is_life_assured":true}}
        """
            .formatted(UNKNOWN);
    String suitability =
        """
        {"pins":{"rules":"%s"},
         "eligibility":{"age_years":34,"tobacco_12m":false,"eligible_uins":[],"flags":[]},
         "needs":{"goals":["income_protection"],"annual_income_inr":"1200000",
                  "income_type":"salaried","existing_annual_premium_inr":"0",
                  "financial_distress":false,"comprehension_difficulty_count":0}}
        """
            .formatted(UNKNOWN);
    String quote =
        """
        {"pins":{"rules":"%s"},"uin":"999N001V02","sum_assured_inr":"10000000","term_years":30,
         "ppt":"regular","rider_uins":[],"age_years":34,"tobacco_12m":false,"frequency":"annual"
         %s}
        """;
    String ranking =
        """
        {"pins":{"rules":"%s"},"eligible_uins":["999N001V02"],"excluded_uins":[],"channel":"web",
         "language":"en-IN","as_of":"2026-09-26T10:00:00Z","tobacco_12m":false,"age_years":34,
         "flags":[],
         "suitability":{"decision_id":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c","outcome":"FIT",
          "profile_sufficiency":1,"fit_types":["TERM"],"excluded":{},"need_inr":"10000000",
          "recommended_cover_inr":"10000000","uw_cap_inr":null,"term_years":26,
          "affordability":"green","vulnerability_flags":[],
          "assumptions":{"cover_to_age":60,"dependency_years":0,"discount_rate":"0.07",
            "income_growth":"0.05","consumption_share":"0.30","final_expenses_inr":"200000",
            "existing_cover_counted_inr":"0"},
          "rule_ids":[],"reason_codes":[],"params_version":"p","rules_version":"r",
          "inputs_sha256":"%s"}}
        """
            .formatted(UNKNOWN, "a".repeat(64));
    for (MockHttpServletRequestBuilder request :
        new MockHttpServletRequestBuilder[] {
          post("/v1/eligibility/evaluate")
              .contentType(MediaType.APPLICATION_JSON)
              .content(eligibility),
          post("/v1/suitability/evaluate")
              .contentType(MediaType.APPLICATION_JSON)
              .content(suitability),
          get("/v1/eligibility/required-attributes?pins.rules=" + UNKNOWN),
          get("/v1/suitability/required-slots?pins.rules=" + UNKNOWN),
          post("/v1/ranking/rank").contentType(MediaType.APPLICATION_JSON).content(ranking),
          post("/v1/quotes")
              .contentType(MediaType.APPLICATION_JSON)
              .content(quote.formatted(UNKNOWN, "")),
          post("/v1/quotes/alternatives")
              .contentType(MediaType.APPLICATION_JSON)
              .content(quote.formatted(UNKNOWN, ",\"recommended_cover_inr\":\"10000000\"")),
        }) {
      api(request)
          .andExpect(status().isConflict())
          .andExpect(jsonPath("$.code").value("RULES_VERSION_UNKNOWN"));
    }
    // A pin that isn't rules-YYYY.MM.N at all is unknown too, not a 500 (found by Schemathesis).
    for (String pin : new String[] {"AAA", "rules-x.y.z"}) {
      api(get("/v1/suitability/required-slots").param("pins.rules", pin))
          .andExpect(status().isConflict())
          .andExpect(jsonPath("$.code").value("RULES_VERSION_UNKNOWN"));
    }
  }

  @Test
  void twoLoadedVersionsAnswerByTheirOwnTables(@TempDir Path dir) throws Exception {
    // An older release 2026.08.1 whose age band ends at 60 instead of 65.
    copy(dir, "dmn/suitability-2026.09.1.dmn", "suitability-2026.08.1.dmn", Map.of(), 0);
    copy(
        dir,
        "dmn/eligibility-2026.09.1.dmn",
        "eligibility-2026.08.1.dmn",
        Map.of("&gt; 65", "&gt; 60", "[18..65]", "[18..60]"),
        4);
    copy(dir, "params/actuarial-2026.09.1.yaml", "actuarial-2026.08.1.yaml", Map.of(), 0);
    for (String file :
        new String[] {
          "dmn/eligibility-2026.09.1.dmn",
          "dmn/suitability-2026.09.1.dmn",
          "params/actuarial-2026.09.1.yaml"
        }) {
      Files.copy(MAIN.resolve(file), dir.resolve(Path.of(file).getFileName()));
    }
    Rules rules = Rules.load("file:" + dir + "/*.dmn", "file:" + dir + "/*.yaml");

    assertThat(rules.active()).containsExactly("rules-2026.09.1", "rules-2026.08.1");
    assertThat(rules.current().rulesVersion()).isEqualTo("rules-2026.09.1");
    assertThat(rules.release("rules-2026.08.1").paramsVersion()).isEqualTo("actuarial-2026.08.1");
    assertThat(outcome(rules, "rules-2026.09.1", 62)).isEqualTo("ELIGIBLE");
    assertThat(outcome(rules, "rules-2026.08.1", 62)).isEqualTo("HUMAN_ESCALATION");
  }

  @Test
  void aReleaseWithoutParamsFailsToLoad(@TempDir Path dir) throws Exception {
    copy(dir, "dmn/eligibility-2026.09.1.dmn", "eligibility-2026.09.1.dmn", Map.of(), 0);
    copy(dir, "dmn/suitability-2026.09.1.dmn", "suitability-2026.09.1.dmn", Map.of(), 0);
    assertThatThrownBy(() -> Rules.load("file:" + dir + "/*.dmn", "file:" + dir + "/*.yaml"))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("params");
  }

  private static String outcome(Rules rules, String version, int age) {
    Rules.Release release = rules.release(version);
    return (String)
        release
            .rows(
                release.eligibility(),
                "Eligibility",
                Rules.inputs("age", age, "residency", "resident", "occupation_risk_class", "1"))
            .getFirst()
            .get("outcome");
  }

  /** Copies a shipped file under a new version, applying replacements (each must be found). */
  private static void copy(
      Path dir, String from, String to, Map<String, String> changes, int expectedChanges)
      throws Exception {
    String text = Files.readString(MAIN.resolve(from));
    int changed = 0;
    for (var change : changes.entrySet()) {
      changed += text.split(java.util.regex.Pattern.quote(change.getKey()), -1).length - 1;
      text = text.replace(change.getKey(), change.getValue());
    }
    assertThat(changed).isEqualTo(expectedChanges);
    String version = to.replaceAll(".*-(\\d{4}\\.\\d{2}\\.\\d+)\\..*", "$1");
    Files.writeString(dir.resolve(to), text.replace("2026.09.1", version));
  }
}
