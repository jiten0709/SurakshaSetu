package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.contract.model.ConsentRecord;
import com.surakshasetu.domain.contract.model.SuitabilityResult;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;
import tools.jackson.databind.json.JsonMapper;

/**
 * The generated contract wires up under Boot 4 and Jackson 3, and every operation is implemented.
 */
class ContextLoadsTest extends DomainApiTestSupport {

  @Autowired JsonMapper mapper;

  @Test
  void everyContractOperationIsImplemented() {
    // An inherited generated default method would answer 501 (common/NotImplementedStubs).
    var handlers =
        context
            .getBean("requestMappingHandlerMapping", RequestMappingHandlerMapping.class)
            .getHandlerMethods();
    var operations =
        handlers.entrySet().stream()
            .filter(e -> e.getKey().getPatternValues().stream().anyMatch(v -> v.startsWith("/v1/")))
            .map(e -> e.getValue())
            .toList();
    assertThat(operations).hasSize(23).noneMatch(h -> h.getMethod().isDefault());
    assertThat(operations).extracting(HandlerMethod::getBeanType).doesNotContainNull();
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
         "affordability":"green","vulnerability_flags":[],
         "assumptions":{"cover_to_age":60,"dependency_years":21,"discount_rate":"0.07",
                        "income_growth":"0.05","consumption_share":"0.30",
                        "final_expenses_inr":"200000","existing_cover_counted_inr":"750000"},
         "rule_ids":["FIT-01"],
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
