package com.surakshasetu.domain.common;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.SQLException;
import org.apache.tomcat.util.http.InvalidParameterException;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.web.context.request.ServletWebRequest;

/**
 * Client errors Schemathesis found reaching the catch-all as 500s. MockMvc never parses a query
 * string the way Tomcat does, so the handlers are called directly.
 */
class ProblemHandlerTest {

  private final ProblemHandler handler = new ProblemHandler();
  private final ServletWebRequest request = new ServletWebRequest(new MockHttpServletRequest());

  @Test
  void aMalformedQueryStringIsABadRequest() {
    assertProblem(
        handler.invalidParameter(new InvalidParameterException("empty name"), request),
        400,
        "BAD_REQUEST");
  }

  @Test
  void aValueTheDatabaseRefusesIsABadRequestButAnyOtherViolationIsNot() {
    // PgJDBC's "zero bytes may not occur in string parameters" is SQLSTATE 22023.
    assertProblem(handler.dataIntegrity(violation("22023"), request), 400, "BAD_REQUEST");
    assertProblem(handler.dataIntegrity(violation("23505"), request), 500, "INTERNAL_SERVER_ERROR");
  }

  private static DataIntegrityViolationException violation(String sqlState) {
    return new DataIntegrityViolationException("x", new SQLException("x", sqlState));
  }

  private static void assertProblem(ResponseEntity<Object> response, int status, String code) {
    assertThat(response.getStatusCode().value()).isEqualTo(status);
    ProblemDetail problem = (ProblemDetail) response.getBody();
    assertThat(problem).isNotNull();
    assertThat(problem.getProperties()).containsEntry("code", code);
  }
}
