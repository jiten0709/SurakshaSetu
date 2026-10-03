package com.surakshasetu.domain.common;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.surakshasetu.domain.DomainApiTestSupport;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.web.servlet.mvc.method.RequestMappingInfo;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/**
 * Step 16: every /v1 operation needs the service token (401 otherwise), the kill switch also the
 * internal-scope token (403 otherwise), health stays open, and no token is ever logged.
 */
@ExtendWith(OutputCaptureExtension.class)
class ServiceAuthTest extends DomainApiTestSupport {

  private static final String SENTINEL = "sentinel-token-7f3a9c";
  private static final String KILL_SWITCH = "/v1/catalog/products/999N001V02/kill-switch";

  /** One request per contract operation, path variables filled with a placeholder. */
  private List<MockHttpServletRequestBuilder> everyOperation() {
    List<MockHttpServletRequestBuilder> requests = new ArrayList<>();
    var handlers =
        context
            .getBean("requestMappingHandlerMapping", RequestMappingHandlerMapping.class)
            .getHandlerMethods();
    for (RequestMappingInfo info : handlers.keySet()) {
      for (String pattern : info.getPatternValues()) {
        if (!pattern.startsWith("/v1/")) {
          continue;
        }
        String path = pattern.replaceAll("\\{[^}]+}", "999N001V02");
        for (var method : info.getMethodsCondition().getMethods()) {
          requests.add(
              request(HttpMethod.valueOf(method.name()), path)
                  .contentType(MediaType.APPLICATION_JSON)
                  .content("{}"));
        }
      }
    }
    return requests;
  }

  @Test
  void everyOperationRefusesAMissingOrWrongServiceToken() throws Exception {
    List<MockHttpServletRequestBuilder> operations = everyOperation();
    assertThat(operations).hasSize(23);
    for (MockHttpServletRequestBuilder operation : operations) {
      unauthenticated(operation)
          .andExpect(status().isUnauthorized())
          .andExpect(content().contentType(MediaType.APPLICATION_PROBLEM_JSON))
          .andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    }
    for (MockHttpServletRequestBuilder operation : everyOperation()) {
      unauthenticated(operation.header("Authorization", "Bearer " + SENTINEL))
          .andExpect(status().isUnauthorized())
          .andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    }
    for (MockHttpServletRequestBuilder operation : everyOperation()) {
      // The internal token is not a service token.
      unauthenticated(operation.header("Authorization", "Bearer " + INTERNAL_TOKEN))
          .andExpect(status().isUnauthorized());
    }
  }

  @Test
  void theKillSwitchNeedsTheInternalScope() throws Exception {
    MockHttpServletRequestBuilder withServiceTokenOnly =
        request(HttpMethod.POST, KILL_SWITCH)
            .header("Authorization", "Bearer " + SERVICE_TOKEN)
            .contentType(MediaType.APPLICATION_JSON)
            .content("{\"reason\":\"test\",\"actor\":\"ops-test\"}");
    unauthenticated(withServiceTokenOnly)
        .andExpect(status().isForbidden())
        .andExpect(content().contentType(MediaType.APPLICATION_PROBLEM_JSON))
        .andExpect(jsonPath("$.code").value("FORBIDDEN"));
    unauthenticated(withServiceTokenOnly.header("X-Internal-Token", SENTINEL))
        .andExpect(status().isForbidden());
    // The product is untouched.
    api(get("/v1/catalog/products/999N001V02"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("in_force"));
  }

  @Test
  void healthNeedsNoToken() throws Exception {
    mvc.perform(get("/actuator/health")).andExpect(status().isOk());
  }

  @Test
  void noTokenIsEverLogged(CapturedOutput output) throws Exception {
    unauthenticated(get("/v1/meta/versions").header("Authorization", "Bearer " + SENTINEL))
        .andExpect(status().isUnauthorized());
    unauthenticated(
            request(HttpMethod.POST, KILL_SWITCH)
                .header("Authorization", "Bearer " + SERVICE_TOKEN)
                .header("X-Internal-Token", SENTINEL)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\"test\",\"actor\":\"ops-test\"}"))
        .andExpect(status().isForbidden());
    api(get("/v1/meta/versions")).andExpect(status().isOk());

    assertThat(output.getAll())
        .doesNotContain(SENTINEL)
        .doesNotContain(SERVICE_TOKEN)
        .doesNotContain(INTERNAL_TOKEN);
  }
}
